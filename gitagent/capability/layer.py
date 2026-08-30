"""Capability discovery, authorization, invocation, recovery, and FailureGuard."""

from __future__ import annotations

import json
import time
import uuid
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

    @staticmethod
    def call_identity(agent_id: str, capability_id: str, arguments: dict[str, Any]) -> tuple[str, str]:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return agent_id, f"{capability_id}\n{canonical}"

    def blocked(
        self,
        run_id: str,
        identity: tuple[str, str],
    ) -> bool:
        return identity in self._failed_calls.get(run_id, {})

    def record(
        self,
        run_id: str,
        identity: tuple[str, str],
        error_type: CapabilityErrorType,
    ) -> None:
        self._failed_calls.setdefault(run_id, {})[identity] = error_type

    def clear(self, run_id: str, identity: tuple[str, str]) -> None:
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

    def add_provider(self, provider: Any) -> None:
        provider_id = str(provider.id)
        if provider_id in self._providers:
            raise ValidationError(f"duplicate capability provider: {provider_id}")
        self._providers[provider_id] = provider

    def load(self) -> None:
        snapshots = {
            provider_id: provider.load()
            for provider_id, provider in self._providers.items()
        }
        owners: dict[str, str] = {}
        for provider_id, registrations in snapshots.items():
            for source_id in {item.capability.source_id for item in registrations}:
                if source_id in owners:
                    raise ValidationError(f"multiple providers attempted to load source: {source_id}")
                owners[source_id] = provider_id
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
        providers = (
            self._providers.items()
            if provider_id is None
            else ((provider_id, self._providers[provider_id]),)
        )
        for current_provider_id, provider in providers:
            if hasattr(provider, "refresh"):
                provider.refresh()
            registrations = provider.load()
            current_sources = {item.capability.source_id for item in registrations}
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

    def invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> CapabilityResult:
        if not isinstance(arguments, dict):
            raise CapabilityInternalError("CapabilityLayer.invoke arguments must be a dict")
        call_id = f"call-{uuid.uuid4().hex}"
        identity = self.failure_guard.call_identity(context.agent_id, capability_id, arguments)
        self._emit(
            context,
            call_id,
            capability_id,
            "call.started",
            {"argument_keys": sorted(arguments), "arguments": arguments},
        )
        if self.failure_guard.blocked(context.run_id, identity):
            error = capability_error(
                CapabilityErrorType.REPEATED_FAILURE,
                "相同 Capability 和参数在本次运行中已经失败",
            )
            return self._finish_failure(context, call_id, capability_id, identity, error, attempts=0, record=False)

        registration = self.registry.resolve(capability_id)
        if registration is None:
            error = capability_error(CapabilityErrorType.CAPABILITY_NOT_FOUND, f"Capability 不存在：{capability_id}")
            return self._finish_failure(context, call_id, capability_id, identity, error, attempts=0)
        capability = registration.capability
        if capability.status != CapabilityStatus.AVAILABLE:
            error = capability_error(CapabilityErrorType.UNAVAILABLE, f"Capability 当前不可用：{capability_id}")
            return self._finish_failure(context, call_id, capability_id, identity, error, attempts=0)

        authorization = self.policy.authorize(capability, arguments, context)
        if authorization.decision == PermissionDecision.DENY:
            error = capability_error(CapabilityErrorType.PERMISSION_DENIED, authorization.reason)
            return self._finish_failure(context, call_id, capability_id, identity, error, attempts=0)
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

        try:
            if capability.input_schema is not None:
                validate_schema(arguments, capability.input_schema, label=f"{capability_id} arguments")
        except ValidationError as exc:
            error = capability_error(CapabilityErrorType.INVALID_INPUT, str(exc))
            return self._finish_failure(context, call_id, capability_id, identity, error, attempts=0)

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
                                identity,
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
                                identity,
                                missing,
                                attempts=attempts,
                            )
                        registration = updated
                    retry_after = (error.details or {}).get("retry_after")
                    if error.type == CapabilityErrorType.RATE_LIMITED and isinstance(retry_after, int | float):
                        time.sleep(float(retry_after))
                    continue
                return self._finish_failure(context, call_id, capability_id, identity, error, attempts=attempts)
            self._emit(context, call_id, capability_id, "attempt.succeeded", {"attempt": attempts})
            if attempts > 1:
                self._emit(context, call_id, capability_id, "recovery.succeeded", {"attempt": attempts})
            self.failure_guard.clear(context.run_id, identity)
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
        identity: tuple[str, str],
        error: CapabilityError,
        *,
        attempts: int,
        record: bool = True,
    ) -> CapabilityResult:
        if record:
            self.failure_guard.record(context.run_id, identity, error.type)
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
