from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from gitagent.agent_loop import (
    AgentCall,
    AgentLoop,
    CapabilityCall,
    ModelResponse,
    StructuredCall,
)
from gitagent.agents.coding import CODING_SPEC, CodingAgent
from gitagent.capability import AccessLevel
from gitagent.domain.errors import StructuredOutputError, ValidationError, WorkflowError
from gitagent.domain.models import CodingTask
from gitagent.harness.context.state import AgentContext

_RESULT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _message(
    *,
    text: str = "",
    calls: list[StructuredCall] | None = None,
) -> ModelResponse:
    calls = list(calls or [])
    assistant: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        assistant["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in calls
        ]
    return ModelResponse(text, calls, assistant)


class _Reasoner:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete_messages(self, **_: Any) -> ModelResponse:
        self.calls += 1
        return self.responses.pop(0)


@dataclass(frozen=True)
class _Capability:
    id: str
    access: AccessLevel
    input_schema: dict[str, Any]


class _Trace:
    def emit(self, **_: Any) -> None:
        return None


class _Coordinator:
    @contextmanager
    def cancellation_scope(self, _: Any) -> Iterator[None]:
        yield

    @contextmanager
    def claim_resources(self, _: Any) -> Iterator[None]:
        yield

    @staticmethod
    def cancellation_requested() -> bool:
        return False


class _Harness:
    def __init__(
        self,
        capabilities: list[_Capability] | None = None,
        *,
        max_structured_retries: int = 1,
    ) -> None:
        self.capabilities = list(capabilities or [])
        self.max_calls_per_turn = 8
        self.max_structured_retries = max_structured_retries
        self.max_provider_retries = 0
        self.coordinator = _Coordinator()
        self.trace = _Trace()
        self.message_sink = None
        self.compaction_sink = None

    def register(self, _: Any) -> None:
        return None

    @staticmethod
    def context_window_for(_: str) -> int:
        return 32_768

    def llm_tools(self, _: AgentContext, *, read_only: bool = False) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": self.function_name(capability.id),
                    "description": capability.id,
                    "parameters": capability.input_schema,
                },
            }
            for capability in self.capabilities
            if not read_only or capability.access == AccessLevel.READ
        ]

    def discover(self, _: AgentContext) -> tuple[_Capability, ...]:
        return tuple(self.capabilities)

    def resolve_model_call(
        self, call: StructuredCall, _: AgentContext
    ) -> CapabilityCall | AgentCall:
        if call.name.startswith("agent__"):
            return AgentCall(call.call_id, call.name.removeprefix("agent__"), call.arguments)
        for capability in self.capabilities:
            if call.name == self.function_name(capability.id):
                return CapabilityCall(call.call_id, capability.id, call.arguments)
        raise ValidationError(f"unknown Capability function: {call.name}")

    @staticmethod
    def capability_permission_decision(_: AgentContext, __: CapabilityCall) -> str:
        return "ALLOW"

    @staticmethod
    def function_name(capability_id: str) -> str:
        return "capability__" + capability_id.replace(".", "__")


class _BatchDispatcher:
    def __init__(self) -> None:
        self.batches: list[list[CapabilityCall]] = []

    def execute_capability_batch(
        self,
        context: AgentContext,
        calls: list[CapabilityCall],
        *,
        summary: str = "",
    ) -> bool:
        del summary
        self.batches.append(list(calls))
        for call in calls:
            context.append_tool_result(
                {"capability_id": call.capability_id, "ok": True},
                call_id=call.call_id,
            )
        return True


def _context(harness: _Harness, *, max_steps: int = 4) -> AgentContext:
    return AgentContext(
        harness,  # type: ignore[arg-type]
        CODING_SPEC,
        "session",
        repository="owner/repo",
        goal="task",
        max_steps=max_steps,
    )


@pytest.mark.parametrize("read_count", [1, 2])
def test_read_capabilities_continue_to_one_typed_result(read_count: int) -> None:
    capabilities = [
        _Capability(f"repository.read_{index}", AccessLevel.READ, {"type": "object"})
        for index in range(read_count)
    ]
    read_calls = [
        StructuredCall(
            f"read-{index}",
            _Harness.function_name(capability.id),
            {},
        )
        for index, capability in enumerate(capabilities)
    ]
    reasoner = _Reasoner(
        [
            _message(calls=read_calls),
            _message(
                calls=[StructuredCall("final", "return_result", {"answer": "done"})]
            ),
        ]
    )
    harness = _Harness(capabilities)
    agent = CodingAgent(harness, reasoner)  # type: ignore[arg-type]
    dispatcher = _BatchDispatcher()
    agent.dispatcher = dispatcher  # type: ignore[assignment]

    value = agent._complete_structured_task(
        _context(harness),
        prompt="complete the task",
        schema=_RESULT_SCHEMA,
        tool_name="return_result",
    )

    assert value == {"answer": "done"}
    assert [[call.capability_id for call in batch] for batch in dispatcher.batches] == [
        [capability.id for capability in capabilities]
    ]


def test_text_only_response_uses_agent_loop_structured_retry() -> None:
    explanation = {
        "behavior_changes": [],
        "key_symbols": [],
        "call_relationships": [],
        "impact_scope": [],
    }
    reasoner = _Reasoner(
        [
            _message(text="plain text is not the typed result"),
            _message(
                calls=[
                    StructuredCall("typed", "explain_code_change", explanation)
                ]
            ),
            _message(text="完成。"),
        ]
    )
    harness = _Harness(max_structured_retries=1)
    agent = CodingAgent(harness, reasoner)  # type: ignore[arg-type]
    context = _context(harness)
    context.coding_task = CodingTask(mode="explain", task="explain", evidence={})

    AgentLoop(harness).start(context, agent)  # type: ignore[arg-type]

    assert context.finished
    assert context.error is None
    assert context.code_explanation is not None
    assert reasoner.calls == 3
    assert any(
        message.get("role") == "user"
        and "structured-call contract" in str(message.get("content") or "")
        for message in context.messages
    )


def test_text_only_response_fails_after_structured_retry_limit() -> None:
    reasoner = _Reasoner([_message(text="invalid"), _message(text="still invalid")])
    harness = _Harness(max_structured_retries=1)
    agent = CodingAgent(harness, reasoner)  # type: ignore[arg-type]
    context = _context(harness)
    context.coding_task = CodingTask(mode="explain", task="explain", evidence={})

    AgentLoop(harness).start(context, agent)  # type: ignore[arg-type]

    assert context.finished
    assert context.error is not None
    assert "重试上限" in context.error
    assert reasoner.calls == 2


@pytest.mark.parametrize(
    ("calls", "error"),
    [
        (
            [
                StructuredCall("final", "return_result", {"answer": "done"}),
                StructuredCall("read", "capability__repository__read", {}),
            ],
            StructuredOutputError,
        ),
        (
            [StructuredCall("invalid-final", "return_result", {"answer": 1})],
            StructuredOutputError,
        ),
        ([StructuredCall("delegate", "agent__repository", {"task": "x"})], WorkflowError),
        ([StructuredCall("unknown", "capability__unknown", {})], StructuredOutputError),
    ],
)
def test_invalid_call_combinations_are_not_accepted(
    calls: list[StructuredCall], error: type[Exception]
) -> None:
    harness = _Harness(
        [_Capability("repository.read", AccessLevel.READ, {"type": "object"})]
    )
    agent = CodingAgent(harness, _Reasoner([_message(calls=calls)]))  # type: ignore[arg-type]

    with pytest.raises(error):
        agent._complete_structured_task(
            _context(harness),
            prompt="complete",
            schema=_RESULT_SCHEMA,
            tool_name="return_result",
        )


def test_write_capability_is_rejected() -> None:
    write = _Capability("repository.write", AccessLevel.WRITE, {"type": "object"})
    harness = _Harness([write])
    agent = CodingAgent(  # type: ignore[arg-type]
        harness,
        _Reasoner(
            [
                _message(
                    calls=[
                        StructuredCall(
                            "write", harness.function_name(write.id), {}
                        )
                    ]
                )
            ]
        ),
    )

    with pytest.raises(WorkflowError, match="READ"):
        agent._complete_structured_task(
            _context(harness),
            prompt="complete",
            schema=_RESULT_SCHEMA,
            tool_name="return_result",
        )


def test_evidence_gathering_honors_context_step_limit() -> None:
    read = _Capability("repository.read", AccessLevel.READ, {"type": "object"})
    harness = _Harness([read])
    agent = CodingAgent(  # type: ignore[arg-type]
        harness,
        _Reasoner(
            [
                _message(
                    calls=[StructuredCall("read", harness.function_name(read.id), {})]
                )
            ]
        ),
    )
    agent.dispatcher = _BatchDispatcher()  # type: ignore[assignment]

    with pytest.raises(WorkflowError, match="evidence limit"):
        agent._complete_structured_task(
            _context(harness, max_steps=1),
            prompt="complete",
            schema=_RESULT_SCHEMA,
            tool_name="return_result",
        )
