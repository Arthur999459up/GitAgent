"""Session routing and direct parent-to-child agent handoff acceptance tests."""

from __future__ import annotations

from typing import Any

from AGENT.GitAgent.gitagent.core.models import DraftResult
from AGENT.GitAgent.gitagent.runtime import AgentContext
from AGENT.GitAgent.tests.support import StubMainReasoner, build_test_service, handle


class IssueFixReasoner:
    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Any = None,
        tool_name: str = "respond",
        tools: Any = None,
    ) -> dict[str, Any]:
        del system, prompt, schema, tools
        if tool_name == "decide_action":
            return {
                "kind": "apply_code_change",
                "summary": "prepare the issue fix",
                "tool": "",
                "arguments": {},
                "specialist": "",
                "question": "",
                "message": "",
            }
        raise AssertionError(f"unexpected structured call: {tool_name}")

    def complete_text(self, *, system: str, prompt: str) -> str:
        del system, prompt
        return "repository"


def test_issue_draft_revision_and_publish_are_session_scoped():
    service = build_test_service()

    first = handle(service, "处理 Issue #1，先给我一版回复草稿")
    assert isinstance(first.output, DraftResult)
    initial_draft = first.output.body
    stored = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert stored is not None
    assert stored.agent_context["agent"] == "issues"
    assert stored.agent_context["reply_draft"] == initial_draft

    revised = handle(service, "再短一点")
    assert isinstance(revised.output, DraftResult)
    assert revised.output.body != ""

    proposal = handle(service, "可以，发布吧")
    assert isinstance(proposal.output, AgentContext)
    assert proposal.output.pending is not None
    assert proposal.output.pending.calls[0].tool == "github.post_comment"
    assert proposal.output.pending.calls[0].arguments["body"] == revised.output.body

    repo = service.harness.server.repositories["sample/widgets"]
    before = len(repo.get("comments", []))
    completed = handle(service, "可以")
    assert len(repo["comments"]) == before + 1
    assert repo["comments"][-1]["body"] == revised.output.body
    assert completed.agent == "issues"

    stored = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert stored is not None and stored.agent_context == {}


def test_simple_conversation_answers_without_child_context():
    service = build_test_service()
    result = handle(service, "你好")

    assert result.agent is None
    assert result.output == "你好，我可以帮你处理这个仓库。"
    session = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert session is not None and session.agent_context == {}


def test_issue_calls_coding_directly_and_parent_context_continues_to_approval():
    service = build_test_service(agent_reasoner=IssueFixReasoner())
    main = StubMainReasoner(
        [
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "2",
                "request": "修复 Issue #2",
                "message": "",
                "clarify": False,
                "requested_fix": True,
                "requested_reply": False,
            }
        ]
    )
    service.main_agent.reasoner = main

    result = handle(service, "修复 Issue #2")

    assert isinstance(result.output, AgentContext)
    parent = result.output
    assert parent.agent == "issues"
    assert not parent.finished
    assert parent.pending is not None
    assert parent.code_candidate is not None
    assert parent.verification is not None and parent.verification.passed
    child_summaries = [item for item in parent.observations if item.get("kind") == "agent"]
    assert child_summaries
    assert child_summaries[-1]["payload"]["agent"] == "coding"
    assert child_summaries[-1]["payload"]["verification_passed"] is True
    assert all("observations" not in item["payload"] for item in child_summaries)
    assert parent.pending.calls[-1].tool == "github.create_draft_pr"


def test_main_agent_routes_from_session_context_without_task_lifecycle_fields():
    reasoner = StubMainReasoner()
    service = build_test_service(main_reasoner=reasoner)
    result = handle(service, "看看 Issue #1")

    assert result.agent == "issues"
    routing_prompt = reasoner.prompts[0]
    assert "session" in routing_prompt
    assert "working_state" in routing_prompt
