"""Separate structured control output from free-form text generation."""

from __future__ import annotations

import json
from typing import Any, Protocol

from ..core.errors import StructuredOutputError, ValidationError
from ..mcp.registry import validate_schema
from ..prompts import get_prompt_library
from .llm import ChatClient

_PROMPTS = get_prompt_library()

# The instruction files contain the sentence without a leading newline; the
# ``\n`` separator stays in ``structured_message_contents`` so the combined
# system content remains byte-identical to the pre-externalization layout.
STRUCTURED_OUTPUT_INSTRUCTION = _PROMPTS.text("reasoning.structured_output_instruction")
STRUCTURED_CALL_INSTRUCTION = _PROMPTS.text("reasoning.structured_call_instruction")
_TOOL_DESCRIPTION = _PROMPTS.text("reasoning.tool_description")


def structured_message_contents(
    system: str, prompt: str, *, schema: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Return the exact text contents sent for one structured model call."""

    instruction = STRUCTURED_CALL_INSTRUCTION if schema is not None else STRUCTURED_OUTPUT_INSTRUCTION
    return system + "\n" + instruction, prompt


class Reasoner(Protocol):
    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    def complete_text(self, *, system: str, prompt: str) -> str: ...


class LLMReasoner:
    """Use structured function calls for control contracts and plain text for content."""

    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        system_content, user_content = structured_message_contents(system, prompt, schema=schema)
        structured_tools = _structured_tools(tool_name, schema) if schema is not None else []
        available_tools = [*structured_tools, *(tools or [])] or None
        response = self.client.chat(
            messages=[
                {
                    "role": "system",
                    "content": system_content,
                },
                {"role": "user", "content": user_content},
            ],
            tools=available_tools,
        )
        if response.tool_calls:
            call = response.tool_calls[0]
            arguments = call.arguments
            if isinstance(arguments, dict):
                if call.name == tool_name:
                    self._validate_structured(arguments, schema)
                    return arguments
                return {
                    "kind": "tool",
                    "summary": f"Call {call.name}",
                    "tool": call.name,
                    "arguments": arguments,
                }
        value = _first_json_object(response.content)
        if not isinstance(value, dict):
            raise StructuredOutputError("structured reasoning output must be a JSON object")
        self._validate_structured(value, schema)
        return value

    @staticmethod
    def _validate_structured(value: dict[str, Any], schema: dict[str, Any] | None) -> None:
        if schema is None:
            return
        try:
            validate_schema(value, schema, label="structured output")
        except ValidationError as exc:
            raise StructuredOutputError(str(exc)) from exc

    def complete_text(self, *, system: str, prompt: str) -> str:
        response = self.client.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            tools=None,
        )
        content = response.content.strip()
        if not content:
            raise ValidationError("text reasoning output cannot be empty")
        return content


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
