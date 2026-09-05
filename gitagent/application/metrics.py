"""Read-only observability projections for GitAgent CLI metrics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from gitagent.domain.models import SessionEvent
from gitagent.infra.persistence import TurnRecord


@dataclass(frozen=True, slots=True)
class ContextUsage:
    agent: str
    run_id: str
    input_tokens: int | None
    context_window_tokens: int
    turn_seq: int | None = None
    state: str = "completed"

    @property
    def ratio(self) -> float | None:
        if self.input_tokens is None:
            return None
        return self.input_tokens / self.context_window_tokens


@dataclass(frozen=True, slots=True)
class TurnLatency:
    seq: int
    status: str
    duration_ms: float | None


def project_context_usage(
    events: Iterable[SessionEvent],
    *,
    context_windows: Mapping[str, int],
    current_context: Mapping[str, Any] | None = None,
) -> tuple[ContextUsage, ...]:
    """Return the latest model-visible context snapshot for each concrete Agent run."""

    default_window = int(context_windows["default"])
    latest: dict[tuple[str, str], tuple[int, ContextUsage]] = {}
    terminal_states: dict[tuple[str, str], str] = {}

    for order, event in enumerate(events):
        terminal = _terminal_run_state(event)
        if terminal is not None:
            key, state = terminal
            terminal_states[key] = state

        details = _context_usage_details(event)
        if details is None or not event.agent:
            continue
        input_tokens = _nonnegative_int(details.get("input_tokens"))
        window = _positive_int(details.get("context_window_tokens"))
        if input_tokens is None or window is None:
            continue
        run_id = _run_id(event.agent, details.get("run_id"))
        if not run_id:
            continue
        key = (event.agent, run_id)
        latest[key] = (
            order,
            ContextUsage(
                agent=event.agent,
                run_id=run_id,
                input_tokens=input_tokens,
                context_window_tokens=window,
                turn_seq=event.turn_seq,
            ),
        )

    current = _current_context_states(current_context)
    main_key = ("main", "main")
    current[main_key] = "active"
    for key, state in current.items():
        if key in latest:
            continue
        agent, run_id = key
        latest[key] = (
            -1,
            ContextUsage(
                agent=agent,
                run_id=run_id,
                input_tokens=None,
                context_window_tokens=int(context_windows.get(agent, default_window)),
                state=state,
            ),
        )

    rows: list[tuple[int, ContextUsage]] = []
    for key, (order, row) in latest.items():
        state = (
            "active"
            if key == main_key
            else current.get(key, terminal_states.get(key, "completed"))
        )
        rows.append((order, replace(row, state=state)))

    rows.sort(key=lambda item: (_context_state_order(item[1]), -item[0]))
    return tuple(row for _, row in rows)


def project_turn_latencies(turns: Sequence[TurnRecord]) -> tuple[TurnLatency, ...]:
    """Project persisted Turn timestamps into end-to-end wall-clock durations."""

    result: list[TurnLatency] = []
    for turn in turns:
        duration_ms: float | None = None
        if turn.completed_at:
            started = datetime.fromisoformat(turn.created_at)
            completed = datetime.fromisoformat(turn.completed_at)
            duration_ms = max(0.0, (completed - started).total_seconds() * 1000)
        result.append(TurnLatency(turn.seq, turn.status, duration_ms))
    return tuple(result)


def _run_id(agent: str, value: Any) -> str:
    if agent == "main":
        return "main"
    return str(value or "").strip()


def _current_context_states(
    context: Mapping[str, Any] | None,
) -> dict[tuple[str, str], str]:
    if not isinstance(context, Mapping):
        return {}

    states: dict[tuple[str, str], str] = {}

    def collect(node: Mapping[str, Any]) -> bool:
        waiting = (
            node.get("pending") is not None
            or node.get("waiting_for_user") is not None
        )
        children = node.get("active_children")
        if isinstance(children, Mapping):
            for child in children.values():
                if isinstance(child, Mapping) and collect(child):
                    waiting = True

        agent = str(node.get("agent") or "")
        run_id = _run_id(agent, node.get("run_id")) if agent else ""
        if agent and run_id:
            states[(agent, run_id)] = "waiting" if waiting else "active"
        return waiting

    collect(context)
    return states


def _terminal_run_state(
    event: SessionEvent,
) -> tuple[tuple[str, str], str] | None:
    if event.type != "agent_completed" or not event.agent:
        return None
    details = event.data.get("details")
    if not isinstance(details, Mapping):
        return None
    run_id = _run_id(event.agent, details.get("run_id"))
    if not run_id:
        return None
    state = str(event.data.get("status") or "completed")
    if state not in {"completed", "failed", "cancelled"}:
        state = "completed"
    return (event.agent, run_id), state


def _context_state_order(row: ContextUsage) -> int:
    if row.agent == "main":
        return 0
    if row.state == "active":
        return 1
    if row.state == "waiting":
        return 2
    return 3


def _context_usage_details(event: SessionEvent) -> Mapping[str, Any] | None:
    if event.type != "workflow_step":
        return None
    details = event.data.get("details")
    if not isinstance(details, Mapping) or details.get("debug_event") != "context_usage":
        return None
    return details


def _nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _positive_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


__all__ = [
    "ContextUsage",
    "TurnLatency",
    "project_context_usage",
    "project_turn_latencies",
]
