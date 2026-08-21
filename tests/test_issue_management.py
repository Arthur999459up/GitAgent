"""Issue creation and metadata-management behavior."""

from __future__ import annotations

from typing import Any

from AGENT.GitAgent.gitagent.core.models import AccessLevel, IssueOperation
from AGENT.GitAgent.gitagent.mcp.memory import InMemoryMCPServer
from AGENT.GitAgent.gitagent.runtime import AgentContext

from .support import build_test_service, handle, sample_repositories


class IssueMutationReasoner:
    def __init__(self, tool: str, arguments: dict[str, Any], message: str) -> None:
        self.tool = tool
        self.arguments = arguments
        self.message = message

    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Any = None,
        tool_name: str = "respond",
        tools: Any = None,
    ) -> dict[str, Any]:
        del system, schema, tools
        if tool_name != "decide_action":
            raise AssertionError(f"unexpected structured call: {tool_name}")
        if f'"tool": "{self.tool}"' in prompt:
            return {
                "kind": "finish",
                "summary": "issue mutation completed",
                "message": self.message,
                "awaiting_user_confirmation": False,
            }
        return {
            "kind": "tool",
            "summary": "apply requested issue mutation",
            "tool": self.tool,
            "arguments": self.arguments,
            "awaiting_user_confirmation": False,
        }

    def complete_text(self, *, system: str, prompt: str) -> str:
        del system, prompt
        return self.message


def test_in_memory_issue_tools_cover_create_update_status_metadata_and_locking():
    repositories = sample_repositories()
    repositories["sample/widgets"]["milestones"] = {
        4: {"number": 4, "title": "v1.0", "state": "open"},
        5: {"number": 5, "title": "v0.9", "state": "closed"},
    }
    server = InMemoryMCPServer(repositories)

    milestones = server.call_tool(
        "github.list_milestones",
        {"repository": "sample/widgets", "state": "all", "limit": 10},
    )
    created = server.call_tool(
        "github.create_issue",
        {
            "repository": "sample/widgets",
            "title": "Document widget lifecycle",
            "body": "Add an end-to-end example.",
            "labels": ["documentation"],
            "assignees": ["alice"],
            "milestone_number": 4,
        },
    )
    updated = server.call_tool(
        "github.update_issue",
        {
            "repository": "sample/widgets",
            "issue_number": created["number"],
            "title": "Document the widget lifecycle",
            "body": "Add two end-to-end examples.",
            "state": "closed",
            "labels": ["documentation", "ready"],
            "assignees": ["alice", "bob"],
            "clear_milestone": True,
        },
    )
    locked = server.call_tool(
        "github.set_issue_lock",
        {
            "repository": "sample/widgets",
            "issue_number": created["number"],
            "locked": True,
            "reason": "resolved",
        },
    )
    unlocked = server.call_tool(
        "github.set_issue_lock",
        {"repository": "sample/widgets", "issue_number": created["number"], "locked": False},
    )

    assert [item["number"] for item in milestones["milestones"]] == [4, 5]
    assert created["milestone"]["title"] == "v1.0"
    assert updated == {
        **created,
        "title": "Document the widget lifecycle",
        "body": "Add two end-to-end examples.",
        "state": "closed",
        "labels": ["documentation", "ready"],
        "assignees": ["alice", "bob"],
        "milestone": None,
    }
    assert locked == {"number": created["number"], "locked": True, "active_lock_reason": "resolved"}
    assert unlocked == {"number": created["number"], "locked": False, "active_lock_reason": None}
    assert server.get_issue("sample/widgets", created["number"])["locked"] is False
    assert server.get_tool("github.create_issue").access == AccessLevel.WRITE
    assert server.get_tool("github.update_issue").access == AccessLevel.WRITE
    assert server.get_tool("github.set_issue_lock").access == AccessLevel.WRITE


def test_issue_agent_updates_only_after_approval_and_returns_the_new_state():
    reasoner = IssueMutationReasoner(
        "github.update_issue",
        {
            "issue_number": 1,
            "state": "closed",
            "labels": ["question", "resolved"],
            "assignees": ["alice"],
        },
        "Issue #1 已关闭并更新元数据。",
    )
    service = build_test_service(
        main_responses=[
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "1",
                "request": "关闭 Issue #1，增加 resolved 标签并分配给 alice",
                "message": "",
                "clarify": False,
                "requested_fix": False,
                "requested_reply": False,
            }
        ],
        agent_reasoner=reasoner,
    )

    proposal = handle(service, "关闭 Issue #1，增加 resolved 标签并分配给 alice")
    issue_before_approval = service.harness.server.get_issue("sample/widgets", 1)

    assert isinstance(proposal.output, AgentContext)
    assert proposal.output.pending is not None
    assert proposal.output.pending.calls[0].tool == "github.update_issue"
    assert issue_before_approval.get("state", "open") == "open"
    assert issue_before_approval["labels"] == ["question"]

    completed = handle(service, "可以")
    issue = service.harness.server.get_issue("sample/widgets", 1)

    assert completed.output.operation == IssueOperation.UPDATE
    assert completed.output.issue_number == 1
    assert completed.output.issues[0].labels == ["question", "resolved"]
    assert completed.output.issues[0].assignees == ["alice"]
    assert issue["state"] == "closed"


def test_issue_agent_can_create_and_lock_issues_through_the_existing_loop():
    create_reasoner = IssueMutationReasoner(
        "github.create_issue",
        {"title": "New issue", "body": "Created by the Issue agent.", "labels": ["task"]},
        "Issue 已创建。",
    )
    create_service = build_test_service(
        main_responses=[
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "",
                "request": "创建 Issue：New issue",
                "message": "",
                "clarify": False,
                "requested_fix": False,
                "requested_reply": False,
            }
        ],
        agent_reasoner=create_reasoner,
    )

    create_proposal = handle(create_service, "创建 Issue：New issue")
    assert create_proposal.output.pending.calls[0].tool == "github.create_issue"
    created = handle(create_service, "可以")

    assert created.output.operation == IssueOperation.CREATE
    assert created.output.issue_number == 8
    assert create_service.harness.server.get_issue("sample/widgets", 8)["title"] == "New issue"

    lock_reasoner = IssueMutationReasoner(
        "github.set_issue_lock",
        {"issue_number": 1, "locked": True, "reason": "resolved"},
        "Issue #1 的讨论已锁定。",
    )
    lock_service = build_test_service(
        main_responses=[
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "1",
                "request": "锁定 Issue #1 的讨论，原因是 resolved",
                "message": "",
                "clarify": False,
                "requested_fix": False,
                "requested_reply": False,
            }
        ],
        agent_reasoner=lock_reasoner,
    )

    lock_proposal = handle(lock_service, "锁定 Issue #1 的讨论，原因是 resolved")
    assert lock_proposal.output.pending.calls[0].tool == "github.set_issue_lock"
    locked = handle(lock_service, "可以")

    assert locked.output.operation == IssueOperation.UPDATE
    assert locked.output.issues[0].locked is True
    assert lock_service.harness.server.get_issue("sample/widgets", 1)["locked"] is True
