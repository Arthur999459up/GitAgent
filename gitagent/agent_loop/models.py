"""Runtime contracts for native model, Capability, and Agent calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from gitagent.domain.errors import StructuredOutputError
from gitagent.domain.models import PlannedCapabilityCall


@dataclass(frozen=True)
class StructuredCall:
    """One provider call after transport normalization."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    """One native model response before the Agent gives it runtime meaning."""

    text: str
    calls: list[StructuredCall]
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


@dataclass(frozen=True)
class WaitForUser:
    """An explicit non-terminal Agent step result requiring one user answer."""

    question: str
    call_id: str | None = None


def wait_for_user_tool() -> dict[str, Any]:
    """Return the native Runtime tool used to make a pause explicit."""

    return {
        "type": "function",
        "function": {
            "name": "runtime__wait_for_user",
            "description": (
                "Pause this Agent call only when one necessary user answer is missing. "
                "The same Agent message thread will resume with that answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1, "maxLength": 4000}
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    }


def explicit_wait(response: ModelResponse) -> ModelResponse | WaitForUser:
    """Translate only the dedicated Runtime call into a waiting step result."""

    waiting = [
        call for call in response.calls if call.name == "runtime__wait_for_user"
    ]
    if not waiting:
        return response
    if len(response.calls) != 1:
        raise StructuredOutputError(
            "runtime__wait_for_user must be the only structured call in a response"
        )
    call = waiting[0]
    question = call.arguments.get("question")
    if not isinstance(question, str) or not question.strip():
        raise StructuredOutputError(
            "runtime__wait_for_user requires a non-empty question"
        )
    if set(call.arguments) != {"question"}:
        raise StructuredOutputError("runtime__wait_for_user accepts only question")
    return WaitForUser(question.strip(), call.call_id)


@dataclass
class PendingCall:
    """An exact set of Capability calls waiting for explicit approval."""

    approval_id: str
    summary: str
    calls: list[PlannedCapabilityCall]
    provider_call_id: str | None = None


class AgentLoopAgent(Protocol):
    def step(self, context: Any) -> ModelResponse | WaitForUser: ...

    def build_result(self, context: Any) -> Any: ...
