"""Errors raised by the pure GitHub HTTP adapter."""

from __future__ import annotations

from gitagent.domain.errors import ExternalExecutionError


class GitHubAPIError(ExternalExecutionError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: float | None = None,
        request_sent: bool = True,
        user_message: str | None = None,
    ) -> None:
        super().__init__(message, user_message=user_message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.request_sent = request_sent


class GitHubTransportError(ExternalExecutionError):
    def __init__(self, message: str, *, timed_out: bool, request_sent: bool = True) -> None:
        super().__init__(message)
        self.timed_out = timed_out
        self.transport_unavailable = not timed_out
        self.request_sent = request_sent
