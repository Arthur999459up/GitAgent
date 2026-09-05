from __future__ import annotations

from rich.console import Console

from gitagent.application.metrics import ContextUsage, project_context_usage
from gitagent.application.terminal_ui import TerminalUI
from gitagent.domain.models import SessionEvent
from gitagent.harness.context import CompactionResult, MessageCompactionPlan
from gitagent.infra.observability import TraceBus

_CONTEXT_WINDOWS = {
    "default": 1_000,
    "main": 1_000,
    "repository": 1_000,
    "issues": 1_000,
    "pull_requests": 1_000,
    "coding": 1_000,
}


def _context_event(
    seq: int,
    agent: str,
    run_id: str,
    input_tokens: int,
    *,
    turn_seq: int = 1,
) -> SessionEvent:
    return SessionEvent(
        version=1,
        seq=seq,
        type="workflow_step",
        time=f"2026-01-01T00:00:{seq:02d}+00:00",
        session_id="session-test",
        turn_seq=turn_seq,
        agent=agent,
        data={
            "details": {
                "debug_event": "context_usage",
                "run_id": run_id,
                "input_tokens": input_tokens,
                "context_window_tokens": 1_000,
            }
        },
    )


def _completed_event(
    seq: int,
    agent: str,
    run_id: str,
    *,
    status: str = "completed",
) -> SessionEvent:
    return SessionEvent(
        version=1,
        seq=seq,
        type="agent_completed",
        time=f"2026-01-01T00:01:{seq:02d}+00:00",
        session_id="session-test",
        turn_seq=1,
        agent=agent,
        data={"status": status, "details": {"run_id": run_id}},
    )


def test_context_usage_keeps_same_agent_runs_separate() -> None:
    rows = project_context_usage(
        (
            _context_event(1, "repository", "run-a", 300),
            _context_event(2, "repository", "run-b", 700),
            _context_event(3, "repository", "run-a", 500),
        ),
        context_windows=_CONTEXT_WINDOWS,
    )

    repository = {row.run_id: row for row in rows if row.agent == "repository"}
    assert set(repository) == {"run-a", "run-b"}
    assert repository["run-a"].input_tokens == 500
    assert repository["run-b"].input_tokens == 700


def test_main_is_one_session_scoped_row() -> None:
    rows = project_context_usage(
        (
            _context_event(1, "main", "run-first", 200),
            _context_event(2, "main", "run-second", 350),
            _context_event(3, "main", "run-third", 420),
        ),
        context_windows=_CONTEXT_WINDOWS,
    )

    main_rows = [row for row in rows if row.agent == "main"]
    assert len(main_rows) == 1
    assert main_rows[0].run_id == "main"
    assert main_rows[0].state == "active"
    assert main_rows[0].input_tokens == 420


def test_context_usage_projects_active_waiting_and_completed_states() -> None:
    current_context = {
        "agent": "main",
        "run_id": "run-main-runtime",
        "pending": None,
        "waiting_for_user": None,
        "active_children": {
            "call-active": {
                "agent": "repository",
                "run_id": "run-active",
                "pending": None,
                "waiting_for_user": None,
                "active_children": {},
            },
            "call-waiting": {
                "agent": "issues",
                "run_id": "run-waiting",
                "pending": None,
                "waiting_for_user": {"question": "continue?"},
                "active_children": {},
            },
        },
    }
    rows = project_context_usage(
        (
            _context_event(1, "repository", "run-active", 300),
            _context_event(2, "issues", "run-waiting", 400),
            _context_event(3, "repository", "run-done", 800),
            _completed_event(4, "repository", "run-done"),
        ),
        context_windows=_CONTEXT_WINDOWS,
        current_context=current_context,
    )
    states = {(row.agent, row.run_id): row.state for row in rows}

    assert states[("main", "main")] == "active"
    assert states[("repository", "run-active")] == "active"
    assert states[("issues", "run-waiting")] == "waiting"
    assert states[("repository", "run-done")] == "completed"


def test_auto_compaction_trace_contains_concrete_run_id() -> None:
    bus = TraceBus()
    event = bus.emit_auto_compaction(
        session_id="session-test",
        agent="repository",
        run_id="run-a91f03c2deadbeef",
        level="light -> summary",
        before_tokens=900,
        after_tokens=400,
        context_window_tokens=1_000,
    )
    main_event = bus.emit_auto_compaction(
        session_id="session-test",
        agent="main",
        level="light",
        before_tokens=700,
        after_tokens=500,
        context_window_tokens=1_000,
    )

    assert event.details["run_id"] == "run-a91f03c2deadbeef"
    assert event.details["level"] == "light -> summary"
    assert main_event.details["run_id"] == "main"


def test_compaction_level_is_derived_from_all_stages() -> None:
    result = CompactionResult(
        messages=[],
        stages=(
            {"level": "light", "before_tokens": 900, "after_tokens": 800},
            {"level": "summary", "before_tokens": 800, "after_tokens": 400},
        ),
        plan=MessageCompactionPlan(),
        before_tokens=900,
        after_tokens=400,
        context_window_tokens=1_000,
    )

    assert result.level == "light -> summary"


def test_terminal_ui_shows_run_state_and_full_compaction_path() -> None:
    console = Console(record=True, width=200)
    ui = TerminalUI(console)
    ui.context_usage(
        (
            ContextUsage(
                agent="repository",
                run_id="run-a91f03c2deadbeef",
                input_tokens=700,
                context_window_tokens=1_000,
                turn_seq=2,
                state="active",
            ),
            ContextUsage(
                agent="repository",
                run_id="run-b78112f0cafebabe",
                input_tokens=300,
                context_window_tokens=1_000,
                turn_seq=1,
                state="completed",
            ),
        ),
        session_id="session-test",
    )
    ui.compaction(
        automatic=True,
        agent="repository",
        run_id="run-a91f03c2deadbeef",
        level="light -> summary",
        before_tokens=900,
        after_tokens=400,
        context_window_tokens=1_000,
    )
    rendered = console.export_text()

    assert "a91f03c2" in rendered
    assert "b78112f0" in rendered
    assert "active" in rendered
    assert "completed" in rendered
    assert "Repository · a91f03c2 · light -> summary" in rendered
