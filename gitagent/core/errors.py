"""Domain errors. Security-sensitive failures are explicit and fail closed."""


class GitAgentError(Exception):
    """Base class for expected GitAgent failures."""


class RoutingError(GitAgentError):
    """The request cannot be routed without guessing critical context."""


class PermissionDenied(GitAgentError):
    """An agent attempted a tool or access level outside its specification."""


class ApprovalRequired(PermissionDenied):
    """A mutation did not carry a valid, exact approval."""


class ToolExecutionError(GitAgentError):
    """An MCP tool failed."""


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
