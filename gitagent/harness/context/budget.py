"""Shared context-window accounting and compression thresholds."""

from __future__ import annotations

LIGHT_THRESHOLD = 0.50
SUMMARY_THRESHOLD = 0.70
EMERGENCY_THRESHOLD = 0.90


def estimate_tokens(value: str) -> int:
    """Estimate tokens using the project's fixed ceil(UTF-8 byte length / 3) rule."""

    return (len(value.encode("utf-8")) + 2) // 3


def context_pressure(tokens: int, effective_input_budget: int) -> float:
    if effective_input_budget < 1:
        raise ValueError("effective_input_budget must be positive")
    return max(0, tokens) / effective_input_budget
