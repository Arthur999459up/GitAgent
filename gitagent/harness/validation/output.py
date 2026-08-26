"""Agent output-schema validation."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from gitagent.domain.errors import ValidationError
from gitagent.domain.models import AgentSpec


def validate_agent_output(spec: AgentSpec, result: Any) -> None:
    if not spec.output_schema:
        return
    if is_dataclass(result):
        keys = set(asdict(result))
    elif isinstance(result, dict):
        keys = set(result)
    else:
        raise ValidationError(f"agent {spec.name} returned non-structured output")
    missing = set(spec.output_schema) - keys
    if missing:
        raise ValidationError(f"agent {spec.name} omitted output fields: {', '.join(sorted(missing))}")
