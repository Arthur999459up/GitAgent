from types import SimpleNamespace

import AGENT.GitAgent.gitagent.app.cli as cli_module
from AGENT.GitAgent.gitagent.app.ui import TerminalUI
from AGENT.GitAgent.gitagent.core.models import MutationRejectedResult
from AGENT.GitAgent.gitagent.core.trace import TraceBus, TraceCategory, TraceEvent, TraceStatus
from AGENT.GitAgent.tests.support import StubMainReasoner, build_test_service, handle
from rich.console import Console


def test_main_agent_and_domain_tool_events_are_emitted_in_execution_order():
    service = build_test_service()

    result = handle(service, "Where is format_name implemented?")

    events = service.harness.trace.events()
    calls = [(event.category.value, event.name, event.status.value) for event in events]
    assert calls[0:2] == [
        ("agent", "main", "started"),
        ("agent", "main", "completed"),
    ]
    assert ("agent", "repo_qa", "started") in calls
    assert ("tool_use", "repository.get_repo_tree", "started") in calls
    assert result.agent == "repo_qa"


def test_issue_draft_and_exact_proposal_trace_share_session_id():
    service = build_test_service()
    handle(service, "处理 Issue #1，先给我回复草稿")
    proposal = handle(service, "发布吧")
    assert proposal.output.pending is not None

    session_id = service.session_scope.session_id
    events = service.harness.trace.events(session_id)
    assert events
    assert all(event.session_id == session_id for event in events)
    assert any(event.name == "issues" for event in events)
    assert any(event.name == "github.get_issue" for event in events)
    assert any(event.name == "issues" and event.status == TraceStatus.WAITING for event in events)


def test_terminal_ui_does_not_repeat_waiting_question_in_compact_trace():
    console = Console(record=True, width=120, color_system=None)
    terminal = TerminalUI(console)
    terminal.trace(
        TraceEvent(
            timestamp="2026-08-11T00:00:00+00:00",
            session_id="session-trace-waiting",
            category=TraceCategory.AGENT,
            name="issues",
            status=TraceStatus.WAITING,
            message="是否进入代码修复流程来解决 Issue #3？",
        )
    )

    rendered = console.export_text()
    assert "Issues" in rendered
    assert "是否进入代码修复流程" not in rendered


def test_code_change_proposal_renders_one_confirmation_and_separates_pr_title_and_body(monkeypatch):
    class ApplyIssueFixReasoner:
        def complete_structured(self, **kwargs):
            assert kwargs.get("tool_name") == "decide_action"
            return {
                "kind": "apply_code_change",
                "summary": "prepare the issue fix",
                "awaiting_user_confirmation": False,
            }

    service = build_test_service(agent_reasoner=ApplyIssueFixReasoner())
    service.main_agent.reasoner = StubMainReasoner(
        [
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "2",
                "request": "修复 Issue #2",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            }
        ]
    )
    proposal = handle(service, "修复 Issue #2")
    captured: list[tuple[str, str]] = []

    class CapturingUI:
        def markdown(self, content, *, title, **kwargs):
            del kwargs
            captured.append((title, content))

        def text(self, content, *, title, **kwargs):
            del kwargs
            captured.append((title, content))

    monkeypatch.setattr(cli_module, "ui", CapturingUI())

    cli_module._render_proposal(SimpleNamespace(service=service), proposal.output)

    assert [title for title, _ in captured].count("需要你的确认") == 1
    code_change = next(content for title, content in captured if title == "代码变更 · 待批准")
    assert "### Draft PR 标题" in code_change
    assert "### Draft PR 正文" in code_change


def test_mutation_rejection_renders_as_explained_business_result(monkeypatch):
    captured: list[tuple[str, str, str]] = []

    class CapturingUI:
        def markdown(self, content, *, title, kind, **kwargs):
            del kwargs
            captured.append((title, kind, content))

    monkeypatch.setattr(cli_module, "ui", CapturingUI())

    cli_module._render_output(
        SimpleNamespace(),
        MutationRejectedResult(
            summary="发布 APPROVE Review 到 PR #11",
            reason="GitHub 拒绝了该操作（HTTP 422）：Review Can not approve your own pull request",
        ),
    )

    assert captured == [
        (
            "操作未执行",
            "agent",
            (
                "**操作：** 发布 APPROVE Review 到 PR #11\n\n"
                "**结果：** 未执行\n\n"
                "**失败原因：** GitHub 拒绝了该操作（HTTP 422）："
                "Review Can not approve your own pull request"
            ),
        )
    ]


def test_terminal_ui_labels_main_agent_tool_and_workflow_output():
    console = Console(record=True, width=120, color_system=None)
    terminal = TerminalUI(console)
    terminal.user("检查 PR #7")
    terminal.markdown("审查完成", title="Agent · PR Review", kind="review")
    for category, name in [
        (TraceCategory.AGENT, "main"),
        (TraceCategory.TOOL_USE, "repository.get_pr_diff"),
        (TraceCategory.WORKFLOW, "code_change"),
    ]:
        terminal.trace(
            TraceEvent(
                timestamp="2026-08-11T00:00:00+00:00",
                session_id="session-trace-ui",
                category=category,
                name=name,
                status=TraceStatus.STARTED,
            )
        )

    rendered = console.export_text()
    assert "You" in rendered
    assert "Agent · PR Review" in rendered
    assert "Main Agent" in rendered
    assert "repository.get_pr_diff" in rendered
    assert "code_change" in rendered
    assert "Router" not in rendered


def test_trace_debug_filter_keeps_agent_owned_tools():
    trace = TraceBus()
    trace.emit(
        session_id="session-debug-filter",
        category=TraceCategory.AGENT,
        name="issues",
        status=TraceStatus.STARTED,
        details={"debug_event": "start"},
    )
    trace.emit(
        session_id="session-debug-filter",
        category=TraceCategory.TOOL_USE,
        name="github.get_issue",
        status=TraceStatus.COMPLETED,
        details={"agent": "issues", "result": {"number": 3}},
    )
    trace.emit(
        session_id="session-debug-filter",
        category=TraceCategory.AGENT,
        name="coding",
        status=TraceStatus.STARTED,
        details={"debug_event": "start"},
    )

    events = trace.debug_events("session-debug-filter", "issues")
    assert [(event.category.value, event.name) for event in events] == [
        ("agent", "issues"),
        ("tool_use", "github.get_issue"),
    ]


def test_terminal_ui_debug_history_renders_structured_agent_decision():
    console = Console(record=True, width=160, color_system=None)
    terminal = TerminalUI(console)
    event = TraceEvent(
        timestamp="2026-08-20T01:00:00+00:00",
        session_id="session-debug-ui",
        category=TraceCategory.AGENT,
        name="issues",
        status=TraceStatus.PROGRESS,
        details={
            "debug_event": "decision",
            "step": 12,
            "decision": {
                "kind": "finish",
                "message": "是否同意按上述方向修改？",
            },
            "context": {
                "finished": False,
                "question": "",
            },
        },
    )

    terminal.debug_history([event], session_id="session-debug-ui", agent="issues")

    rendered = console.export_text()
    assert "Agent Debug History" in rendered
    assert '"kind": "finish"' in rendered
    assert "是否同意按上述方向修改" in rendered
    assert '"finished": false' in rendered
