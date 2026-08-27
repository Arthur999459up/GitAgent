"""Pure queries over capability observations recorded in an Agent context."""

from __future__ import annotations

from typing import Any


def find_capability_observation(
    context: Any,
    capability_id: str,
    *,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the latest matching success or failure observation."""

    for observation in reversed(context.observations):
        if observation.get("kind") not in {"capability", "capability_error"}:
            continue
        payload = observation.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("capability_id") != capability_id:
            continue
        if arguments is not None and payload.get("arguments") != arguments:
            continue
        return observation
    return None


def capability_attempted(
    context: Any,
    capability_id: str,
    *,
    arguments: dict[str, Any] | None = None,
) -> bool:
    """Whether the matching capability call has already produced an observation."""

    return find_capability_observation(context, capability_id, arguments=arguments) is not None


def capability_failure_observed(context: Any) -> bool:
    """Whether this agent run has observed at least one capability failure."""

    return any(observation.get("kind") == "capability_error" for observation in context.observations)


__all__ = [
    "capability_attempted",
    "capability_failure_observed",
    "find_capability_observation",
]
