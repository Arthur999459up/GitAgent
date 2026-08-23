"""Runtime loop mechanics over isolated AgentContext working memory."""

from __future__ import annotations

import json
from typing import Any

import pytest
from AGENT.GitAgent.gitagent.context import estimate_tokens
from AGENT.GitAgent.gitagent.core.errors import WorkflowError
from AGENT.GitAgent.gitagent.core.models import (
    AccessLevel,
    AgentSpec,
    ApprovalIntent,
    MutationRejectedResult,
    WorkflowTurnDecision,
)
from AGENT.GitAgent.gitagent.core.trace import TraceBus
from AGENT.GitAgent.gitagent.mcp.memory import InMemoryMCPServer
from AGENT.GitAgent.gitagent.runtime import (
    AgentAction,
    AgentActionKind,
    AgentContext,
    AgentHarness,
    AgentLoop,
    register_github_mutator,
    rejection_feedback,
    render_observations,
)

from .support import sample_repositories


class ScriptedAgent:
    def __init__(self, *actions: AgentAction) -> None:
        self.actions = list(actions)

    def decide(self, context: AgentContext) -> AgentAction:
        return self.actions.pop(0)

    def build_result(self, context: AgentContext) -> dict[str, Any]:
        return {"done": True}


class RepeatReadAgent:
    def decide(self, context: AgentContext) -> AgentAction:
        return AgentAction(
            AgentActionKind.TOOL,
            tool="github.list_issues",
            arguments={"state": "open", "limit": 20},
        )

    def build_result(self, context: AgentContext) -> dict[str, Any]:
        return {"done": True}


def _harness() -> tuple[InMemoryMCPServer, AgentHarness]:
    server = InMemoryMCPServer(sample_repositories())
    harness = AgentHarness(server, trace=TraceBus())
    register_github_mutator(harness)
    harness.register(
        AgentSpec(
            name="test_agent",
            role="scripted test agent",
            system_prompt="test agent",
            allowed_tools=frozenset({"github.list_issues", "github.post_comment", "github.merge"}),
            output_schema=(),
            capabilities=frozenset(),
            required_context=("repository",),
            routing_examples=(),
        )
    )
    return server, harness


def _context(harness: AgentHarness, *, session_id: str = "session-test", max_steps: int = 8) -> AgentContext:
    return harness.context(
        "test_agent",
        session_id,
        repository="sample/widgets",
        goal="scripted goal",
        max_steps=max_steps,
    )


def test_read_tool_executes_autonomously_without_approval():
    _, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(AgentActionKind.TOOL, tool="github.list_issues", arguments={"state": "open", "limit": 20}),
        AgentAction(AgentActionKind.FINISH, summary="done"),
    )

    context = loop.start(_context(harness, session_id="session-read"), agent)

    assert context.finished
    assert context.error is None
    assert context.pending is None
    assert context.steps == 2
    tool_observations = [obs for obs in context.observations if obs["kind"] == "tool"]
    assert [obs["payload"]["tool"] for obs in tool_observations] == ["github.list_issues"]
    assert tool_observations[0]["payload"]["data"]["issues"]
    assert not any(
        event.classification in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
        for event in harness.audit.events()
    )


def test_equivalent_read_is_served_from_context_cache_without_second_tool_execution():
    _, harness = _harness()
    loop = AgentLoop(harness)
    action = AgentAction(
        AgentActionKind.TOOL,
        tool="github.list_issues",
        arguments={"state": "open", "limit": 20},
    )
    agent = ScriptedAgent(action, action, AgentAction(AgentActionKind.FINISH, summary="done"))
    context = loop.start(_context(harness, session_id="session-cache"), agent)

    assert context.finished
    observations = [obs for obs in context.observations if obs["kind"] == "tool"]
    assert len(observations) == 2
    assert observations[0]["payload"].get("cached") is None
    assert observations[1]["payload"]["cached"] is True
    tool_trace = [event for event in harness.trace.events(context.session_id) if event.name == "github.list_issues"]
    assert len(tool_trace) == 2
    assert tool_trace[0].details["agent"] == "test_agent"
    assert tool_trace[-1].details["result"]["issues"]


def test_read_only_context_cannot_create_a_write_proposal():
    server, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(
            AgentActionKind.TOOL,
            tool="github.post_comment",
            arguments={"issue_number": 1, "body": "must not be proposed"},
        )
    )
    context = _context(harness, session_id="session-read-only")
    context.read_only = True

    completed = loop.start(context, agent)

    assert completed.finished
    assert completed.pending is None
    assert server.repositories["sample/widgets"].get("comments", []) == []
    policy = [obs for obs in completed.observations if obs["kind"] == "policy"]
    assert policy[-1]["payload"]["tool"] == "github.post_comment"


def test_write_tool_is_gated_until_an_explicit_decision():
    server, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(
            AgentActionKind.TOOL,
            tool="github.post_comment",
            arguments={"issue_number": 1, "body": "thanks"},
        )
    )

    context = loop.start(_context(harness, session_id="session-write"), agent)

    assert not context.finished
    assert context.pending is not None
    assert context.pending.calls[0].tool == "github.post_comment"
    assert context.pending.calls[0].arguments["body"] == "thanks"
    assert server.repositories["sample/widgets"].get("comments", []) == []


def test_approve_executes_exactly_the_approved_mutation_plan():
    server, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(
            AgentActionKind.TOOL,
            tool="github.post_comment",
            arguments={"issue_number": 1, "body": "approved comment"},
        ),
        AgentAction(AgentActionKind.FINISH, summary="done"),
    )
    context = loop.start(_context(harness, session_id="session-approve"), agent)
    approval_id = context.pending.approval_id

    resumed = loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.APPROVE))

    assert resumed.finished
    assert resumed.error is None
    assert resumed.pending is None
    comments = server.repositories["sample/widgets"]["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "approved comment"
    assert harness.approvals.get(approval_id).decision == "Approve"


def test_reject_feeds_structured_instruction_back_for_replanning():
    server, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(
            AgentActionKind.TOOL,
            tool="github.post_comment",
            arguments={"issue_number": 1, "body": "first draft"},
        ),
        AgentAction(
            AgentActionKind.TOOL,
            tool="github.post_comment",
            arguments={"issue_number": 1, "body": "revised draft"},
        ),
    )
    context = loop.start(_context(harness, session_id="session-reject"), agent)
    first_approval = context.pending.approval_id

    resumed = loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.REJECT, instruction="别用第一版"))

    assert not resumed.finished
    assert rejection_feedback(resumed) == "别用第一版"
    assert resumed.pending is not None
    assert resumed.pending.calls[0].arguments["body"] == "revised draft"
    assert resumed.pending.approval_id != first_approval
    assert harness.approvals.get(first_approval).decision == "Reject"
    assert server.repositories["sample/widgets"].get("comments", []) == []


def test_bare_rejection_reports_empty_instruction():
    _, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(AgentActionKind.TOOL, tool="github.post_comment", arguments={"issue_number": 1, "body": "draft"}),
        AgentAction(AgentActionKind.FINISH, summary="done"),
    )
    context = loop.start(_context(harness, session_id="session-bare"), agent)

    resumed = loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.REJECT))

    assert resumed.finished
    assert rejection_feedback(resumed) == ""
    assert resumed.pending is None


def test_approved_mutation_remote_rejection_returns_business_result_on_stale_head_sha():
    server, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(
            AgentActionKind.TOOL,
            tool="github.merge",
            arguments={"pr_number": 7, "expected_head_sha": "abc123"},
        )
    )
    context = loop.start(_context(harness, session_id="session-merge"), agent)
    server.repositories["sample/widgets"]["prs"][7]["head"]["sha"] = "new456"

    resumed = loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.APPROVE))

    assert resumed.finished
    assert resumed.error is None
    assert resumed.pending is None
    assert not resumed.waiting
    assert "merged" not in server.repositories["sample/widgets"]["prs"][7]
    assert isinstance(resumed.result, MutationRejectedResult)
    assert "head changed" in resumed.result.reason


def test_ask_pauses_and_reply_resumes_as_observation():
    _, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(
        AgentAction(AgentActionKind.ASK, question="Which issue do you mean?"),
        AgentAction(AgentActionKind.FINISH, summary="done"),
    )
    context = loop.start(_context(harness, session_id="session-ask"), agent)
    assert not context.finished
    assert context.question == "Which issue do you mean?"

    resumed = loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.APPROVE, instruction="issue #1"))

    assert resumed.finished
    assert resumed.question == ""
    assistant_observations = [obs for obs in resumed.observations if obs["kind"] == "assistant"]
    user_observations = [obs for obs in resumed.observations if obs["kind"] == "user"]
    assert assistant_observations[-1]["payload"] == "Which issue do you mean?"
    assert user_observations[-1]["payload"] == "issue #1"


def test_debug_trace_records_decision_and_waiting_context():
    _, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(AgentAction(AgentActionKind.ASK, summary="need confirmation", question="Continue?"))

    context = loop.start(_context(harness, session_id="session-debug-ask"), agent)

    events = harness.trace.debug_events(context.session_id, "test_agent")
    decision = next(event for event in events if event.details.get("debug_event") == "decision")
    waiting = next(event for event in events if event.details.get("debug_event") == "waiting")
    assert decision.details["decision"]["kind"] == "ask"
    assert decision.details["decision"]["question"] == "Continue?"
    assert waiting.details["context"]["question"] == "Continue?"
    assert waiting.details["context"]["finished"] is False


def test_debug_trace_preserves_nested_error_types():
    _, harness = _harness()
    loop = AgentLoop(harness)

    class FailingAgent(ScriptedAgent):
        def decide(self, context: AgentContext) -> AgentAction:
            try:
                raise TimeoutError("provider timed out")
            except TimeoutError as exc:
                raise RuntimeError("model request failed") from exc

    context = loop.start(_context(harness, session_id="session-debug-error"), FailingAgent())

    failed = next(event for event in reversed(harness.trace.events(context.session_id)) if event.status.value == "failed")
    assert [item["type"] for item in failed.details["error"]] == ["RuntimeError", "TimeoutError"]
    assert failed.details["context"]["finished"] is True


def test_default_step_budget_is_twenty():
    _, harness = _harness()
    assert AgentLoop(harness).max_steps == 20
    context = harness.context("test_agent", "session-default", repository="sample/widgets", goal="check")
    assert context.max_steps == 20


def test_step_limit_halts_a_runaway_loop():
    _, harness = _harness()
    loop = AgentLoop(harness)
    context = loop.start(_context(harness, session_id="session-limit", max_steps=3), RepeatReadAgent())

    assert context.finished
    assert context.steps == 3
    assert context.error is not None and "上限" in context.error


def test_finish_can_skip_unused_result_rendering():
    _, harness = _harness()
    loop = AgentLoop(harness)

    class NoResultAgent(ScriptedAgent):
        def build_result(self, context: AgentContext) -> dict[str, Any]:
            raise AssertionError("result rendering must be skipped")

    context = _context(harness, session_id="session-collect-only")
    context.result_required = False
    completed = loop.start(context, NoResultAgent(AgentAction(AgentActionKind.FINISH, summary="evidence complete")))

    assert completed.finished
    assert completed.result is None


def test_resume_rejects_decision_for_finished_context():
    _, harness = _harness()
    loop = AgentLoop(harness)
    agent = ScriptedAgent(AgentAction(AgentActionKind.FINISH, summary="done"))
    context = loop.start(_context(harness, session_id="session-done"), agent)
    assert context.finished

    with pytest.raises(WorkflowError, match="not waiting"):
        loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.APPROVE))


def test_render_observations_stays_valid_json_under_large_payloads():
    _, harness = _harness()
    context = _context(harness, session_id="session-observations")
    context.observations = [
        {
            "kind": "tool",
            "payload": {
                "tool": "repository.read_file",
                "arguments": {"repository": "sample/widgets", "path": "src/formatting.py"},
                "data": {"content": "x" * 50_000},
            },
        }
        for _ in range(30)
    ]

    rendered = render_observations(context)
    parsed = json.loads(rendered)

    assert isinstance(parsed, list)
    assert parsed[0]["context_projection"]["level"] in {"light", "summary", "emergency"}
    assert parsed[-1]["arguments"]["path"] == "src/formatting.py"
    assert len(rendered) < 50_000 * 30
    assert estimate_tokens(rendered) + context.fixed_input_tokens() <= context.input_budget_tokens


def test_render_observations_preserves_a_complete_target_file_that_fits_the_budget():
    _, harness = _harness()
    context = _context(harness, session_id="session-complete-file")
    content = "x" * 3_000 + "\ndef list_sessions() -> list[dict]:\n    return []\n"
    context.observations = [
        {
            "kind": "tool",
            "payload": {
                "tool": "repository.read_file",
                "arguments": {"repository": "sample/widgets", "path": "corecoder/session.py"},
                "data": {
                    "path": "corecoder/session.py",
                    "start_line": 1,
                    "end_line": 99,
                    "content": content,
                    "truncated": False,
                },
            },
        }
    ]

    parsed = json.loads(render_observations(context))

    assert parsed[-1]["data"]["content"] == content
    assert "__content_projection__" not in parsed[-1]["data"]


def test_render_observations_does_not_project_content_below_the_light_threshold():
    _, harness = _harness()
    context = _context(harness, session_id="session-projected-file")
    context.observations = [
        {
            "kind": "tool",
            "payload": {
                "tool": "repository.read_file",
                "arguments": {"repository": "sample/widgets", "path": "src/large.py"},
                "data": {
                    "path": "src/large.py",
                    "start_line": 1,
                    "end_line": 400,
                    "content": "x" * 20_000,
                    "truncated": False,
                },
            },
        }
    ]

    parsed = json.loads(render_observations(context))

    assert parsed[-1]["data"]["truncated"] is False
    assert parsed[-1]["data"]["content"] == "x" * 20_000
    assert "__content_projection__" not in parsed[-1]["data"]


def test_render_observations_projects_one_large_tool_result_only_after_the_shared_threshold():
    _, harness = _harness()
    context = _context(harness, session_id="session-large-projected-file")
    context.observations = [
        {
            "kind": "tool",
            "payload": {
                "tool": "repository.read_file",
                "arguments": {"repository": "sample/widgets", "path": "src/large.py"},
                "data": {
                    "path": "src/large.py",
                    "start_line": 1,
                    "end_line": 400,
                    "content": "x" * 100_000,
                    "truncated": False,
                },
            },
        }
    ]

    parsed = json.loads(render_observations(context))

    assert parsed[0]["context_projection"]["level"] == "light"
    assert parsed[-1]["data"]["__content_projection__"] == {
        "projected": True,
        "original_chars": 100_000,
        "retained_chars": 6_000,
    }
