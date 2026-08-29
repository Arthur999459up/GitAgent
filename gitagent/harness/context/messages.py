"""Canonical model messages and request-level token accounting."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from gitagent.domain.errors import ValidationError

from .budget import estimate_tokens


def canonical_message(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one standard provider message without rewriting its content.

    The returned object is the single model-visible projection used both by the
    live thread and durable JSONL persistence.
    """

    if not isinstance(value, Mapping):
        raise ValidationError("model message must be an object")
    role = value.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValidationError("model message has an invalid role")
    allowed = {
        "system": {"role", "content"},
        "user": {"role", "content"},
        "assistant": {"role", "content", "tool_calls", "reasoning_content"},
        "tool": {"role", "tool_call_id", "content"},
    }[str(role)]
    if set(value) - allowed:
        raise ValidationError("model message contains non-standard fields")

    if role == "tool":
        call_id = value.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValidationError("tool message requires tool_call_id")
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _content_text(value.get("content", "")),
        }

    content = value.get("content")
    reasoning_content = value.get("reasoning_content") if role == "assistant" else None
    if reasoning_content is not None and not isinstance(reasoning_content, str):
        raise ValidationError("assistant reasoning_content must be text or null")

    if role != "assistant" or value.get("tool_calls") is None:
        if not isinstance(content, str):
            raise ValidationError(f"{role} message content must be text")
        projected = {"role": role, "content": content}
        if role == "assistant" and reasoning_content is not None:
            projected["reasoning_content"] = reasoning_content
        return projected

    if content is not None and not isinstance(content, str):
        raise ValidationError("assistant message content must be text or null")
    calls = value.get("tool_calls")
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)) or not calls:
        raise ValidationError("assistant tool_calls must be a non-empty list")
    projected_calls = [_canonical_tool_call(call) for call in calls]
    projected = {
        "role": "assistant",
        "content": content if isinstance(content, str) else None,
        "tool_calls": projected_calls,
    }
    if reasoning_content is not None:
        projected["reasoning_content"] = reasoning_content
    return projected


def assistant_tool_call(
    call_id: str,
    name: str,
    arguments: Mapping[str, Any] | str,
) -> dict[str, Any]:
    raw_arguments = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return canonical_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": raw_arguments},
                }
            ],
        }
    )


def tool_result_message(call_id: str, content: Any) -> dict[str, Any]:
    return canonical_message(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": content if isinstance(content, str) else json.dumps(
                content, ensure_ascii=False, separators=(",", ":"), default=str
            ),
        }
    )


def request_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    payload = {"messages": list(messages), "tools": list(tools or ())}
    return estimate_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _content_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _canonical_tool_call(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"id", "type", "function"}:
        raise ValidationError("assistant tool call has an invalid shape")
    call_id = value.get("id")
    if not isinstance(call_id, str) or not call_id or value.get("type") != "function":
        raise ValidationError("assistant tool call identity is invalid")
    function = value.get("function")
    if not isinstance(function, Mapping) or set(function) != {"name", "arguments"}:
        raise ValidationError("assistant tool call function has an invalid shape")
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str) or not name or not isinstance(arguments, str):
        raise ValidationError("assistant tool call function is invalid")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


__all__ = [
    "assistant_tool_call",
    "canonical_message",
    "request_tokens",
    "tool_result_message",
]
