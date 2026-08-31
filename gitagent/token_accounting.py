"""Deterministic token estimates for complete model-visible requests."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def estimate_tokens(value: str) -> int:
    """Estimate tokens using the project's fixed ceil(UTF-8 byte length / 3) rule."""

    return (len(value.encode("utf-8")) + 2) // 3


def request_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Estimate the exact messages/tools payload that will be model-visible."""

    payload: dict[str, Any] = {"messages": list(messages)}
    if tools:
        payload["tools"] = list(tools)
    return estimate_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


__all__ = ["estimate_tokens", "request_tokens"]
