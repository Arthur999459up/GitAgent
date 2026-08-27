from __future__ import annotations

from pathlib import Path
from typing import Any

from gitagent.agent_loop import AgentLoop
from gitagent.agents.coding import CodingAgent
from gitagent.agents.issues import IssueAgent
from gitagent.agents.pull_requests import PullRequestAgent
from gitagent.agents.repository import RepositoryAgent
from gitagent.application.capabilities import build_capability_layer
from gitagent.application.terminal_ui import TerminalUI
from gitagent.domain.errors import ResourceNotFoundError
from gitagent.domain.models import (
    ApprovalIntent,
    RepositoryOperation,
    WorkflowTurnDecision,
)
from gitagent.harness import AgentHarness
from gitagent.harness.recovery.github_mutations import register_github_mutator
from gitagent.infra.github import GitHubTransportError, InMemoryGitHubClient
from gitagent.infra.observability import TraceCategory, TraceEvent, TraceStatus


class CountingGitHubClient(InMemoryGitHubClient):
    def __init__(self) -> None:
        super().__init__({"owner/repo": {"issues": {}, "prs": {}, "files": {}}})
        self.issue_calls = 0
        self.pr_calls = 0
        self.pr_list_calls = 0
        self.tree_calls = 0
        self.search_calls = 0

    def get_issue(self, repository: str, issue_number: int) -> dict[str, Any]:
        self.issue_calls += 1
        return super().get_issue(repository, issue_number)

    def get_pr(self, repository: str, pr_number: int) -> dict[str, Any]:
        self.pr_calls += 1
        return super().get_pr(repository, pr_number)

    def list_pull_requests(
        self,
        repository: str,
        state: str = "open",
        base: str = "",
        head: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        self.pr_list_calls += 1
        return super().list_pull_requests(repository, state=state, base=base, head=head, limit=limit)

    def search_code(
        self,
        repository: str,
        query: str,
        path: str = "",
        max_results: int = 20,
    ) -> dict[str, Any]:
        self.search_calls += 1
        return super().search_code(repository, query=query, path=path, max_results=max_results)

    def get_repo_tree(
        self,
        repository: str,
        path: str = "",
        depth: int = 2,
        max_entries: int = 300,
        ref: str | None = None,
    ) -> dict[str, Any]:
        del repository, path, depth, max_entries, ref
        self.tree_calls += 1
        raise ResourceNotFoundError("repository tree unavailable")


class MutationFailingGitHubClient(InMemoryGitHubClient):
    def __init__(self) -> None:
        super().__init__(
            {
                "owner/repo": {
                    "issues": {1: {"number": 1, "title": "Issue", "state": "open", "labels": []}},
                    "prs": {},
                    "files": {},
                }
            }
        )
        self.post_comment_calls = 0

    def post_comment(self, repository: str, issue_number: int, body: str) -> dict[str, Any]:
        del repository, issue_number, body
        self.post_comment_calls += 1
        raise GitHubTransportError("comment outcome unknown", timed_out=True, request_sent=True)


class CodingReadFailingGitHubClient(InMemoryGitHubClient):
    def __init__(self) -> None:
        super().__init__({"owner/repo": {"issues": {}, "prs": {}, "files": {}}})
        self.default_branch_calls = 0

    def get_default_branch(self, repository: str) -> dict[str, Any]:
        del repository
        self.default_branch_calls += 1
        raise ResourceNotFoundError("default branch unavailable")


class IssueFailureReasoner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.prompts.append(str(kwargs.get("prompt") or ""))
        assert kwargs.get("tool_name") == "decide_action"
        return {
            "kind": "finish",
            "summary": "Issue 不存在",
            "message": "当前仓库中未找到 Issue #888。",
            "awaiting_user_confirmation": False,
        }


class IssueMutationFailureReasoner:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("tool_name") == "decide_action"
        self.calls += 1
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if self.calls == 1:
            return {
                "kind": "capability",
                "summary": "发布 Issue 回复",
                "capability_id": "github.post_comment",
                "arguments": {"issue_number": 1, "body": "reply"},
                "awaiting_user_confirmation": False,
            }
        if self.calls == 2:
            return {
                "kind": "capability",
                "summary": "读取 Issue 评论确认远端状态",
                "capability_id": "github.get_issue_comments",
                "arguments": {"issue_number": 1, "limit": 30},
                "awaiting_user_confirmation": False,
            }
        return {
            "kind": "finish",
            "summary": "发布结果不确定",
            "message": "读取远端评论后仍无法确认首次发布结果，因此未再次提交。",
            "awaiting_user_confirmation": False,
        }


class PullRequestFailureReasoner:
    def __init__(self) -> None:
        self.recovery_prompts: list[str] = []

    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name == "select_pull_request_operation":
            return {"operation": "get", "review_event": ""}
        assert tool_name == "decide_action"
        prompt = str(kwargs.get("prompt") or "")
        self.recovery_prompts.append(prompt)
        if len(self.recovery_prompts) == 1:
            return {
                "kind": "capability",
                "summary": "列出 Pull Requests 交叉确认",
                "capability_id": "github.list_pull_requests",
                "arguments": {"state": "all", "limit": 20},
            }
        return {
            "kind": "finish",
            "summary": "根据失败调用和列表结果完成判断",
            "message": "get_pr(888) 返回 resource_not_found，随后完整 PR 列表也没有 #888，因此当前仓库没有该 PR。",
        }


class RepositoryFailureReasoner:
    def __init__(self) -> None:
        self.recovery_prompts: list[str] = []

    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("tool_name") == "decide_action"
        prompt = str(kwargs.get("prompt") or "")
        self.recovery_prompts.append(prompt)
        if len(self.recovery_prompts) == 1:
            return {
                "kind": "capability",
                "summary": "搜索仓库内容补充证据",
                "capability_id": "repository.search_code",
                "arguments": {"query": "README", "max_results": 5},
            }
        return {
            "kind": "finish",
            "summary": "根据仓库树失败和搜索结果完成判断",
            "message": "仓库搜索可以执行，但仓库树读取仍是 resource_not_found，因此当前无法给出完整结构内容。",
        }

    def complete_text(self, **kwargs: Any) -> str:
        del kwargs
        return "不应覆盖 recovery-mode 的最终回答。"


class RepositoryCodingFailureReasoner:
    def __init__(self) -> None:
        self.recovery_prompts: list[str] = []

    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("tool_name") == "decide_action"
        prompt = str(kwargs.get("prompt") or "")
        self.recovery_prompts.append(prompt)
        return {
            "kind": "finish",
            "summary": "Coding 读取失败",
            "message": "Coding 所需仓库读取失败，已停止当前修改。",
        }


class RecordingConsole:
    def __init__(self) -> None:
        self.values: list[Any] = []

    def print(self, value: Any) -> None:
        self.values.append(value)


def test_issue_missing_resource_enters_context_and_agent_finishes_without_replay(tmp_path: Path) -> None:
    github = CountingGitHubClient()
    harness = AgentHarness(build_capability_layer(github, workspace_root=tmp_path))
    reasoner = IssueFailureReasoner()
    agent = IssueAgent(harness, object(), object(), reasoner=reasoner)  # type: ignore[arg-type]
    context = harness.context(
        "issues",
        "session-issue-888",
        repository="owner/repo",
        goal="查询 issue 888",
        entity_type="issue",
        entity_id="888",
    )

    AgentLoop(harness).start(context, agent)

    assert github.issue_calls == 1
    assert context.finished is True
    assert context.error is None
    assert context.steps == 2
    assert context.final_message == "当前仓库中未找到 Issue #888。"
    assert context.observations == [
        {
            "kind": "capability_error",
            "payload": {
                "capability_id": "github.get_issue",
                "arguments": {"issue_number": 888},
                "error": "resource_not_found",
                "message": "issue not found: 888",
                "details": None,
                "attempts": 1,
            },
        }
    ]
    assert len(reasoner.prompts) == 1
    assert '"issue_number": 888' in reasoner.prompts[0]
    assert '"error": "resource_not_found"' in reasoner.prompts[0]


def test_issue_mutation_failure_returns_to_model_before_deterministic_reproposal(tmp_path: Path) -> None:
    github = MutationFailingGitHubClient()
    harness = AgentHarness(build_capability_layer(github, workspace_root=tmp_path))
    register_github_mutator(harness)
    reasoner = IssueMutationFailureReasoner()
    agent = IssueAgent(harness, object(), object(), reasoner=reasoner)  # type: ignore[arg-type]
    context = harness.context(
        "issues",
        "session-issue-mutation-failure",
        repository="owner/repo",
        goal="给 issue 1 回复",
        entity_type="issue",
        entity_id="1",
    )
    loop = AgentLoop(harness)

    loop.start(context, agent)
    assert context.pending is not None

    loop.resume(
        context,
        agent,
        WorkflowTurnDecision(ApprovalIntent.APPROVE),
    )

    assert github.post_comment_calls == 1, context.observations
    assert context.finished is True
    assert context.error is None
    assert context.final_message == "读取远端评论后仍无法确认首次发布结果，因此未再次提交。"
    errors = [item for item in context.observations if item.get("kind") == "capability_error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["capability_id"] == "github.post_comment"
    assert errors[0]["payload"]["arguments"] == {"issue_number": 1, "body": "reply"}
    assert errors[0]["payload"]["error"] == "execution_uncertain"
    comments = [
        item
        for item in context.observations
        if item.get("kind") == "capability"
        and item.get("payload", {}).get("capability_id") == "github.get_issue_comments"
    ]
    assert len(comments) == 1
    assert reasoner.calls == 3
    assert '"error": "execution_uncertain"' in reasoner.prompts[-1]
    assert '"capability_id": "github.get_issue_comments"' in reasoner.prompts[-1]


def test_pull_request_failure_switches_to_model_recovery_without_replay(tmp_path: Path) -> None:
    github = CountingGitHubClient()
    harness = AgentHarness(build_capability_layer(github, workspace_root=tmp_path))
    reasoner = PullRequestFailureReasoner()
    agent = PullRequestAgent(harness, object(), object(), reasoner=reasoner)  # type: ignore[arg-type]
    context = harness.context(
        "pull_requests",
        "session-pr-888",
        repository="owner/repo",
        goal="查询 PR 888",
        entity_type="pull_request",
        entity_id="888",
    )

    AgentLoop(harness).start(context, agent)

    assert github.pr_calls == 1
    assert github.pr_list_calls == 1
    assert context.finished is True
    assert context.error is None
    assert context.steps == 3
    assert context.final_message == (
        "get_pr(888) 返回 resource_not_found，随后完整 PR 列表也没有 #888，因此当前仓库没有该 PR。"
    )
    assert "请确认编号是否正确" not in context.final_message
    assert len(reasoner.recovery_prompts) == 2
    assert "Entity: Pull Request #888" in reasoner.recovery_prompts[0]
    assert "Preserve an explicitly selected Pull Request as the target" in reasoner.recovery_prompts[0]
    assert '"pr_number": 888' in reasoner.recovery_prompts[0]
    assert '"error": "resource_not_found"' in reasoner.recovery_prompts[0]
    assert '"capability_id": "github.list_pull_requests"' in reasoner.recovery_prompts[1]


def test_repository_failure_switches_to_model_recovery_without_replay(tmp_path: Path) -> None:
    github = CountingGitHubClient()
    harness = AgentHarness(build_capability_layer(github, workspace_root=tmp_path))
    reasoner = RepositoryFailureReasoner()
    agent = RepositoryAgent(harness, object(), object(), reasoner=reasoner)  # type: ignore[arg-type]
    context = harness.context(
        "repository",
        "session-repository-failure",
        repository="owner/repo",
        goal="查看仓库结构",
    )
    agent.prepare(context, RepositoryOperation.EXPLORE)

    AgentLoop(harness).start(context, agent)

    assert github.tree_calls == 1
    assert github.search_calls == 1
    assert context.finished is True
    assert context.error is None
    assert context.steps == 3
    assert context.final_message == (
        "仓库搜索可以执行，但仓库树读取仍是 resource_not_found，因此当前无法给出完整结构内容。"
    )
    assert context.result.answer == context.final_message
    assert len(reasoner.recovery_prompts) == 2
    assert '"depth": 4' in reasoner.recovery_prompts[0]
    assert '"error": "resource_not_found"' in reasoner.recovery_prompts[0]
    assert '"capability_id": "repository.search_code"' in reasoner.recovery_prompts[1]


def test_repository_loop_recovers_from_coding_capability_failure(tmp_path: Path) -> None:
    github = CodingReadFailingGitHubClient()
    harness = AgentHarness(build_capability_layer(github, workspace_root=tmp_path))
    reasoner = RepositoryCodingFailureReasoner()
    coding = CodingAgent(harness, reasoner=reasoner)  # type: ignore[arg-type]
    agent = RepositoryAgent(harness, coding, object(), reasoner=reasoner)  # type: ignore[arg-type]
    context = harness.context(
        "repository",
        "session-repository-coding-failure",
        repository="owner/repo",
        goal="修改 README",
    )
    agent.prepare(context, RepositoryOperation.MODIFY)

    AgentLoop(harness).start(context, agent)

    assert github.default_branch_calls == 1
    assert context.finished is True
    assert context.error is None
    assert context.steps == 2
    assert context.final_message == "Coding 所需仓库读取失败，已停止当前修改。"
    errors = [item for item in context.observations if item.get("kind") == "capability_error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["capability_id"] == "repository.get_default_branch"
    assert errors[0]["payload"]["arguments"] == {}
    assert errors[0]["payload"]["error"] == "resource_not_found"
    assert len(reasoner.recovery_prompts) == 1
    assert '"error": "resource_not_found"' in reasoner.recovery_prompts[0]


def test_live_trace_renders_logical_capability_call_once() -> None:
    console = RecordingConsole()
    ui = TerminalUI(console)  # type: ignore[arg-type]
    common = {
        "timestamp": "2026-08-27T00:00:00+00:00",
        "session_id": "session-1",
        "category": TraceCategory.CAPABILITY,
        "name": "github.get_issue",
    }

    ui.trace(TraceEvent(**common, status=TraceStatus.STARTED, details={"event": "call.started"}))
    ui.trace(TraceEvent(**common, status=TraceStatus.STARTED, details={"event": "attempt.started"}))
    ui.trace(TraceEvent(**common, status=TraceStatus.FAILED, details={"event": "attempt.failed"}))
    ui.trace(TraceEvent(**common, status=TraceStatus.FAILED, details={"event": "call.failed"}))

    assert len(console.values) == 2
