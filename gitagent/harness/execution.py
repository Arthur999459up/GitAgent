"""Harness orchestration over the public Capability Layer API."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any, TypeVar

from gitagent.capability import CapabilityLayer, CapabilityResult, InvocationContext
from gitagent.domain.errors import ValidationError
from gitagent.domain.models import AgentGuidance, AgentSpec
from gitagent.harness.context.state import AgentContext
from gitagent.harness.validation.output import validate_agent_output
from gitagent.infra.observability import AuditLog, TraceBus, TraceCategory, TraceStatus

T = TypeVar("T")


class AgentHarness:
    """Own agent state, approval workflow, observations, and capability access."""

    def __init__(
        self,
        capabilities: CapabilityLayer,
        *,
        audit: AuditLog | None = None,
        trace: TraceBus | None = None,
        context_budget: int = 26_112,
    ) -> None:
        if not isinstance(context_budget, int) or isinstance(context_budget, bool) or context_budget < 4096:
            raise ValueError("context_budget must be an integer of at least 4096")
        self._capabilities = capabilities
        self.approvals = capabilities.policy.approvals
        self.audit = audit or AuditLog()
        self.trace = trace or TraceBus()
        self.context_budget = context_budget
        self._specs: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        if spec.name in self._specs:
            raise ValidationError(f"duplicate agent spec: {spec.name}")
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

    def invoke(
        self,
        context: AgentContext,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        approval_id: str | None = None,
    ) -> CapabilityResult:
        invocation = self.invocation_context(context, approval_id=approval_id)
        visible = next((item for item in self.discover(context) if item.id == capability_id), None)
        result = self._capabilities.invoke(capability_id, arguments, invocation)
        audit_result = (
            "OK"
            if result.status == "success"
            else "DENIED"
            if result.status == "approval_required"
            else "FAILED"
        )
        details: dict[str, Any] = {"argument_keys": sorted(arguments), "attempts": result.attempts}
        if result.error is not None:
            details["error"] = result.error.type.value
        self.audit.record(
            session_id=context.session_id,
            agent=context.agent,
            capability_id=capability_id,
            action=capability_id,
            classification=visible.access if visible is not None else None,
            approval_id=approval_id,
            result=audit_result,
            details=details,
        )
        return result

    def discover(self, context: AgentContext) -> tuple[Any, ...]:
        return self._capabilities.discover(self.invocation_context(context))

    def llm_tools(self, context: AgentContext) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": self._function_name(capability.id),
                    "description": capability.description,
                    "parameters": capability.input_schema,
                },
            }
            for capability in self.discover(context)
            if capability.input_schema is not None
        ]

    def resolve_llm_name(self, name: str, context: AgentContext) -> str:
        for capability in self.discover(context):
            if self._function_name(capability.id) == name:
                return capability.id
        return name

    @staticmethod
    def _function_name(capability_id: str) -> str:
        return "capability__" + capability_id.replace(".", "__").replace("-", "_")

    @staticmethod
    def invocation_context(context: AgentContext, *, approval_id: str | None = None) -> InvocationContext:
        return InvocationContext(
            run_id=context.run_id,
            session_id=context.session_id,
            agent_id=context.agent,
            repository=context.repository,
            approval_id=approval_id,
            delegation_depth=context.delegation_depth,
        )

    def specs_for(self, route: Any) -> tuple[AgentSpec, ...]:
        return tuple(spec for spec in self._specs.values() if route in spec.routes)

    def spec(self, agent_name: str) -> AgentSpec:
        try:
            return self._specs[agent_name]
        except KeyError as exc:
            raise ValidationError(f"unknown agent: {agent_name}") from exc
