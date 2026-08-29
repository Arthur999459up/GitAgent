from gitagent.harness.context.builder import _safe_history_unit
from gitagent.infra.persistence.sessions import TurnRecord


def _turn(*, assistant_text: str, route_summary: list[dict[str, object]]) -> TurnRecord:
    return TurnRecord(
        session_id="session-00000000000000000000000000000000",
        seq=1,
        status="completed",
        user_text="user text stays in durable history only",
        assistant_text=assistant_text,
        history_text="issues | goal=inspect issue | completed",
        route_summary=route_summary,
        entity_manifests=[],
        created_at="2026-08-29T00:00:00+00:00",
        completed_at="2026-08-29T00:00:01+00:00",
    )


def test_domain_turn_excludes_assistant_text_from_main_history_projection() -> None:
    unit = _safe_history_unit(
        _turn(
            assistant_text="domain transcript must not enter the Main Agent context budget",
            route_summary=[
                {
                    "route": "issues",
                    "session_goal": "inspect issue",
                    "resolved_references": [],
                    "workflow_type": "issues",
                    "workflow_status": "completed",
                }
            ],
        )
    )

    assert "assistant_text" not in unit
    assert unit["history_text"] == "issues | goal=inspect issue | completed"
    assert unit["route_summary"][0]["route"] == "issues"


def test_main_direct_turn_keeps_assistant_text_in_recent_history() -> None:
    unit = _safe_history_unit(
        _turn(
            assistant_text="direct Main Agent reply remains conversational history",
            route_summary=[],
        )
    )

    assert unit["assistant_text"] == "direct Main Agent reply remains conversational history"
