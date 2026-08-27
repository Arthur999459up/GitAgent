"""Structured capability failures and provider error normalization."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any

from gitagent.domain.errors import (
    ExternalExecutionError,
    PermissionDenied,
    ResourceNotFoundError,
    ValidationError,
)


class CapabilityErrorType(str, Enum):
    CAPABILITY_NOT_FOUND = "capability_not_found"
    PERMISSION_DENIED = "permission_denied"
    INVALID_INPUT = "invalid_input"
    RESOURCE_NOT_FOUND = "resource_not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_UNCERTAIN = "execution_uncertain"
    REPEATED_FAILURE = "repeated_failure"


@dataclass(frozen=True)
class CapabilityError:
    type: CapabilityErrorType
    message: str
    details: dict[str, Any] | None = None


class CapabilityInternalError(RuntimeError):
    """Programming errors and broken Capability Layer invariants."""


class ProviderUnavailableError(Exception):
    pass


class ProviderTimeoutError(Exception):
    def __init__(self, message: str, *, request_sent: bool = False) -> None:
        super().__init__(message)
        self.request_sent = request_sent


class ProviderRateLimitError(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderAuthenticationError(Exception):
    pass


class ProviderConflictError(Exception):
    pass


class ProviderExecutionError(Exception):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


def capability_error(
    error_type: CapabilityErrorType,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> CapabilityError:
    return CapabilityError(error_type, message, details)


def normalize_provider_error(exc: Exception, *, mutation: bool) -> CapabilityError:
    if isinstance(exc, ProviderTimeoutError):
        error_type = (
            CapabilityErrorType.EXECUTION_UNCERTAIN
            if mutation and exc.request_sent
            else CapabilityErrorType.TIMEOUT
        )
        return capability_error(error_type, str(exc))
    if isinstance(exc, TimeoutError | subprocess.TimeoutExpired):
        error_type = CapabilityErrorType.EXECUTION_UNCERTAIN if mutation else CapabilityErrorType.TIMEOUT
        return capability_error(error_type, str(exc))
    if isinstance(exc, ProviderRateLimitError):
        details = {"retry_after": exc.retry_after} if exc.retry_after is not None else None
        return capability_error(CapabilityErrorType.RATE_LIMITED, str(exc), details=details)
    if isinstance(exc, ProviderAuthenticationError):
        return capability_error(CapabilityErrorType.AUTHENTICATION_FAILED, str(exc))
    if isinstance(exc, ProviderUnavailableError | ConnectionError):
        error_type = CapabilityErrorType.EXECUTION_UNCERTAIN if mutation else CapabilityErrorType.UNAVAILABLE
        return capability_error(error_type, str(exc))
    if isinstance(exc, ResourceNotFoundError | FileNotFoundError):
        return capability_error(CapabilityErrorType.RESOURCE_NOT_FOUND, str(exc))
    if isinstance(exc, ProviderConflictError):
        return capability_error(CapabilityErrorType.CONFLICT, str(exc))
    if isinstance(exc, PermissionDenied | PermissionError):
        return capability_error(CapabilityErrorType.PERMISSION_DENIED, str(exc))
    if isinstance(exc, ValidationError | ValueError | TypeError):
        return capability_error(CapabilityErrorType.INVALID_INPUT, str(exc))
    if isinstance(exc, subprocess.CalledProcessError):
        details = {
            "exit_code": exc.returncode,
            "stdout_tail": str(exc.stdout or "")[-4000:],
            "stderr_tail": str(exc.stderr or "")[-4000:],
        }
        return capability_error(CapabilityErrorType.EXECUTION_FAILED, str(exc), details=details)
    if isinstance(exc, ProviderExecutionError):
        return capability_error(CapabilityErrorType.EXECUTION_FAILED, str(exc), details=exc.details)
    if isinstance(exc, ExternalExecutionError | OSError):
        return capability_error(CapabilityErrorType.EXECUTION_FAILED, str(exc))
    return capability_error(CapabilityErrorType.EXECUTION_FAILED, str(exc))
