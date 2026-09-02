from gitagent.agents.main import _MAIN_SYSTEM
from gitagent.prompts import get_prompt_library


def test_main_routes_issue_and_pull_request_domains_explicitly() -> None:
    assert "All GitHub Issue work belongs to agent__issues" in _MAIN_SYSTEM
    assert "All Pull Request work belongs to" in _MAIN_SYSTEM
    assert "agent__pull_requests" in _MAIN_SYSTEM
    assert "Never use agent__repository as a" in _MAIN_SYSTEM
    assert "substitute for discovering or querying GitHub Issues or Pull Requests" in _MAIN_SYSTEM


def test_pr_and_coding_prompts_preserve_fact_inference_boundary() -> None:
    prompts = get_prompt_library()
    coding = prompts.text("system.coding")
    pull_requests = prompts.text("system.pull_requests")

    assert "Never upgrade an inference into a confirmed fact" in coding
    assert "Never upgrade an inference into a confirmed fact" in pull_requests
    assert "mergeable_state=dirty" in pull_requests
