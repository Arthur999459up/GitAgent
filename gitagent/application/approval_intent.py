"""Natural-language intent classification for an already-bound approval proposal."""

from __future__ import annotations

import json
from typing import Any

from gitagent.domain.errors import LLMProviderError, ValidationError
from gitagent.domain.models import ApprovalIntent, WorkflowTurnDecision
from gitagent.harness.context import compact_messages
from gitagent.model import Reasoner, structured_tools
from gitagent.prompts import get_prompt_library

_PROMPTS = get_prompt_library()
_APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["approve", "reject", "revise", "question", "ambiguous"],
        },
        "instruction": {
            "type": "string",
            "description": "For revise: the cleaned concrete revision instruction without approval wording.",
        },
        "message": {
            "type": "string",
            "description": "For ambiguous: one concise clarifying question; otherwise a short acknowledgement.",
        },
    },
    "required": ["action"],
}
_ACTIONS = {
    "approve": ApprovalIntent.APPROVE,
    "reject": ApprovalIntent.REJECT,
    "revise": ApprovalIntent.REVISE,
    "question": ApprovalIntent.QUESTION,
    "ambiguous": ApprovalIntent.AMBIGUOUS,
}


def _ambiguous_decision() -> WorkflowTurnDecision:
    return WorkflowTurnDecision(
        action=ApprovalIntent.AMBIGUOUS,
        message="你是想批准这个方案、继续修改，还是想了解方案内容？请直接说，例如「可以」或「把描述改短一点」。",
    )


class ApprovalIntentClassifier:
    """Classify user meaning only; the deterministic ApprovalStore grants authority."""

    def __init__(
        self, reasoner: Reasoner | None, *, context_window_tokens: int
    ) -> None:
        self.reasoner = reasoner
        self.context_window_tokens = context_window_tokens

    def classify(self, *, user_input: str, proposal_context: dict[str, Any]) -> WorkflowTurnDecision:
        if self.reasoner is None:
            return _ambiguous_decision()
        prompt = _PROMPTS.render(
            "approval.input",
            user_input=user_input,
            proposal_context=json.dumps(proposal_context, ensure_ascii=False),
        )
        messages = [
            {"role": "system", "content": _PROMPTS.text("approval.system")},
            {"role": "user", "content": prompt},
        ]
        final_tools = structured_tools(
            "classify_approval_intent", _APPROVAL_SCHEMA
        )
        messages = compact_messages(
            messages,
            final_tools,
            context_window_tokens=self.context_window_tokens,
        ).messages
        try:
            value = self.reasoner.complete_structured_messages(
                messages=messages,
                schema=_APPROVAL_SCHEMA,
                tool_name="classify_approval_intent",
                final_tools=final_tools,
                context_window_tokens=self.context_window_tokens,
            )
        except (LLMProviderError, ValidationError):
            return _ambiguous_decision()
        if not isinstance(value, dict):
            return _ambiguous_decision()
        action = _ACTIONS.get(str(value.get("action", "")).casefold(), ApprovalIntent.AMBIGUOUS)
        return WorkflowTurnDecision(
            action=action,
            instruction=str(value.get("instruction") or "").strip(),
            message=str(value.get("message") or "").strip(),
        )
