"""Rendering for bounded, non-authoritative agent guidance."""

from __future__ import annotations

import json

from gitagent.domain.models import AgentGuidance, to_plain
from gitagent.prompts import get_prompt_library

_PROMPTS = get_prompt_library()


def guidance_section(guidance: AgentGuidance | None) -> str:
    if guidance is None or guidance.empty:
        return ""
    payload = {
        "memory_index": guidance.memory_index,
        "resolved_references": [
            to_plain(item) for item in guidance.resolved_references
        ],
    }
    return "\n\n" + _PROMPTS.render(
        "agents.guidance_section", payload=json.dumps(payload, ensure_ascii=False)
    )
