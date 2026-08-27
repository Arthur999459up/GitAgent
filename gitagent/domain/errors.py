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


class ValidationError(GitAgentError):
    """Structured input or output did not match its contract."""


class StructuredOutputError(ValidationError):
    """Model structured output was malformed or violated its declared schema."""


class WorkflowError(GitAgentError):
    """A workflow cannot make a valid state transition."""


class StateError(GitAgentError):
    """Persistent Session or Memory state failed closed."""
