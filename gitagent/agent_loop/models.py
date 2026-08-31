"""Runtime contracts for native model, Capability, and Agent calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from gitagent.domain.models import PlannedCapabilityCall


@dataclass(frozen=True)
class StructuredCall:
    """One provider call after transport normalization."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    """The only two model response channels visible to an Agent Loop."""

    text: str
    call: StructuredCall | None
    assistant_message: dict[str, Any]


@dataclass(frozen=True)
class CapabilityCall:
    call_id: str
    capability_id: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AgentCall:
    call_id: str
    agent_id: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AgentResult:
    """The semantic child-to-parent result; typed artifacts stay in runtime state."""

    call_id: str
    agent_id: str
    status: str
    content: str
    error: dict[str, Any] | None = None


@dataclass
class PendingCall:
    """An exact set of Capability calls waiting for explicit approval."""

    approval_id: str
    summary: str
    calls: list[PlannedCapabilityCall]
    provider_call_id: str | None = None


class AgentLoopAgent(Protocol):
    def step(self, context: Any) -> ModelResponse: ...

    def build_result(self, context: Any) -> Any: ...
