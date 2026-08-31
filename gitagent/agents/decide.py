"""The thin, shared decision boundary used by every autonomous Domain Agent."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop import AgentAction, AgentActionKind
from gitagent.domain.errors import ValidationError, WorkflowError
from gitagent.model import Reasoner

_BASE_ACTION_KINDS = (
    AgentActionKind.CAPABILITY.value,
    AgentActionKind.ASK.value,
    AgentActionKind.FINISH.value,
)


def action_schema(*protected_kinds: AgentActionKind) -> dict[str, Any]:
    """Describe the small action space visible to one Domain Agent."""

    return {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    *_BASE_ACTION_KINDS,
                    *(kind.value for kind in protected_kinds),
                ],
            },
            "summary": {
                "type": "string",
                "description": "One-line user-facing explanation of the next action.",
            },
            "capability_id": {
                "type": "string",
                "description": "The discovered capability ID when kind is capability.",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments for a capability or allowed protected action.",
            },
            "question": {
                "type": "string",
                "description": "The question to ask the user when kind is ask.",
            },
            "message": {
                "type": "string",
                "description": "The complete user-facing answer when kind is finish.",
            },
        },
        "required": ["kind", "summary"],
        "additionalProperties": False,
    }


def decide_action(
    context: Any,
    harness: Any,
    reasoner: Reasoner | None,
    *,
    protected_kinds: tuple[AgentActionKind, ...] = (),
) -> AgentAction:
    """Let the model choose exactly one currently authorized tool or action."""

    if reasoner is None:
        raise WorkflowError("autonomous Domain Agent decision requires a reasoner")
    value = context.reason_structured(
        reasoner,
        schema=action_schema(*protected_kinds),
        tool_name="decide_action",
        tools=harness.llm_tools(context),
    )
    context.record_model_response(value, tool_name="decide_action")
    if value.get("kind") == AgentActionKind.CAPABILITY.value:
        value["capability_id"] = harness.resolve_llm_name(
            str(value.get("capability_id", "")), context
        )
    return parse_action(value, allowed_protected=frozenset(protected_kinds))


def parse_action(
    value: Any,
    *,
    allowed_protected: frozenset[AgentActionKind] = frozenset(),
) -> AgentAction:
    """Validate and normalize one decided action, or raise a clean error."""

    if not isinstance(value, dict):
        raise ValidationError("agent decide returned a non-object value")
    try:
        kind = AgentActionKind(str(value.get("kind", "")).strip())
    except ValueError as exc:
        raise ValidationError("agent decide returned an invalid action kind") from exc
    if kind not in {
        AgentActionKind.CAPABILITY,
        AgentActionKind.ASK,
        AgentActionKind.FINISH,
        *allowed_protected,
    }:
        raise ValidationError(f"{kind.value} is not allowed for this agent")
    action = AgentAction(kind=kind, summary=str(value.get("summary", ""))[:500])
    if kind == AgentActionKind.CAPABILITY:
        action.capability_id = str(value.get("capability_id", "")).strip()
        if not action.capability_id:
            raise ValidationError("capability action requires a capability ID")
    if kind == AgentActionKind.CAPABILITY or kind in allowed_protected:
        arguments = value.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValidationError("action arguments must be an object")
        action.arguments = dict(arguments or {})
    if kind == AgentActionKind.ASK:
        action.question = str(value.get("question", "")).strip()
        if not action.question:
            raise ValidationError("ask action requires a question")
    elif kind == AgentActionKind.FINISH:
        action.message = str(value.get("message") or action.summary).strip()
    return action
