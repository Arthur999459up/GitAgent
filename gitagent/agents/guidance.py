"""Compatibility helper for prompts after guidance became ephemeral."""

from __future__ import annotations

from gitagent.domain.models import AgentGuidance


def guidance_section(guidance: AgentGuidance | None) -> str:
    """Guidance is injected ephemerally by AgentContext, never into a durable prompt."""

    del guidance
    return ""
