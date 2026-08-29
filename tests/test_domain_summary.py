import json

from gitagent.agent_loop.actions import PendingAction
from gitagent.application.projection import domain_summary, project_service_result
from gitagent.application.service import ServiceResult
from gitagent.domain.models import AgentSpec, MainDecision, PlannedCapabilityCall
from gitagent.harness.context.state import AgentContext


class _Harness:
    context_budget = 8000
    message_sink = None

    @staticmethod
    def function_name(capability_id: str) -> str:
        return capability_id


def _context() -> AgentContext:
    return AgentContext(
        _Harness(),
        AgentSpec("issues", "role", "system", (), frozenset()),
        "session-" + "c" * 32,
        repository="owner/repo",
        goal="update issue 7",
        entity_type="issue",
        entity_id="7",
    )


def test_turn_projection_keeps_full_main_assistant_text() -> None:
    text = "a" * 20_000

    projection = project_service_result(
        ServiceResult(MainDecision(), text),
        turn_seq=1,
        text_sanitizer=lambda value: value,
    )

    assert projection.assistant_text == text


def test_domain_summary_covers_completed_mutation_pending_input_and_failure() -> None:
    completed = _context()
    completed.observations.append(
        {
            "kind": "capability",
            "payload": {
                "capability_id": "github.update_issue",
                "data": {"number": 7, "state": "closed"},
            },
        }
    )
    payload = json.loads(
        domain_summary(
            agent="issues",
            goal=completed.goal,
            output="Issue updated",
            entity_type="issue",
            entity_id="7",
            context=completed,
        )
    )
    assert payload["status"] == "completed"
    assert payload["mutation_executed"] is True
    assert payload["key_references"] == ["issue:7"]

    pending = _context()
    pending.pending = PendingAction(
        "approval-1",
        "Close Issue #7",
        [PlannedCapabilityCall("github.update_issue", {"issue_number": 7})],
    )
    payload = json.loads(
        domain_summary(agent="issues", goal=pending.goal, output=pending, context=pending)
    )
    assert payload["status"] == "awaiting_approval"
    assert "Close Issue #7" in payload["pending_confirmation"]
    assert payload["mutation_executed"] is False

    awaiting_input = _context()
    awaiting_input.question = "Which label?"
    payload = json.loads(
        domain_summary(
            agent="issues",
            goal=awaiting_input.goal,
            output=awaiting_input,
            context=awaiting_input,
        )
    )
    assert payload["status"] == "awaiting_input"
    assert payload["unfinished_or_next"] == "Which label?"

    failed = _context()
    failed.error = "provider unavailable"
    payload = json.loads(
        domain_summary(agent="issues", goal=failed.goal, output=failed, context=failed)
    )
    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "provider unavailable"
