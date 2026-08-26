"""Harness orchestration for agent execution and tool dispatch."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any, TypeVar

from gitagent.domain.errors import PermissionDenied, ValidationError
from gitagent.domain.models import AccessLevel, AgentGuidance, AgentSpec
from gitagent.harness.constraints import ApprovalStore
from gitagent.harness.context.state import AgentContext
from gitagent.harness.tools import MCPClient
from gitagent.harness.validation.output import validate_agent_output
from gitagent.infra.observability import AuditLog, TraceBus, TraceCategory, TraceStatus
from gitagent.infra.tool_hosts import MCPServer

T = TypeVar("T")


class AgentHarness:
    """Compose context, tools, constraints, validation, recovery, and observability."""

    def __init__(
        self,
        server: MCPServer,
        *,
        approvals: ApprovalStore | None = None,
        audit: AuditLog | None = None,
        trace: TraceBus | None = None,
        context_budget: int = 26_112,
    ) -> None:
        if not isinstance(context_budget, int) or isinstance(context_budget, bool) or context_budget < 4096:
            raise ValueError("context_budget must be an integer of at least 4096")
        self.server = server
        self.client = MCPClient(server)
        self.approvals = approvals or ApprovalStore()
        self.audit = audit or AuditLog()
        self.trace = trace or TraceBus()
        self.context_budget = context_budget
        self._specs: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        if spec.name in self._specs:
            raise ValidationError(f"duplicate agent spec: {spec.name}")
        available = {tool.name for tool in self.server.tools}
        unknown_tools = sorted(spec.allowed_tools - available)
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
        return AgentContext(
            self,
            self.spec(agent_name),
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
        )
        try:
            result = operation(context)
            validate_agent_output(context.spec, result)
        except Exception as exc:
            context.error = str(exc)
            self.trace.emit(
                session_id=session_id,
                category=TraceCategory.AGENT,
                name=agent_name,
                status=TraceStatus.FAILED,
                message=str(exc),
                details={"error_type": type(exc).__name__},
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
        if name not in context.spec.allowed_tools:
            self._record_denied(context, tool.access, name, approval_id, "tool is outside agent allowlist")
            raise PermissionDenied(f"agent {context.agent} is not allowed to use {name}")

        started = perf_counter()
        details = {
            "agent": context.agent,
            "classification": tool.access.value,
            "argument_keys": sorted(arguments),
        }
        self.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.TOOL_USE,
            name=name,
            status=TraceStatus.STARTED,
            details=details,
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
                agent=context.agent,
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
                details={**details, "error_type": type(exc).__name__},
                duration_ms=(perf_counter() - started) * 1000,
            )
            raise

        self.audit.record(
            session_id=context.session_id,
            agent=context.agent,
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
            details={**details, "result_type": type(result).__name__},
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
            agent=context.agent,
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
