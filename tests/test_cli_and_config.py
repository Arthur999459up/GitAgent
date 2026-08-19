"""CLI/config smoke tests for the refactored application surface."""

import pytest
from AGENT.GitAgent.gitagent.app.cli import build_parser
from AGENT.GitAgent.gitagent.app.config import CLIConfig
from AGENT.GitAgent.gitagent.core.models import DraftResult


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
