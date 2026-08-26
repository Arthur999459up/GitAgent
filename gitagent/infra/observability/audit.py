"""Append-only, in-process audit trail."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from gitagent.domain.models import AccessLevel


@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    session_id: str
    agent: str
    tool: str
    action: str
    classification: AccessLevel
    approval_id: str | None
    result: str
    details: dict[str, Any]


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = Lock()

    def record(
        self,
        *,
        session_id: str,
        agent: str,
        tool: str,
        action: str,
        classification: AccessLevel,
        approval_id: str | None,
        result: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            session_id=session_id,
            agent=agent,
            tool=tool,
            action=action,
            classification=classification,
            approval_id=approval_id,
            result=result,
            details=details or {},
        )
        with self._lock:
            self._events.append(event)
        return event

    def events(self, session_id: str | None = None) -> list[AuditEvent]:
        with self._lock:
            events = list(self._events)
        return events if session_id is None else [event for event in events if event.session_id == session_id]
