"""Session context selection and deterministic compaction."""

from .budget import (
    EMERGENCY_THRESHOLD,
    LIGHT_THRESHOLD,
    SUMMARY_THRESHOLD,
    context_pressure,
    estimate_tokens,
)
from .builder import (
    CompactResult,
    ContextBudgetExceeded,
    ContextBuilder,
    ContextBuildError,
    MessageCompactionPlan,
    fit_messages,
    fit_messages_with_plan,
)
from .capability_history import (
    capability_attempted,
    capability_failure_observed,
    find_capability_observation,
)
from .messages import (
    assistant_tool_call,
    canonical_message,
    request_tokens,
    tool_result_message,
)
from .projector import derive_domain_messages, derive_main_messages

__all__ = [
    "EMERGENCY_THRESHOLD",
    "LIGHT_THRESHOLD",
    "SUMMARY_THRESHOLD",
    "CompactResult",
    "ContextBudgetExceeded",
    "ContextBuildError",
    "ContextBuilder",
    "MessageCompactionPlan",
    "assistant_tool_call",
    "canonical_message",
    "capability_attempted",
    "capability_failure_observed",
    "context_pressure",
    "derive_domain_messages",
    "derive_main_messages",
    "estimate_tokens",
    "find_capability_observation",
    "fit_messages",
    "fit_messages_with_plan",
    "request_tokens",
    "tool_result_message",
]
