"""Persistence/restart behavior around the Session-scoped service boundary."""

from types import SimpleNamespace

import pytest
from AGENT.GitAgent.gitagent.app.config import CLIConfig
from AGENT.GitAgent.gitagent.app.factory import LiveApplication
from AGENT.GitAgent.gitagent.app.service import GitAgentService
from AGENT.GitAgent.gitagent.context import ContextBuilder
from AGENT.GitAgent.gitagent.core.errors import RoutingError, StateError
from AGENT.GitAgent.gitagent.core.models import DraftResult
from AGENT.GitAgent.gitagent.core.trace import TraceBus
from AGENT.GitAgent.gitagent.mcp.memory import InMemoryMCPServer
from AGENT.GitAgent.gitagent.state import (
    SessionManager,
    StateStore,
    build_account_key,
    build_repository_key,
)
from AGENT.GitAgent.tests.support import (
    StubMainReasoner,
    build_test_service,
    routing_context,
    sample_repositories,
)


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


def test_application_resume_rebuilds_exact_session_service_and_loads_scoped_memory(tmp_path):
    api_url = "https://api.github.test"
    store = StateStore(tmp_path / "state.db")
    sessions = SessionManager(store)
    account = build_account_key(api_url, 7)
    repository = build_repository_key(api_url, 11)
    target = sessions.create_session(account, repository, "sample/widgets")
    sibling = sessions.create_session(account, repository, "sample/widgets")
    sessions.save_agent_context(
        target.scope,
        {
            "agent": "issues",
            "repository": "sample/widgets",
            "goal": "继续处理 Issue #1",
        },
    )
    memory, _ = sessions.remember(
        account,
        repository,
        scope="repository",
        kind="constraint",
        content="恢复后仍需加载的仓库约束",
    )
    reasoner = StubMainReasoner()
    server = InMemoryMCPServer(sample_repositories())
    trace = TraceBus()

    def service_factory(scope):
        return GitAgentService(
            server,
            main_reasoner=reasoner,
            session_manager=sessions,
            trace=trace,
            session_scope=scope,
        )

    stateless_service = service_factory(None)
    application = LiveApplication(
        config=CLIConfig(github_api_url=api_url),
        github=server,
        llm=SimpleNamespace(),
        reasoner=reasoner,
        trace=trace,
        service=stateless_service,
        store=store,
        sessions=sessions,
        context_builder=ContextBuilder(sessions),
        _service_factory=service_factory,
    )

    resumed = application.resume_session(7, target.session_id)
    restored_agent = application.service._load_context()
    assert restored_agent is not None and restored_agent.guidance is not None
    assert [item.memory_id for item in restored_agent.guidance.repository_memories] == [memory.memory_id]

    sessions.forget(account, repository, memory.memory_id)
    without_forgotten_memory = application.service._load_context()
    assert without_forgotten_memory is not None and without_forgotten_memory.guidance is None
    current_memory, _ = sessions.remember(
        account,
        repository,
        scope="repository",
        kind="constraint",
        content="更新后的仓库约束",
    )
    restored_routing = application.context_builder.build(target.scope, "sample/widgets", "继续")
    restored_with_memory = application.service._load_context(restored_routing)

    assert resumed.session_id == target.session_id
    assert resumed.agent_context["goal"] == "继续处理 Issue #1"
    assert application.scope == target.scope
    assert application.repository == "sample/widgets"
    assert stateless_service._invalidated is True
    assert restored_agent.session_id == target.session_id
    assert restored_agent.goal == "继续处理 Issue #1"
    assert [item.memory_id for item in restored_routing.repository_memories] == [current_memory.memory_id]
    assert restored_with_memory is not None and restored_with_memory.guidance is not None
    assert [item.memory_id for item in restored_with_memory.guidance.repository_memories] == [current_memory.memory_id]
    assert sessions.list_turns(sibling.account_key, sibling.repository_key, sibling.session_id) == ()
    with pytest.raises(StateError, match="not found"):
        application.resume_session(8, target.session_id)

    sessions.save_agent_context(
        sibling.scope,
        {
            "agent": "issues",
            "repository": "other/private",
            "goal": "不应跨仓库恢复",
        },
    )
    application.switch_session(sibling.session_id)
    with pytest.raises(RoutingError, match="different repository"):
        application.service._load_context()
