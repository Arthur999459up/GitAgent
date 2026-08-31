"""Shared context-window accounting and compression thresholds."""

from __future__ import annotations

LIGHT_THRESHOLD = 0.50
SUMMARY_THRESHOLD = 0.70
EMERGENCY_THRESHOLD = 0.90


def context_pressure(tokens: int, context_window_tokens: int) -> float:
    if context_window_tokens < 1:
        raise ValueError("context_window_tokens must be positive")
    return max(0, tokens) / context_window_tokens
