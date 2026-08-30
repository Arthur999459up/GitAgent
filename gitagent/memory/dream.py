"""Opportunistic lifecycle maintenance for active Memory Pages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from gitagent.domain.models import SessionScope
from gitagent.infra.persistence import SessionManager

from .pages import MemoryPageStore


@dataclass(frozen=True)
class DreamEligibility:
    eligible: bool
    reason: str
    sessions_since_last_dream: int = 0


class AutoDream:
    def __init__(
        self,
        sessions: SessionManager,
        memory: MemoryPageStore,
        *,
        now: Callable[[], datetime] | None = None,
        min_interval_hours: int = 24,
        min_sessions: int = 5,
        gate_throttle_minutes: int = 10,
    ) -> None:
        self.sessions = sessions
        self.memory = memory
        self._now = now or (lambda: datetime.now().astimezone())
        self.min_interval = timedelta(hours=min_interval_hours)
        self.min_sessions = min_sessions
        self.gate_throttle = timedelta(minutes=gate_throttle_minutes)
        self._last_checks: dict[tuple[str, str], datetime] = {}
        self._lock = Lock()

    def eligible(self, scope: SessionScope, *, force: bool = False) -> DreamEligibility:
        now = self._aware_now()
        key = (scope.account_key, scope.repository_key)
        with self._lock:
            previous_check = self._last_checks.get(key)
            if not force and previous_check is not None and now - previous_check < self.gate_throttle:
                return DreamEligibility(False, "gate_throttled")
            self._last_checks[key] = now
        state = self.sessions.get_memory_dream_state(scope.account_key, scope.repository_key)
        if force:
            return DreamEligibility(True, "forced")
        if state.last_dream_at:
            last = datetime.fromisoformat(state.last_dream_at)
            if now - last < self.min_interval:
                return DreamEligibility(False, "minimum_interval")
        count = self.sessions.count_memory_sessions_since(
            scope.account_key,
            scope.repository_key,
            state.last_dream_session_marker,
        )
        if count < self.min_sessions:
            return DreamEligibility(False, "insufficient_sessions", count)
        return DreamEligibility(True, "eligible", count)

    def run(self, scope: SessionScope) -> dict[str, tuple[str, ...]]:
        result = self.memory.maintain(scope.account_key, scope.repository_key)
        now = self._aware_now().astimezone(UTC).isoformat(timespec="seconds")
        self.sessions.complete_memory_dream(
            scope.account_key,
            scope.repository_key,
            completed_at=now,
            session_marker=now,
        )
        return result

    def _aware_now(self) -> datetime:
        value = self._now()
        return value.astimezone() if value.tzinfo is None else value


__all__ = ["AutoDream", "DreamEligibility"]
