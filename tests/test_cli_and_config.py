"""CLI/config smoke tests for the refactored application surface."""

from dataclasses import replace
from types import SimpleNamespace

import AGENT.GitAgent.gitagent.app.cli as cli_module
import pytest
from AGENT.GitAgent.gitagent.app.cli import build_parser
from AGENT.GitAgent.gitagent.app.config import CLIConfig
from AGENT.GitAgent.gitagent.core.models import DraftResult
from AGENT.GitAgent.gitagent.core.trace import TraceBus, TraceCategory, TraceStatus
from AGENT.GitAgent.gitagent.state import SessionRecord, default_working_state
from rich.console import Console


def _session_record(index: int, *, repository: str = "sample/widgets") -> SessionRecord:
    return SessionRecord(
        session_id=f"session-{index:032x}",
        account_key="https://api.github.test#user:7",
        repository_key=f"https://api.github.test#repo:{index}",
        repository_full_name=repository,
        title=f"Session {index}",
        created_at=f"2026-08-{index:02d}T00:00:00+00:00",
        updated_at=f"2026-08-{index:02d}T00:00:00+00:00",
        context_boundary_seq=0,
        summary="",
        summary_through_seq=0,
        working_state=default_working_state(),
        agent_context={},
    )


def test_cli_parser_keeps_plain_gitagent_entrypoint():
    parser = build_parser()
    args = parser.parse_args(["--provider", "openai", "--model", "test-model"])
    assert args.provider == "openai"
    assert args.model == "test-model"


def test_config_rejects_too_small_context_budget():
    config = CLIConfig(context_window_tokens=5000, max_tokens=4096, context_safety_tokens=1024)
    with pytest.raises(ValueError, match="Context 输入预算过小"):
        config.validate()


def test_default_state_path_is_absolute():
    config = CLIConfig()
    assert config.state_path.startswith("/")


def test_default_model_output_budget_is_16k(monkeypatch):
    monkeypatch.delenv("GITAGENT_MAX_TOKENS", raising=False)

    config = CLIConfig.from_env()

    assert config.max_tokens == 16_384


def test_llm_and_github_timeouts_can_be_configured_independently(monkeypatch):
    monkeypatch.setenv("GITAGENT_REQUEST_TIMEOUT", "31")
    monkeypatch.setenv("GITAGENT_LLM_TIMEOUT", "90")
    monkeypatch.setenv("GITAGENT_GITHUB_TIMEOUT", "12")

    config = CLIConfig.from_env()

    assert config.request_timeout == 31
    assert config.effective_llm_timeout == 90
    assert config.effective_github_timeout == 12


def test_runtime_default_allows_slower_code_generation(monkeypatch):
    monkeypatch.delenv("GITAGENT_REQUEST_TIMEOUT", raising=False)
    monkeypatch.delenv("GITAGENT_LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("GITAGENT_GITHUB_TIMEOUT", raising=False)

    config = CLIConfig.from_env()

    assert config.effective_llm_timeout == 300
    assert config.effective_github_timeout == 30


def test_legacy_request_timeout_remains_the_default_for_both_clients():
    config = CLIConfig(request_timeout=45)

    assert config.effective_llm_timeout == 45
    assert config.effective_github_timeout == 45


def test_draft_result_is_a_user_visible_first_class_output():
    draft = DraftResult(
        entity_type="issue",
        entity_id="1",
        title="Issue 回复草稿",
        body="Thanks for the report.",
        note="尚未发布。",
    )
    assert draft.body == "Thanks for the report."
    assert "未发布" in draft.note


def test_debug_command_filters_current_session_by_agent(monkeypatch):
    trace = TraceBus()
    session_id = "session-debug-cli"
    trace.emit(
        session_id=session_id,
        category=TraceCategory.AGENT,
        name="issues",
        status=TraceStatus.STARTED,
        details={"debug_event": "start"},
    )
    trace.emit(
        session_id=session_id,
        category=TraceCategory.AGENT,
        name="coding",
        status=TraceStatus.STARTED,
        details={"debug_event": "start"},
    )
    captured = {}

    def capture(events, *, session_id, agent):
        captured["events"] = events
        captured["session_id"] = session_id
        captured["agent"] = agent

    monkeypatch.setattr(cli_module.ui, "debug_history", capture)
    application = SimpleNamespace(session_id=session_id, trace=trace)

    cli_module._run_command(application, "/debug issues")

    assert captured["session_id"] == session_id
    assert captured["agent"] == "issues"
    assert [event.name for event in captured["events"]] == ["issues"]


def test_startup_menu_deletes_by_number_then_returns_and_resumes(monkeypatch):
    first = _session_record(1, repository="sample/one")
    second = _session_record(2, repository="sample/two")

    class Application:
        github = SimpleNamespace(get_authenticated_user=lambda: {"id": 7, "login": "alice"})

        def __init__(self):
            self.sessions = [first, second]
            self.deleted = []
            self.resumed = []

        def list_account_sessions(self, authenticated_user_id):
            assert authenticated_user_id == 7
            return tuple(self.sessions)

        def delete_account_session(self, authenticated_user_id, session_id):
            assert authenticated_user_id == 7
            self.deleted.append(session_id)
            target = next(session for session in self.sessions if session.session_id == session_id)
            self.sessions.remove(target)
            return target

        def resume_session(self, authenticated_user_id, session_id):
            assert authenticated_user_id == 7
            self.resumed.append(session_id)
            return next(session for session in self.sessions if session.session_id == session_id)

    application = Application()
    choices = iter(["d 1", "1"])
    snapshots = []
    monkeypatch.setattr(cli_module, "terminal_prompt", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(
        cli_module,
        "_show_sessions",
        lambda sessions, **kwargs: snapshots.append(tuple(session.session_id for session in sessions)),
    )

    assert cli_module._select_startup_session(application) is True
    assert snapshots == [(first.session_id, second.session_id), (second.session_id,)]
    assert application.deleted == [first.session_id]
    assert application.resumed == [second.session_id]


def test_startup_prompt_uses_consistent_short_commands(monkeypatch):
    prompts = []
    application = SimpleNamespace(
        github=SimpleNamespace(get_authenticated_user=lambda: {"id": 7, "login": "alice"}),
        list_account_sessions=lambda authenticated_user_id: (),
    )
    monkeypatch.setattr(cli_module, "_show_sessions", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli_module,
        "terminal_prompt",
        lambda prompt, **kwargs: prompts.append(prompt) or "q",
    )

    assert cli_module._select_startup_session(application) is False
    assert prompts == ["操作：[编号] 恢复  [n] 新建  [d 编号] 删除  [q] 退出\n> "]


def test_delete_command_resolves_display_number(monkeypatch):
    current = _session_record(1)
    target = replace(_session_record(2), repository_key=current.repository_key)
    deleted = []
    application = SimpleNamespace(
        session_id=current.session_id,
        list_sessions=lambda: (current, target),
        delete_session=lambda session_id: deleted.append(session_id),
    )
    monkeypatch.setattr(cli_module, "_show_session_safety_note", lambda: None)

    cli_module._run_command(application, "/delete 2")

    assert deleted == [target.session_id]


def test_startup_session_table_omits_current_column(monkeypatch):
    captured = []
    monkeypatch.setattr(cli_module.console, "print", captured.append)

    cli_module._show_sessions((_session_record(1),), active_session_id=None)

    table = captured[0]
    assert [column.header for column in table.columns] == [
        "#",
        "标题",
        "仓库",
        "创建时间",
        "更新时间",
        "Session ID",
    ]


def test_in_session_table_marks_the_current_session(monkeypatch):
    session = _session_record(1)
    rendered_console = Console(record=True, width=240)
    monkeypatch.setattr(cli_module, "console", rendered_console)

    cli_module._show_sessions((session,), active_session_id=session.session_id)

    rendered = rendered_console.export_text()
    assert "当前" in rendered
    assert "●" in rendered
