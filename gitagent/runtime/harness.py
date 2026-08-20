"""Agent harness: specs, MCP access, permissions, audit, and live trace."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from time import perf_counter
from typing import Any, TypeVar

from ..core.approval import ApprovalStore
from ..core.audit import AuditLog
from ..core.errors import PermissionDenied, ToolExecutionError, ValidationError
from ..core.models import (
    AccessLevel,
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    VerificationReport,
)
from ..core.trace import TraceBus, TraceCategory, TraceStatus
from ..mcp import MCPClient, MCPServer

T = TypeVar("T")


class AgentContext:
    """The isolated working memory and execution capability for one agent invocation."""

    def __init__(
        self,
        harness: AgentHarness,
        spec: AgentSpec,
        session_id: str,
        *,
        repository: str = "",
        goal: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        guidance: AgentGuidance | None = None,
        max_steps: int = 20,
    ) -> None:
        self._harness = harness
        self.spec = spec
        self.session_id = session_id
        self.repository = repository
        self.goal = goal
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.guidance = guidance
        self.phase = ""
        self.steps = 0
        self.max_steps = max_steps
        self.observations: list[dict[str, Any]] = []
        self.pending: Any = None
        self.question = ""
        self.result: Any = None
        self.final_message = ""
        self.code_candidate: CandidatePatch | None = None
        self.change_request: ChangeRequest | None = None
        self.verification: VerificationReport | None = None
        self.reply_draft: str | None = None
        self.read_only = False
        self.result_required = True
        self.read_cache: dict[str, Any] = {}
        self.error: str | None = None
        self.finished = False

    @property
    def agent(self) -> str:
        return self.spec.name

    @property
    def system_prompt(self) -> str:
        return self.spec.system_prompt

    @property
    def waiting(self) -> bool:
        return self.pending is not None or bool(self.question)

    def tool(self, name: str, *, approval_id: str | None = None, **arguments: Any) -> Any:
        return self._harness.execute_tool(self, name, arguments, approval_id=approval_id)


class AgentHarness:
    def __init__(
        self,
        server: MCPServer,
        *,
        approvals: ApprovalStore | None = None,
        audit: AuditLog | None = None,
        trace: TraceBus | None = None,
    ) -> None:
        self.server = server
        self.client = MCPClient(server)
        self.approvals = approvals or ApprovalStore()
        self.audit = audit or AuditLog()
        self.trace = trace or TraceBus()
        self._specs: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        if spec.name in self._specs:
            raise ValidationError(f"duplicate agent spec: {spec.name}")
        unknown_tools = sorted(spec.allowed_tools - {tool.name for tool in self.server.tools})
        if unknown_tools:
            raise ValidationError(f"agent {spec.name} references unknown tools: {', '.join(unknown_tools)}")
        self._specs[spec.name] = spec

    def context(
        self,
        agent_name: str,
        session_id: str,
        *,
        repository: str = "",
        goal: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        guidance: AgentGuidance | None = None,
        max_steps: int = 20,
    ) -> AgentContext:
        try:
            spec = self._specs[agent_name]
        except KeyError as exc:
            raise ValidationError(f"unknown agent: {agent_name}") from exc
        return AgentContext(
            self,
            spec,
            session_id,
            repository=repository,
            goal=goal,
            entity_type=entity_type,
            entity_id=entity_id,
            guidance=guidance,
            max_steps=max_steps,
        )

    def run(
        self,
        agent_name: str,
        *,
        session_id: str,
        operation: Callable[[AgentContext], T],
        repository: str = "",
        goal: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        guidance: AgentGuidance | None = None,
    ) -> T:
        context = self.context(
            agent_name,
            session_id,
            repository=repository,
            goal=goal,
            entity_type=entity_type,
            entity_id=entity_id,
            guidance=guidance,
        )
        started = perf_counter()
        self.trace.emit(
            session_id=session_id,
            category=TraceCategory.AGENT,
            name=agent_name,
            status=TraceStatus.STARTED,
            details={
                "debug_event": "start",
                "context": debug_context_snapshot(context),
            },
        )
        try:
            result = operation(context)
            self._validate_output(context.spec, result)
        except Exception as exc:
            context.error = str(exc)
            self.trace.emit(
                session_id=session_id,
                category=TraceCategory.AGENT,
                name=agent_name,
                status=TraceStatus.FAILED,
                message=str(exc),
                details={
                    "debug_event": "failed",
                    "context": debug_context_snapshot(context),
                    "error": debug_error_details(exc),
                },
                duration_ms=(perf_counter() - started) * 1000,
            )
            raise
        self.trace.emit(
            session_id=session_id,
            category=TraceCategory.AGENT,
            name=agent_name,
            status=TraceStatus.COMPLETED,
            details={
                "debug_event": "completed",
                "output_type": type(result).__name__,
                "context": debug_context_snapshot(context),
                "result": debug_value(result),
            },
            duration_ms=(perf_counter() - started) * 1000,
        )
        return result

    def execute_tool(
        self,
        context: AgentContext,
        name: str,
        arguments: dict[str, Any],
        *,
        approval_id: str | None,
    ) -> Any:
        tool = self.server.get_tool(name)
        spec = context.spec
        if name not in spec.allowed_tools:
            self._record_denied(context, tool.access, name, approval_id, "tool is outside agent allowlist")
            raise PermissionDenied(f"agent {spec.name} is not allowed to use {name}")
        started = perf_counter()
        trace_details = {
            "agent": spec.name,
            "classification": tool.access.value,
            "arguments": self._safe_trace_arguments(arguments),
            "debug_arguments": debug_value(arguments),
            "argument_keys": sorted(arguments),
        }
        self.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.TOOL_USE,
            name=name,
            status=TraceStatus.STARTED,
            details=trace_details,
        )
        try:
            if tool.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}:
                self.approvals.authorize(
                    approval_id=approval_id,
                    session_id=context.session_id,
                    tool=name,
                    arguments=arguments,
                )
            result = self.client.call(name, arguments)
        except Exception as exc:
            denied = isinstance(exc, PermissionDenied)
            self.audit.record(
                session_id=context.session_id,
                agent=spec.name,
                tool=name,
                action=name,
                classification=tool.access,
                approval_id=approval_id,
                result="DENIED" if denied else "FAILED",
                details={"error": str(exc), "argument_keys": sorted(arguments)},
            )
            self.trace.emit(
                session_id=context.session_id,
                category=TraceCategory.TOOL_USE,
                name=name,
                status=TraceStatus.DENIED if denied else TraceStatus.FAILED,
                message=str(exc),
                details={**trace_details, "error": debug_error_details(exc)},
                duration_ms=(perf_counter() - started) * 1000,
            )
            raise
        self.audit.record(
            session_id=context.session_id,
            agent=spec.name,
            tool=name,
            action=name,
            classification=tool.access,
            approval_id=approval_id,
            result="OK",
            details={"argument_keys": sorted(arguments)},
        )
        self.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.TOOL_USE,
            name=name,
            status=TraceStatus.COMPLETED,
            details={**trace_details, "result": debug_value(result)},
            duration_ms=(perf_counter() - started) * 1000,
        )
        return result

    def specs_for(self, capability: Any) -> tuple[AgentSpec, ...]:
        return tuple(spec for spec in self._specs.values() if capability in spec.capabilities)

    def spec(self, agent_name: str) -> AgentSpec:
        try:
            return self._specs[agent_name]
        except KeyError as exc:
            raise ValidationError(f"unknown agent: {agent_name}") from exc

    def _record_denied(
        self,
        context: AgentContext,
        access: AccessLevel,
        tool: str,
        approval_id: str | None,
        reason: str,
    ) -> None:
        self.audit.record(
            session_id=context.session_id,
            agent=context.spec.name,
            tool=tool,
            action=tool,
            classification=access,
            approval_id=approval_id,
            result="DENIED",
            details={"error": reason},
        )
        self.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.TOOL_USE,
            name=tool,
            status=TraceStatus.DENIED,
            message=reason,
            details={"agent": context.agent, "classification": access.value},
        )

    @staticmethod
    def _safe_trace_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        """只暴露定位调用所需的非敏感参数，正文和文件内容始终省略。"""
        safe_keys = {
            "base",
            "branch",
            "depth",
            "draft",
            "head",
            "issue_number",
            "job_id",
            "limit",
            "labels",
            "max_results",
            "path",
            "paths",
            "pr_number",
            "ref",
            "repository",
            "run_id",
            "start_line",
            "state",
            "symbol",
            "workflow_run_id",
        }
        safe: dict[str, Any] = {}
        for key in sorted(arguments):
            if key not in safe_keys:
                continue
            value = arguments[key]
            if isinstance(value, list):
                safe[key] = [str(item)[:120] for item in value[:8]]
            elif isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value if not isinstance(value, str) else value[:200]
        return safe

    @staticmethod
    def _validate_output(spec: AgentSpec, result: Any) -> None:
        if not spec.output_schema:
            return
        if is_dataclass(result):
            keys = set(asdict(result))
        elif isinstance(result, dict):
            keys = set(result)
        else:
            raise ValidationError(f"agent {spec.name} returned non-structured output")
        missing = set(spec.output_schema) - keys
        if missing:
            raise ValidationError(f"agent {spec.name} omitted output fields: {', '.join(sorted(missing))}")


def debug_context_snapshot(context: AgentContext) -> dict[str, Any]:
    """Return bounded, non-executable agent state for developer diagnostics."""

    pending = context.pending
    pending_summary: dict[str, Any] | None = None
    if pending is not None:
        pending_summary = {
            "summary": debug_value(getattr(pending, "summary", "")),
            "specialist": getattr(pending, "specialist", None),
            "calls": debug_value(getattr(pending, "calls", [])),
        }
    verification_summary: dict[str, Any] | None = None
    if context.verification is not None:
        verification_summary = {
            "passed": bool(context.verification.passed),
            "checks": debug_value(context.verification.checks),
        }
    change_request_summary: dict[str, Any] | None = None
    if context.change_request is not None:
        change_request_summary = {
            "description": debug_value(context.change_request.description, key="summary"),
            "target_files": debug_value(context.change_request.target_files),
            "issue_number": context.change_request.issue_number,
            "suggested_title": debug_value(context.change_request.suggested_title),
        }
    candidate_summary: dict[str, Any] | None = None
    if context.code_candidate is not None:
        candidate_summary = {
            "summary": debug_value(context.code_candidate.summary, key="summary"),
            "root_cause": debug_value(context.code_candidate.root_cause, key="summary"),
            "changed_files": debug_value(context.code_candidate.changed_files),
            "risks": debug_value(context.code_candidate.risks),
            "verification_required": debug_value(context.code_candidate.verification_required),
        }
    return {
        "agent": context.agent,
        "phase": context.phase,
        "repository": context.repository,
        "goal": debug_value(context.goal, key="goal"),
        "entity_type": context.entity_type,
        "entity_id": context.entity_id,
        "steps": context.steps,
        "max_steps": context.max_steps,
        "waiting": context.waiting,
        "question": debug_value(context.question, key="question"),
        "pending": pending_summary,
        "finished": context.finished,
        "error": debug_value(context.error, key="message"),
        "final_message": debug_value(context.final_message, key="message"),
        "result": debug_value(context.result),
        "read_only": context.read_only,
        "result_required": context.result_required,
        "change_request": change_request_summary,
        "code_candidate": candidate_summary,
        "verification": verification_summary,
        "reply_draft": "<present>" if context.reply_draft is not None else None,
        "observations": [debug_observation(item) for item in context.observations[-40:]],
    }


def debug_observation(observation: Any) -> Any:
    if not isinstance(observation, dict):
        return debug_value(observation)
    kind = str(observation.get("kind") or "")
    payload = observation.get("payload")
    if kind == "tool" and isinstance(payload, dict):
        return {
            "kind": "tool",
            "tool": str(payload.get("tool") or ""),
            "arguments": debug_value(payload.get("arguments", {})),
            "data": debug_value(payload.get("data")),
            "cached": bool(payload.get("cached", False)),
        }
    return {"kind": kind, "payload": debug_value(payload)}


def debug_error_details(exc: BaseException) -> list[dict[str, Any]]:
    """Expose error types/statuses without serializing provider request bodies or credentials."""

    chain: list[dict[str, Any]] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        item: dict[str, Any] = {"type": type(current).__name__}
        for attribute in ("status_code", "code"):
            value = getattr(current, attribute, None)
            if isinstance(value, (str, int)) and value != "":
                item[attribute] = value
        if current is exc:
            item["message"] = debug_value(str(current), key="message")
        chain.append(item)
        current = current.__cause__ or current.__context__
    return chain


def debug_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Bound debug payloads and omit secret-like or source-heavy fields."""

    if depth > 5:
        return "<max depth>"
    if is_dataclass(value):
        return debug_value(asdict(value), key=key, depth=depth + 1)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key in {"api_key", "access_token", "secret", "password", "authorization", "github_token"}:
            return "[REDACTED]"
        if key == "content":
            return f"<text {len(value)} chars>"
        limit = 2_000 if key in {"message", "question", "summary", "goal", "body", "patch", "query"} else 1_000
        return value if len(value) <= limit else value[:limit] + f"… <{len(value) - limit} chars omitted>"
    if isinstance(value, dict):
        if key == "files":
            paths = [str(path)[:240] for path in list(value)[:30]]
            return {"count": len(value), "paths": paths}
        result: dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= 30:
                result["__omitted__"] = f"{len(value) - 30} more keys"
                break
            name = str(item_key)
            result[name] = debug_value(item_value, key=name, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        result = [debug_value(item, depth=depth + 1) for item in items[:30]]
        if len(items) > 30:
            result.append(f"<{len(items) - 30} more items>")
        return result
    return debug_value(str(value), key=key, depth=depth + 1)


def tool_error(message: str, exc: Exception) -> ToolExecutionError:
    return ToolExecutionError(f"{message}: {exc}")
