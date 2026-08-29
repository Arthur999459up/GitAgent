import json
from copy import deepcopy

from gitagent.domain.models import SessionEvent
from gitagent.harness.context import (
    assistant_tool_call,
    derive_domain_messages,
    derive_main_messages,
    fit_messages,
    tool_result_message,
)
from gitagent.model import ChatResponse, LLMReasoner


def _event(seq: int, event_type: str, *, agent=None, data=None, turn=1) -> SessionEvent:
    return SessionEvent(
        version=1,
        seq=seq,
        type=event_type,
        time="2026-01-01T00:00:00+00:00",
        session_id="session-" + "a" * 32,
        turn_seq=turn,
        agent=agent,
        data=data or {},
    )


class _CaptureClient:
    model = "fake"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self) -> None:
        self.requests = []

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        return ChatResponse(content="ok")


def test_main_exact_history_reaches_provider_unmodified() -> None:
    events = [
        _event(1, "user_message", data={"content": "U1"}),
        _event(2, "assistant_message", data={"content": "A1"}),
        _event(3, "user_message", data={"content": "U2"}, turn=2),
        _event(4, "assistant_message", data={"content": "A2"}, turn=2),
        _event(5, "user_message", data={"content": "U3"}, turn=3),
    ]
    request = [
        {"role": "system", "content": "current system"},
        *derive_main_messages(events),
    ]
    client = _CaptureClient()

    LLMReasoner(client).complete_text_messages(messages=request)

    assert client.requests == [(request, None)]
    assert [message["content"] for message in request] == [
        "current system",
        "U1",
        "A1",
        "U2",
        "A2",
        "U3",
    ]


def test_main_exact_history_and_domain_isolation() -> None:
    route = assistant_tool_call("call-route", "route_session_turn", {"target_agent": "issues"})
    summary = tool_result_message("call-route", "semantic Domain Summary")
    events = [
        _event(1, "user_message", data={"content": "U1"}),
        _event(2, "model_message", agent="issues", data={"run_id": "r", "message": {"role": "system", "content": "domain system"}}),
        _event(3, "model_message", agent="issues", data={"run_id": "r", "message": {"role": "assistant", "content": "raw domain thought"}}),
        _event(4, "model_message", agent="main", data={"message": route}),
        _event(5, "model_message", agent="main", data={"message": summary}),
        _event(6, "assistant_message", data={"content": "A1"}),
        _event(7, "user_message", data={"content": "U2"}, turn=2),
        _event(8, "assistant_message", data={"content": "A2"}, turn=2),
        _event(9, "user_message", data={"content": "U3"}, turn=3),
    ]

    messages = derive_main_messages(events)

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    encoded = json.dumps(messages, ensure_ascii=False)
    assert "semantic Domain Summary" in encoded
    assert "raw domain thought" not in encoded
    assert "domain system" not in encoded


def test_domain_exact_history_replays_one_run() -> None:
    messages = [
        {"role": "system", "content": "domain system"},
        {"role": "user", "content": "delegated task"},
        assistant_tool_call("a", "capability__one", {"q": "x"}),
        tool_result_message("a", "result A"),
        assistant_tool_call("b", "capability__two", {"q": "y"}),
        tool_result_message("b", "result B"),
        {"role": "assistant", "content": "final"},
    ]
    events = [
        _event(index, "model_message", agent="issues", data={"run_id": "run-1", "message": message})
        for index, message in enumerate(messages, 1)
    ]

    assert derive_domain_messages(events, agent="issues", run_id="run-1") == messages


def test_domain_exact_history_reaches_provider_unmodified() -> None:
    messages = [
        {"role": "system", "content": "domain system"},
        {"role": "user", "content": "delegated task"},
        assistant_tool_call("a", "capability__one", {"q": "x"}),
        tool_result_message("a", "result A"),
        assistant_tool_call("b", "capability__two", {"q": "y"}),
        tool_result_message("b", "result B"),
        {"role": "assistant", "content": "final"},
    ]
    events = [
        _event(
            index,
            "model_message",
            agent="issues",
            data={"run_id": "run-2", "message": message},
        )
        for index, message in enumerate(messages, 1)
    ]
    request = derive_domain_messages(events, agent="issues", run_id="run-2")
    client = _CaptureClient()

    LLMReasoner(client).complete_text_messages(messages=request)

    assert client.requests == [(messages, None)]


def test_large_tool_arguments_remain_complete_valid_json() -> None:
    payload = {"path": "large.txt", "content": "x" * 20_000}

    message = assistant_tool_call("large-args", "repository_write", payload)
    arguments = message["tool_calls"][0]["function"]["arguments"]

    assert json.loads(arguments) == payload
    assert len(arguments) > 16 * 1024


def test_token_pressure_uses_content_size_and_preserves_tool_protocol() -> None:
    small = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert fit_messages(small, None, effective_input_budget=8000)[1] == "none"

    large_tool = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        assistant_tool_call("large", "capability__read", {}),
        tool_result_message("large", "x" * 16_000),
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "next"},
    ]
    fitted, level, _ = fit_messages(large_tool, None, effective_input_budget=8000)
    assert level == "light"
    visible_calls = {
        call["id"]
        for message in fitted
        for call in (message.get("tool_calls") or [])
    }
    visible_results = {
        message["tool_call_id"]
        for message in fitted
        if message.get("role") == "tool"
    }
    assert visible_calls == visible_results == {"large"}

    long_history = [{"role": "system", "content": "s"}]
    for index in range(30):
        long_history.extend(
            [
                {"role": "user", "content": f"U{index} " + "u" * 900},
                {"role": "assistant", "content": f"A{index} " + "a" * 900},
            ]
        )
    long_history.append({"role": "user", "content": "current"})
    fitted, level, _ = fit_messages(long_history, None, effective_input_budget=8000)
    assert level in {"summary", "emergency"}
    if level == "summary":
        assert fitted[1]["role"] == "system"
    assert fitted[-1] == {"role": "user", "content": "current"}

    emergency = [
        {"role": "system", "content": "s" * 16_000},
        {"role": "user", "content": "u" * 6_000},
    ]
    fitted, level, _ = fit_messages(emergency, None, effective_input_budget=8000)
    assert level == "emergency"
    assert fitted[-1]["role"] == "user"


def test_summary_or_emergency_compaction_never_splits_tool_pairs() -> None:
    history = [{"role": "system", "content": "system"}]
    for index in range(14):
        history.extend(
            [
                {"role": "user", "content": f"U{index} " + "u" * 1200},
                assistant_tool_call(f"call-{index}", "capability__read", {"index": index}),
                tool_result_message(f"call-{index}", f"result-{index}"),
                {"role": "assistant", "content": f"A{index} " + "a" * 1200},
            ]
        )
    history.append({"role": "user", "content": "current"})

    fitted, level, _ = fit_messages(history, None, effective_input_budget=8000)

    assert level in {"summary", "emergency"}
    visible_calls = {
        call["id"]
        for message in fitted
        for call in (message.get("tool_calls") or [])
    }
    visible_results = {
        message["tool_call_id"]
        for message in fitted
        if message.get("role") == "tool"
    }
    assert visible_calls == visible_results
