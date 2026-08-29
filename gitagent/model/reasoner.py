"""Separate structured control output from free-form text generation."""

from __future__ import annotations

import json
from typing import Any, Protocol

from gitagent.capability import validate_schema
from gitagent.domain.errors import StructuredOutputError, ValidationError
from gitagent.prompts import get_prompt_library

from .chat_client import ChatClient, ChatResponse

_PROMPTS = get_prompt_library()

_TOOL_DESCRIPTION = _PROMPTS.text("reasoning.tool_description")


class StructuredValue(dict[str, Any]):
    """Structured value with the provider's canonical assistant message."""

    def __init__(self, value: dict[str, Any], message: dict[str, Any]) -> None:
        super().__init__(value)
        self.assistant_message = message


class Reasoner(Protocol):
    def complete_structured_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    def complete_text_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> str: ...

class LLMReasoner:
    """Use structured function calls for control contracts and plain text for content."""

    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def complete_structured_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        available_tools = structured_tools(tool_name, schema, tools)
        response: ChatResponse | None = None
        try:
            response = self.client.chat(
                messages=messages,
                tools=available_tools or None,
            )
            return self._structured_value(
                response,
                schema=schema,
                tool_name=tool_name,
                domain_tools_available=bool(tools),
            )
        except StructuredOutputError as first_error:
            if schema is None:
                raise
            # An invalid tool-call message cannot be followed by a user correction
            # until every call has a matching tool result. Keep rejected tool calls
            # out of the canonical thread and retry from the last valid message.
            if response is not None and not response.tool_calls:
                messages.append(response.message)
            if tools:
                correction = (
                    "The previous response could not be parsed or did not satisfy the required "
                    f"structured-output contract ({first_error}). Correct only the output format now: "
                    "call exactly one available tool. For a capability action, call that capability tool "
                    f"directly; otherwise call {tool_name} exactly once with all required arguments. "
                    "Do not explain the correction or use Markdown."
                )
            else:
                correction = (
                    "The previous response could not be parsed or did not satisfy the required "
                    f"structured-output contract ({first_error}). Correct only the output format now: "
                    f"call {tool_name} exactly once with all required arguments. Do not explain the "
                    "correction or use Markdown."
                )
            messages.append({"role": "user", "content": correction})
            try:
                retry = self.client.chat(
                    messages=messages,
                    tools=available_tools or None,
                )
                return self._structured_value(
                    retry,
                    schema=schema,
                    tool_name=tool_name,
                    domain_tools_available=bool(tools),
                )
            except StructuredOutputError as retry_error:
                raise StructuredOutputError(
                    f"{retry_error} (after one structured-output format retry)"
                ) from retry_error

    def _structured_value(
        self,
        response: ChatResponse,
        *,
        schema: dict[str, Any] | None,
        tool_name: str,
        domain_tools_available: bool,
    ) -> dict[str, Any]:
        if len(response.tool_calls) > 1:
            raise StructuredOutputError(
                "model must call at most one tool per agent step"
            )
        if response.tool_calls:
            call = response.tool_calls[0]
            arguments = call.arguments
            if isinstance(arguments, dict):
                if call.name == tool_name:
                    self._validate_structured(arguments, schema)
                    return StructuredValue(arguments, response.message)
                if not domain_tools_available:
                    raise StructuredOutputError(
                        f"model called {call.name or '<unnamed>'} instead of required function {tool_name}"
                    )
                return StructuredValue(
                    {
                        "kind": "capability",
                        "summary": f"Call {call.name}",
                        "capability_id": call.name,
                        "arguments": arguments,
                    },
                    response.message,
                )
        value = _first_json_object(response.content)
        if not isinstance(value, dict):
            raise StructuredOutputError("structured reasoning output must be a JSON object")
        self._validate_structured(value, schema)
        return StructuredValue(value, response.message)

    @staticmethod
    def _validate_structured(value: dict[str, Any], schema: dict[str, Any] | None) -> None:
        if schema is None:
            return
        try:
            validate_schema(value, schema, label="structured output")
        except ValidationError as exc:
            raise StructuredOutputError(str(exc)) from exc

    def complete_text_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        response = self.client.chat(messages=messages, tools=tools)
        return response.content.strip()

def _first_json_object(content: str) -> Any:
    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise StructuredOutputError("model did not return a valid JSON object")


def _structured_tools(tool_name: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a native function-calling tool for one structured control contract."""

    return [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": _TOOL_DESCRIPTION,
                "parameters": schema,
            },
        }
    ]


def structured_tools(
    tool_name: str,
    schema: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]] | None:
    """Return one deduplicated provider tool list for token accounting and calls."""

    result = list(tools or ())
    if schema is not None and not any(
        tool.get("type") == "function"
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == tool_name
        for tool in result
    ):
        result = [*_structured_tools(tool_name, schema), *result]
    return result or None
