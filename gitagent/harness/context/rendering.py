"""Render an AgentContext's observations under the Harness context budget."""

from __future__ import annotations

from typing import Any

from .observations import render_agent_observations


def render_context_observations(context: Any) -> str:
    return render_agent_observations(
        context.observations,
        file_coverage=context.file_reads.summaries(),
        effective_input_budget=context.context_budget,
        prompt_overhead=context.prompt_overhead(),
    )
