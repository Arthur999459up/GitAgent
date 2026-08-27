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
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None


TraceListener = Callable[[TraceEvent], None]


class TraceBus:
    """保存当前会话事件，并把新事件同步通知 CLI。"""

    def __init__(self) -> None:
        self._events: list[TraceEvent] = []
        self._listeners: list[TraceListener] = []
        self._lock = Lock()

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
        details: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            timestamp=datetime.now(UTC).isoformat(),
            session_id=session_id,
            category=category,
            name=name,
            status=status,
            message=message,
            details=details or {},
            duration_ms=duration_ms,
        )
        with self._lock:
            self._events.append(event)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001, S112 - UI 监听器不能破坏 Agent 执行
                continue
        return event

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
