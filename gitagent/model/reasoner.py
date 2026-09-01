"""Provider-neutral model responses and typed business-output helpers."""

from __future__ import annotations

from typing import Any, Protocol

from gitagent.agent_loop.models import ModelResponse, StructuredCall
from gitagent.capability import validate_schema
from gitagent.domain.errors import StructuredOutputError, ValidationError
from gitagent.prompts import get_prompt_library

from .chat_client import ChatClient

_PROMPTS = get_prompt_library()

_TOOL_DESCRIPTION = _PROMPTS.text("reasoning.tool_description")


class StructuredValue(dict[str, Any]):
    """Structured value with the provider's canonical assistant message."""

    def __init__(self, value: dict[str, Any], message: dict[str, Any]) -> None:
        super().__init__(value)
        self.assistant_message = message


class Reasoner(Protocol):
    def complete_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        context_window_tokens: int | None = None,
    ) -> ModelResponse: ...

    def complete_structured_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        final_tools: list[dict[str, Any]] | None = None,
        context_window_tokens: int | None = None,
    ) -> dict[str, Any]: ...

    def complete_text_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        context_window_tokens: int | None = None,
    ) -> str: ...


class LLMReasoner:
    """Normalize native Text/structured calls and validate typed business outputs."""

    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def complete_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        context_window_tokens: int | None = None,
    ) -> ModelResponse:
        response = self.client.chat(
            messages=messages,
            tools=tools,
            context_window_tokens=context_window_tokens,
        )
        calls = [
            StructuredCall(
                provider_call.id,
                provider_call.name,
                dict(provider_call.arguments),
            )
            for provider_call in response.tool_calls
        ]
        text = response.content
        if not calls and not text.strip():
            raise StructuredOutputError(
                "model returned neither Text nor a structured call"
            )
        return ModelResponse(text, calls, response.message)

    def complete_structured_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        final_tools: list[dict[str, Any]] | None = None,
        context_window_tokens: int | None = None,
    ) -> dict[str, Any]:
        _validate_final_tools(final_tools, schema=schema, tool_name=tool_name)
        response = self.complete_messages(
            messages=messages,
            tools=final_tools,
            context_window_tokens=context_window_tokens,
        )
        return self._structured_value(
            response,
            schema=schema,
            tool_name=tool_name,
        )

    def _structured_value(
        self,
        response: ModelResponse,
        *,
        schema: dict[str, Any] | None,
        tool_name: str,
    ) -> dict[str, Any]:
        if len(response.calls) == 1:
            call = response.calls[0]
            arguments = call.arguments
            if isinstance(arguments, dict):
                if call.name == tool_name:
                    self._validate_structured(arguments, schema)
                    return StructuredValue(arguments, response.assistant_message)
                raise StructuredOutputError(
                    f"model called {call.name or '<unnamed>'} instead of required function {tool_name}",
                    expected_tool=tool_name,
                    actual_tool=call.name or "<unnamed>",
                )
        if len(response.calls) > 1:
            raise StructuredOutputError(
                "typed structured output requires exactly one function call",
                expected_tool_calls=1,
                actual_tool_calls=len(response.calls),
            )
        raise StructuredOutputError(
            "model returned Text where a typed structured result was required",
            expected_tool=tool_name,
        )

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
        context_window_tokens: int | None = None,
    ) -> str:
        response = self.client.chat(
            messages=messages,
            tools=tools,
            context_window_tokens=context_window_tokens,
        )
        return response.content.strip()


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

    result = [
        tool
        for tool in tools or ()
        if not (
            tool.get("type") == "function"
            and isinstance(tool.get("function"), dict)
            and tool["function"].get("name") == tool_name
        )
    ]
    if schema is not None:
        result = [*_structured_tools(tool_name, schema), *result]
    return result or None


def _validate_final_tools(
    final_tools: list[dict[str, Any]] | None,
    *,
    schema: dict[str, Any] | None,
    tool_name: str,
) -> None:
    if schema is None:
        return
    if not any(
        tool.get("type") == "function"
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == tool_name
        and tool["function"].get("parameters") == schema
        for tool in final_tools or ()
    ):
        raise ValidationError(
            f"final_tools must contain the structured output function {tool_name}"
        )
