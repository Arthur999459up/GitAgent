from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from gitagent.agent_loop import AgentLoop
from gitagent.agents.main import _MAIN_SCHEMA, MainAgent
from gitagent.domain.errors import ContextWindowExceeded, StructuredOutputError
from gitagent.domain.models import AgentSpec
from gitagent.harness.context import fit_messages, request_tokens
from gitagent.harness.context.state import AgentContext
from gitagent.model import (
    ChatResponse,
    LLMReasoner,
    OpenAIChatClient,
    ToolCall,
    structured_tools,
)

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        return _raw_response()


class _OpenAITransport:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions())


class _RecordingChatClient:
    model = "test"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self, response: ChatResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_token: Any | None = None,
        *,
        context_window_tokens: int | None = None,
    ) -> ChatResponse:
        del on_token
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "context_window_tokens": context_window_tokens,
            }
        )
        return self.response


class _Trace:
    def emit(self, **details: Any) -> None:
        del details


class _ContextHarness:
    def __init__(self, context_window_tokens: int = 32_768) -> None:
        self.window = context_window_tokens
        self.message_sink = None
        self.compaction_sink = None
        self.trace = _Trace()

    def context_window_for(self, agent_name: str) -> int:
        del agent_name
        return self.window


def _raw_response() -> Any:
    message = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _context(window: int = 32_768) -> AgentContext:
    spec = AgentSpec("issues", "test", "system", (), frozenset())
    return AgentContext(_ContextHarness(window), spec, "session", goal="test")


def test_fit_uses_the_context_window_without_an_earlier_input_limit() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "x" * 45_000},
    ]
    assert 13_824 < request_tokens(messages) < 32_768

    fitted, _, _ = fit_messages(
        messages, None, context_window_tokens=32_768
    )

    assert fitted == messages


def test_all_model_visible_input_shares_one_context_window() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "history question"},
        {"role": "assistant", "content": "history answer"},
        {"role": "user", "content": "current"},
    ]
    tools = structured_tools("respond", SCHEMA)
    input_tokens = request_tokens(messages, tools)
    transport = _OpenAITransport()
    client = OpenAIChatClient(
        "test",
        "key",
        client=transport,
        context_window_tokens=input_tokens,
    )

    with pytest.raises(ContextWindowExceeded) as raised:
        client.chat(messages, tools)

    assert raised.value.input_tokens == input_tokens
    assert raised.value.context_window_tokens == input_tokens
    assert raised.value.remaining_tokens == 0
    assert transport.chat.completions.calls == []


def test_structured_schema_is_counted_before_context_fitting() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current"},
    ]
    final_tools = structured_tools("respond", SCHEMA)
    window = request_tokens(messages) + 1
    assert request_tokens(messages, final_tools) >= window

    with pytest.raises(ContextWindowExceeded):
        fit_messages(
            messages,
            final_tools,
            context_window_tokens=window,
        )


def test_domain_estimator_and_provider_receive_the_same_final_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = ChatResponse(tool_calls=[ToolCall("call-1", "respond", {"value": "ok"})])
    client = _RecordingChatClient(response)
    reasoner = LLMReasoner(client)
    context = _context()
    estimated: list[dict[str, Any]] | None = None

    from gitagent.harness.context import state as state_module

    original_fit = state_module.fit_messages_with_plan

    def recording_fit(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        context_window_tokens: int,
    ) -> Any:
        nonlocal estimated
        estimated = tools
        return original_fit(
            messages,
            tools,
            context_window_tokens=context_window_tokens,
        )

    monkeypatch.setattr(state_module, "fit_messages_with_plan", recording_fit)

    value = context.reason_structured(
        reasoner,
        schema=SCHEMA,
        tool_name="respond",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "capability__read",
                    "description": "read",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert value == {"value": "ok"}
    assert estimated is client.calls[0]["tools"]
    assert [tool["function"]["name"] for tool in estimated or []] == [
        "respond",
        "capability__read",
    ]


def test_openai_request_shrinks_output_and_disables_parallel_tools() -> None:
    transport = _OpenAITransport()
    messages = [{"role": "user", "content": "edge"}]
    tools = structured_tools("respond", SCHEMA)
    remaining = 7
    client = OpenAIChatClient(
        "test",
        "key",
        client=transport,
        max_output_tokens=100,
        context_window_tokens=request_tokens(messages, tools) + remaining,
    )

    client.chat(messages, tools)

    params = transport.chat.completions.calls[0]
    assert params["tools"] is tools
    assert params["parallel_tool_calls"] is False
    assert params["max_tokens"] == remaining
    assert request_tokens(params["messages"], params["tools"]) + params["max_tokens"] <= client.context_window_tokens


def test_client_rejects_edge_request_before_provider_call() -> None:
    transport = _OpenAITransport()
    messages = [{"role": "user", "content": "edge"}]
    tools = structured_tools("respond", SCHEMA)
    input_tokens = request_tokens(messages, tools)
    client = OpenAIChatClient(
        "test",
        "key",
        client=transport,
        max_output_tokens=100,
        context_window_tokens=input_tokens,
    )

    with pytest.raises(ContextWindowExceeded) as raised:
        client.chat(messages, tools)

    assert transport.chat.completions.calls == []
    assert raised.value.requested_output_tokens == 100
    assert raised.value.remaining_tokens == 0


def test_structured_failure_does_not_mutate_caller_messages() -> None:
    client = _RecordingChatClient(ChatResponse(content="not structured"))
    reasoner = LLMReasoner(client)
    messages = [{"role": "user", "content": "original"}]
    original = copy.deepcopy(messages)
    final_tools = structured_tools("respond", SCHEMA)

    with pytest.raises(StructuredOutputError):
        reasoner.complete_structured_messages(
            messages=messages,
            schema=SCHEMA,
            tool_name="respond",
            final_tools=final_tools,
            context_window_tokens=32_768,
        )

    assert messages == original
    assert len(client.calls) == 1


def test_main_durable_history_does_not_receive_internal_correction() -> None:
    client = _RecordingChatClient(ChatResponse(content="not structured"))
    reasoner = LLMReasoner(client)
    durable: list[dict[str, Any]] = []

    class MainHarness:
        def register(self, spec: AgentSpec) -> None:
            del spec

        def context_window_for(self, agent_name: str) -> int:
            assert agent_name == "main"
            return 32_768

    agent = MainAgent(
        MainHarness(),
        reasoner,
        message_sink=lambda message: durable.append(message) or message,
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "real input"},
    ]
    original = copy.deepcopy(messages)
    final_tools = structured_tools("route_session_turn", _MAIN_SCHEMA)

    with pytest.raises(StructuredOutputError):
        agent._semantic_decision(
            SimpleNamespace(), "real input", messages, final_tools
        )

    assert messages == original
    assert durable == []


def test_domain_loop_records_structured_error_without_correction_messages() -> None:
    client = _RecordingChatClient(ChatResponse(content="not structured"))
    reasoner = LLMReasoner(client)
    context = _context()
    context.max_steps = 2
    durable: list[dict[str, Any]] = []
    context.origin_turn_seq = 1
    context._harness.message_sink = (
        lambda current, message: durable.append(message) or message
    )

    class FailingAgent:
        def decide(self, current: AgentContext) -> Any:
            return current.reason_structured(
                reasoner,
                schema=SCHEMA,
                tool_name="respond",
            )

        def build_result(self, current: AgentContext) -> Any:
            del current
            return None

    AgentLoop(context._harness).start(context, FailingAgent())

    assert context.finished
    assert len(context.observations) == 2
    assert {item["kind"] for item in context.observations} == {
        "structured_output_error"
    }
    assert [message["role"] for message in context.messages] == ["system", "user"]
    assert durable == context.messages
    assert all("previous response" not in str(message).casefold() for message in context.messages)

    context.append_message({"role": "user", "content": "next real input"})
    assert context.model_messages()[-1]["content"] == "next real input"


def test_multiple_tool_calls_are_rejected_but_one_tool_call_succeeds() -> None:
    final_tools = structured_tools("respond", SCHEMA)
    messages = [{"role": "user", "content": "respond"}]
    multiple = LLMReasoner(
        _RecordingChatClient(
            ChatResponse(
                tool_calls=[
                    ToolCall("call-1", "respond", {"value": "one"}),
                    ToolCall("call-2", "respond", {"value": "two"}),
                ]
            )
        )
    )

    with pytest.raises(StructuredOutputError) as raised:
        multiple.complete_structured_messages(
            messages=messages,
            schema=SCHEMA,
            tool_name="respond",
            final_tools=final_tools,
            context_window_tokens=32_768,
        )
    assert raised.value.details == {
        "expected_tool_calls": 1,
        "actual_tool_calls": 2,
    }

    single = LLMReasoner(
        _RecordingChatClient(
            ChatResponse(
                tool_calls=[ToolCall("call-1", "respond", {"value": "one"})]
            )
        )
    )
    assert single.complete_structured_messages(
        messages=messages,
        schema=SCHEMA,
        tool_name="respond",
        final_tools=final_tools,
        context_window_tokens=32_768,
    ) == {"value": "one"}
