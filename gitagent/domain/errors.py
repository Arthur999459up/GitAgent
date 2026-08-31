"""Domain errors. Security-sensitive failures are explicit and fail closed."""


class GitAgentError(Exception):
    """Base class for expected GitAgent failures."""


class RoutingError(GitAgentError):
    """The request cannot be routed without guessing critical context."""


class PermissionDenied(GitAgentError):
    """An operation crossed an explicit permission boundary."""


class ApprovalRequired(PermissionDenied):
    """A mutation did not carry a valid, exact approval."""


class ExternalExecutionError(GitAgentError):
    """A pure infrastructure adapter or approved external operation failed."""

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class ResourceNotFoundError(ExternalExecutionError):
    """A remote lookup could not find the requested resource."""


class LLMProviderError(GitAgentError):
    """The configured model provider request failed before a valid response was produced."""


class ContextWindowExceeded(GitAgentError):
    """The complete model-visible request leaves no valid output space."""

    def __init__(
        self,
        *,
        context_window_tokens: int,
        input_tokens: int,
        requested_output_tokens: int,
        breakdown: dict[str, int] | None = None,
    ) -> None:
        self.context_window_tokens = context_window_tokens
        self.input_tokens = input_tokens
        self.requested_output_tokens = requested_output_tokens
        self.remaining_tokens = context_window_tokens - input_tokens
        self.breakdown = dict(breakdown or {})
        super().__init__(
            "ContextWindowExceeded: "
            f"context_window_tokens={context_window_tokens}, "
            f"input_tokens={input_tokens}, "
            f"requested_output_tokens={requested_output_tokens}, "
            f"remaining_tokens={self.remaining_tokens}"
        )


class ValidationError(GitAgentError):
    """Structured input or output did not match its contract."""


class StructuredOutputError(ValidationError):
    """Model structured output was malformed or violated its declared schema."""

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details


class WorkflowError(GitAgentError):
    """A workflow cannot make a valid state transition."""


class StateError(GitAgentError):
    """Persistent Session or Memory state failed closed."""
