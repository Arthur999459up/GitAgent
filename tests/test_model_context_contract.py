from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gitagent.domain.errors import ContextWindowExceeded, StructuredOutputError
from gitagent.domain.models import SessionEvent
from gitagent.harness.context import (
    ContextBuilder,
    assistant_tool_call,
    derive_main_messages,
    fit_messages,
    request_tokens,
    tool_result_message,
)
from gitagent.infra.persistence import (
    SessionEventLog,
    SessionManager,
    StateStore,
    build_account_key,
    build_repository_key,
    default_working_state,
)
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


class RecordingClient:
    model = "test"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self, response: ChatResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> ChatResponse:
        self.calls.append(kwargs)
        return self.response


class Completions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        message = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_plain_json_looking_text_stays_text() -> None:
    reasoner = LLMReasoner(RecordingClient(ChatResponse(content='{"kind":"finish"}')))

    response = reasoner.complete_messages(messages=[{"role": "user", "content": "answer"}])

    assert response.text == '{"kind":"finish"}'
    assert response.call is None


def test_native_text_is_preserved_verbatim() -> None:
    reasoner = LLMReasoner(RecordingClient(ChatResponse(content="  exact child text\n")))

    response = reasoner.complete_messages(messages=[{"role": "user", "content": "answer"}])

    assert response.text == "  exact child text\n"


def test_text_and_one_structured_call_coexist_with_call_id() -> None:
    reasoner = LLMReasoner(
        RecordingClient(
            ChatResponse(
                content="I will inspect this.",
                tool_calls=[ToolCall("provider-call-7", "capability__repo__read", {"path": "a.py"})],
            )
        )
    )

    response = reasoner.complete_messages(messages=[{"role": "user", "content": "inspect"}])

    assert response.text == "I will inspect this."
    assert response.call is not None
    assert response.call.call_id == "provider-call-7"
    assert response.call.name == "capability__repo__read"
    assert response.assistant_message["tool_calls"][0]["id"] == "provider-call-7"


def test_multiple_calls_and_empty_response_are_rejected() -> None:
    multiple = LLMReasoner(
        RecordingClient(
            ChatResponse(
                tool_calls=[
                    ToolCall("one", "capability__a", {}),
                    ToolCall("two", "capability__b", {}),
                ]
            )
        )
    )
    with pytest.raises(StructuredOutputError) as raised:
        multiple.complete_messages(messages=[{"role": "user", "content": "go"}])
    assert raised.value.details["actual_tool_calls"] == 2

    empty = LLMReasoner(RecordingClient(ChatResponse()))
    with pytest.raises(StructuredOutputError):
        empty.complete_messages(messages=[{"role": "user", "content": "go"}])


def test_typed_output_never_falls_back_to_parsing_text_json() -> None:
    reasoner = LLMReasoner(RecordingClient(ChatResponse(content='{"value":"text"}')))
    tools = structured_tools("typed_result", SCHEMA)

    with pytest.raises(StructuredOutputError):
        reasoner.complete_structured_messages(
            messages=[{"role": "user", "content": "typed"}],
            schema=SCHEMA,
            tool_name="typed_result",
            final_tools=tools,
        )


def test_typed_output_requires_the_exact_function() -> None:
    reasoner = LLMReasoner(
        RecordingClient(
            ChatResponse(tool_calls=[ToolCall("call-1", "other", {"value": "x"})])
        )
    )
    with pytest.raises(StructuredOutputError):
        reasoner.complete_structured_messages(
            messages=[{"role": "user", "content": "typed"}],
            schema=SCHEMA,
            tool_name="typed_result",
            final_tools=structured_tools("typed_result", SCHEMA),
        )


def test_context_window_accounts_for_the_exact_tool_payload() -> None:
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "x"}]
    tools = structured_tools("typed_result", SCHEMA)
    window = request_tokens(messages, tools)
    with pytest.raises(ContextWindowExceeded):
        fit_messages(messages, tools, context_window_tokens=window)


def test_openai_transport_disables_parallel_calls_and_preserves_tools() -> None:
    completions = Completions()
    transport = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    messages = [{"role": "user", "content": "edge"}]
    tools = structured_tools("typed_result", SCHEMA)
    client = OpenAIChatClient(
        "test",
        "key",
        client=transport,
        context_window_tokens=request_tokens(messages, tools) + 10,
    )

    client.chat(messages, tools)

    params = completions.calls[0]
    assert params["parallel_tool_calls"] is False
    assert params["tools"] is tools


def test_paused_child_user_input_is_not_copied_to_main_and_result_is_correlated() -> None:
    messages = [
        SessionEvent(
            1,
            1,
            "user_message",
            "2026-09-01T00:00:00Z",
            "session",
            1,
            None,
            {"content": "fix issue 7"},
        ),
        SessionEvent(
            1,
            2,
            "model_message",
            "2026-09-01T00:00:01Z",
            "session",
            1,
            "main",
            {
                "message": assistant_tool_call(
                    "agent-call-7",
                    "agent__issues",
                    {"task": "fix issue 7", "issue_number": 7, "mode": "task"},
                )
            },
        ),
        SessionEvent(
            1,
            3,
            "user_message",
            "2026-09-01T00:00:02Z",
            "session",
            2,
            "issues",
            {"content": "approve the proposed mutation"},
        ),
        SessionEvent(
            1,
            4,
            "assistant_message",
            "2026-09-01T00:00:02Z",
            "session",
            2,
            "issues",
            {"content": "Approve this exact mutation?"},
        ),
        SessionEvent(
            1,
            5,
            "user_message",
            "2026-09-01T00:00:03Z",
            "session",
            3,
            None,
            {"content": "now inspect the tests"},
        ),
        SessionEvent(
            1,
            6,
            "model_message",
            "2026-09-01T00:00:04Z",
            "session",
            3,
            "main",
            {
                "message": tool_result_message(
                    "agent-call-7",
                    {"status": "completed", "content": "Issue fixed."},
                )
            },
        ),
    ]

    projected = derive_main_messages(messages)

    assert [message["role"] for message in projected] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert all(
        "approve the proposed mutation" not in (message.get("content") or "")
        for message in projected
    )
    assert all(
        "Approve this exact mutation?" not in (message.get("content") or "")
        for message in projected
    )
    assert projected[2]["tool_call_id"] == "agent-call-7"


def test_main_projection_does_not_duplicate_native_final_text_from_ui_event() -> None:
    events = [
        SessionEvent(
            1,
            1,
            "user_message",
            "2026-09-01T00:00:00Z",
            "session",
            1,
            None,
            {"content": "hello"},
        ),
        SessionEvent(
            1,
            2,
            "model_message",
            "2026-09-01T00:00:01Z",
            "session",
            1,
            "main",
            {"message": {"role": "assistant", "content": "final answer"}},
        ),
        SessionEvent(
            1,
            3,
            "assistant_message",
            "2026-09-01T00:00:02Z",
            "session",
            1,
            None,
            {"content": "final answer"},
        ),
    ]

    assert derive_main_messages(events) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "final answer"},
    ]


def test_persisted_child_pause_keeps_main_agent_call_open_and_atomic(tmp_path: Any) -> None:
    store = StateStore(tmp_path / "state.db")
    event_log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    sessions = SessionManager(store, event_log)
    account_key = build_account_key("https://api.github.test", 1)
    repository_key = build_repository_key("https://api.github.test", 7)
    session = sessions.create_session(account_key, repository_key, "owner/repo")
    scope = session.scope

    first = sessions.start_turn(scope, "fix issue 7")
    sessions.record_model_message(
        scope,
        assistant_tool_call(
            "agent-call-7",
            "agent__issues",
            {"task": "fix issue 7", "issue_number": 7, "mode": "task"},
        ),
        turn_seq=first.seq,
        agent="main",
    )
    sessions.complete_turn(
        scope,
        first.seq,
        assistant_text="Approve this mutation?",
        assistant_agent="issues",
        workflow_summary="waiting for approval",
        route=None,
        entity_manifests=(),
        working_state=default_working_state(),
    )
    second = sessions.start_turn(scope, "approve", agent="issues")

    messages, _ = ContextBuilder(sessions).build(
        scope,
        "owner/repo",
        "approve",
        system="main system",
        turn_seq=second.seq,
        current_user_is_main=False,
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert messages[-1]["tool_calls"][0]["id"] == "agent-call-7"
    assert all(
        "Approve this mutation?" not in (message.get("content") or "")
        for message in messages
    )
