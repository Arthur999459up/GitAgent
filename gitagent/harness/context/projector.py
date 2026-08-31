"""Read-only SessionEvent compatibility projectors for model threads."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from gitagent.domain.models import SessionEvent

from .messages import assistant_tool_call, canonical_message, tool_result_message

CHECKPOINT_PREFIX = "Conversation checkpoint from compacted history:\n"


def derive_main_messages(
    events: Iterable[SessionEvent],
    *,
    legacy_checkpoint: str = "",
) -> list[dict[str, Any]]:
    """Project only the transcript visible to Main, ignoring Domain internals."""

    materialized = tuple(events)
    has_native_model_events = any(
        event.type == "model_message" and event.agent == "main"
        for event in materialized
    )
    messages: list[dict[str, Any]] = []
    checkpoint = legacy_checkpoint.strip()
    if checkpoint:
        messages.append(
            {"role": "system", "content": CHECKPOINT_PREFIX + checkpoint}
        )
    for event in materialized:
        if event.type == "compaction_checkpoint" and event.agent == "main":
            messages = _apply_compaction(messages, event.data)
            continue
        if event.type == "model_message" and event.agent == "main":
            message = event.data.get("message")
            if isinstance(message, Mapping):
                messages.append(canonical_message(message))
            continue
        if event.agent not in {None, "main"}:
            continue
        if event.type == "user_message":
            content = event.data.get("content")
            if isinstance(content, str):
                messages.append(canonical_message({"role": "user", "content": content}))
        elif event.type == "assistant_message" and not has_native_model_events:
            content = event.data.get("content")
            if isinstance(content, str):
                messages.append(
                    canonical_message({"role": "assistant", "content": content})
                )
        elif (
            event.type == "tool_call"
            and event.agent == "main"
            and not has_native_model_events
        ):
            messages.append(_event_tool_call(event.data))
        elif (
            event.type == "tool_result"
            and event.agent == "main"
            and not has_native_model_events
        ):
            messages.append(
                tool_result_message(
                    str(event.data.get("call_id") or ""),
                    event.data.get("content", ""),
                )
            )
    return correlate_tool_results(_deduplicate_trace_pairs(messages))


def derive_domain_messages(
    events: Iterable[SessionEvent],
    *,
    agent: str,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Replay one persisted Domain run's canonical model messages."""

    messages: list[dict[str, Any]] = []
    for event in events:
        if event.agent != agent:
            continue
        event_run_id = event.data.get("run_id")
        if run_id is not None and event_run_id != run_id:
            continue
        if event.type == "compaction_checkpoint":
            if not messages:
                continue
            messages = [messages[0], *_apply_compaction(messages[1:], event.data)]
            continue
        if event.type != "model_message":
            continue
        message = event.data.get("message")
        if isinstance(message, Mapping):
            messages.append(canonical_message(message))
    return messages


def _apply_compaction(
    messages: list[dict[str, Any]], data: Mapping[str, Any]
) -> list[dict[str, Any]]:
    projected = [dict(message) for message in messages]
    replacements = data.get("tool_replacements")
    if isinstance(replacements, list):
        by_call_id = {
            str(item.get("tool_call_id") or ""): str(item.get("content") or "")
            for item in replacements
            if isinstance(item, Mapping) and item.get("tool_call_id")
        }
        for index, message in enumerate(projected):
            if message.get("role") != "tool":
                continue
            call_id = str(message.get("tool_call_id") or "")
            if call_id in by_call_id:
                projected[index] = tool_result_message(call_id, by_call_id[call_id])

    retain_indexes = data.get("retain_message_indexes")
    content = data.get("content")
    if isinstance(retain_indexes, list) and all(
        isinstance(index, int) and not isinstance(index, bool) and index >= 0
        for index in retain_indexes
    ):
        retained = [
            projected[index]
            for index in retain_indexes
            if index < len(projected)
        ]
        if isinstance(content, str) and content:
            return [
                canonical_message(
                    {"role": "system", "content": CHECKPOINT_PREFIX + content}
                ),
                *retained,
            ]
        return retained

    if isinstance(content, str) and content:
        retain_from = data.get("retain_from_message")
        suffix = (
            projected[retain_from:]
            if isinstance(retain_from, int)
            and not isinstance(retain_from, bool)
            and 0 <= retain_from <= len(projected)
            else []
        )
        return [
            canonical_message(
                {"role": "system", "content": CHECKPOINT_PREFIX + content}
            ),
            *suffix,
        ]
    return projected


def _event_tool_call(data: Mapping[str, Any]) -> dict[str, Any]:
    tool = str(data.get("tool") or "")
    name = (
        tool
        if tool.startswith(("capability__", "agent__"))
        else "capability__" + tool.replace(".", "__").replace("-", "_")
    )
    arguments = data.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    return assistant_tool_call(str(data.get("call_id") or ""), name, arguments)


def _deduplicate_trace_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ignore trace copies when a new model_message already persisted the pair."""

    result: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    for message in messages:
        if message["role"] == "assistant" and message.get("tool_calls"):
            ids = {str(call["id"]) for call in message["tool_calls"]}
            if ids <= seen_calls:
                continue
            seen_calls.update(ids)
        elif message["role"] == "tool":
            call_id = str(message["tool_call_id"])
            if call_id in seen_results:
                continue
            seen_results.add(call_id)
        result.append(message)
    return result


def correlate_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep delayed Agent results adjacent to their original provider calls."""

    results = {
        str(message.get("tool_call_id") or ""): message
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    call_ids = {
        str(call.get("id") or "")
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    }
    matched = call_ids & results.keys()
    if not matched:
        return messages

    ordered: list[dict[str, Any]] = []
    for message in messages:
        if (
            message.get("role") == "tool"
            and str(message.get("tool_call_id") or "") in matched
        ):
            continue
        ordered.append(message)
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = str(call.get("id") or "")
            if call_id in matched:
                ordered.append(results[call_id])
    return ordered


__all__ = [
    "CHECKPOINT_PREFIX",
    "correlate_tool_results",
    "derive_domain_messages",
    "derive_main_messages",
]
