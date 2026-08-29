from gitagent.agent_loop.actions import AgentAction, AgentActionKind, PendingAction
from gitagent.application.service import GitAgentService
from gitagent.capability import CapabilityLayer, PermissionPolicy
from gitagent.domain.models import (
    ApprovalIntent,
    ChangeRequest,
    PlannedCapabilityCall,
    RepositoryOperation,
    SessionScope,
    WorkflowTurnDecision,
)
from gitagent.harness.context import ContextBuilder, derive_domain_messages
from gitagent.harness.context.projector import CHECKPOINT_PREFIX
from gitagent.infra.persistence import SessionEventLog, SessionManager, StateStore
from gitagent.memory import MemoryStore


class _UnexpectedReasoner:
    def complete_structured_messages(self, **kwargs):
        raise AssertionError(f"unexpected structured inference: {kwargs}")

    def complete_text_messages(self, **kwargs):
        raise AssertionError(f"unexpected text inference: {kwargs}")


def _manager(tmp_path):
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    return store, SessionManager(store, log)


def _service(tmp_path, manager: SessionManager, scope: SessionScope) -> GitAgentService:
    return GitAgentService(
        CapabilityLayer(policy=PermissionPolicy({})),
        main_reasoner=_UnexpectedReasoner(),
        session_manager=manager,
        memory_store=MemoryStore(tmp_path / "memory"),
        session_scope=scope,
        auto_learning=False,
    )


def _working_state(goal: str) -> dict:
    return {
        "version": 4,
        "goal": goal,
        "focus": None,
        "manifests": [],
        "open_question": "",
    }


def test_pending_domain_thread_survives_restart_and_user_reply_appends_to_same_run(
    tmp_path,
) -> None:
    _, manager = _manager(tmp_path)
    scope = SessionScope(
        "https://api.github.com#user:51",
        "https://api.github.com#repo:52",
        "session-" + "1" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    first_turn = manager.start_turn(scope, "准备发布 Issue 回复")
    first = _service(tmp_path, manager, scope)
    first._active_turn_seq = first_turn.seq
    context = first.harness.context(
        "issues",
        scope.session_id,
        repository="owner/repo",
        goal="publish issue reply",
        entity_type="issue",
        entity_id="7",
    )
    context.origin_turn_seq = first_turn.seq
    context.reply_draft = "draft body"
    context.start_message_thread()
    context.pending = PendingAction(
        "pre-restart-approval",
        "发布 Issue #7 回复",
        [
            PlannedCapabilityCall(
                "github.create_issue_comment",
                {
                    "repository": "owner/repo",
                    "issue_number": 7,
                    "body": "draft body",
                },
            )
        ],
    )
    context.append_message(
        {"role": "assistant", "content": "发布 Issue #7 回复\n\n请确认是否执行。"}
    )
    original_thread = list(context.messages)
    run_id = context.run_id
    first._save_context(context)
    stored = manager.get_session(scope.account_key, scope.repository_key, scope.session_id)
    assert stored is not None
    assert "messages" not in stored.agent_context
    manager.complete_turn(
        scope,
        first_turn.seq,
        assistant_text="发布 Issue #7 回复\n\n请确认是否执行。",
        workflow_summary="awaiting approval",
        route={"route": "issues", "status": "awaiting_approval"},
        entity_manifests=[],
        working_state=_working_state("publish issue reply"),
    )

    _, restarted_manager = _manager(tmp_path)
    second = _service(tmp_path, restarted_manager, scope)
    restored = second._load_context()
    assert restored is not None
    assert restored.run_id == run_id
    assert restored.pending is not None
    assert restored.messages == original_thread

    second_turn = restarted_manager.start_turn(scope, "不要发布")
    second._active_turn_seq = second_turn.seq
    second._ensure_domain_thread_durable(restored)
    second._continue_approval(
        restored,
        WorkflowTurnDecision(ApprovalIntent.REJECT, instruction="不要发布"),
        "不要发布",
    )

    assert restored.pending is None
    assert restored.messages[:-1] == original_thread
    assert restored.messages[-1] == {"role": "user", "content": "不要发布"}
    replay = derive_domain_messages(
        restarted_manager.event_log.iter_events(scope),
        agent="issues",
        run_id=run_id,
    )
    assert replay == restored.messages

    _, restarted_again_manager = _manager(tmp_path)
    third = _service(tmp_path, restarted_again_manager, scope)
    restored_again = third._load_context()
    assert restored_again is not None
    assert restored_again.run_id == run_id
    assert restored_again.pending is None
    assert restored_again.messages == restored.messages


def test_unrelated_reply_to_stale_pr_question_reroutes_without_resume_call(
    tmp_path, monkeypatch
) -> None:
    _, manager = _manager(tmp_path)
    scope = SessionScope(
        "https://api.github.com#user:81",
        "https://api.github.com#repo:82",
        "session-" + "4" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    first_turn = manager.start_turn(scope, "review a PR")
    service = _service(tmp_path, manager, scope)
    service._active_turn_seq = first_turn.seq
    context = service.harness.context(
        "pull_requests",
        scope.session_id,
        repository="owner/repo",
        goal="review a PR",
        entity_type="pull_request",
    )
    context.origin_turn_seq = first_turn.seq
    context.start_message_thread()
    context.question = "请提供 Pull Request 编号"
    context.append_message({"role": "assistant", "content": context.question})
    service._save_context(context)
    manager.complete_turn(
        scope,
        first_turn.seq,
        assistant_text=context.question,
        workflow_summary="awaiting pull request number",
        route={"route": "pull_requests", "status": "awaiting_input"},
        entity_manifests=[],
        working_state=_working_state("review a PR"),
    )

    second_turn = manager.start_turn(scope, "actually list open issues")
    main_messages = [
        {"role": "system", "content": "main system"},
        {"role": "user", "content": "actually list open issues"},
    ]
    monkeypatch.setattr(
        service.pull_request_agent,
        "accept_question_reply",
        lambda current, user_input: False,
    )
    reached_reroute = {"value": False}

    def reroute(messages, **kwargs):
        reached_reroute["value"] = True
        assert messages[-1] == {
            "role": "user",
            "content": "actually list open issues",
        }
        assert not any(
            str(call.get("id") or "").startswith("call-resume-")
            for message in messages
            for call in (message.get("tool_calls") or [])
        )
        raise RuntimeError("reroute reached")

    monkeypatch.setattr(service.main_agent, "decide", reroute)

    try:
        service.handle(
            "actually list open issues",
            repository="owner/repo",
            main_messages=main_messages,
            session_scope=scope,
            turn_seq=second_turn.seq,
        )
    except RuntimeError as exc:
        assert str(exc) == "reroute reached"
    else:
        raise AssertionError("unrelated reply should have reached Main rerouting")

    assert reached_reroute["value"] is True



def test_repository_revision_resumes_same_domain_run(tmp_path, monkeypatch) -> None:
    _, manager = _manager(tmp_path)
    scope = SessionScope(
        "https://api.github.com#user:71",
        "https://api.github.com#repo:72",
        "session-" + "3" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    first_turn = manager.start_turn(scope, "修改仓库配置")
    service = _service(tmp_path, manager, scope)
    service._active_turn_seq = first_turn.seq
    context = service.harness.context(
        "repository",
        scope.session_id,
        repository="owner/repo",
        goal="change A",
        entity_type="repository",
    )
    context.origin_turn_seq = first_turn.seq
    context.operation = RepositoryOperation.MODIFY.value
    context.change_request = ChangeRequest("owner/repo", "change A")
    context.result_required = False
    context.start_message_thread()
    context.pending = PendingAction(
        "approval-before-revision",
        "apply change A",
        [
            PlannedCapabilityCall(
                "github.commit_to_default_branch",
                {"repository": "owner/repo"},
            )
        ],
    )
    context.append_message(
        {"role": "assistant", "content": "apply change A\n\n请确认是否执行。"}
    )
    original_thread = list(context.messages)
    run_id = context.run_id
    service._save_context(context)

    second_turn = manager.start_turn(scope, "改成 change B")
    service._active_turn_seq = second_turn.seq
    monkeypatch.setattr(
        service.harness.approvals,
        "decide",
        lambda approval_id, decision: None,
    )
    monkeypatch.setattr(
        service.repository_agent,
        "decide",
        lambda current: AgentAction(
            AgentActionKind.FINISH,
            summary="revision accepted",
            message="revised candidate ready",
        ),
    )

    output = service._continue_approval(
        context,
        WorkflowTurnDecision(ApprovalIntent.REVISE, instruction="改成 change B"),
        "改成 change B",
    )

    assert output == "revised candidate ready"
    assert context.run_id == run_id
    assert context.messages[: len(original_thread)] == original_thread
    assert context.messages[len(original_thread)] == {
        "role": "user",
        "content": "改成 change B",
    }
    assert context.messages[-1] == {
        "role": "assistant",
        "content": "revised candidate ready",
    }
    assert context.change_request is not None
    assert context.change_request.description == "change A\n\nUser revision: 改成 change B"
    replay = derive_domain_messages(
        manager.event_log.iter_events(scope),
        agent="repository",
        run_id=run_id,
    )
    assert replay == context.messages


def test_legacy_sqlite_summary_and_legacy_jsonl_resume_into_main_messages(tmp_path) -> None:
    store, manager = _manager(tmp_path)
    scope = SessionScope(
        "https://api.github.com#user:61",
        "https://api.github.com#repo:62",
        "session-" + "2" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    old_turn = manager.start_turn(scope, "legacy U1")
    manager.complete_turn(
        scope,
        old_turn.seq,
        assistant_text="legacy A1",
        workflow_summary="legacy workflow",
        route=None,
        entity_manifests=[],
        working_state=_working_state("legacy U1"),
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE sessions SET summary=?,summary_through_seq=? WHERE session_id=?",
            ("legacy checkpoint", old_turn.seq, scope.session_id),
        )

    _, restarted = _manager(tmp_path)
    current = restarted.start_turn(scope, "current U2")
    builder = ContextBuilder(
        restarted,
        MemoryStore(tmp_path / "legacy-memory"),
    )
    messages, _ = builder.build(
        scope,
        "owner/repo",
        "current U2",
        system="current system",
        turn_seq=current.seq,
    )

    assert messages == [
        {"role": "system", "content": "current system"},
        {"role": "system", "content": CHECKPOINT_PREFIX + "legacy checkpoint"},
        {"role": "user", "content": "current U2"},
    ]
