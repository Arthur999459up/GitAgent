import json
from types import SimpleNamespace
from typing import Any

import pytest
from AGENT.GitAgent.gitagent.core.errors import LLMProviderError, StructuredOutputError, ValidationError
from AGENT.GitAgent.gitagent.reasoning import ChatResponse, LLMReasoner, OpenAIChatClient, ToolCall


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.params = None

    def create(self, **params):
        self.params = params
        return self.response


class TimeoutCompletions:
    def create(self, **params):
        del params
        raise TimeoutError("provider stalled")


class FakeReasoningClient:
    model = "fake-model"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self, content):
        self.content = content
        self.messages = None

    def chat(self, messages, tools=None, on_token=None):
        del tools, on_token
        self.messages = messages
        return ChatResponse(content=self.content)


class FormatRetryClient:
    model = "fake-model"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, on_token=None):
        del on_token
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) == 1:
            return ChatResponse(content="I changed the file, but forgot the structured result.")
        return ChatResponse(
            tool_calls=[
                ToolCall(
                    id="retry-1",
                    name="prepare_candidate",
                    arguments={"files": {"src/app.py": "fixed\n"}},
                )
            ]
        )


def test_openai_client_normalizes_response_and_counts_tokens():
    function = SimpleNamespace(name="lookup", arguments=json.dumps({"path": "README.md"}))
    message = SimpleNamespace(
        content="done",
        tool_calls=[SimpleNamespace(id="call-1", function=function)],
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )
    completions = FakeCompletions(response)
    transport = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = OpenAIChatClient("test-model", "test-key", client=transport)

    result = client.chat(
        [{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )

    assert result.content == "done"
    assert result.tool_calls[0].arguments == {"path": "README.md"}
    assert client.total_prompt_tokens == 11
    assert client.total_completion_tokens == 7
    assert completions.params["model"] == "test-model"


def test_openai_client_rejects_malformed_tool_arguments():
    function = SimpleNamespace(name="lookup", arguments='{"path":')
    message = SimpleNamespace(content="", tool_calls=[SimpleNamespace(id="call-1", function=function)])
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
    transport = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(response)))
    client = OpenAIChatClient("test-model", "test-key", client=transport)

    with pytest.raises(StructuredOutputError, match="合法 JSON"):
        client.chat([{"role": "user", "content": "hello"}])


def test_openai_client_reports_timeout_separately_from_other_provider_failures():
    transport = SimpleNamespace(chat=SimpleNamespace(completions=TimeoutCompletions()))
    client = OpenAIChatClient("test-model", "test-key", timeout=42, client=transport)

    with pytest.raises(LLMProviderError, match="请求超时.*42 秒"):
        client.chat([{"role": "user", "content": "hello"}])


def test_structured_reasoner_validates_declared_schema():
    reasoner = LLMReasoner(FakeReasoningClient('{"answer": 1}'))
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    with pytest.raises(StructuredOutputError, match="structured output.answer"):
        reasoner.complete_structured(system="system", prompt="question", schema=schema)


def test_structured_reasoner_extracts_first_valid_object():
    client = FakeReasoningClient('说明文字\n```json\n{"answer": {"ok": true}}\n```')
    reasoner = LLMReasoner(client)

    result = reasoner.complete_structured(system="system", prompt="question")

    assert result == {"answer": {"ok": True}}
    assert client.messages[0]["role"] == "system"
    assert "JSON" in client.messages[0]["content"]


def test_structured_reasoner_retries_invalid_text_as_the_required_function_call():
    client = FormatRetryClient()
    reasoner = LLMReasoner(client)
    schema = {
        "type": "object",
        "properties": {
            "files": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "required": ["files"],
        "additionalProperties": False,
    }

    result = reasoner.complete_structured(
        system="system",
        prompt="fix src/app.py",
        schema=schema,
        tool_name="prepare_candidate",
    )

    assert result == {"files": {"src/app.py": "fixed\n"}}
    assert len(client.calls) == 2
    assert "call prepare_candidate exactly once" in client.calls[1]["messages"][-1]["content"]


def test_structured_reasoner_rejects_non_json_response():
    reasoner = LLMReasoner(FakeReasoningClient("not structured"))

    with pytest.raises(ValidationError, match="JSON"):
        reasoner.complete_structured(system="system", prompt="question")


def test_text_reasoner_preserves_markdown_without_json_wrapping():
    content = '## Review\n\nKeep "quotes", `code`, and C:\\workspace\\file.py intact.'
    client = FakeReasoningClient(content)
    reasoner = LLMReasoner(client)

    result = reasoner.complete_text(system="system", prompt="revise this proposal")

    assert result == content
    assert "JSON" not in client.messages[0]["content"]


def test_text_reasoner_rejects_empty_output():
    reasoner = LLMReasoner(FakeReasoningClient("  \n"))

    with pytest.raises(ValidationError, match="text"):
        reasoner.complete_text(system="system", prompt="question")
