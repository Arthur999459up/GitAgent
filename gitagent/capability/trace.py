"""Best-effort call/attempt/recovery trace for capabilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class CapabilityTraceEvent:
    timestamp: str
    run_id: str
    call_id: str
    capability_id: str
    event: str
    details: dict[str, Any] = field(default_factory=dict)


class CapabilityTrace:
    def __init__(self, bus: Any | None = None) -> None:
        self._events: list[CapabilityTraceEvent] = []
        self._listeners: list[Callable[[CapabilityTraceEvent], None]] = []
        self._lock = Lock()
        self._bus = bus

    def subscribe(self, listener: Callable[[CapabilityTraceEvent], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def emit(
        self,
        *,
        run_id: str,
        call_id: str,
        capability_id: str,
        event: str,
        details: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> CapabilityTraceEvent:
        trace_event = CapabilityTraceEvent(
            datetime.now(UTC).isoformat(),
            run_id,
            call_id,
            capability_id,
            event,
            details or {},
        )
        with self._lock:
            self._events.append(trace_event)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(trace_event)
            except Exception:  # noqa: BLE001, S112 - trace listeners are best effort
                continue
        if self._bus is not None:
            try:
                from gitagent.infra.observability import TraceCategory, TraceStatus

                status = (
                    TraceStatus.STARTED
                    if event.endswith("started")
                    else TraceStatus.COMPLETED
                    if event.endswith("succeeded")
                    else TraceStatus.FAILED
                )
                self._bus.emit(
                    session_id=session_id,
                    category=TraceCategory.CAPABILITY,
                    name=capability_id,
                    status=status,
                    details={
                        "run_id": run_id,
                        "call_id": call_id,
                        "event": event,
                        **(details or {}),
                    },
                )
            except Exception:  # noqa: BLE001, S110 - observability cannot affect invocation
                pass
        return trace_event

    def events(self, run_id: str | None = None) -> list[CapabilityTraceEvent]:
        with self._lock:
            events = list(self._events)
        return events if run_id is None else [event for event in events if event.run_id == run_id]
