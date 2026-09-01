"""Build canonical provider requests directly from durable Session events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gitagent.domain.errors import ContextWindowExceeded
from gitagent.domain.models import SessionScope
from gitagent.infra.persistence import SessionManager
from gitagent.token_accounting import estimate_tokens

from .budget import (
    EMERGENCY_THRESHOLD,
    LIGHT_THRESHOLD,
    SUMMARY_THRESHOLD,
    context_pressure,
)
from .messages import canonical_message, request_tokens
from .projector import CHECKPOINT_PREFIX, derive_main_messages


class ContextBuildError(RuntimeError):
    """Base error for missing state or an invalid context-building operation."""


@dataclass(frozen=True)
class CompactResult:
    changed: bool
    level: str
    before_tokens: int
    after_tokens: int
    context_window_tokens: int


@dataclass(frozen=True)
class MessageCompactionPlan:
    """Durable delta needed to replay one model-visible compaction exactly."""

    tool_replacements: tuple[tuple[str, str], ...] = ()
    checkpoint: str = ""
    retain_message_indexes: tuple[int, ...] | None = None

    @property
    def changed(self) -> bool:
        return bool(
            self.tool_replacements
            or self.checkpoint
            or self.retain_message_indexes is not None
        )


@dataclass(frozen=True)
class _ProjectedCompaction:
    messages: list[dict[str, Any]]
    checkpoint: str = ""
    retain_message_indexes: tuple[int, ...] | None = None


class ContextBuilder:
    """Assemble current system, canonical messages and tools under token pressure."""

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        context_window_tokens: int = 32768,
    ) -> None:
        self.session_manager = session_manager
        self.context_window_tokens = _integer(
            context_window_tokens, "context_window_tokens", positive=True
        )
        self.last_compression_level = "none"
        self.last_stages: tuple[dict[str, Any], ...] = ()
        self.last_compaction_changed = False

    def build(
        self,
        scope: SessionScope,
        repository_full_name: str,
        user_input: str,
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        turn_seq: int | None = None,
        current_user_is_main: bool = True,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return the exact Main provider messages and tools for this turn."""

        if not isinstance(scope, SessionScope):
            raise TypeError("scope must be a SessionScope")
        if not isinstance(user_input, str) or not user_input.strip():
            raise ContextBuildError("latest user input cannot be empty")
        if not isinstance(system, str) or not system.strip():
            raise ContextBuildError("current system message cannot be empty")
        session = self.session_manager.get_session(
            scope.account_key, scope.repository_key, scope.session_id
        )
        if session is None:
            raise ContextBuildError("session not found in the requested scope")
        if repository_full_name and session.repository_full_name != repository_full_name:
            raise ContextBuildError("session belongs to a different repository")

        events = tuple(self.session_manager.event_log.iter_events(scope))
        projected_events = events
        legacy_checkpoint = session.summary
        if legacy_checkpoint:
            projected_events = tuple(
                event
                for event in events
                if event.turn_seq is None or event.turn_seq > session.summary_through_seq
            )
        current_user_event = next(
            (
                event
                for event in reversed(projected_events)
                if event.type == "user_message"
                and (turn_seq is None or event.turn_seq == turn_seq)
            ),
            None,
        )
        if current_user_event is None or current_user_event.data.get("content") != user_input:
            raise ContextBuildError("durable history is missing the current user message")
        event_is_main = current_user_event.agent in {None, "main"}
        if event_is_main != current_user_is_main:
            raise ContextBuildError("current user message has the wrong Agent owner")

        history = derive_main_messages(
            projected_events,
            legacy_checkpoint=legacy_checkpoint,
        )
        if current_user_is_main:
            if not history or history[-1].get("role") != "user":
                raise ContextBuildError(
                    "durable Main history is missing the current user message"
                )
            if history[-1].get("content") != user_input:
                raise ContextBuildError(
                    "current user message differs from durable Main history"
                )
        messages = [canonical_message({"role": "system", "content": system}), *history]
        fitted, level, stages, plan = fit_messages_with_plan(
            messages,
            tools,
            context_window_tokens=self.context_window_tokens,
        )
        if plan.changed:
            if not isinstance(turn_seq, int) or isinstance(turn_seq, bool) or turn_seq < 1:
                raise ContextBuildError(
                    "durable Main compaction requires the current Turn sequence"
                )
            self.session_manager.record_message_compaction(
                scope,
                turn_seq=turn_seq,
                agent="main",
                checkpoint=plan.checkpoint,
                retain_message_indexes=plan.retain_message_indexes,
                tool_replacements=plan.tool_replacements,
            )
        self.last_compression_level = level
        self.last_stages = tuple(stages)
        self.last_compaction_changed = plan.changed
        return fitted, tools or None

    def compact(self, scope: SessionScope) -> CompactResult:
        """Persist a context-pressure-derived Main checkpoint."""

        session = self.session_manager.get_session(
            scope.account_key, scope.repository_key, scope.session_id
        )
        if session is None:
            raise ContextBuildError("session not found in the requested scope")
        events = tuple(self.session_manager.event_log.iter_events(scope))
        history = derive_main_messages(events, legacy_checkpoint=session.summary)
        before = request_tokens(history)
        spans = _atomic_spans(history)
        if len(spans) < 2:
            return CompactResult(
                changed=False,
                level="none",
                before_tokens=before,
                after_tokens=before,
                context_window_tokens=self.context_window_tokens,
            )
        prefix_end = spans[-1][0]
        summary = _checkpoint_content(history[:prefix_end])
        if not summary:
            return CompactResult(
                changed=False,
                level="none",
                before_tokens=before,
                after_tokens=before,
                context_window_tokens=self.context_window_tokens,
            )
        self.session_manager.event_log.append(
            scope,
            "compaction_checkpoint",
            agent="main",
            data={"content": summary, "retain_from_message": prefix_end},
        )
        replayed = derive_main_messages(
            self.session_manager.event_log.iter_events(scope)
        )
        after = request_tokens(replayed)
        return CompactResult(
            changed=True,
            level="summary",
            before_tokens=before,
            after_tokens=after,
            context_window_tokens=self.context_window_tokens,
        )


def fit_messages(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    *,
    context_window_tokens: int,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """Apply pressure policy and return the exact provider request."""

    fitted, level, stages, _ = fit_messages_with_plan(
        messages,
        tools,
        context_window_tokens=context_window_tokens,
    )
    return fitted, level, stages


def fit_messages_with_plan(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    *,
    context_window_tokens: int,
) -> tuple[
    list[dict[str, Any]],
    str,
    list[dict[str, Any]],
    MessageCompactionPlan,
]:
    """Apply pressure policy and describe the durable replay delta."""

    canonical = [canonical_message(message) for message in messages]
    _validate_tool_protocol(canonical)
    stages: list[dict[str, Any]] = []
    initial = request_tokens(canonical, tools)
    current = canonical
    light_replacements: tuple[tuple[str, str], ...] = ()
    level = "none"

    context_window_tokens = _integer(
        context_window_tokens, "context_window_tokens", positive=True
    )
    if context_pressure(initial, context_window_tokens) >= LIGHT_THRESHOLD:
        level = "light"
        current, light_replacements = _light_compact(
            current, tools, context_window_tokens
        )
        stages.append(
            {
                "level": "light",
                "before_tokens": initial,
                "after_tokens": request_tokens(current, tools),
            }
        )

    durable_base = current
    projected = _ProjectedCompaction(current)
    current_tokens = request_tokens(current, tools)
    if context_pressure(current_tokens, context_window_tokens) >= SUMMARY_THRESHOLD:
        level = "summary"
        before = current_tokens
        projected = _summary_compact(current, tools, context_window_tokens)
        current = projected.messages
        stages.append(
            {
                "level": "summary",
                "before_tokens": before,
                "after_tokens": request_tokens(current, tools),
            }
        )

    current_tokens = request_tokens(current, tools)
    if context_pressure(current_tokens, context_window_tokens) >= EMERGENCY_THRESHOLD:
        level = "emergency"
        before = current_tokens
        projected = _emergency_compact(
            durable_base, tools, context_window_tokens
        )
        current = projected.messages
        stages.append(
            {
                "level": "emergency",
                "before_tokens": before,
                "after_tokens": request_tokens(current, tools),
            }
        )

    final_input_tokens = request_tokens(current, tools)
    if final_input_tokens >= context_window_tokens:
        raise ContextWindowExceeded(
            context_window_tokens=context_window_tokens,
            input_tokens=final_input_tokens,
            requested_output_tokens=1,
        )
    _validate_tool_protocol(current)
    plan = MessageCompactionPlan(
        tool_replacements=light_replacements,
        checkpoint=projected.checkpoint,
        retain_message_indexes=projected.retain_message_indexes,
    )
    return current, level, stages, plan


def _light_compact(
    messages: list[dict[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    context_window_tokens: int,
) -> tuple[list[dict[str, Any]], tuple[tuple[str, str], ...]]:
    projected = [dict(message) for message in messages]
    replacements: list[tuple[str, str]] = []
    for index, message in enumerate(projected):
        if (
            context_pressure(request_tokens(projected, tools), context_window_tokens)
            < LIGHT_THRESHOLD
        ):
            break
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if estimate_tokens(content) < 256:
            continue
        replacement = (
            "Large retrievable tool output compacted after use. "
            f"Original estimated tokens: {estimate_tokens(content)}. Re-fetch if needed."
        )
        projected[index] = {
            "role": "tool",
            "tool_call_id": message["tool_call_id"],
            "content": replacement,
        }
        replacements.append((str(message["tool_call_id"]), replacement))
    return projected, tuple(replacements)


def _summary_compact(
    messages: list[dict[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    context_window_tokens: int,
) -> _ProjectedCompaction:
    system, history = _split_current_system(messages)
    spans = _atomic_spans(history)
    if len(spans) < 2:
        return _ProjectedCompaction(messages)
    best = _ProjectedCompaction(messages)
    best_tokens = request_tokens(messages, tools)
    for _, end in spans[:-1]:
        checkpoint_content = _checkpoint_content(history[:end])
        checkpoint = canonical_message(
            {
                "role": "system",
                "content": CHECKPOINT_PREFIX + checkpoint_content,
            }
        )
        visible_checkpoint = str(checkpoint["content"])
        durable_checkpoint = visible_checkpoint.removeprefix(CHECKPOINT_PREFIX)
        candidate = [system, checkpoint, *history[end:]]
        tokens = request_tokens(candidate, tools)
        if tokens < best_tokens:
            best = _ProjectedCompaction(
                candidate,
                durable_checkpoint,
                tuple(range(end, len(history))),
            )
            best_tokens = tokens
        if context_pressure(tokens, context_window_tokens) < LIGHT_THRESHOLD:
            return _ProjectedCompaction(
                candidate,
                durable_checkpoint,
                tuple(range(end, len(history))),
            )
    return best


def _emergency_compact(
    messages: list[dict[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    context_window_tokens: int,
) -> _ProjectedCompaction:
    system, history = _split_current_system(messages)
    spans = _atomic_spans(history)
    current_user_index = max(
        (index for index, message in enumerate(history) if message.get("role") == "user"),
        default=-1,
    )
    required_indexes = {current_user_index} if current_user_index >= 0 else set()
    for start, end in spans:
        if _span_has_open_tool_protocol(history[start:end]):
            required_indexes.update(range(start, end))
    required = [
        message for index, message in enumerate(history) if index in required_indexes
    ]
    base = [system, *required]
    base_tokens = request_tokens(base, tools)
    if base_tokens >= context_window_tokens:
        raise ContextWindowExceeded(
            context_window_tokens=context_window_tokens,
            input_tokens=base_tokens,
            requested_output_tokens=1,
        )

    selected: list[tuple[int, int]] = []
    for span in reversed(spans):
        start, end = span
        if any(index in required_indexes for index in range(start, end)):
            continue
        candidate_spans = sorted([span, *selected])
        candidate_indexes = {
            index
            for span_start, span_end in candidate_spans
            for index in range(span_start, span_end)
        } | required_indexes
        candidate = [
            system,
            *[
                message
                for index, message in enumerate(history)
                if index in candidate_indexes
            ],
        ]
        if request_tokens(candidate, tools) < context_window_tokens:
            selected.append(span)

    chosen_indexes = required_indexes | {
        index for start, end in selected for index in range(start, end)
    }
    omitted = [
        message for index, message in enumerate(history) if index not in chosen_indexes
    ]
    chosen = [
        message for index, message in enumerate(history) if index in chosen_indexes
    ]
    retained = tuple(index for index in range(len(history)) if index in chosen_indexes)
    if omitted:
        checkpoint_content = _checkpoint_content(omitted)
        checkpoint = canonical_message(
            {
                "role": "system",
                "content": CHECKPOINT_PREFIX + checkpoint_content,
            }
        )
        visible_checkpoint = str(checkpoint["content"])
        durable_checkpoint = visible_checkpoint.removeprefix(CHECKPOINT_PREFIX)
        candidate = [system, checkpoint, *chosen]
        if request_tokens(candidate, tools) < context_window_tokens:
            return _ProjectedCompaction(candidate, durable_checkpoint, retained)
    return _ProjectedCompaction([system, *chosen], retain_message_indexes=retained)


def _atomic_spans(messages: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            spans.append((index, index + 1))
            index += 1
            continue
        pending = {str(call["id"]) for call in calls}
        end = index + 1
        while end < len(messages) and pending:
            candidate = messages[end]
            if candidate.get("role") != "tool":
                break
            pending.discard(str(candidate.get("tool_call_id") or ""))
            end += 1
        names = {str(call["function"]["name"]) for call in calls}
        if (
            not pending
            and end < len(messages)
            and any(
                name.startswith("agent__")
                for name in names
            )
            and messages[end].get("role") == "assistant"
            and not messages[end].get("tool_calls")
        ):
            end += 1
        spans.append((index, end))
        index = end
    return spans


def _checkpoint_content(messages: Sequence[Mapping[str, Any]]) -> str:
    records: list[str] = []
    for message in messages:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "system" and content.startswith(CHECKPOINT_PREFIX):
            records.append(content[len(CHECKPOINT_PREFIX) :])
        elif role in {"user", "assistant"} and not message.get("tool_calls"):
            content = " ".join(content.split())
            if content:
                records.append(f"{role.title()}: {_bounded_line(content, 800)}")
        elif role == "assistant":
            rendered = []
            for call in message.get("tool_calls") or []:
                function = call["function"]
                rendered.append(
                    f"{function['name']}({_bounded_line(str(function['arguments']), 400)})"
                )
            if rendered:
                records.append("Tool calls: " + "; ".join(rendered))
        elif role == "tool":
            records.append("Tool result: " + _bounded_line(" ".join(content.split()), 1200))
    return "\n".join(records) or "Earlier conversation contained no durable semantic content."


def _split_current_system(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not messages or messages[0].get("role") != "system":
        raise ContextBuildError("provider request must start with the current system message")
    return messages[0], messages[1:]


def _span_has_open_tool_protocol(span: Sequence[Mapping[str, Any]]) -> bool:
    calls = {
        str(call["id"])
        for message in span
        for call in (message.get("tool_calls") or [])
    }
    results = {
        str(message.get("tool_call_id") or "")
        for message in span
        if message.get("role") == "tool"
    }
    return bool(calls - results)


def _validate_tool_protocol(messages: Sequence[Mapping[str, Any]]) -> None:
    pending: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            if pending:
                raise ContextBuildError("assistant tool calls precede unresolved tool results")
            pending = {str(call["id"]) for call in message["tool_calls"]}
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in pending:
                raise ContextBuildError("tool result has no matching assistant tool call")
            pending.remove(call_id)
        elif pending:
            raise ContextBuildError("tool protocol is interrupted before all results")


def _bounded_line(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, (limit - 3) // 2)
    return value[:half] + " … " + value[-half:]


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{label} must be a {qualifier} integer")
    return value


__all__ = [
    "CompactResult",
    "ContextBuildError",
    "ContextBuilder",
    "MessageCompactionPlan",
    "fit_messages",
    "fit_messages_with_plan",
]
