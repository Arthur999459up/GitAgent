from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gitagent.agent_loop import AgentAction, AgentActionKind, AgentLoop
from gitagent.agents.issues import IssueAgent
from gitagent.application.capabilities import build_capability_layer
from gitagent.capability import (
    BashCommandPolicy,
    CapabilityErrorType,
    InvocationContext,
    PermissionDecision,
    PermissionPolicy,
)
from gitagent.domain.errors import ApprovalRequired, ValidationError
from gitagent.domain.models import (
    AgentSpec,
    ApprovalIntent,
    PlannedCapabilityCall,
    WorkflowTurnDecision,
)
from gitagent.harness import AgentHarness
from gitagent.harness.constraints import ApprovalStore
from gitagent.infra.github import InMemoryGitHubClient


class RecordingGitHubClient(InMemoryGitHubClient):
    def __init__(self) -> None:
        super().__init__(
            {
                "owner/repo": {
                    "files": {"README.md": "evidence\n"},
                    "issues": {
                        2: {
                            "number": 2,
                            "title": "Issue two",
                            "body": "Please investigate",
                            "state": "open",
                            "labels": [],
                            "comments": [],
                        }
                    },
                    "prs": {},
                }
            }
        )
        self.post_comment_calls: list[tuple[str, int, str]] = []

    def post_comment(self, repository: str, issue_number: int, body: str) -> dict[str, Any]:
        self.post_comment_calls.append((repository, issue_number, body))
        return super().post_comment(repository, issue_number, body)


class OneCapabilityAgent:
    def __init__(self, capability_id: str, arguments: dict[str, Any]) -> None:
        self.capability_id = capability_id
        self.arguments = arguments

    def decide(self, context: Any) -> AgentAction:
        if not context.observations:
            return AgentAction(
                AgentActionKind.CAPABILITY,
                capability_id=self.capability_id,
                arguments=dict(self.arguments),
                summary=f"execute {self.capability_id}",
            )
        return AgentAction(AgentActionKind.FINISH, message="done")

    def build_result(self, context: Any) -> str:
        del context
        return "done"


def harness_for(tmp_path: Path, github: RecordingGitHubClient | None = None) -> tuple[AgentHarness, RecordingGitHubClient]:
    client = github or RecordingGitHubClient()
    return AgentHarness(build_capability_layer(client, workspace_root=tmp_path)), client


def register(harness: AgentHarness, agent: str) -> None:
    harness.register(AgentSpec(agent, "test", "test", (), frozenset()))


def approve(loop: AgentLoop, context: Any, agent: Any) -> None:
    loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.APPROVE))


def test_issue_read_executes_directly_without_pending_action(tmp_path: Path) -> None:
    harness, github = harness_for(tmp_path)
    register(harness, "issues")
    context = harness.context("issues", "session-read", repository="owner/repo")

    issue = context.invoke("github.get_issue", issue_number=2)

    assert issue["number"] == 2
    assert context.last_capability_call is not None
    assert context.last_capability_call.result.status == "success"
    assert context.pending is None
    assert github.post_comment_calls == []


def test_issue_write_waits_then_original_agent_executes_exact_call_once(tmp_path: Path) -> None:
    harness, github = harness_for(tmp_path)
    register(harness, "issues")
    context = harness.context("issues", "session-write", repository="owner/repo")
    agent = OneCapabilityAgent("github.post_comment", {"issue_number": 2, "body": "hello"})
    loop = AgentLoop(harness)

    loop.start(context, agent)

    assert context.pending is not None
    assert context.pending.calls == [
        PlannedCapabilityCall("github.post_comment", {"issue_number": 2, "body": "hello"})
    ]
    assert context.last_capability_call is not None
    assert context.last_capability_call.result.status == "approval_required"
    assert github.post_comment_calls == []
    approval_id = context.pending.approval_id

    approve(loop, context, agent)

    assert context.finished is True
    assert context.error is None
    assert github.post_comment_calls == [("owner/repo", 2, "hello")]
    approved_events = [
        event
        for event in harness.audit.events("session-write")
        if event.capability_id == "github.post_comment" and event.approval_id == approval_id
    ]
    assert len(approved_events) == 1
    assert approved_events[0].agent == "issues"
    assert approved_events[0].result == "OK"


def test_rejected_issue_write_never_reaches_provider(tmp_path: Path) -> None:
    harness, github = harness_for(tmp_path)
    register(harness, "issues")
    context = harness.context("issues", "session-reject", repository="owner/repo")
    agent = OneCapabilityAgent("github.post_comment", {"issue_number": 2, "body": "no"})
    loop = AgentLoop(harness)
    loop.start(context, agent)

    loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.REJECT))

    assert context.pending is None
    assert github.post_comment_calls == []


def test_approval_rejects_tampering_other_session_and_replay(tmp_path: Path) -> None:
    github = RecordingGitHubClient()
    layer = build_capability_layer(github, workspace_root=tmp_path)
    arguments = {"issue_number": 2, "body": "A"}
    proposed = layer.invoke(
        "github.post_comment",
        arguments,
        InvocationContext("run-propose", "session-A", "issues", "owner/repo"),
    )
    request = layer.policy.approvals.create(
        session_id="session-A",
        repository="owner/repo",
        summary="post exact comment",
        calls=[PlannedCapabilityCall("github.post_comment", arguments)],
    )
    layer.policy.approvals.decide(request.approval_id, "Approve")

    tampered = layer.invoke(
        "github.post_comment",
        {"issue_number": 2, "body": "B"},
        InvocationContext(
            "run-tampered",
            "session-A",
            "issues",
            "owner/repo",
            approval_id=request.approval_id,
        ),
    )
    other_session = layer.invoke(
        "github.post_comment",
        arguments,
        InvocationContext(
            "run-other-session",
            "session-B",
            "issues",
            "owner/repo",
            approval_id=request.approval_id,
        ),
    )
    exact = layer.invoke(
        "github.post_comment",
        arguments,
        InvocationContext(
            "run-exact",
            "session-A",
            "issues",
            "owner/repo",
            approval_id=request.approval_id,
        ),
    )
    replay = layer.invoke(
        "github.post_comment",
        arguments,
        InvocationContext(
            "run-replay",
            "session-A",
            "issues",
            "owner/repo",
            approval_id=request.approval_id,
        ),
    )

    assert proposed.status == "approval_required"
    assert tampered.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert other_session.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert exact.status == "success"
    assert replay.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert github.post_comment_calls == [("owner/repo", 2, "A")]


def test_approval_store_enforces_order() -> None:
    store = ApprovalStore()
    calls = [
        PlannedCapabilityCall("sample.first", {"value": "one"}),
        PlannedCapabilityCall("sample.second", {"value": "two"}),
    ]
    request = store.create(
        session_id="session-order",
        repository="owner/repo",
        summary="ordered plan",
        calls=calls,
    )
    store.decide(request.approval_id, "Approve")

    with pytest.raises(ApprovalRequired, match="order"):
        store.authorize(
            approval_id=request.approval_id,
            session_id="session-order",
            capability_id="sample.second",
            arguments={"value": "two"},
        )

    for call in calls:
        store.authorize(
            approval_id=request.approval_id,
            session_id="session-order",
            capability_id=call.capability_id,
            arguments=call.arguments,
        )
    assert store.complete(request.approval_id) is True


def test_ordered_mutation_plan_executes_entirely_as_issues_agent(tmp_path: Path) -> None:
    harness, _ = harness_for(tmp_path)
    register(harness, "issues")
    context = harness.context("issues", "session-plan", repository="owner/repo")
    calls = [
        PlannedCapabilityCall("github.create_branch", {"base": "main", "branch": "gitagent/test-plan"}),
        PlannedCapabilityCall(
            "github.commit",
            {
                "branch": "gitagent/test-plan",
                "files": {"fix.py": "fixed = True\n"},
                "deleted_files": [],
                "message": "Fix issue",
            },
        ),
        PlannedCapabilityCall("github.push", {"branch": "gitagent/test-plan"}),
        PlannedCapabilityCall(
            "github.create_draft_pr",
            {
                "title": "Fix issue",
                "body": "Reviewed change",
                "base": "main",
                "head": "gitagent/test-plan",
                "draft": True,
            },
        ),
    ]
    loop = AgentLoop(harness)
    loop.restore_pending(context, summary="apply reviewed issue fix", calls=calls)
    approval_id = context.pending.approval_id

    approve(loop, context, OneCapabilityAgent("unused.read", {}))

    approved_events = [
        event for event in harness.audit.events("session-plan") if event.approval_id == approval_id
    ]
    assert [event.capability_id for event in approved_events] == [call.capability_id for call in calls]
    assert {event.agent for event in approved_events} == {"issues"}
    assert all(event.result == "OK" for event in approved_events)
    assert context.error is None


def test_issue_reply_without_code_change_still_uses_unified_approval(tmp_path: Path) -> None:
    harness, github = harness_for(tmp_path)
    issue_agent = IssueAgent(harness, object(), object())  # type: ignore[arg-type]
    context = harness.context(
        "issues",
        "session-reply",
        repository="owner/repo",
        goal="帮我回复 Issue #2，但我不打算改代码",
        entity_type="issue",
        entity_id="2",
    )
    issue = context.invoke("github.get_issue", issue_number=2)
    context.reply_draft = "Thanks, this is the evidence-backed reply."
    loop = AgentLoop(harness)

    assert issue["number"] == 2
    assert not hasattr(context, "read" + "_only")
    assert context.code_candidate is None

    loop.start(context, issue_agent)

    assert context.pending is not None
    assert context.pending.calls[0].capability_id == "github.post_comment"
    assert github.post_comment_calls == []

    approve(loop, context, issue_agent)

    assert context.error is None
    assert github.post_comment_calls == [
        ("owner/repo", 2, "Thanks, this is the evidence-backed reply.")
    ]
    assert context.code_candidate is None


@pytest.mark.parametrize(
    ("capability_id", "arguments", "initial", "expected"),
    [
        ("native.write", {"path": "new.txt", "content": "created"}, None, "created"),
        (
            "native.edit",
            {"path": "edit.txt", "old_text": "before", "new_text": "after"},
            "before",
            "after",
        ),
    ],
)
def test_coding_file_mutations_wait_for_approval(
    tmp_path: Path,
    capability_id: str,
    arguments: dict[str, Any],
    initial: str | None,
    expected: str,
) -> None:
    harness, _ = harness_for(tmp_path)
    register(harness, "coding")
    path = tmp_path / str(arguments["path"])
    if initial is not None:
        path.write_text(initial, encoding="utf-8")
    context = harness.context("coding", f"session-{capability_id}")
    agent = OneCapabilityAgent(capability_id, arguments)
    loop = AgentLoop(harness)

    loop.start(context, agent)

    assert context.pending is not None
    assert context.last_capability_call is not None
    assert context.last_capability_call.result.status == "approval_required"
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == initial

    approve(loop, context, agent)

    assert context.error is None
    assert path.read_text(encoding="utf-8") == expected


def test_static_verifier_write_is_denied_not_asked(tmp_path: Path) -> None:
    harness, _ = harness_for(tmp_path)
    register(harness, "static_verifier")
    context = harness.context("static_verifier", "session-static")
    agent = OneCapabilityAgent("native.write", {"path": "forbidden.txt", "content": "no"})

    AgentLoop(harness).start(context, agent)

    assert context.pending is None
    assert context.finished is True
    assert context.observations[0]["payload"]["error"] == "permission_denied"
    assert not (tmp_path / "forbidden.txt").exists()


def test_bash_policy_has_only_allow_ask_deny_outcomes() -> None:
    policy = BashCommandPolicy()

    assert policy.decide("git status", "coding").decision == PermissionDecision.ALLOW
    assert policy.decide("git diff", "coding").decision == PermissionDecision.ALLOW
    assert policy.decide("git commit -m change", "coding").decision == PermissionDecision.ASK
    assert policy.decide("python -m py_compile sample.py", "static_only").decision == PermissionDecision.ALLOW
    assert policy.decide("python sample.py", "coding").decision == PermissionDecision.ASK
    assert policy.decide("python sample.py", "static_only").decision == PermissionDecision.DENY
    assert policy.decide("git status | tee status.txt", "coding").decision == PermissionDecision.ASK
    assert policy.decide("git status > status.txt", "coding").decision == PermissionDecision.ASK
    assert policy.decide("git status && git diff", "coding").decision == PermissionDecision.ASK


def test_coding_bash_ask_is_consumed_by_same_context(tmp_path: Path) -> None:
    harness, _ = harness_for(tmp_path)
    register(harness, "coding")
    context = harness.context("coding", "session-bash")
    agent = OneCapabilityAgent("native.bash", {"command": "echo approved"})
    loop = AgentLoop(harness)

    loop.start(context, agent)

    assert context.pending is not None
    assert context.last_capability_call is not None
    assert context.last_capability_call.result.status == "approval_required"

    approve(loop, context, agent)

    assert context.error is None
    assert context.last_capability_call is not None
    assert context.last_capability_call.result.status == "success"
    assert context.last_capability_call.result.content["stdout_tail"] == "approved\n"


def test_ask_result_is_not_recorded_by_failure_guard(tmp_path: Path) -> None:
    layer = build_capability_layer(RecordingGitHubClient(), workspace_root=tmp_path)
    context = InvocationContext("run-ask", "session-ask", "issues", "owner/repo")
    arguments = {"issue_number": 2, "body": "hello"}

    first = layer.invoke("github.post_comment", arguments, context)
    second = layer.invoke("github.post_comment", arguments, context)

    assert first.status == "approval_required"
    assert second.status == "approval_required"
    assert first.error is None
    assert second.error is None


def test_real_permission_denial_is_recorded_by_failure_guard(tmp_path: Path) -> None:
    layer = build_capability_layer(RecordingGitHubClient(), workspace_root=tmp_path)
    context = InvocationContext("run-deny", "session-deny", "static_verifier", "owner/repo")
    arguments = {"path": "forbidden.txt", "content": "no"}

    first = layer.invoke("native.write", arguments, context)
    repeated = layer.invoke("native.write", arguments, context)

    assert first.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert repeated.error.type == CapabilityErrorType.REPEATED_FAILURE
    assert not (tmp_path / "forbidden.txt").exists()


def test_removed_policy_keys_are_rejected_at_startup(tmp_path: Path) -> None:
    policy_path = tmp_path / "removed-key.yaml"
    removed_key = "approval" + "_required"
    policy_path.write_text(
        f"""
defaults:
  discover: deny
  invoke: deny
agents:
  issues:
    discover: []
    invoke:
      allow: []
      {removed_key}: [github.post_comment]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unknown keys"):
        PermissionPolicy.from_file(policy_path)
