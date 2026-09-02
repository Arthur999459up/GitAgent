"""Harness-owned execution coordination and Capability orchestration."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from threading import Condition, Event, Lock, Semaphore, local
from time import perf_counter
from typing import Any, TypeVar

from gitagent.agent_loop.models import AgentCall, CapabilityCall, StructuredCall
from gitagent.capability import (
    AccessLevel,
    CapabilityLayer,
    CapabilityResult,
    InvocationContext,
)
from gitagent.capability.schema import validate_schema, validate_schema_definition
from gitagent.domain.errors import StructuredOutputError, ValidationError
from gitagent.domain.models import AgentGuidance, AgentSpec, VerificationReport
from gitagent.harness.context.state import AgentContext
from gitagent.harness.validation.output import validate_agent_output
from gitagent.infra.observability import AuditLog, TraceBus, TraceCategory, TraceStatus

T = TypeVar("T")


class _ExecutionCancelled(Exception):
    """Internal signal used to quiesce a cancelled execution batch."""


@dataclass(frozen=True)
class _GroupRun:
    """Outcomes that are safe to commit after one execution group quiesces."""

    outcomes: tuple[Any, ...]
    resolved: tuple[bool, ...]
    interruption: BaseException | None = None


class _CancellationHandle:
    """Transient cancellation tree; never stored in an AgentContext."""

    def __init__(self, wake_waiters: Callable[[], None]) -> None:
        self.cancelled = Event()
        self._wake_waiters = wake_waiters
        self._lock = Lock()
        self._futures: set[Future[Any]] = set()
        self._children: set[_CancellationHandle] = set()

    def add_future(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.add(future)
            cancelled = self.cancelled.is_set()
        if cancelled:
            future.cancel()

    def add_child(self, child: _CancellationHandle) -> None:
        with self._lock:
            self._children.add(child)
            cancelled = self.cancelled.is_set()
        if cancelled:
            child.cancel()

    def remove_child(self, child: _CancellationHandle) -> None:
        with self._lock:
            self._children.discard(child)

    def cancel(self) -> None:
        self.cancelled.set()
        self._wake_waiters()
        with self._lock:
            futures = tuple(self._futures)
            children = tuple(self._children)
        for future in futures:
            future.cancel()
        for child in children:
            child.cancel()


class ConcurrencyMode(str, Enum):
    CONCURRENT = "CONCURRENT"
    EXCLUSIVE = "EXCLUSIVE"
    UNKNOWN = "UNKNOWN"


class FailureScope(str, Enum):
    ISOLATED = "ISOLATED"
    FENCE = "FENCE"


@dataclass(frozen=True)
class ResourceClaims:
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        reads = tuple(dict.fromkeys(self.read))
        writes = tuple(dict.fromkeys(self.write))
        if any(not isinstance(key, str) or not key.strip() for key in (*reads, *writes)):
            raise ValidationError("resource claim keys must be non-empty strings")
        if set(reads) & set(writes):
            raise ValidationError("one resource cannot be both read and written")
        object.__setattr__(self, "read", reads)
        object.__setattr__(self, "write", writes)

    def compatible_with(self, other: ResourceClaims) -> bool:
        return not (
            set(self.write) & (set(other.read) | set(other.write))
            or set(other.write) & set(self.read)
        )


@dataclass(frozen=True)
class ExecutionProfile:
    concurrency_mode: ConcurrencyMode
    resource_claims: ResourceClaims = field(default_factory=ResourceClaims)
    failure_scope: FailureScope = FailureScope.FENCE

    def __post_init__(self) -> None:
        if not isinstance(self.concurrency_mode, ConcurrencyMode):
            raise ValidationError("execution concurrency_mode is invalid")
        if not isinstance(self.resource_claims, ResourceClaims):
            raise ValidationError("execution resource_claims are invalid")
        if not isinstance(self.failure_scope, FailureScope):
            raise ValidationError("execution failure_scope is invalid")
        if (
            self.concurrency_mode == ConcurrencyMode.CONCURRENT
            and self.failure_scope != FailureScope.ISOLATED
        ):
            raise ValidationError("CONCURRENT execution must have ISOLATED failure scope")
        if (
            self.concurrency_mode != ConcurrencyMode.CONCURRENT
            and self.failure_scope != FailureScope.FENCE
        ):
            raise ValidationError("EXCLUSIVE/UNKNOWN execution must have FENCE failure scope")
        if (
            self.concurrency_mode == ConcurrencyMode.UNKNOWN
            and not self.resource_claims.write
        ):
            raise ValidationError(
                "UNKNOWN execution requires a conservative write resource claim"
            )

    @classmethod
    def concurrent(
        cls, *, read: Sequence[str] = (), write: Sequence[str] = ()
    ) -> ExecutionProfile:
        return cls(
            ConcurrencyMode.CONCURRENT,
            ResourceClaims(tuple(read), tuple(write)),
            FailureScope.ISOLATED,
        )

    @classmethod
    def exclusive(
        cls, *, read: Sequence[str] = (), write: Sequence[str] = ()
    ) -> ExecutionProfile:
        return cls(
            ConcurrencyMode.EXCLUSIVE,
            ResourceClaims(tuple(read), tuple(write)),
            FailureScope.FENCE,
        )

    @classmethod
    def unknown(cls, *, repository: str = "") -> ExecutionProfile:
        scope = repository.strip() or "<unscoped>"
        return cls(
            ConcurrencyMode.UNKNOWN,
            ResourceClaims(
                write=(f"workspace:{scope}", f"repo:{scope}"),
            ),
            FailureScope.FENCE,
        )


class ResourceClaimManager:
    """Atomically admit FIFO read/write claims over shared resource keys."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._waiters: deque[object] = deque()
        self._readers: dict[str, int] = {}
        self._writers: set[str] = set()

    @contextmanager
    def claim(
        self,
        claims: ResourceClaims,
        *,
        cancelled: Event | None = None,
    ) -> Iterator[None]:
        ticket = object()
        with self._condition:
            self._waiters.append(ticket)
            try:
                self._condition.wait_for(
                    lambda: bool(cancelled and cancelled.is_set())
                    or (
                        self._waiters[0] is ticket
                        and self._available(claims)
                    )
                )
                if cancelled is not None and cancelled.is_set():
                    self._waiters.remove(ticket)
                    self._condition.notify_all()
                    raise _ExecutionCancelled
            except BaseException:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiters.popleft()
            for key in claims.read:
                self._readers[key] = self._readers.get(key, 0) + 1
            self._writers.update(claims.write)
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                for key in claims.read:
                    remaining = self._readers[key] - 1
                    if remaining:
                        self._readers[key] = remaining
                    else:
                        self._readers.pop(key)
                self._writers.difference_update(claims.write)
                self._condition.notify_all()

    def wake_waiters(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _available(self, claims: ResourceClaims) -> bool:
        return not (
            set(claims.read) & self._writers
            or set(claims.write) & (self._writers | set(self._readers))
        )


class ExecutionCoordinator:
    """Run one ordered model batch with bounded admission and ordered commit."""

    def __init__(
        self,
        *,
        capability_max_concurrency: int,
        provider_concurrency: Mapping[str, int],
        domain_agent_max_concurrency: int,
    ) -> None:
        self.resource_claims = ResourceClaimManager()
        self._capability_executor = ThreadPoolExecutor(
            max_workers=capability_max_concurrency,
            thread_name_prefix="gitagent-capability",
        )
        self._domain_executor = ThreadPoolExecutor(
            max_workers=domain_agent_max_concurrency,
            thread_name_prefix="gitagent-domain",
        )
        self._provider_slots = {
            provider_id: Semaphore(limit)
            for provider_id, limit in provider_concurrency.items()
        }
        self._active_lock = Lock()
        self._active: dict[int, set[_CancellationHandle]] = {}
        self._worker_local = local()

    def execute(
        self,
        calls: Sequence[CapabilityCall | AgentCall],
        profiles: Sequence[ExecutionProfile],
        *,
        prepare_call: Callable[[CapabilityCall | AgentCall], None],
        run_call: Callable[[CapabilityCall | AgentCall], Any],
        commit_call: Callable[[CapabilityCall | AgentCall, Any], str],
        suspend_call: Callable[[CapabilityCall | AgentCall, Any], None],
        cancel_call: Callable[[CapabilityCall | AgentCall, str], None],
        lane_for: Callable[[CapabilityCall | AgentCall], str],
        provider_for: Callable[[CapabilityCall | AgentCall], str | None],
        owner: object,
        observe_failure: Callable[
            [CapabilityCall | AgentCall, ExecutionProfile, str], None
        ]
        | None = None,
    ) -> bool:
        if len(calls) != len(profiles):
            raise ValidationError("calls and execution profiles must stay aligned")
        cancellation, parent = self._register_cancellation(owner)
        settled: set[str] = set()

        def cancel_unsettled(
            pending_calls: Sequence[CapabilityCall | AgentCall], reason: str
        ) -> None:
            for pending_call in pending_calls:
                if pending_call.call_id in settled:
                    continue
                cancel_call(pending_call, reason)
                settled.add(pending_call.call_id)

        try:
            position = 0
            while position < len(calls):
                if cancellation.cancelled.is_set():
                    cancel_unsettled(calls[position:], "execution was cancelled")
                    return False
                end = self._group_end(profiles, position)
                group_calls = calls[position:end]
                group_profiles = profiles[position:end]
                for call in group_calls:
                    if cancellation.cancelled.is_set():
                        cancel_unsettled(calls[position:], "execution was cancelled")
                        return False
                    prepare_call(call)
                group = self._run_group(
                    group_calls,
                    group_profiles,
                    run_call=run_call,
                    lane_for=lane_for,
                    provider_for=provider_for,
                    cancellation=cancellation,
                )
                interrupted = group.interruption is not None
                cancelled = cancellation.cancelled.is_set()
                reason = (
                    self._interruption_reason(group.interruption)
                    if interrupted
                    else "execution was cancelled"
                )
                if cancelled and not interrupted:
                    cancel_unsettled(calls[position:], reason)
                    return False
                for offset, (call, profile, outcome, resolved) in enumerate(
                    zip(
                        group_calls,
                        group_profiles,
                        group.outcomes,
                        group.resolved,
                        strict=True,
                    )
                ):
                    if not resolved:
                        cancel_unsettled([call], reason)
                        continue
                    decision = commit_call(call, outcome)
                    if decision == "waiting":
                        if interrupted or cancelled:
                            cancel_unsettled([call], reason)
                            continue
                        for suspended_call, suspended_outcome in zip(
                            group_calls[offset + 1 :],
                            group.outcomes[offset + 1 :],
                            strict=True,
                        ):
                            suspend_call(suspended_call, suspended_outcome)
                        return False
                    settled.add(call.call_id)
                    if decision == "failed" and self.failure_stops_batch(
                        call,
                        profile,
                        observe_failure=observe_failure,
                    ):
                        cancel_unsettled(
                            calls[position + offset + 1 :],
                            "stopped after an execution fence failed",
                        )
                        return True
                    if decision != "continue" and decision != "failed":
                        raise ValidationError(
                            f"invalid ordered commit decision: {decision}"
                        )
                if group.interruption is not None:
                    cancel_unsettled(calls[end:], reason)
                    raise group.interruption
                if cancellation.cancelled.is_set():
                    cancel_unsettled(calls[end:], "execution was cancelled")
                    return False
                position = end
            return True
        except BaseException as exc:
            cancellation.cancel()
            cancel_unsettled(calls, self._interruption_reason(exc))
            raise
        finally:
            self._unregister_cancellation(owner, cancellation, parent)

    @staticmethod
    def failure_stops_batch(
        call: CapabilityCall | AgentCall,
        profile: ExecutionProfile,
        *,
        observe_failure: Callable[
            [CapabilityCall | AgentCall, ExecutionProfile, str], None
        ]
        | None = None,
    ) -> bool:
        """Own the one decision that propagates a settled call failure."""

        stops_batch = profile.failure_scope == FailureScope.FENCE
        if observe_failure is not None:
            observe_failure(
                call,
                profile,
                "cancel_siblings" if stops_batch else "isolate",
            )
        return stops_batch

    @staticmethod
    def _group_end(profiles: Sequence[ExecutionProfile], start: int) -> int:
        first = profiles[start]
        if first.concurrency_mode != ConcurrencyMode.CONCURRENT:
            return start + 1
        claims = [first.resource_claims]
        end = start + 1
        while end < len(profiles):
            candidate = profiles[end]
            if candidate.concurrency_mode != ConcurrencyMode.CONCURRENT:
                break
            if not all(candidate.resource_claims.compatible_with(item) for item in claims):
                break
            claims.append(candidate.resource_claims)
            end += 1
        return end

    def _run_group(
        self,
        calls: Sequence[CapabilityCall | AgentCall],
        profiles: Sequence[ExecutionProfile],
        *,
        run_call: Callable[[CapabilityCall | AgentCall], Any],
        lane_for: Callable[[CapabilityCall | AgentCall], str],
        provider_for: Callable[[CapabilityCall | AgentCall], str | None],
        cancellation: _CancellationHandle,
    ) -> _GroupRun:
        futures: list[Future[Any] | None] = [None] * len(calls)
        scheduled = [False] * len(calls)
        immediate: list[Any] = [None] * len(calls)
        resolved = [False] * len(calls)
        interruption: BaseException | None = None
        for index, (call, profile) in enumerate(zip(calls, profiles, strict=True)):
            try:
                lane = lane_for(call)
                task = lambda call=call, profile=profile: self._admitted_run(
                    call,
                    profile,
                    run_call=run_call,
                    provider_id=provider_for(call),
                    cancellation=cancellation,
                )
                if lane == "capability":
                    future = self._capability_executor.submit(task)
                    cancellation.add_future(future)
                    futures[index] = future
                    scheduled[index] = True
                elif lane == "domain":
                    future = self._domain_executor.submit(task)
                    cancellation.add_future(future)
                    futures[index] = future
                    scheduled[index] = True
                elif lane == "inline":
                    scheduled[index] = True
                    try:
                        immediate[index] = task()
                        resolved[index] = True
                    except Exception as exc:  # noqa: BLE001 - ordered result
                        immediate[index] = exc
                        resolved[index] = True
                else:
                    raise ValidationError(f"unknown execution lane: {lane}")
            except BaseException as exc:  # noqa: BLE001 - interrupts cancel the batch
                interruption = exc
                break

        worker_futures = [future for future in futures if future is not None]
        if worker_futures and interruption is None:
            try:
                done, _ = wait(worker_futures, return_when=FIRST_EXCEPTION)
            except BaseException as exc:  # noqa: BLE001 - interrupts cancel the batch
                interruption = exc
                done = {future for future in worker_futures if future.done()}
            if interruption is None:
                for future in done:
                    if future.cancelled():
                        continue
                    outcome_error = future.exception()
                    if outcome_error is not None and not isinstance(
                        outcome_error, Exception
                    ):
                        interruption = outcome_error
                        break

        if interruption is not None:
            cancellation.cancel()
        if cancellation.cancelled.is_set():
            for future in futures:
                if future is not None:
                    future.cancel()

        pending = [future for future in worker_futures if not future.done()]
        while pending:
            try:
                wait(pending)
            except BaseException as exc:  # noqa: BLE001 - interrupts cancel the batch
                if interruption is None:
                    interruption = exc
                cancellation.cancel()
                for future in pending:
                    future.cancel()
            pending = [future for future in pending if not future.done()]

        outcomes = list(immediate)
        for index, future in enumerate(futures):
            if future is None or not scheduled[index] or future.cancelled():
                continue
            outcome_error = future.exception()
            if isinstance(outcome_error, _ExecutionCancelled):
                continue
            if outcome_error is not None and not isinstance(outcome_error, Exception):
                if interruption is None:
                    interruption = outcome_error
                continue
            outcomes[index] = outcome_error if outcome_error is not None else future.result()
            resolved[index] = True
        return _GroupRun(tuple(outcomes), tuple(resolved), interruption)

    def _admitted_run(
        self,
        call: CapabilityCall | AgentCall,
        profile: ExecutionProfile,
        *,
        run_call: Callable[[CapabilityCall | AgentCall], Any],
        provider_id: str | None,
        cancellation: _CancellationHandle,
    ) -> Any:
        if cancellation.cancelled.is_set():
            raise _ExecutionCancelled
        provider_slot = None
        if isinstance(call, CapabilityCall) and provider_id is not None:
            provider_slot = self._provider_slots.get(provider_id)
            if provider_slot is None:
                raise ValidationError(
                    f"execution.provider_concurrency has no limit for {provider_id}"
                )
        previous = getattr(self._worker_local, "cancellation", None)
        self._worker_local.cancellation = cancellation
        try:
            if provider_slot is None:
                with self.resource_claims.claim(
                    profile.resource_claims,
                    cancelled=cancellation.cancelled,
                ):
                    if cancellation.cancelled.is_set():
                        raise _ExecutionCancelled
                    return run_call(call)
            self._acquire_provider(provider_slot, cancellation)
            try:
                with self.resource_claims.claim(
                    profile.resource_claims,
                    cancelled=cancellation.cancelled,
                ):
                    if cancellation.cancelled.is_set():
                        raise _ExecutionCancelled
                    return run_call(call)
            finally:
                provider_slot.release()
        finally:
            if previous is None:
                del self._worker_local.cancellation
            else:
                self._worker_local.cancellation = previous

    @staticmethod
    def _acquire_provider(
        provider_slot: Semaphore, cancellation: _CancellationHandle
    ) -> None:
        while not provider_slot.acquire(timeout=0.05):
            if cancellation.cancelled.is_set():
                raise _ExecutionCancelled
        if cancellation.cancelled.is_set():
            provider_slot.release()
            raise _ExecutionCancelled

    def _register_cancellation(
        self, owner: object
    ) -> tuple[_CancellationHandle, _CancellationHandle | None]:
        cancellation = _CancellationHandle(self.resource_claims.wake_waiters)
        parent = getattr(self._worker_local, "cancellation", None)
        if parent is not None:
            parent.add_child(cancellation)
        with self._active_lock:
            self._active.setdefault(id(owner), set()).add(cancellation)
        return cancellation, parent

    def _unregister_cancellation(
        self,
        owner: object,
        cancellation: _CancellationHandle,
        parent: _CancellationHandle | None,
    ) -> None:
        with self._active_lock:
            handles = self._active.get(id(owner))
            if handles is not None:
                handles.discard(cancellation)
                if not handles:
                    self._active.pop(id(owner), None)
        if parent is not None:
            parent.remove_child(cancellation)

    @staticmethod
    def _interruption_reason(interruption: BaseException | None) -> str:
        if interruption is None:
            return "execution was cancelled"
        return f"execution interrupted by {type(interruption).__name__}"

    def cancel(self, owner: object) -> bool:
        """Request cancellation of an active batch and all nested child batches."""

        with self._active_lock:
            handles = tuple(self._active.get(id(owner), ()))
        for cancellation in handles:
            cancellation.cancel()
        return bool(handles)

    @contextmanager
    def cancellation_scope(self, owner: object) -> Iterator[None]:
        """Keep one Agent turn cancellable across model and execution phases."""

        cancellation, parent = self._register_cancellation(owner)
        previous = getattr(self._worker_local, "cancellation", None)
        self._worker_local.cancellation = cancellation
        try:
            yield
        finally:
            if previous is None:
                del self._worker_local.cancellation
            else:
                self._worker_local.cancellation = previous
            self._unregister_cancellation(owner, cancellation, parent)

    def cancellation_requested(self) -> bool:
        cancellation = getattr(self._worker_local, "cancellation", None)
        return bool(cancellation and cancellation.cancelled.is_set())

    @contextmanager
    def claim_resources(self, claims: ResourceClaims) -> Iterator[None]:
        """Acquire a direct Runtime lease under the current worker cancellation."""

        cancellation = getattr(self._worker_local, "cancellation", None)
        with self.resource_claims.claim(
            claims,
            cancelled=(cancellation.cancelled if cancellation is not None else None),
        ):
            yield

    def _cancel_all(self) -> None:
        with self._active_lock:
            handles = tuple(
                handle for active in self._active.values() for handle in active
            )
        for cancellation in handles:
            cancellation.cancel()

    def close(self) -> None:
        self._cancel_all()
        self._capability_executor.shutdown(wait=True, cancel_futures=True)
        self._domain_executor.shutdown(wait=True, cancel_futures=True)


class AgentHarness:
    """Own agent state, approval workflow, observations, and capability access."""

    def __init__(
        self,
        capabilities: CapabilityLayer,
        *,
        audit: AuditLog | None = None,
        trace: TraceBus | None = None,
        context_window_tokens: Mapping[str, int] | None = None,
        execution: Mapping[str, Any],
    ) -> None:
        windows = (
            {"default": 32_768}
            if context_window_tokens is None
            else dict(context_window_tokens)
        )
        if "default" not in windows:
            raise ValueError("context_window_tokens must define a default")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in windows.values()
        ):
            raise ValueError(
                "every context_window_tokens value must be a positive integer"
            )
        self._capabilities = capabilities
        self.approvals = capabilities.policy.approvals
        self.audit = audit or AuditLog()
        self.trace = trace or TraceBus()
        self.context_window_tokens = windows
        required_execution = {
            "max_calls_per_turn",
            "capability_max_concurrency",
            "provider_concurrency",
            "domain_agent_max_concurrency",
            "max_agent_depth",
            "default_agent_max_steps",
            "agent_max_steps_overrides",
            "max_structured_retries",
            "max_provider_retries",
        }
        if set(execution) != required_execution:
            raise ValidationError("Harness execution configuration is incomplete")
        self.max_calls_per_turn = int(execution["max_calls_per_turn"])
        self.max_agent_depth = int(execution["max_agent_depth"])
        self.default_agent_max_steps = int(execution["default_agent_max_steps"])
        self.agent_max_steps_overrides = dict(execution["agent_max_steps_overrides"])
        self.max_structured_retries = int(execution["max_structured_retries"])
        self.max_provider_retries = int(execution["max_provider_retries"])
        self.coordinator = ExecutionCoordinator(
            capability_max_concurrency=int(execution["capability_max_concurrency"]),
            provider_concurrency=dict(execution["provider_concurrency"]),
            domain_agent_max_concurrency=int(execution["domain_agent_max_concurrency"]),
        )
        self._specs: dict[str, AgentSpec] = {}
        self.message_sink: (
            Callable[[AgentContext, dict[str, Any]], dict[str, Any]] | None
        ) = None
        self.compaction_sink: (
            Callable[[AgentContext, Any, str, int, int], None] | None
        ) = None

    def context_window_for(self, agent_name: str) -> int:
        return self.context_window_tokens.get(
            agent_name, self.context_window_tokens["default"]
        )

    def register(self, spec: AgentSpec) -> None:
        if spec.name in self._specs:
            raise ValidationError(f"duplicate agent spec: {spec.name}")
        if (
            not isinstance(spec.agent_depth, int)
            or isinstance(spec.agent_depth, bool)
            or spec.agent_depth < 0
        ):
            raise ValidationError(
                f"agent {spec.name} has an invalid execution depth"
            )
        self._specs[spec.name] = spec

    def context(
        self,
        agent_name: str,
        session_id: str,
        *,
        repository: str = "",
        goal: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        guidance: AgentGuidance | None = None,
    ) -> AgentContext:
        return AgentContext(
            self,
            self.spec(agent_name),
            session_id,
            repository=repository,
            goal=goal,
            entity_type=entity_type,
            entity_id=entity_id,
            guidance=guidance,
            max_steps=self.agent_max_steps_overrides.get(
                agent_name, self.default_agent_max_steps
            ),
        )

    def run(
        self,
        agent_name: str,
        *,
        session_id: str,
        operation: Callable[[AgentContext], T],
        repository: str = "",
        goal: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        guidance: AgentGuidance | None = None,
    ) -> T:
        context = self.context(
            agent_name,
            session_id,
            repository=repository,
            goal=goal,
            entity_type=entity_type,
            entity_id=entity_id,
            guidance=guidance,
        )
        started = perf_counter()
        self.trace.emit(
            session_id=session_id,
            category=TraceCategory.AGENT,
            name=agent_name,
            status=TraceStatus.STARTED,
        )
        try:
            result = operation(context)
            validate_agent_output(context.spec, result)
        except Exception as exc:
            context.error = str(exc)
            self.trace.emit(
                session_id=session_id,
                category=TraceCategory.AGENT,
                name=agent_name,
                status=TraceStatus.FAILED,
                message=str(exc),
                details={"error_type": type(exc).__name__},
                duration_ms=(perf_counter() - started) * 1000,
            )
            raise
        self.trace.emit(
            session_id=session_id,
            category=TraceCategory.AGENT,
            name=agent_name,
            status=TraceStatus.COMPLETED,
            details={"output_type": type(result).__name__},
            duration_ms=(perf_counter() - started) * 1000,
        )
        if isinstance(result, VerificationReport):
            self.trace.emit(
                session_id=session_id,
                category=TraceCategory.WORKFLOW,
                name="verification",
                status=TraceStatus.COMPLETED,
                details={
                    "agent": agent_name,
                    "result": asdict(result),
                    "status": "passed" if result.passed else "failed",
                },
            )
        return result

    def invoke(
        self,
        context: AgentContext,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        approval_id: str | None = None,
        call_id: str | None = None,
        preflighted: bool = False,
    ) -> CapabilityResult:
        invocation = self.invocation_context(
            context, approval_id=approval_id, call_id=call_id
        )
        return self._capabilities.invoke(
            capability_id,
            arguments,
            invocation,
            preflighted=preflighted,
        )

    def audit_capability_result(
        self,
        context: AgentContext,
        record: Any,
        *,
        approval_id: str | None = None,
    ) -> None:
        """Record the authoritative outcome after Harness post-processing."""

        result = record.result
        capability_id = result.capability_id
        visible = next(
            (item for item in self.discover(context) if item.id == capability_id), None
        )
        audit_result = (
            "OK"
            if result.status == "success"
            else "DENIED"
            if result.status == "approval_required"
            else "FAILED"
        )
        arguments = (
            record.execution_arguments
            if record.execution_arguments is not None
            else record.arguments
        )
        details: dict[str, Any] = {
            "call_id": record.call_id,
            "argument_keys": sorted(arguments),
            "attempts": result.attempts,
        }
        if result.error is not None:
            details["error"] = result.error.type.value
        self.audit.record(
            session_id=context.session_id,
            agent=context.agent,
            capability_id=capability_id,
            action=capability_id,
            classification=visible.access if visible is not None else None,
            approval_id=approval_id,
            result=audit_result,
            details=details,
        )

    def discover(self, context: AgentContext) -> tuple[Any, ...]:
        return self._capabilities.discover(self.invocation_context(context))

    def llm_tools(
        self,
        context: AgentContext,
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": self._function_name(capability.id),
                    "description": capability.description,
                    "parameters": capability.input_schema,
                },
            }
            for capability in self.discover(context)
            if capability.input_schema is not None
            and (not read_only or capability.access == AccessLevel.READ)
        ]

    @staticmethod
    def agent_tool(
        agent_id: str,
        description: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        validate_schema_definition(schema, f"agent {agent_id} input schema")
        if schema.get("type") != "object":
            raise ValidationError(f"agent {agent_id} input schema must describe an object")
        return {
            "type": "function",
            "function": {
                "name": f"agent__{agent_id}",
                "description": description,
                "parameters": schema,
            },
        }

    def resolve_model_call(
        self,
        call: StructuredCall,
        context: AgentContext,
        *,
        agent_schemas: Mapping[str, dict[str, Any]] | None = None,
    ) -> CapabilityCall | AgentCall:
        if call.name.startswith("capability__"):
            capability_id = self.resolve_llm_name(call.name, context)
            if capability_id == call.name:
                raise StructuredOutputError(
                    f"unknown Capability function: {call.name}"
                )
            return CapabilityCall(call.call_id, capability_id, dict(call.arguments))
        if call.name.startswith("agent__"):
            agent_id = call.name.removeprefix("agent__")
            schemas = agent_schemas or {}
            schema = schemas.get(agent_id)
            if schema is None:
                raise StructuredOutputError(
                    f"Agent call is not allowed here: {call.name}"
                )
            try:
                validate_schema(
                    call.arguments,
                    schema,
                    label=f"{call.name} arguments",
                )
            except ValidationError as exc:
                raise StructuredOutputError(str(exc)) from exc
            return AgentCall(call.call_id, agent_id, dict(call.arguments))
        raise StructuredOutputError(
            "structured function name must use capability__ or agent__ namespace"
        )

    def describe_capability_execution(
        self,
        context: AgentContext,
        call: CapabilityCall,
    ) -> ExecutionProfile:
        return self._capabilities.describe_execution(
            call.capability_id,
            call.arguments,
            self.invocation_context(context, call_id=call.call_id),
        )

    def capability_permission_decision(
        self, context: AgentContext, call: CapabilityCall
    ) -> str:
        return self._capabilities.permission_decision(
            call.capability_id,
            call.arguments,
            self.invocation_context(context, call_id=call.call_id),
        )

    def capability_failure_blocked(
        self,
        context: AgentContext,
        call: CapabilityCall,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> bool:
        identity = self._capabilities.failure_guard.call_identity(
            context.agent,
            call.capability_id,
            dict(call.arguments if arguments is None else arguments),
        )
        return self._capabilities.failure_guard.blocked(context.run_id, identity)

    def commit_capability_failure_guard(
        self,
        context: AgentContext,
        record: Any,
    ) -> None:
        arguments = record.execution_arguments
        self._capabilities.commit_failure_guard(
            record.result.capability_id,
            dict(arguments if arguments is not None else record.arguments),
            self.invocation_context(context, call_id=record.call_id),
            record.result,
        )

    def provider_id(self, capability_id: str) -> str | None:
        registration = self._capabilities.registry.resolve(capability_id)
        if registration is None:
            return None
        return registration.binding.provider_id

    def execute_capability_invocation(
        self,
        context: AgentContext,
        prepared: Any,
        *,
        approval_id: str | None,
    ) -> Any:
        """Run a single non-batch Capability through the shared admission path."""

        call = CapabilityCall(
            prepared.call_id,
            prepared.capability_id,
            dict(prepared.arguments),
        )
        profile = self.describe_capability_execution(context, call)
        decision = self.capability_permission_decision(context, call)
        if decision in {"ASK", "DENY"}:
            profile = ExecutionProfile.exclusive(
                read=profile.resource_claims.read,
                write=profile.resource_claims.write,
            )
        outcome: list[Any] = []

        def commit(_: CapabilityCall | AgentCall, record: Any) -> str:
            if isinstance(record, Exception):
                raise record
            outcome.append(
                context.commit_capability_call(record, approval_id=approval_id)
            )
            return "continue"

        self.coordinator.execute(
            [call],
            [profile],
            prepare_call=lambda call: None,
            run_call=lambda _: context.execute_capability_call(
                prepared,
                approval_id=approval_id,
                preflighted=not self.capability_failure_blocked(
                    context,
                    call,
                    arguments=(
                        prepared.execution_arguments
                        if prepared.execution_arguments is not None
                        else prepared.arguments
                    ),
                ),
            ),
            commit_call=commit,
            suspend_call=lambda call, record: None,
            cancel_call=lambda call, reason: None,
            lane_for=lambda call: "capability",
            provider_for=lambda call: self.provider_id(call.capability_id),
            owner=context,
        )
        if len(outcome) != 1:
            raise ValidationError("single Capability execution did not commit one result")
        return outcome[0]

    def describe_agent_execution(
        self, context: AgentContext, call: AgentCall
    ) -> ExecutionProfile:
        spec = self.spec(call.agent_id)
        if spec.agent_depth != context.spec.agent_depth + 1:
            raise ValidationError("Agent call violates the configured parent-child depth")
        if spec.agent_depth > self.max_agent_depth:
            raise ValidationError("Agent call exceeds execution.max_agent_depth")
        description = spec.execution_profile
        try:
            profile = description(context, call) if callable(description) else description
        except Exception:  # noqa: BLE001 - classification failures must fail closed
            return ExecutionProfile.unknown(repository=context.repository)
        if (
            not isinstance(profile, ExecutionProfile)
            or profile.concurrency_mode == ConcurrencyMode.UNKNOWN
        ):
            return ExecutionProfile.unknown(repository=context.repository)
        return profile

    def resolve_llm_name(self, name: str, context: AgentContext) -> str:
        for capability in self.discover(context):
            if self._function_name(capability.id) == name:
                return capability.id
        return name

    def function_name(self, capability_id: str) -> str:
        return self._function_name(capability_id)

    @staticmethod
    def _function_name(capability_id: str) -> str:
        return "capability__" + capability_id.replace(".", "__").replace("-", "_")

    @staticmethod
    def invocation_context(
        context: AgentContext,
        *,
        approval_id: str | None = None,
        call_id: str | None = None,
    ) -> InvocationContext:
        workspace = context.coding_workspace
        return InvocationContext(
            run_id=context.run_id,
            session_id=context.session_id,
            agent_id=context.agent,
            repository=context.repository,
            approval_id=approval_id,
            call_id=call_id,
            workspace_root=str(workspace.root) if workspace is not None else None,
        )

    def is_bash_verification(self, context: AgentContext, command: str) -> bool:
        return self._capabilities.policy.is_bash_verification(
            command, self.invocation_context(context)
        )

    def spec(self, agent_name: str) -> AgentSpec:
        try:
            return self._specs[agent_name]
        except KeyError as exc:
            raise ValidationError(f"unknown agent: {agent_name}") from exc

    def close(self) -> None:
        self.coordinator.close()
