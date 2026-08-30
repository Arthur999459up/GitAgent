"""Turn-stop coordinator for coalesced extraction and opportunistic Dream."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any

from gitagent.domain.errors import StateError
from gitagent.domain.models import SessionScope
from gitagent.infra.observability import TraceBus, TraceCategory, TraceStatus
from gitagent.infra.persistence import SessionManager

from .dream import AutoDream
from .extractor import (
    MemoryExtractionContextBuilder,
    MemoryExtractionResult,
    MemoryExtractor,
)


class MemoryStopHooks:
    """Memory failures never change an already-durable business Turn result."""

    def __init__(
        self,
        sessions: SessionManager,
        extractor: MemoryExtractor,
        contexts: MemoryExtractionContextBuilder,
        dream: AutoDream,
        trace: TraceBus,
        *,
        enabled: bool = True,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self.sessions = sessions
        self.extractor = extractor
        self.contexts = contexts
        self.dream = dream
        self.trace = trace
        self.enabled = enabled
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="gitagent-memory"
        )
        self._owns_executor = executor is None
        self._lock = Lock()
        self._extracting: set[str] = set()
        self._dreaming = False
        self._futures: set[Future[Any]] = set()

    def handle_turn_stop(
        self,
        scope: SessionScope,
        repository_full_name: str,
        *,
        through_seq: int,
    ) -> None:
        try:
            self.sessions.mark_memory_extraction_pending(scope, through_seq)
        except Exception as exc:  # noqa: BLE001 - business Turn is already durable
            self.trace.emit(
                session_id=scope.session_id,
                category=TraceCategory.WORKFLOW,
                name="memory_extract",
                status=TraceStatus.FAILED,
                message=str(exc),
                display_message="无法登记本轮 Memory 提取任务，请查看 /debug。",
                details={"error_type": type(exc).__name__, "through_seq": through_seq},
            )
            return
        if self.enabled:
            self._schedule_extract(scope, repository_full_name)
            try:
                self.maybe_schedule_dream(scope)
            except Exception as exc:  # noqa: BLE001 - opportunistic gate is best effort
                self.trace.emit(
                    session_id=scope.session_id,
                    category=TraceCategory.WORKFLOW,
                    name="memory_dream",
                    status=TraceStatus.FAILED,
                    message=str(exc),
                    display_message="自动整理检查失败，维护状态未推进。",
                    details={"error_type": type(exc).__name__, "phase": "gate"},
                )

    def resume_pending(
        self, scope: SessionScope, repository_full_name: str
    ) -> bool:
        """Catch up a durable pending cursor after process/Service reconstruction."""

        if not self.enabled:
            return False
        try:
            state = self.sessions.get_memory_extraction_state(scope)
        except StateError as exc:
            self.trace.emit(
                session_id=scope.session_id,
                category=TraceCategory.WORKFLOW,
                name="memory_extract",
                status=TraceStatus.FAILED,
                message=str(exc),
                display_message="恢复待处理的 Memory 提取任务失败，请查看 /debug。",
                details={"error_type": type(exc).__name__, "phase": "resume"},
            )
            return False
        if state.pending_through_seq <= state.extracted_through_seq:
            return False
        return self._schedule_extract(scope, repository_full_name)

    def maybe_schedule_dream(self, scope: SessionScope, *, force: bool = False) -> bool:
        eligibility = self.dream.eligible(scope, force=force)
        if not eligibility.eligible:
            return False
        with self._lock:
            if self._dreaming:
                return False
            self._dreaming = True
        future = self._submit(self._run_dream, scope)
        future.add_done_callback(lambda _: self._finish_dream())
        return True

    def dream_now(self, scope: SessionScope) -> dict[str, tuple[str, ...]] | None:
        with self._lock:
            if self._dreaming:
                return None
            self._dreaming = True
        try:
            return self._run_dream(scope)
        finally:
            self._finish_dream()

    def wait_for_idle(self, timeout: float | None = None) -> None:
        while True:
            with self._lock:
                futures = tuple(self._futures)
                active = bool(self._extracting or self._dreaming)
            if not futures and not active:
                return
            for future in futures:
                future.result(timeout=timeout)

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=False)

    def _schedule_extract(self, scope: SessionScope, repository_full_name: str) -> bool:
        with self._lock:
            if scope.session_id in self._extracting:
                return False
            self._extracting.add(scope.session_id)
        self._launch_extract(scope, repository_full_name)
        return True

    def _launch_extract(
        self, scope: SessionScope, repository_full_name: str
    ) -> None:
        future = self._submit(self._run_extract, scope, repository_full_name)
        future.add_done_callback(
            lambda completed: self._finish_extract(
                completed, scope, repository_full_name
            )
        )

    def _run_extract(self, scope: SessionScope, repository_full_name: str) -> bool:
        try:
            state = self.sessions.get_memory_extraction_state(scope)
        except StateError as exc:
            self.trace.emit(
                session_id=scope.session_id,
                category=TraceCategory.WORKFLOW,
                name="memory_extract",
                status=TraceStatus.FAILED,
                message=str(exc),
                display_message="读取 Memory 提取进度失败，游标未推进。",
                details={"error_type": type(exc).__name__, "phase": "load_state"},
            )
            return False
        if state.pending_through_seq <= state.extracted_through_seq:
            return True
        target = state.pending_through_seq
        self.trace.emit(
            session_id=scope.session_id,
            category=TraceCategory.WORKFLOW,
            name="memory_extract",
            status=TraceStatus.STARTED,
            display_message="正在检查本轮是否有值得长期保存的内容…",
            details={
                "extracted_through_seq": state.extracted_through_seq,
                "target_through_seq": target,
            },
        )
        try:
            context = self.contexts.build(
                scope,
                repository_full_name,
                extracted_through_seq=state.extracted_through_seq,
                target_through_seq=target,
            )
            result = self.extractor.extract(context)
            self.sessions.complete_memory_extraction(scope, result.through_seq)
            self.trace.emit(
                session_id=scope.session_id,
                category=TraceCategory.WORKFLOW,
                name="memory_extract",
                status=TraceStatus.COMPLETED,
                display_message=_extraction_summary(result),
                details={
                    "reason": "noop" if result.noop else "written",
                    "candidates": result.candidates,
                    "written": list(result.written),
                    "skipped": list(result.skipped),
                    "through_seq": result.through_seq,
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001 - business Turn is already durable
            self.trace.emit(
                session_id=scope.session_id,
                category=TraceCategory.WORKFLOW,
                name="memory_extract",
                status=TraceStatus.FAILED,
                message=str(exc),
                display_message="提取失败，游标未推进；后续 Turn 会自动重试。",
                details={"error_type": type(exc).__name__, "through_seq": target},
            )
            return False

    def _finish_extract(
        self,
        future: Future[Any],
        scope: SessionScope,
        repository_full_name: str,
    ) -> None:
        error = future.exception()
        succeeded = error is None and bool(future.result())
        try:
            state = self.sessions.get_memory_extraction_state(scope)
        except StateError:
            state = None
        trailing = (
            succeeded
            and self.enabled
            and state is not None
            and state.pending_through_seq > state.extracted_through_seq
        )
        if trailing:
            # Keep the Session marked active while launching the coalesced trailing run,
            # so waiters cannot observe a false idle gap between the two Futures.
            self._launch_extract(scope, repository_full_name)
            return
        with self._lock:
            self._extracting.discard(scope.session_id)

    def _run_dream(self, scope: SessionScope) -> dict[str, tuple[str, ...]] | None:
        self.trace.emit(
            session_id=scope.session_id,
            category=TraceCategory.WORKFLOW,
            name="memory_dream",
            status=TraceStatus.STARTED,
            display_message="正在检查并整理 Memory Pages…",
        )
        try:
            result = self.dream.run(scope)
            self.trace.emit(
                session_id=scope.session_id,
                category=TraceCategory.WORKFLOW,
                name="memory_dream",
                status=TraceStatus.COMPLETED,
                display_message=_dream_summary(result),
                details={key: list(value) for key, value in result.items()},
            )
            return result
        except Exception as exc:  # noqa: BLE001 - maintenance state advances only on success
            self.trace.emit(
                session_id=scope.session_id,
                category=TraceCategory.WORKFLOW,
                name="memory_dream",
                status=TraceStatus.FAILED,
                message=str(exc),
                display_message="整理失败，维护状态未推进；请查看 /debug。",
                details={"error_type": type(exc).__name__},
            )
            return None

    def _finish_dream(self) -> None:
        with self._lock:
            self._dreaming = False

    def _submit(self, function: Any, *args: Any) -> Future[Any]:
        future = self._executor.submit(function, *args)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        return future

    def _discard_future(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)


def _extraction_summary(result: MemoryExtractionResult) -> str:
    written = len(result.written)
    skipped = len(result.skipped)
    if written:
        labels = "、".join(result.written_labels[:3])
        if written > 3:
            labels += f" 等 {written} 条"
        summary = f"已保存 {written} 条"
        if labels:
            summary += f"：{labels}"
        if skipped:
            summary += f"；另有 {skipped} 条已存在"
        return summary + "。"
    if skipped:
        return f"未新增；{skipped} 条候选已存在。"
    return "本轮未发现需要保存的长期记忆。"


def _dream_summary(result: dict[str, tuple[str, ...]]) -> str:
    disabled = len(result.get("disabled", ()))
    preserved = len(result.get("preserved", ()))
    if not disabled and not preserved:
        return "整理完成，当前没有需要合并或停用的 Page。"
    parts: list[str] = []
    if disabled:
        parts.append(f"停用 {disabled} 条重复 Page")
    if preserved:
        parts.append(f"保留 {preserved} 条代表 Page")
    return "整理完成：" + "，".join(parts) + "。"


__all__ = ["MemoryStopHooks"]
