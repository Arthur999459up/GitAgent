"""PromptLibrary inventory and rendering tests for the refactored prompt set."""

import pytest
from AGENT.GitAgent.gitagent.prompts import PromptError, PromptLibrary, get_prompt_library

_EXPECTED_KEYS = frozenset(
    {
        "system.issues",
        "system.pull_requests",
        "system.coding",
        "system.pr_review",
        "system.ci_diagnosis",
        "system.repo_qa",
        "system.github_mutator",
        "system.static_verifier",
        "approval.system",
        "approval.input",
        "agents.issue_decide",
        "agents.pull_request_decide",
        "agents.issue_list_summarize",
        "agents.issue_detail_answer",
        "agents.issue_fix_guide",
        "agents.issue_reply_draft",
        "agents.pull_request_list_summarize",
        "agents.pull_request_detail_answer",
        "agents.coding_create",
        "agents.coding_repair",
        "agents.pr_review",
        "agents.ci_diagnosis",
        "agents.repo_qa",
        "agents.guidance_section",
        "reasoning.structured_output_instruction",
        "reasoning.structured_call_instruction",
        "reasoning.tool_description",
    }
)


def _library() -> PromptLibrary:
    return get_prompt_library()


def test_keys_match_refactored_inventory_without_semantic_router_prompts():
    keys = _library().keys()
    assert keys == _EXPECTED_KEYS
    assert not any(key.startswith("routing.") for key in keys)
    assert "system.router" not in keys


def test_approval_prompt_has_only_explicit_user_and_proposal_placeholders():
    library = _library()
    assert library.placeholders("approval.input") == ("user_input", "proposal_context")
    assert library.placeholders("approval.system") == ()


def test_approval_input_renders_expected_sections():
    rendered = _library().render(
        "approval.input",
        user_input="可以",
        proposal_context='{"tool":"github.post_comment"}',
    )
    assert rendered.startswith("User turn:\n可以\n\nOpen proposal context:\n")
    assert "github.post_comment" in rendered
    assert rendered.endswith("Return only the classification.")


def test_static_prompt_text_rejects_dynamic_template_access():
    with pytest.raises(PromptError, match="use render"):
        _library().text("approval.input")


def test_render_missing_extra_and_none_values_fail_fast():
    library = _library()
    with pytest.raises(PromptError, match="missing values"):
        library.render("approval.input", user_input="可以")
    with pytest.raises(PromptError, match="unexpected values"):
        library.render("approval.input", user_input="可以", proposal_context="{}", extra="x")
    with pytest.raises(PromptError, match="must not be None"):
        library.render("approval.input", user_input="可以", proposal_context=None)


def test_every_template_has_unique_placeholder_names():
    library = _library()
    for key in library.keys():  # noqa: SIM118 - PromptLibrary is not itself iterable
        placeholders = library.placeholders(key)
        assert len(placeholders) == len(set(placeholders))


def test_unknown_deleted_router_prompt_fails_fast():
    with pytest.raises(PromptError, match="unknown prompt template"):
        _library().text("routing.input")


def test_library_validation_succeeds_for_refactored_inventory():
    _library().validate()
