from types import SimpleNamespace

import pytest

from gitagent.domain.errors import LLMProviderError
from gitagent.model.chat_client import OpenAIChatClient


class _Completions:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests = []

    def create(self, **params):
        self.requests.append(params)
        if self.error is not None:
            raise self.error
        return self.response


class _Client:
    def __init__(self, completions: _Completions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _tool_call(call_id: str = "call-1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="lookup_issue", arguments='{"number":888}'),
    )


def _response(*, reasoning_content: str | None = None):
    message = SimpleNamespace(
        content=None,
        reasoning_content=reasoning_content,
        tool_calls=[_tool_call("provider-call")],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _synthetic_tool_turn():
    return [
        {"role": "user", "content": "查询 issue 888"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "synthetic-1",
                    "type": "function",
                    "function": {
                        "name": "lookup_issue",
                        "arguments": '{"number":888}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "synthetic-1",
            "content": '{"status":"failed","error":"resource_not_found"}',
        },
    ]


def test_deepseek_outbound_adds_empty_reasoning_for_synthetic_tool_call_and_preserves_response():
    completions = _Completions(_response(reasoning_content="provider reasoning"))
    client = OpenAIChatClient(
        "deepseek-v4-flash",
        "test-key",
        "https://api.deepseek.com",
        client=_Client(completions),
    )

    response = client.chat(messages=_synthetic_tool_turn(), tools=[{"type": "function"}])

    sent = completions.requests[0]["messages"]
    assert "reasoning_content" not in _synthetic_tool_turn()[1]
    assert sent[1]["reasoning_content"] == ""
    assert response.reasoning_content == "provider reasoning"
    assert response.message["reasoning_content"] == "provider reasoning"


def test_non_deepseek_outbound_strips_reasoning_extension():
    completions = _Completions(_response())
    client = OpenAIChatClient(
        "gpt-test",
        "test-key",
        "https://api.openai.com/v1",
        client=_Client(completions),
    )
    messages = _synthetic_tool_turn()
    messages[1]["reasoning_content"] = "persisted provider metadata"

    client.chat(messages=messages, tools=[{"type": "function"}])

    sent = completions.requests[0]["messages"]
    assert "reasoning_content" not in sent[1]
    assert messages[1]["reasoning_content"] == "persisted provider metadata"


def test_provider_error_keeps_safe_status_and_reason():
    error = RuntimeError("bad request")
    error.status_code = 400
    error.body = {
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_request_error",
            "message": "reasoning_content must be passed back",
        }
    }
    completions = _Completions(error=error)
    client = OpenAIChatClient(
        "deepseek-v4-flash",
        "test-key",
        "https://api.deepseek.com",
        client=_Client(completions),
    )

    with pytest.raises(LLMProviderError) as captured:
        client.chat(messages=[{"role": "user", "content": "hello"}])

    message = str(captured.value)
    assert "HTTP 400" in message
    assert "invalid_request_error" in message
    assert "reasoning_content must be passed back" in message
