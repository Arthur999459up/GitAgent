"""MainAgent decision validation and Session safety boundaries."""

import pytest
from AGENT.GitAgent.gitagent.core.errors import ValidationError
from AGENT.GitAgent.tests.support import StubMainReasoner, build_test_service, handle


def test_model_cannot_route_to_unknown_agent():
    reasoner = StubMainReasoner(
        [
            {
                "target_agent": "unknown_agent",
                "entity_type": "",
                "entity_id": "",
                "request": "继续",
                "message": "",
                "clarify": False,
                "requested_fix": False,
                "requested_reply": False,
            }
        ]
    )
    service = build_test_service(main_reasoner=reasoner)
    with pytest.raises(ValidationError, match="unknown domain agent"):
        handle(service, "帮我处理一个新的仓库事项")


def test_model_cannot_silently_switch_repository():
    reasoner = StubMainReasoner(
        [
            {
                "target_agent": "repo_qa",
                "entity_type": "repository",
                "entity_id": "",
                "request": "find format_name",
                "message": "",
                "clarify": False,
                "requested_fix": False,
                "requested_reply": False,
                "repository": "evil/other",
            }
        ]
    )
    service = build_test_service(main_reasoner=reasoner)
    result = handle(service, "format_name 在哪里？")
    assert result.agent == "repo_qa"
    assert "src/formatting.py" in result.output.answer
    tool_events = [event for event in service.harness.audit.events() if event.result == "OK"]
    assert all(event.details.get("repository") != "evil/other" for event in tool_events)


def test_direct_answer_never_creates_child_context():
    reasoner = StubMainReasoner(
        [
            {
                "target_agent": "",
                "entity_type": "",
                "entity_id": "",
                "request": "thanks",
                "message": "不客气。",
                "clarify": False,
                "requested_fix": False,
                "requested_reply": False,
            }
        ]
    )
    service = build_test_service(main_reasoner=reasoner)
    result = handle(service, "谢谢")
    assert result.agent is None
    assert result.output == "不客气。"
    session = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert session is not None and session.agent_context == {}


def test_main_agent_has_no_mcp_tool_capability():
    service = build_test_service()
    spec = service.harness.spec("main")
    assert spec.allowed_tools == frozenset()
