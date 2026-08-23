from __future__ import annotations

import json
from typing import Any

import pytest
from AGENT.GitAgent.gitagent.app.service import GitAgentService
from AGENT.GitAgent.gitagent.core.errors import ToolExecutionError
from AGENT.GitAgent.gitagent.core.models import (
    AccessLevel,
    MutationRejectedResult,
    PullRequestAgentResult,
    PullRequestOperation,
)
from AGENT.GitAgent.gitagent.mcp.memory import InMemoryMCPServer
from AGENT.GitAgent.gitagent.runtime import AgentContext
from AGENT.GitAgent.tests.support import StubMainReasoner, build_test_service, handle, sample_repositories


class RejectingReviewServer(InMemoryMCPServer):
    def post_review(self, repository: str, pr_number: int, event: str, body: str) -> dict[str, Any]:
        del repository, pr_number, event, body
        raise ToolExecutionError(
            "GitHub API failed (422): raw response",
            user_message="GitHub 拒绝了该操作（HTTP 422）：Review Can not approve your own pull request",
        )


class PullRequestReasoner:
    def __init__(
        self,
        operation: PullRequestOperation,
        *,
        review_event: str = "",
        recommendation: str = "APPROVE",
        goal_alignment: str = "ALIGNED",
        reported_blocking_issues: list[str] | None = None,
    ) -> None:
        self.operation = operation
        self.review_event = review_event
        self.recommendation = recommendation
        self.goal_alignment = goal_alignment
        self.reported_blocking_issues = reported_blocking_issues
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

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
        self.calls.append(tool_name)
        self.prompts[tool_name] = prompt
        if tool_name == "select_pull_request_operation":
            return {"operation": self.operation.value, "review_event": self.review_event}
        if tool_name == "explain_code_change":
            return {
                "behavior_changes": ["add now evaluates a generated expression"],
                "key_symbols": ["add"],
                "call_relationships": ["callers of add receive the evaluated result"],
                "impact_scope": ["src/math_utils.py", "all add callers"],
            }
        if tool_name == "review_code_change":
            blocking_issues = (
                list(self.reported_blocking_issues)
                if self.reported_blocking_issues is not None
                else ["src/math_utils.py uses eval for arithmetic"]
                if self.recommendation == "REQUEST_CHANGES"
                else []
            )
            return {
                "summary": "The PR changes addition behavior.",
                "blocking_issues": blocking_issues,
                "impacts": ["add callers"],
                "suggestions": ["return a + b directly"],
                "test_assessment": "The existing addition test should cover the corrected behavior.",
                "risk_level": "HIGH" if blocking_issues else "LOW",
                "recommendation": self.recommendation,
                "goal_alignment": self.goal_alignment,
            }
        if tool_name == "plan_code_change":
            return {
                "direction": "Replace expression evaluation with direct addition.",
                "files": ["src/math_utils.py"],
                "tradeoffs": ["Keeps the change local to add."],
                "tests": ["Run tests/test_math_utils.py."],
            }
        if tool_name in {"prepare_candidate", "repair_candidate"}:
            return {
                "summary": "Use direct addition",
                "root_cause": "The current implementation does not directly add both arguments.",
                "files": {"src/math_utils.py": "def add(a: int, b: int) -> int:\n    return a + b\n"},
                "risks": [],
                "verification_required": ["Run tests/test_math_utils.py"],
            }
        if tool_name == "summarize_review_dialogue":
            return {
                "resolved": ["Naming feedback was addressed."],
                "explained": [],
                "needs_changes": ["Replace eval."],
                "discussion": ["Confirm compatibility expectations."],
                "conflicts": [],
                "reply_draft": "I will replace eval and add regression coverage.",
            }
        if tool_name == "analyze_pull_request_ci":
            return {
                "facts": ["static-check reports Returning Any"],
                "suspected_causes": ["eval returns Any to the type checker"],
                "related_changes": ["src/math_utils.py"],
                "actions": ["replace eval with direct addition and rerun static-check"],
            }
        raise AssertionError(f"unexpected structured call: {tool_name}")

    def complete_text(self, *, system: str, prompt: str) -> str:
        if "GitHub Pull Request review body" in system:
            payload = json.loads(prompt)
            assert payload["instruction"] == "把 review 正文改为英文表述"
            return "The implementation does not match the stated goal. Please restore the module and add a test."
        return "Pull Request evidence summarized."


@pytest.mark.parametrize(
    ("operation", "user_input", "expected_call"),
    [
        (PullRequestOperation.EXPLAIN, "解释 PR #7 改了什么", "explain_code_change"),
        (PullRequestOperation.REVIEW, "审查 PR #7", "review_code_change"),
        (PullRequestOperation.REVIEW_DIALOGUE, "汇总 PR #7 的 Review 对话", "summarize_review_dialogue"),
        (PullRequestOperation.CI_ANALYZE, "分析 PR #7 的 CI", "analyze_pull_request_ci"),
        (PullRequestOperation.PLAN, "给出 PR #7 的修改方案", "plan_code_change"),
    ],
)
def test_pull_request_agent_selects_only_the_requested_capability(operation, user_input, expected_call):
    reasoner = PullRequestReasoner(operation)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)

    result = handle(service, user_input)

    assert result.agent == "pull_requests"
    assert isinstance(result.output, PullRequestAgentResult)
    assert result.output.operation == operation
    assert expected_call in reasoner.calls
    unrelated = {
        "explain_code_change",
        "review_code_change",
        "summarize_review_dialogue",
        "analyze_pull_request_ci",
        "plan_code_change",
        "prepare_candidate",
    } - {expected_call}
    assert unrelated.isdisjoint(reasoner.calls)
    assert not any(
        event.classification in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
        for event in service.harness.audit.events()
    )


def test_ci_reports_unavailable_logs_even_when_reasoner_omits_them():
    reasoner = PullRequestReasoner(PullRequestOperation.CI_ANALYZE)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)
    job = service.harness.server.repositories["sample/widgets"]["workflow_runs"][42]["jobs"][0]
    job.update({"log": "", "log_unavailable": True})

    result = handle(service, "查看 PR #7 的 CI 状态")

    assert isinstance(result.output, PullRequestAgentResult)
    assert result.output.ci_analysis is not None
    assert "workflow run #42：failure" in result.output.ci_analysis["facts"]
    assert "job static-check：failure，日志暂不可用。" in result.output.ci_analysis["facts"]
    assert "workflow run #42：failure" in result.output.answer
    assert "job static-check：failure，日志暂不可用。" in result.output.answer


def test_ci_uses_latest_run_per_workflow():
    reasoner = PullRequestReasoner(PullRequestOperation.CI_ANALYZE)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)
    repository = service.harness.server.repositories["sample/widgets"]
    repository["workflow_runs"][42].update({"workflow_id": 9, "run_number": 1})
    repository["workflow_runs"][43] = {
        "id": 43,
        "workflow_id": 9,
        "run_number": 2,
        "pr_number": 7,
        "status": "completed",
        "conclusion": "success",
        "jobs": [],
    }

    result = handle(service, "查看 PR #7 的 CI 状态")

    assert isinstance(result.output, PullRequestAgentResult)
    assert "workflow run #43：success" in result.output.answer
    assert "workflow run #42" not in result.output.answer
    assert "github.get_job_logs" not in {
        event.name for event in service.harness.trace.events(service.session_scope.session_id)
    }


def test_explanation_reads_diff_but_not_reviews_or_ci():
    reasoner = PullRequestReasoner(PullRequestOperation.EXPLAIN)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)

    handle(service, "解释 PR #7 改了什么")

    tool_names = {event.name for event in service.harness.trace.events(service.session_scope.session_id)}
    assert {"github.get_pr", "repository.get_changed_files", "repository.get_pr_diff"}.issubset(tool_names)
    assert "github.get_pr_reviews" not in tool_names
    assert "github.get_workflow_runs" not in tool_names


def test_missing_pull_request_returns_a_normal_agent_reply():
    reasoner = PullRequestReasoner(PullRequestOperation.GET)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)

    result = handle(service, "查看 PR #999999")

    assert result.agent == "pull_requests"
    assert isinstance(result.output, PullRequestAgentResult)
    assert result.output.operation == PullRequestOperation.GET
    assert result.output.pr_number == 999999
    assert result.output.pull_requests == []
    assert "未找到 PR #999999" in result.output.answer
    agent_events = [
        event
        for event in service.harness.trace.events(service.session_scope.session_id)
        if event.name == "pull_requests"
    ]
    assert agent_events[-1].status.value == "completed"


def test_multi_pr_entity_is_rejected_without_persisting_a_waiting_context():
    main = StubMainReasoner(
        [
            {
                "target_agent": "pull_requests",
                "entity_type": "pull_request",
                "entity_id": "12,13",
                "request": "比较 PR #12 和 PR #13",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            }
        ]
    )
    service = build_test_service(main_reasoner=main)

    result = handle(service, "比较 PR #12 和 PR #13")

    assert result.agent is None
    assert "多 PR 对比尚未实现" in result.output
    saved = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert saved is not None and saved.agent_context == {}


def test_pr_number_question_binds_a_concise_followup_and_completes():
    main = StubMainReasoner(
        [
            {
                "target_agent": "pull_requests",
                "entity_type": "pull_request",
                "entity_id": "",
                "request": "查看一个 Pull Request",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            }
        ]
    )
    reasoner = PullRequestReasoner(PullRequestOperation.GET)
    service = build_test_service(main_reasoner=main, agent_reasoner=reasoner)

    waiting = handle(service, "查看一个 Pull Request")
    completed = handle(service, "7")

    assert isinstance(waiting.output, AgentContext)
    assert waiting.output.question == "请指定 Pull Request 编号。"
    assert isinstance(completed.output, PullRequestAgentResult)
    assert completed.output.pr_number == 7
    assert completed.output.pull_requests[0].number == 7
    saved = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert saved is not None and saved.agent_context == {}


def test_pr_number_followup_recovers_an_existing_composite_waiting_context():
    main = StubMainReasoner(
        [
            {
                "target_agent": "pull_requests",
                "entity_type": "pull_request",
                "entity_id": "",
                "request": "查看一个 Pull Request",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            }
        ]
    )
    reasoner = PullRequestReasoner(PullRequestOperation.GET)
    service = build_test_service(main_reasoner=main, agent_reasoner=reasoner)
    handle(service, "查看一个 Pull Request")
    saved = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert saved is not None
    invalid_context = dict(saved.agent_context)
    invalid_context["entity_id"] = "12,13"
    service._test_sessions.save_agent_context(service.session_scope, invalid_context)

    completed = handle(service, "7")

    assert isinstance(completed.output, PullRequestAgentResult)
    assert completed.output.pr_number == 7


def test_new_pr_command_escapes_an_old_number_question_and_is_routed_again():
    main = StubMainReasoner(
        [
            {
                "target_agent": "pull_requests",
                "entity_type": "pull_request",
                "entity_id": "",
                "request": "查看一个 Pull Request",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            },
            {
                "target_agent": "pull_requests",
                "entity_type": "pull_request",
                "entity_id": "3",
                "request": "查看 PR #3",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            },
        ]
    )
    reasoner = PullRequestReasoner(PullRequestOperation.GET)
    service = build_test_service(main_reasoner=main, agent_reasoner=reasoner)

    waiting = handle(service, "查看一个 Pull Request")
    completed = handle(service, "查看 PR #3")

    assert isinstance(waiting.output, AgentContext)
    assert isinstance(completed.output, PullRequestAgentResult)
    assert completed.output.pr_number == 3
    assert len(main.prompts) == 2


def test_ci_fix_combines_analysis_plan_candidate_and_waits_before_applying():
    reasoner = PullRequestReasoner(PullRequestOperation.CI_FIX)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)
    repository = service.harness.server.repositories["sample/widgets"]

    proposal = handle(service, "修复 workflow #42 的 CI 失败")

    assert proposal.agent == "pull_requests"
    assert isinstance(proposal.output, AgentContext)
    assert proposal.output.agent == "pull_requests"
    assert proposal.output.operation == PullRequestOperation.CI_FIX.value
    assert proposal.output.pending is not None
    assert proposal.output.pending.calls[0].tool == "github.commit"
    assert proposal.output.code_candidate is not None
    assert proposal.output.verification is not None and proposal.output.verification.passed
    assert repository["branches"]["expression-add"]["commits"] == []
    assert {
        "analyze_pull_request_ci",
        "plan_code_change",
        "prepare_candidate",
    }.issubset(reasoner.calls)
    assert "src/math_utils.py" in reasoner.prompts["analyze_pull_request_ci"]

    completed = handle(service, "可以")

    assert isinstance(completed.output, PullRequestAgentResult)
    assert completed.output.operation == PullRequestOperation.CI_FIX
    assert completed.output.execution_result is not None
    assert repository["branches"]["expression-add"]["commits"]


def test_pending_pr_review_revision_supersedes_the_old_proposal_and_waits_for_new_approval():
    reasoner = PullRequestReasoner(
        PullRequestOperation.POST_REVIEW,
        review_event="REQUEST_CHANGES",
        recommendation="REQUEST_CHANGES",
    )
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)
    repository = service.harness.server.repositories["sample/widgets"]

    proposal = handle(service, "发布 PR #7 的 REQUEST_CHANGES Review")
    assert isinstance(proposal.output, AgentContext)
    old_approval_id = proposal.output.pending.approval_id
    old_body = proposal.output.pending.calls[0].arguments["body"]

    revised = handle(service, "把 review 正文改为英文表述")

    assert isinstance(revised.output, AgentContext)
    assert revised.output.pending is not None
    assert revised.output.pending.approval_id != old_approval_id
    revised_call = revised.output.pending.calls[0]
    assert revised_call.tool == "github.post_review"
    assert revised_call.arguments["event"] == "REQUEST_CHANGES"
    assert revised_call.arguments["body"] != old_body
    assert revised_call.arguments["body"].startswith("The implementation")
    assert service.harness.approvals.get(old_approval_id).decision != "Approve"
    assert not any(observation["kind"] == "rejection" for observation in revised.output.observations)
    assert repository["reviews"] == []

    completed = handle(service, "可以")

    assert isinstance(completed.output, PullRequestAgentResult)
    assert repository["reviews"][-1]["body"] == revised_call.arguments["body"]


def test_approve_review_and_merge_are_distinct_confirmed_mutations():
    approve_reasoner = PullRequestReasoner(PullRequestOperation.POST_REVIEW, review_event="APPROVE")
    approve_service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=approve_reasoner)
    approve_repo = approve_service.harness.server.repositories["sample/widgets"]

    approve_proposal = handle(approve_service, "批准 PR #7")

    assert isinstance(approve_proposal.output, AgentContext)
    assert approve_proposal.output.pending.calls[0].tool == "github.post_review"
    assert approve_proposal.output.pending.calls[0].arguments["event"] == "APPROVE"
    assert approve_repo["reviews"] == []
    handle(approve_service, "可以")
    assert approve_repo["reviews"][-1]["event"] == "APPROVE"
    assert approve_repo["prs"][7]["state"] == "open"

    merge_reasoner = PullRequestReasoner(PullRequestOperation.MERGE)
    merge_service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=merge_reasoner)
    merge_repo = merge_service.harness.server.repositories["sample/widgets"]
    merge_repo.setdefault("reviews", []).extend(
        [
            {
                "id": 101,
                "pr_number": 3,
                "state": "CHANGES_REQUESTED",
                "body": "Please revise",
                "submitted_at": "2026-08-11T01:00:00Z",
                "user": {"login": "reviewer"},
            },
            {
                "id": 102,
                "pr_number": 3,
                "state": "APPROVED",
                "body": "Ready",
                "submitted_at": "2026-08-11T02:00:00Z",
                "user": {"login": "reviewer"},
            },
            {
                "id": 103,
                "pr_number": 3,
                "state": "PENDING",
                "submitted_at": "2026-08-11T03:00:00Z",
                "user": {"login": "another-reviewer"},
            },
        ]
    )
    merge_repo["workflow_runs"][44] = {
        "id": 44,
        "workflow_id": 9,
        "run_number": 1,
        "pr_number": 3,
        "status": "completed",
        "conclusion": "failure",
        "jobs": [],
    }
    merge_repo["workflow_runs"][43] = {
        "id": 43,
        "workflow_id": 9,
        "run_number": 2,
        "pr_number": 3,
        "status": "completed",
        "conclusion": "success",
        "jobs": [],
    }

    merge_proposal = handle(merge_service, "合并 PR #3")

    assert isinstance(merge_proposal.output, AgentContext)
    assert merge_proposal.output.pending.calls[0].tool == "github.merge"
    assert merge_repo["prs"][3]["state"] == "open"
    handle(merge_service, "可以")
    assert merge_repo["prs"][3]["merged"] is True


def test_review_api_failure_returns_reason_without_leaving_a_retryable_workflow():
    reasoner = PullRequestReasoner(PullRequestOperation.POST_REVIEW, review_event="APPROVE")
    service = build_test_service(
        main_reasoner=StubMainReasoner(),
        agent_reasoner=reasoner,
        server=RejectingReviewServer(sample_repositories()),
    )

    proposal = handle(service, "批准 PR #7")
    assert isinstance(proposal.output, AgentContext)
    assert proposal.output.pending is not None

    failed = handle(service, "可以")

    assert isinstance(failed.output, MutationRejectedResult)
    assert failed.output.summary == "发布 APPROVE Review 到 PR #7"
    assert failed.output.reason == (
        "GitHub 拒绝了该操作（HTTP 422）：Review Can not approve your own pull request"
    )
    assert "raw response" not in failed.output.reason
    session = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert session is not None
    assert session.agent_context == {}

    next_turn = handle(service, "你好")
    assert next_turn.agent is None
    assert next_turn.output == "你好，我可以帮你处理这个仓库。"


def test_merge_request_stops_when_readiness_has_blockers():
    reasoner = PullRequestReasoner(
        PullRequestOperation.MERGE,
        recommendation="REQUEST_CHANGES",
        goal_alignment="MISMATCH",
    )
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)

    result = handle(service, "合并 PR #7")

    assert isinstance(result.output, PullRequestAgentResult)
    assert result.output.merge_readiness == "需要继续修改"
    assert "github.merge" not in {event.name for event in service.harness.trace.events(service.session_scope.session_id)}


def test_latest_request_changes_supersedes_older_approval():
    reasoner = PullRequestReasoner(PullRequestOperation.MERGE)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)
    repository = service.harness.server.repositories["sample/widgets"]
    repository.setdefault("reviews", []).extend(
        [
            {
                "id": 201,
                "pr_number": 3,
                "state": "APPROVED",
                "submitted_at": "2026-08-11T01:00:00Z",
                "user": {"login": "reviewer"},
            },
            {
                "id": 202,
                "pr_number": 3,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-08-11T02:00:00Z",
                "user": {"login": "reviewer"},
            },
        ]
    )
    repository["workflow_runs"][43] = {
        "id": 43,
        "workflow_id": 9,
        "run_number": 2,
        "pr_number": 3,
        "status": "completed",
        "conclusion": "success",
        "jobs": [],
    }

    result = handle(service, "合并 PR #3")

    assert isinstance(result.output, PullRequestAgentResult)
    assert result.output.merge_readiness == "需要继续修改"
    assert "已有 Review 要求修改。" in result.output.answer
    assert "尚未观察到 APPROVE Review。" not in result.output.answer
    assert "github.merge" not in {
        event.name for event in service.harness.trace.events(service.session_scope.session_id)
    }


def test_merge_readiness_reports_approval_without_promoting_non_blocking_notes():
    reasoner = PullRequestReasoner(
        PullRequestOperation.MERGE_READINESS,
        recommendation="APPROVE",
        reported_blocking_issues=[
            "无阻塞性问题。变更仅涉及 Markdown 文档。",
            "缺少关联链接（低优先级，非阻塞）。",
        ],
    )
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)
    repository = service.harness.server.repositories["sample/widgets"]
    repository.setdefault("reviews", []).append(
        {
            "id": 301,
            "pr_number": 3,
            "state": "APPROVED",
            "submitted_at": "2026-08-11T02:00:00Z",
            "user": {"login": "reviewer"},
        }
    )
    repository["workflow_runs"][43] = {
        "id": 43,
        "workflow_id": 9,
        "run_number": 2,
        "pr_number": 3,
        "status": "completed",
        "conclusion": "success",
        "jobs": [],
    }

    result = handle(service, "你是否看到了 PR #3 的批准？")

    assert isinstance(result.output, PullRequestAgentResult)
    assert result.output.merge_readiness == "准备合并"
    assert "已满足条件" in result.output.answer
    assert "已观察到有效的 APPROVE Review。" in result.output.answer
    assert "阻塞项\n- 无" in result.output.answer
    assert "无阻塞性问题。变更仅涉及 Markdown 文档。" not in result.output.answer
    assert "缺少关联链接（低优先级，非阻塞）。" not in result.output.answer
    assert "你是否看到了" not in reasoner.prompts["review_code_change"]
    assert "GitHub approvals" in reasoner.prompts["review_code_change"]


def test_fork_pull_request_returns_candidate_diff_without_apply_proposal():
    reasoner = PullRequestReasoner(PullRequestOperation.MODIFY)
    service = build_test_service(main_reasoner=StubMainReasoner(), agent_reasoner=reasoner)
    repository = service.harness.server.repositories["sample/widgets"]
    repository["prs"][7]["head"]["repo"] = {"full_name": "contributor/widgets"}

    result = handle(service, "修复 PR #7 中的实现")

    assert isinstance(result.output, PullRequestAgentResult)
    assert result.output.candidate is not None
    assert "Fork Pull Request" in result.output.answer
    assert repository["branches"]["expression-add"]["commits"] == []
    assert not any(event.name == "github.commit" for event in service.harness.trace.events(service.session_scope.session_id))


def test_main_redirects_pr_scoped_code_route_to_pull_requests():
    reasoner = PullRequestReasoner(PullRequestOperation.MODIFY)
    main = StubMainReasoner(
        [
            {
                "target_agent": "code_change",
                "entity_type": "pull_request",
                "entity_id": "7",
                "request": "Fix the implementation in PR #7",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            }
        ]
    )
    service = build_test_service(main_reasoner=main, agent_reasoner=reasoner)

    result = handle(service, "Fix the implementation in PR #7")

    assert result.agent == "pull_requests"
    assert isinstance(result.output, AgentContext)
    assert result.output.agent == "pull_requests"


def test_pending_pr_review_restores_as_the_same_parent_context():
    reasoner = PullRequestReasoner(PullRequestOperation.POST_REVIEW, review_event="COMMENT")
    main = StubMainReasoner()
    service = build_test_service(main_reasoner=main, agent_reasoner=reasoner)
    proposal = handle(service, "发布 PR #7 的 Review 评论")
    assert isinstance(proposal.output, AgentContext)

    saved = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert saved is not None
    assert saved.agent_context["agent"] == "pull_requests"
    assert saved.agent_context["operation"] == PullRequestOperation.POST_REVIEW.value

    restored = GitAgentService(
        service.harness.server,
        main_reasoner=main,
        agent_reasoner=reasoner,
        session_manager=service._test_sessions,
        session_scope=service.session_scope,
    )
    routing = service._test_context_builder.build(
        service.session_scope,
        "sample/widgets",
        "可以",
        prompt_renderer=lambda context: restored.main_agent.render_input_context(
            "可以", "sample/widgets", context
        ),
    )

    completed = restored.handle(
        "可以",
        repository="sample/widgets",
        routing_context=routing,
        session_scope=service.session_scope,
    )

    assert completed.agent == "pull_requests"
    assert isinstance(completed.output, PullRequestAgentResult)
    assert completed.output.operation == PullRequestOperation.POST_REVIEW
    assert service.harness.server.repositories["sample/widgets"]["reviews"][-1]["event"] == "COMMENT"
