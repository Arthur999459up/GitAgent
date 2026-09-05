"""面向交互界面的实时执行事件流。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from threading import Lock
from typing import Any


class TraceCategory(str, Enum):
    AGENT = "agent"
    CAPABILITY = "capability"
    WORKFLOW = "workflow"


class TraceStatus(str, Enum):
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    WAITING = "waiting"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TraceEvent:
    timestamp: str
    session_id: str
    category: TraceCategory
    name: str
    status: TraceStatus
    message: str = ""
    # UI-only text: SessionEventRecorder intentionally persists message/details,
    # never this field, so a friendly summary cannot enter durable history.
    display_message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    turn_seq: int | None = None


TraceListener = Callable[[TraceEvent], None]


class TraceBus:
    """保存当前会话事件，并把新事件同步通知 CLI。"""

    def __init__(self, *, persistent_sink: TraceListener | None = None) -> None:
        self._events: list[TraceEvent] = []
        self._listeners: list[TraceListener] = []
        self._turns: dict[str, int] = {}
        self._persistent_sink = persistent_sink
        self._lock = Lock()

    def bind_turn(self, session_id: str, turn_seq: int) -> None:
        if not isinstance(turn_seq, int) or isinstance(turn_seq, bool) or turn_seq < 1:
            raise ValueError("turn_seq must be a positive integer")
        with self._lock:
            self._turns[session_id] = turn_seq

    def subscribe(self, listener: TraceListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def emit(
        self,
        *,
        session_id: str,
        category: TraceCategory,
        name: str,
        status: TraceStatus,
        message: str = "",
        display_message: str = "",
        details: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        turn_seq: int | None = None,
    ) -> TraceEvent:
        with self._lock:
            effective_turn_seq = turn_seq or self._turns.get(session_id)
        event = TraceEvent(
            timestamp=datetime.now(UTC).isoformat(),
            session_id=session_id,
            category=category,
            name=name,
            status=status,
            message=message,
            display_message=display_message,
            details=details or {},
            duration_ms=duration_ms,
            turn_seq=effective_turn_seq,
        )
        if self._persistent_sink is not None:
            self._persistent_sink(event)
        with self._lock:
            self._events.append(event)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001, S112 - UI 监听器不能破坏 Agent 执行
                continue
        return event

    def emit_auto_compaction(
        self,
        *,
        session_id: str,
        agent: str,
        level: str,
        before_tokens: int,
        after_tokens: int,
        context_window_tokens: int,
        run_id: str = "",
        turn_seq: int | None = None,
    ) -> TraceEvent:
        """Emit the one normalized event used for automatic compact visibility."""

        return self.emit(
            session_id=session_id,
            category=TraceCategory.WORKFLOW,
            name="auto_compact",
            status=TraceStatus.COMPLETED,
            details={
                "agent": agent,
                "run_id": "main" if agent == "main" else run_id,
                "level": level,
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "context_window_tokens": context_window_tokens,
            },
            turn_seq=turn_seq,
        )

    def events(self, session_id: str | None = None) -> list[TraceEvent]:
        with self._lock:
            events = list(self._events)
        return events if session_id is None else [event for event in events if event.session_id == session_id]

    def debug_events(self, session_id: str, agent: str | None = None) -> list[TraceEvent]:
        """Return one Session's trace, optionally restricted to events owned by one agent."""

        events = self.events(session_id)
        if agent is None:
            return events
        return [
            event
            for event in events
            if (event.category == TraceCategory.AGENT and event.name == agent)
            or (event.category == TraceCategory.CAPABILITY and event.details.get("agent") == agent)
            or (event.category == TraceCategory.WORKFLOW and event.details.get("agent") == agent)
        ]

    def clear(self) -> None:
        """Clear process-local trace events while preserving live UI subscriptions."""
        with self._lock:
            self._events.clear()
