"""Best-effort reflection and file-backed Memory updates."""

from __future__ import annotations

from collections.abc import Iterable

from gitagent.agents.main import MainAgent
from gitagent.domain.learning import LearningTrace, ReflectionChanges
from gitagent.domain.models import SessionScope
from gitagent.infra.observability import TraceBus, TraceCategory, TraceStatus
from gitagent.infra.persistence import SessionManager
from gitagent.memory import MemoryStore

from .context import ReflectionContextBuilder


class LearningCoordinator:
    """Keep optional learning outside the already-successful business boundary."""

    def __init__(
        self,
        main_agent: MainAgent,
        sessions: SessionManager,
        memory: MemoryStore,
        trace: TraceBus,
        *,
        input_budget_tokens: int,
        enabled: bool = True,
    ) -> None:
        self.main_agent = main_agent
        self.memory = memory
        self.trace = trace
        self.enabled = enabled
        self.contexts = ReflectionContextBuilder(
            sessions,
            memory,
            input_budget_tokens=input_budget_tokens,
        )

    def reflect_domain(
        self,
        scope: SessionScope,
        repository_full_name: str,
        learning_trace: LearningTrace,
        *,
        turn_seq: int,
        accessed_paths: Iterable[tuple[str, str]] = (),
    ) -> dict[str, tuple[str, ...]] | None:
        accessed = tuple(accessed_paths)
        if not self.enabled:
            return self._touch_only(scope, accessed)
        try:
            context = self.contexts.for_domain(
                scope,
                repository_full_name,
                learning_trace,
                turn_seq=turn_seq,
            )
            changes = self.main_agent.reflect(context)
            result = self.memory.apply_changes(
                scope.account_key,
                scope.repository_key,
                changes,
                accessed_paths=accessed,
            )
            self._emit(scope, TraceStatus.COMPLETED, result, changes)
            return result
        except Exception as exc:  # noqa: BLE001 - learning never changes Domain success
            self._touch_after_failure(scope, accessed)
            self._emit_failure(scope, exc)
            return None

    def reflect_conversation(
        self,
        scope: SessionScope,
        repository_full_name: str,
        *,
        turn_seq: int,
        accessed_paths: Iterable[tuple[str, str]] = (),
    ) -> dict[str, tuple[str, ...]] | None:
        accessed = tuple(accessed_paths)
        if not self.enabled:
            return self._touch_only(scope, accessed)
        try:
            context = self.contexts.for_conversation(
                scope,
                repository_full_name,
                turn_seq=turn_seq,
            )
            changes = self.main_agent.reflect(context)
            result = self.memory.apply_changes(
                scope.account_key,
                scope.repository_key,
                changes,
                accessed_paths=accessed,
            )
            self._emit(scope, TraceStatus.COMPLETED, result, changes)
            return result
        except Exception as exc:  # noqa: BLE001 - response is already durable
            self._touch_after_failure(scope, accessed)
            self._emit_failure(scope, exc)
            return None

    def compact(
        self,
        scope: SessionScope,
        repository_full_name: str,
    ) -> dict[str, tuple[str, ...]] | None:
        """Run user-requested semantic consolidation over the complete index."""

        try:
            changes = self.main_agent.reflect(
                self.contexts.for_compaction(scope, repository_full_name)
            )
            result = self.memory.apply_changes(
                scope.account_key, scope.repository_key, changes
            )
            self._emit(scope, TraceStatus.COMPLETED, result, changes)
            return result
        except Exception as exc:  # noqa: BLE001 - management command reports through trace/result
            self._emit_failure(scope, exc)
            return None

    def _touch_only(
        self,
        scope: SessionScope,
        accessed: tuple[tuple[str, str], ...],
    ) -> dict[str, tuple[str, ...]]:
        return self.memory.apply_changes(
            scope.account_key,
            scope.repository_key,
            ReflectionChanges(),
            accessed_paths=accessed,
        )

    def _touch_after_failure(
        self,
        scope: SessionScope,
        accessed: tuple[tuple[str, str], ...],
    ) -> None:
        try:
            self._touch_only(scope, accessed)
        except Exception:  # noqa: BLE001, S110 - keep the original reflection failure
            pass

    def _emit(
        self,
        scope: SessionScope,
        status: TraceStatus,
        result: dict[str, tuple[str, ...]],
        changes: ReflectionChanges,
    ) -> None:
        self.trace.emit(
            session_id=scope.session_id,
            category=TraceCategory.WORKFLOW,
            name="long_term_learning",
            status=status,
            details={
                "triggered": True,
                "reason": "stored"
                if any(result[key] for key in ("added", "replaced", "deleted"))
                else "no_changes",
                "proposed": len(changes.add)
                + len(changes.replace)
                + len(changes.delete),
                **{key: list(value) for key, value in result.items()},
            },
        )

    def _emit_failure(self, scope: SessionScope, error: Exception) -> None:
        self.trace.emit(
            session_id=scope.session_id,
            category=TraceCategory.WORKFLOW,
            name="long_term_learning",
            status=TraceStatus.FAILED,
            message=str(error),
            details={"error_type": type(error).__name__},
        )


__all__ = ["LearningCoordinator"]
