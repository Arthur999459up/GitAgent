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
    ) -> T:
        context = self.context(agent_name, session_id)
        started = perf_counter()
        self.trace.emit(
            session_id=session_id,
            category=TraceCategory.AGENT,
            name=agent_name,
            status=TraceStatus.STARTED,
        )
        try:
            result = operation(context)
            self._validate_output(context.spec, result)
        except Exception as exc:
            self.trace.emit(
                session_id=session_id,
                category=TraceCategory.AGENT,
                name=agent_name,
                status=TraceStatus.FAILED,
                message=str(exc),
                duration_ms=(perf_counter() - started) * 1000,
            )
            raise
        self.trace.emit(
            session_id=session_id,
            category=TraceCategory.AGENT,
            name=agent_name,
            status=TraceStatus.COMPLETED,
            details={"output_type": type(result).__name__},
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
            "classification": tool.access.value,
            "arguments": self._safe_trace_arguments(arguments),
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
                details=trace_details,
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
            details=trace_details,
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
            details={"classification": access.value},
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


def tool_error(message: str, exc: Exception) -> ToolExecutionError:
    return ToolExecutionError(f"{message}: {exc}")
