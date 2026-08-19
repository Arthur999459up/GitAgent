"""Persistence/restart behavior around the Session-scoped service boundary."""

from AGENT.GitAgent.gitagent.app.service import GitAgentService
from AGENT.GitAgent.gitagent.core.models import DraftResult
from AGENT.GitAgent.tests.support import StubMainReasoner, build_test_service, routing_context


def _restart(service):
    restarted = GitAgentService(
        service.harness.server,
        main_reasoner=StubMainReasoner(),
        session_manager=service._test_sessions,
        session_scope=service.session_scope,
    )
    restarted._test_sessions = service._test_sessions
    restarted._test_store = service._test_store
    restarted._test_context_builder = service._test_context_builder
    return restarted


def _handle(service, text):
    return service.handle(
        text,
        repository="sample/widgets",
        routing_context=routing_context(service, text),
        session_scope=service.session_scope,
    )


def test_service_restart_preserves_issue_draft_context_and_can_continue():
    service = build_test_service()
    first = _handle(service, "处理 Issue #1，先给我回复草稿")
    assert isinstance(first.output, DraftResult)

    restarted = _restart(service)
    continued = _handle(restarted, "再短一点")

    assert isinstance(continued.output, DraftResult)
    session = restarted._test_sessions.get_session(
        restarted.session_scope.account_key,
        restarted.session_scope.repository_key,
        restarted.session_scope.session_id,
    )
    assert session is not None
    assert session.agent_context["agent"] == "issues"
    assert session.agent_context["reply_draft"] == continued.output.body


def test_runtime_approval_id_is_rebuilt_after_restart_before_explicit_execution():
    service = build_test_service()
    draft = _handle(service, "处理 Issue #1，先给我回复草稿").output.body
    proposal = _handle(service, "发布吧")
    old_approval_id = proposal.output.pending.approval_id
    repo = service.harness.server.repositories["sample/widgets"]
    before = len(repo.get("comments", []))

    restarted = _restart(service)
    restored = restarted._load_context()
    assert restored is not None and restored.pending is not None
    assert restored.pending.approval_id != old_approval_id
    assert len(repo.get("comments", [])) == before

    completed = _handle(restarted, "可以")
    assert completed.agent == "issues"
    assert len(repo["comments"]) == before + 1
    assert repo["comments"][-1]["body"] == draft


def test_session_working_state_contains_main_context_not_task_lifecycle():
    service = build_test_service()
    session = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert session is not None
    assert session.working_state == {
        "version": 4,
        "goal": "",
        "focus": None,
        "manifests": [],
        "open_question": "",
    }
    assert session.agent_context == {}
