"""Capability discovery, authorization, invocation, recovery, and FailureGuard."""

from __future__ import annotations

import json
import time
import uuid
from threading import Lock, RLock
from typing import Any

from gitagent.domain.errors import ValidationError

from .errors import (
    CapabilityError,
    CapabilityErrorType,
    CapabilityInternalError,
    capability_error,
    normalize_provider_error,
)
from .models import (
    AccessLevel,
    Capability,
    CapabilityResult,
    CapabilityStatus,
    InvocationContext,
)
from .policy import PermissionDecision, PermissionPolicy
from .registry import CapabilityRegistry
from .schema import validate_schema
from .trace import CapabilityTrace


class FailureGuard:
    """Block an identical failed call in one run using direct canonical-text comparison."""

    def __init__(self) -> None:
        self._failed_calls: dict[str, dict[tuple[str, str], CapabilityErrorType]] = {}
        self._lock = Lock()

    @staticmethod
    def call_identity(agent_id: str, capability_id: str, arguments: dict[str, Any]) -> tuple[str, str]:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return agent_id, f"{capability_id}\n{canonical}"

    def blocked(
        self,
        run_id: str,
        identity: tuple[str, str],
    ) -> bool:
        with self._lock:
            return identity in self._failed_calls.get(run_id, {})

    def record(
        self,
        run_id: str,
        identity: tuple[str, str],
        error_type: CapabilityErrorType,
    ) -> None:
        with self._lock:
            self._failed_calls.setdefault(run_id, {})[identity] = error_type

    def clear(self, run_id: str, identity: tuple[str, str]) -> None:
        with self._lock:
            failures = self._failed_calls.get(run_id)
            if failures is None:
                return
            failures.pop(identity, None)
            if not failures:
                self._failed_calls.pop(run_id, None)


class CapabilityLayer:
    def __init__(
        self,
        *,
        policy: PermissionPolicy,
        trace: CapabilityTrace | Any | None = None,
        registry: CapabilityRegistry | None = None,
        failure_guard: FailureGuard | None = None,
    ) -> None:
        self.policy = policy
        self.registry = registry or CapabilityRegistry()
        self.trace = trace if isinstance(trace, CapabilityTrace) else CapabilityTrace(trace)
        self.failure_guard = failure_guard or FailureGuard()
        self._providers: dict[str, Any] = {}
        self._provider_sources: dict[str, set[str]] = {}
        self._control_lock = RLock()

    def add_provider(self, provider: Any) -> None:
        provider_id = str(provider.id)
        with self._control_lock:
            if provider_id in self._providers:
                raise ValidationError(f"duplicate capability provider: {provider_id}")
            self._providers[provider_id] = provider

    def load(self) -> None:
        with self._control_lock:
            providers = tuple(self._providers.items())
        snapshots = {provider_id: provider.load() for provider_id, provider in providers}
        owners: dict[str, str] = {}
        for provider_id, registrations in snapshots.items():
            for source_id in {item.capability.source_id for item in registrations}:
                if source_id in owners:
                    raise ValidationError(f"multiple providers attempted to load source: {source_id}")
                owners[source_id] = provider_id
        with self._control_lock:
            for provider_id, registrations in snapshots.items():
                sources = {item.capability.source_id for item in registrations}
                for source_id in self._provider_sources.get(provider_id, set()) | sources:
                    self.registry.replace_source(
                        source_id,
                        [item for item in registrations if item.capability.source_id == source_id],
                    )
                self._provider_sources[provider_id] = sources
        self.policy.validate_capabilities(self.registry.list())

    def refresh(self, provider_id: str | None = None) -> None:
        with self._control_lock:
            providers = tuple(
                self._providers.items()
                if provider_id is None
                else ((provider_id, self._providers[provider_id]),)
            )
        for current_provider_id, provider in providers:
            if hasattr(provider, "refresh"):
                provider.refresh()
            registrations = provider.load()
            current_sources = {item.capability.source_id for item in registrations}
            with self._control_lock:
                other_sources = {
                    source
                    for owner, sources in self._provider_sources.items()
                    if owner != current_provider_id
                    for source in sources
                }
                overlap = current_sources & other_sources
                if overlap:
                    raise ValidationError(
                        f"multiple providers attempted to load source: {min(overlap)}"
                    )
                all_sources = self._provider_sources.get(current_provider_id, set()) | current_sources
                for source_id in all_sources:
                    self.registry.replace_source(
                        source_id,
                        [item for item in registrations if item.capability.source_id == source_id],
                    )
                self._provider_sources[current_provider_id] = current_sources
        self.policy.validate_capabilities(self.registry.list())

    def discover(self, context: InvocationContext) -> tuple[Capability, ...]:
        return tuple(
            capability
            for capability in self.registry.list()
            if capability.status == CapabilityStatus.AVAILABLE and self.policy.can_discover(capability, context)
        )

    def permission_decision(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> str:
        """Perform the no-side-effect permission portion of Coordinator preflight."""

        registration = self.registry.resolve(capability_id)
        if registration is None:
            return PermissionDecision.DENY.value
        capability = registration.capability
        if capability.status != CapabilityStatus.AVAILABLE:
            return PermissionDecision.DENY.value
        if capability.input_schema is not None:
            validate_schema(
                arguments,
                capability.input_schema,
                label=f"{capability_id} arguments",
            )
        return self.policy.authorize(capability, arguments, context).decision.value

    def describe_execution(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        """Ask the bound provider for per-invocation scheduling semantics."""

        from gitagent.harness.execution import ConcurrencyMode, ExecutionProfile

        registration = self.registry.resolve(capability_id)
        if registration is None:
            return ExecutionProfile.unknown(repository=context.repository)
        with self._control_lock:
            provider = self._providers.get(registration.binding.provider_id)
        describe = getattr(provider, "describe_execution", None)
        if not callable(describe):
            return ExecutionProfile.unknown(repository=context.repository)
        try:
            profile = describe(registration.binding, dict(arguments), context)
        except Exception:  # noqa: BLE001 - classification failures must fail closed
            return ExecutionProfile.unknown(repository=context.repository)
        if (
            not isinstance(profile, ExecutionProfile)
            or profile.concurrency_mode == ConcurrencyMode.UNKNOWN
        ):
            return ExecutionProfile.unknown(repository=context.repository)
        return profile

    def invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        context: InvocationContext,
        *,
        preflighted: bool = False,
    ) -> CapabilityResult:
        if not isinstance(arguments, dict):
            raise CapabilityInternalError("CapabilityLayer.invoke arguments must be a dict")
        call_id = context.call_id or f"call-{uuid.uuid4().hex}"
        identity = self.failure_guard.call_identity(context.agent_id, capability_id, arguments)
        self._emit(
            context,
            call_id,
            capability_id,
            "call.started",
            {"argument_keys": sorted(arguments), **_trace_arguments(capability_id, arguments)},
        )
        if not preflighted and self.failure_guard.blocked(context.run_id, identity):
            error = capability_error(
                CapabilityErrorType.REPEATED_FAILURE,
                "相同 Capability 和参数在本次运行中已经失败",
            )
            return self._finish_failure(
                context, call_id, capability_id, error, attempts=0
            )

        registration = self.registry.resolve(capability_id)
        if registration is None:
            error = capability_error(CapabilityErrorType.CAPABILITY_NOT_FOUND, f"Capability 不存在：{capability_id}")
            return self._finish_failure(context, call_id, capability_id, error, attempts=0)
        capability = registration.capability
        if capability.status != CapabilityStatus.AVAILABLE:
            unavailable_reason = str(
                getattr(registration.binding.target, "unavailable_reason", "") or ""
            ).strip()
            message = f"Capability 当前不可用：{capability_id}"
            if unavailable_reason:
                message += f"（{unavailable_reason}）"
            error = capability_error(CapabilityErrorType.UNAVAILABLE, message)
            return self._finish_failure(context, call_id, capability_id, error, attempts=0)

        try:
            if capability.input_schema is not None:
                validate_schema(arguments, capability.input_schema, label=f"{capability_id} arguments")
        except ValidationError as exc:
            error = capability_error(CapabilityErrorType.INVALID_INPUT, str(exc))
            return self._finish_failure(context, call_id, capability_id, error, attempts=0)

        authorization = self.policy.authorize(capability, arguments, context)
        if authorization.decision == PermissionDecision.DENY:
            error = capability_error(CapabilityErrorType.PERMISSION_DENIED, authorization.reason)
            return self._finish_failure(context, call_id, capability_id, error, attempts=0)
        if authorization.decision == PermissionDecision.ASK:
            result = CapabilityResult(capability_id, "approval_required", "none", attempts=0)
            self._emit(
                context,
                call_id,
                capability_id,
                "call.succeeded",
                {"status": result.status, "content": ""},
            )
            return result

        with self._control_lock:
            provider = self._providers.get(registration.binding.provider_id)
        if provider is None:
            raise CapabilityInternalError(f"provider is not loaded: {registration.binding.provider_id}")
        mutation = capability.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
        attempts = 0
        while attempts < 2:
            attempts += 1
            self._emit(context, call_id, capability_id, "attempt.started", {"attempt": attempts})
            try:
                raw = provider.invoke(registration.binding, dict(arguments), context)
            except CapabilityInternalError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider failures are normalized here
                error = normalize_provider_error(exc, mutation=mutation)
                self._emit(
                    context,
                    call_id,
                    capability_id,
                    "attempt.failed",
                    {"attempt": attempts, "error": error.type.value},
                )
                if attempts == 1 and self._recoverable(error, capability.access):
                    self._emit(context, call_id, capability_id, "recovery.started", {"error": error.type.value})
                    if error.type == CapabilityErrorType.UNAVAILABLE and hasattr(provider, "reconnect"):
                        try:
                            provider.reconnect(registration.binding)
                            self.refresh(registration.binding.provider_id)
                        except CapabilityInternalError:
                            raise
                        except Exception as recovery_exc:  # noqa: BLE001 - recovery failures cross the provider boundary
                            recovery_error = normalize_provider_error(recovery_exc, mutation=False)
                            return self._finish_failure(
                                context,
                                call_id,
                                capability_id,
                                recovery_error,
                                attempts=attempts,
                            )
                        updated = self.registry.resolve(capability_id)
                        if updated is None:
                            missing = capability_error(
                                CapabilityErrorType.CAPABILITY_NOT_FOUND,
                                f"Capability 在 reconnect 后已不存在：{capability_id}",
                            )
                            return self._finish_failure(
                                context,
                                call_id,
                                capability_id,
                                missing,
                                attempts=attempts,
                            )
                        registration = updated
                    retry_after = (error.details or {}).get("retry_after")
                    if error.type == CapabilityErrorType.RATE_LIMITED and isinstance(retry_after, int | float):
                        time.sleep(float(retry_after))
                    continue
                return self._finish_failure(
                    context, call_id, capability_id, error, attempts=attempts
                )
            self._emit(context, call_id, capability_id, "attempt.succeeded", {"attempt": attempts})
            try:
                if capability.output_schema is not None:
                    validate_schema(
                        raw,
                        capability.output_schema,
                        label=f"{capability_id} result",
                    )
            except ValidationError as exc:
                self._emit(
                    context,
                    call_id,
                    capability_id,
                    "output_validation.failed",
                    {
                        "attempt": attempts,
                        "provider_executed": True,
                        "side_effect_possible": mutation,
                        "error": str(exc),
                    },
                )
                error = capability_error(
                    CapabilityErrorType.INVALID_OUTPUT,
                    str(exc),
                    details={
                        "provider_executed": True,
                        "side_effect_possible": mutation,
                    },
                )
                return self._finish_failure(
                    context,
                    call_id,
                    capability_id,
                    error,
                    attempts=attempts,
                )
            if attempts > 1:
                self._emit(context, call_id, capability_id, "recovery.succeeded", {"attempt": attempts})
            result_type = {
                "native_tool": "data",
                "mcp_tool": "data",
                "skill": "context",
                "rag": "retrieval",
            }[capability.kind.value]
            result = CapabilityResult(capability_id, "success", result_type, raw, attempts=attempts)
            self._emit(
                context,
                call_id,
                capability_id,
                "call.succeeded",
                {
                    "attempts": attempts,
                    "status": "ok",
                    "content": _trace_content(capability_id, arguments, raw),
                },
            )
            return result
        raise CapabilityInternalError("capability recovery loop exceeded its invariant")

    def commit_failure_guard(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        context: InvocationContext,
        result: CapabilityResult,
    ) -> None:
        """Apply FailureGuard state only from the ordered commit path."""

        identity = self.failure_guard.call_identity(
            context.agent_id, capability_id, arguments
        )
        if result.status == "success":
            self.failure_guard.clear(context.run_id, identity)
            return
        error = result.error
        if (
            result.status == "failed"
            and error is not None
            and error.type
            not in {
                CapabilityErrorType.REPEATED_FAILURE,
                CapabilityErrorType.DUPLICATE_CALL,
            }
        ):
            self.failure_guard.record(context.run_id, identity, error.type)

    @staticmethod
    def _recoverable(error: CapabilityError, access: AccessLevel) -> bool:
        if access != AccessLevel.READ:
            return False
        if error.type in {CapabilityErrorType.TIMEOUT, CapabilityErrorType.UNAVAILABLE}:
            return True
        if error.type == CapabilityErrorType.RATE_LIMITED:
            retry_after = (error.details or {}).get("retry_after")
            return isinstance(retry_after, int | float) and 0 <= retry_after <= 2
        return False

    def _finish_failure(
        self,
        context: InvocationContext,
        call_id: str,
        capability_id: str,
        error: CapabilityError,
        *,
        attempts: int,
    ) -> CapabilityResult:
        result = CapabilityResult(capability_id, "failed", "none", error=error, attempts=attempts)
        self._emit(
            context,
            call_id,
            capability_id,
            "call.failed",
            {"attempts": attempts, "error": error.type.value},
        )
        return result

    def _emit(
        self,
        context: InvocationContext,
        call_id: str,
        capability_id: str,
        event: str,
        details: dict[str, Any],
    ) -> None:
        self.trace.emit(
            run_id=context.run_id,
            call_id=call_id,
            capability_id=capability_id,
            event=event,
            details={"agent": context.agent_id, **details},
            session_id=context.session_id,
        )


def _trace_content(
    capability_id: str, arguments: dict[str, Any], content: Any
) -> Any:
    if capability_id.startswith("rag."):
        value = content if isinstance(content, dict) else {}
        hits = value.get("hits") if isinstance(value.get("hits"), list) else []
        return {
            "knowledge_base": value.get("knowledge_base") or capability_id.split(".", 1)[1],
            "query_characters": len(str(arguments.get("query") or "")),
            "status": "STALE" if value.get("stale") else "READY",
            "stale": bool(value.get("stale")),
            "hit_count": len(hits),
            "hits": [
                {
                    "document_id": hit.get("document_id"),
                    "section_id": hit.get("section_id"),
                    "chunk_id": hit.get("chunk_id"),
                }
                for hit in hits
                if isinstance(hit, dict)
            ],
            "elapsed_ms": value.get("elapsed_ms"),
        }
    if capability_id == "native.read" and arguments.get("root") in {
        "private_memory",
        "project_memory",
    }:
        return {
            "memory_page_loaded": True,
            "root": arguments.get("root"),
            "path": arguments.get("path"),
        }
    return content


def _trace_arguments(capability_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if capability_id.startswith("rag."):
        query = str(arguments.get("query") or "")
        return {"query_characters": len(query)}
    return {"arguments": arguments}
