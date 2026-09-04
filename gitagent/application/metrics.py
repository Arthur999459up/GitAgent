"""Read-only observability projections for GitAgent CLI metrics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gitagent.domain.models import SessionEvent
from gitagent.infra.persistence import TurnRecord

from .config import AGENT_NAMES


@dataclass(frozen=True, slots=True)
class ContextUsage:
    agent: str
    input_tokens: int | None
    context_window_tokens: int
    turn_seq: int | None = None
    run_id: str = ""

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
    agents: Sequence[str],
    context_windows: Mapping[str, int],
) -> tuple[ContextUsage, ...]:
    """Return the latest persisted model-visible context snapshot per Agent."""

    default_window = int(context_windows["default"])
    latest: dict[str, ContextUsage] = {}
    for event in events:
        details = _context_usage_details(event)
        if details is None or not event.agent:
            continue
        input_tokens = _nonnegative_int(details.get("input_tokens"))
        window = _positive_int(details.get("context_window_tokens"))
        if input_tokens is None or window is None:
            continue
        latest[event.agent] = ContextUsage(
            event.agent,
            input_tokens,
            window,
            turn_seq=event.turn_seq,
            run_id=str(details.get("run_id") or ""),
        )

    return tuple(
        latest.get(
            agent,
            ContextUsage(
                agent,
                None,
                int(context_windows.get(agent, default_window)),
            ),
        )
        for agent in agents
    )


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
    "AGENT_NAMES",
    "ContextUsage",
    "TurnLatency",
    "project_context_usage",
    "project_turn_latencies",
]
