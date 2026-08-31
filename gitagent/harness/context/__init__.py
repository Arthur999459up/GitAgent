"""Session context selection and deterministic compaction."""

from gitagent.token_accounting import estimate_tokens

from .budget import (
    EMERGENCY_THRESHOLD,
    LIGHT_THRESHOLD,
    SUMMARY_THRESHOLD,
    context_pressure,
)
from .builder import (
    CompactResult,
    ContextBuilder,
    ContextBuildError,
    MessageCompactionPlan,
    fit_messages,
    fit_messages_with_plan,
)
from .messages import (
    assistant_tool_call,
    canonical_message,
    request_tokens,
    tool_result_message,
)
from .projector import (
    correlate_tool_results,
    derive_domain_messages,
    derive_main_messages,
)

__all__ = [
    "EMERGENCY_THRESHOLD",
    "LIGHT_THRESHOLD",
    "SUMMARY_THRESHOLD",
    "CompactResult",
    "ContextBuildError",
    "ContextBuilder",
    "MessageCompactionPlan",
    "assistant_tool_call",
    "canonical_message",
    "context_pressure",
    "correlate_tool_results",
    "derive_domain_messages",
    "derive_main_messages",
    "estimate_tokens",
    "fit_messages",
    "fit_messages_with_plan",
    "request_tokens",
    "tool_result_message",
]
