"""Shared structured-action schema and parsing for autonomous agent decide steps."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop import AgentAction, AgentActionKind
from gitagent.domain.errors import ValidationError

AGENT_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["apply_issue_fix", "ask", "finish"],
        },
        "summary": {"type": "string", "description": "One-line user-facing explanation of the next action."},
        "capability_id": {
            "type": "string",
            "description": "The discovered capability ID when kind is capability.",
        },
        "arguments": {"type": "object", "description": "Capability arguments when kind is capability."},
        "question": {"type": "string", "description": "The question to ask the user when kind is ask."},
        "message": {"type": "string", "description": "One-line result message when kind is finish."},
    },
    "required": ["kind", "summary"],
}


def parse_action(value: Any, *, requires_candidate: bool) -> AgentAction:
    """Validate and normalize one decided action, or raise a clean error."""
    if not isinstance(value, dict):
        raise ValidationError("agent decide returned a non-object value")
    try:
        kind = AgentActionKind(str(value.get("kind", "")).strip())
    except ValueError as exc:
        raise ValidationError("agent decide returned an invalid action kind") from exc
    action = AgentAction(kind=kind, summary=str(value.get("summary", ""))[:500])
    if kind == AgentActionKind.CAPABILITY:
        action.capability_id = str(value.get("capability_id", "")).strip()
        if not action.capability_id:
            raise ValidationError("capability action requires a capability ID")
        arguments = value.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValidationError("capability arguments must be an object")
        action.arguments = dict(arguments or {})
    elif kind == AgentActionKind.ASK:
        action.question = str(value.get("question", "")).strip()
        if not action.question:
            raise ValidationError("ask action requires a question")
    elif kind == AgentActionKind.APPLY_ISSUE_FIX:
        if not requires_candidate:
            raise ValidationError("apply_issue_fix is not allowed for this agent")
    elif kind == AgentActionKind.FINISH:
        action.message = str(value.get("message") or action.summary).strip()
    return action
