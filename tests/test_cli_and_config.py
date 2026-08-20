"""CLI/config smoke tests for the refactored application surface."""

from types import SimpleNamespace

import AGENT.GitAgent.gitagent.app.cli as cli_module
import pytest
from AGENT.GitAgent.gitagent.app.cli import build_parser
from AGENT.GitAgent.gitagent.app.config import CLIConfig
from AGENT.GitAgent.gitagent.core.models import DraftResult
from AGENT.GitAgent.gitagent.core.trace import TraceBus, TraceCategory, TraceStatus


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
