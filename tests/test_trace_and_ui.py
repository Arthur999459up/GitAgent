from AGENT.GitAgent.gitagent.app.ui import TerminalUI
from AGENT.GitAgent.gitagent.core.trace import TraceCategory, TraceEvent, TraceStatus
from AGENT.GitAgent.tests.support import build_test_service, handle
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
