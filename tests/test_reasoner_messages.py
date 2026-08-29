from copy import deepcopy

from gitagent.model import ChatResponse, LLMReasoner, ToolCall


class _Client:
    model = "fake"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self) -> None:
        self.requests = []
        self.responses = [
            ChatResponse(content="not structured"),
            ChatResponse(tool_calls=[ToolCall("fixed", "respond", {"answer": "ok"})]),
        ]

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        return self.responses.pop(0)


def test_structured_retry_extends_the_original_message_history() -> None:
    client = _Client()
    reasoner = LLMReasoner(client)
    original = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]

    initial = deepcopy(original)
    value = reasoner.complete_structured_messages(
        messages=original,
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    assert value == {"answer": "ok"}
    assert client.requests[0][0] == initial
    retry = client.requests[1][0]
    assert retry[: len(initial)] == initial
    assert retry[-2]["role"] == "assistant"
    assert retry[-1]["role"] == "user"
    assert original == retry


class _ParallelToolClient:
    model = "fake"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self) -> None:
        self.requests = []
        self.responses = [
            ChatResponse(
                tool_calls=[
                    ToolCall("a", "capability__github__get_issue", {"issue_number": 7}),
                    ToolCall("b", "capability__github__get_issue_comments", {"issue_number": 7}),
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall("c", "capability__github__get_issue", {"issue_number": 7})
                ]
            ),
        ]

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        return self.responses.pop(0)


def test_parallel_tool_calls_are_rejected_without_entering_canonical_history() -> None:
    client = _ParallelToolClient()
    reasoner = LLMReasoner(client)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect issue 7"},
    ]
    initial = deepcopy(messages)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "capability__github__get_issue",
                "description": "read issue",
                "parameters": {"type": "object"},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "capability__github__get_issue_comments",
                "description": "read comments",
                "parameters": {"type": "object"},
            },
        },
    ]

    value = reasoner.complete_structured_messages(
        messages=messages,
        schema={
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "required": ["kind"],
        },
        tool_name="decide_action",
        tools=tools,
    )

    assert value["kind"] == "capability"
    assert value["capability_id"] == "capability__github__get_issue"
    assert client.requests[0][0] == initial
    assert client.requests[1][0][:-1] == initial
    assert client.requests[1][0][-1]["role"] == "user"
    assert all(message.get("role") != "assistant" for message in messages[len(initial) :])
    assert len(value.assistant_message["tool_calls"]) == 1
