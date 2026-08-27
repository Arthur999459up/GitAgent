"""Intent-driven Pull Request domain orchestration."""

from __future__ import annotations

import json
import re
from typing import Any

from gitagent.agent_loop import AgentAction, AgentActionKind, rejection_feedback
from gitagent.domain.errors import LLMProviderError, ValidationError, WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    DomainAction,
    PullRequestAgentResult,
    PullRequestOperation,
    PullRequestSummary,
    Recommendation,
    Route,
    VerificationReport,
    to_plain,
)
from gitagent.domain.reviews import canonical_review_event, effective_review_events
from gitagent.harness.context import (
    capability_attempted,
    capability_failure_observed,
    render_context_observations,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.recovery.github_mutations import code_change_review_package
from gitagent.harness.validation.static import StaticVerifier
from gitagent.infra.observability.trace import TraceCategory, TraceStatus
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .coding import (
    CodingAgent,
    prepare_verified_candidate,
    record_candidate_capability_error,
)
from .decide import AGENT_ACTION_SCHEMA, parse_action
from .guidance import guidance_section

_PROMPTS = get_prompt_library()
_PR_NUMBER_QUESTION_FRAGMENT = "Pull Request 编号"
_PR_NUMBER_REPLY = re.compile(
    r"\s*(?:(?:PR|PULL\s+REQUEST)\s*)?#?\s*([1-9][0-9]*)\s*[。.]?\s*",
    re.IGNORECASE,
)
_MERGE_CODE_REVIEW_REQUEST = (
    "Review the supplied Pull Request change for concrete code or content defects that must be fixed before merge. "
    "Do not assess GitHub approvals, CI status, branch protection, or operational instructions in the PR body; "
    "the Pull Request agent evaluates those gates separately."
)

_OPERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": [item.value for item in PullRequestOperation]},
        "review_event": {"type": "string", "enum": ["", "COMMENT", "APPROVE", "REQUEST_CHANGES"]},
    },
    "required": ["operation", "review_event"],
    "additionalProperties": False,
}

_DIALOGUE_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved": {"type": "array", "items": {"type": "string"}},
        "explained": {"type": "array", "items": {"type": "string"}},
        "needs_changes": {"type": "array", "items": {"type": "string"}},
        "discussion": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "reply_draft": {"type": "string"},
    },
    "required": ["resolved", "explained", "needs_changes", "discussion", "conflicts", "reply_draft"],
    "additionalProperties": False,
}

_CI_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {"type": "string"}},
        "suspected_causes": {"type": "array", "items": {"type": "string"}},
        "related_changes": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["facts", "suspected_causes", "related_changes", "actions"],
    "additionalProperties": False,
}

PULL_REQUEST_AGENT_SPEC = AgentSpec(
    name="pull_requests",
    role=(
        "Own Pull Request browsing, change explanation, review, review dialogue, CI analysis, "
        "targeted code improvement, review publication, merge readiness, and merge orchestration."
    ),
    system_prompt=_PROMPTS.text("system.pull_requests"),
    output_schema=(
        "action",
        "operation",
        "answer",
        "pull_requests",
        "pr_number",
        "requested_outcome",
        "changed_files",
        "interpretation",
        "review",
        "review_dialogue",
        "ci_analysis",
        "plan",
        "candidate",
        "verification",
        "merge_readiness",
        "execution_result",
        "question",
    ),
    routes=frozenset({Route.PULL_REQUEST}),
    required_context=("repository",),
    routing_examples=(
        "看看有哪些开放的 PR",
        "解释 PR #17 改了什么",
        "审阅 PR #17",
        "汇总 PR #17 的 Review 对话",
        "分析 PR #17 的 CI 失败",
        "给出 PR #17 的修改方案",
        "修复 PR #17 的问题",
        "批准 PR #17",
        "合并 PR #17",
    ),
)


class PullRequestAgent:
    def __init__(
        self,
        harness: AgentHarness,
        coding: CodingAgent,
        verifier: StaticVerifier,
        reasoner: Reasoner | None = None,
    ) -> None:
        self.harness = harness
        self.coding = coding
        self.verifier = verifier
        self.reasoner = reasoner
        harness.register(PULL_REQUEST_AGENT_SPEC)

    def accept_question_reply(self, context: AgentContext, reply: str) -> bool:
        """Bind a concise PR-number answer, or decline so the turn can be rerouted."""

        if self._pr_number(context) is not None or _PR_NUMBER_QUESTION_FRAGMENT not in context.question:
            return True
        match = _PR_NUMBER_REPLY.fullmatch(reply)
        if match is None:
            return False
        context.entity_type = "pull_request"
        context.entity_id = match.group(1)
        return True

    def decide(self, context: AgentContext) -> AgentAction:
        if not context.operation:
            self._select_operation(context)
        if rejection_feedback(context) is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="已取消待确认操作",
                message="已按你的要求取消，未执行 Pull Request 写操作。",
            )
        if self.reasoner is not None and capability_failure_observed(context):
            return self._llm_decide_after_failure(context)

        operation = PullRequestOperation(context.operation)
        if operation in {PullRequestOperation.LIST, PullRequestOperation.SEARCH, PullRequestOperation.SUMMARIZE}:
            return self._list_step(context)
        if operation == PullRequestOperation.GET:
            return self._get_step(context)
        if operation == PullRequestOperation.EXPLAIN:
            return self._explain_step(context)
        if operation == PullRequestOperation.REVIEW:
            return self._review_step(context)
        if operation == PullRequestOperation.REVIEW_DIALOGUE:
            return self._dialogue_step(context)
        if operation == PullRequestOperation.CI_ANALYZE:
            return self._ci_step(context, fix=False)
        if operation == PullRequestOperation.PLAN:
            return self._plan_step(context)
        if operation == PullRequestOperation.MODIFY:
            return self._modify_step(context, ci_fix=False)
        if operation == PullRequestOperation.CI_FIX:
            return self._ci_step(context, fix=True)
        if operation == PullRequestOperation.POST_REVIEW:
            return self._post_review_step(context)
        if operation in {PullRequestOperation.MERGE_READINESS, PullRequestOperation.MERGE}:
            return self._merge_step(context, execute=operation == PullRequestOperation.MERGE)
        raise WorkflowError(f"unsupported Pull Request operation: {operation.value}")

    def build_result(self, context: AgentContext) -> PullRequestAgentResult:
        operation = PullRequestOperation(context.operation) if context.operation else None
        pull_request = self._last_capability(context, "github.get_pr")
        raw_list = self._last_capability(context, "github.list_pull_requests")
        changed_files = self._changed_files(context)
        pull_requests: list[PullRequestSummary] = []
        if raw_list is not None:
            pull_requests = [self._summary(item) for item in raw_list.get("pull_requests", [])]
        elif pull_request is not None:
            pull_requests = [self._summary(pull_request)]

        explanation = self._explanation(context)
        review = self._review(context)
        dialogue = self._domain_data(context, "review_dialogue")
        ci_analysis = self._domain_data(context, "ci_analysis")
        plan = self._plan(context)
        readiness = self._domain_data(context, "merge_readiness")
        execution = self._last_write(context)
        answer = context.final_message
        if not answer and operation in {PullRequestOperation.LIST, PullRequestOperation.SEARCH, PullRequestOperation.SUMMARIZE}:
            answer = self._list_answer(context, (raw_list or {}).get("pull_requests", []), pull_requests)
        if not answer and pull_request is not None:
            comments = (self._last_capability(context, "github.get_pr_comments") or {}).get("comments", [])
            diff = (self._last_capability(context, "repository.get_pr_diff") or {}).get("diff", "")
            answer = self._detail_answer(context, pull_request, comments, changed_files, diff)
        if not answer:
            answer = "Pull Request 请求已处理。"

        return PullRequestAgentResult(
            action=DomainAction.ANSWER,
            operation=operation,
            answer=answer,
            pull_requests=pull_requests,
            pr_number=self._pr_number(context),
            requested_outcome=context.requested_outcome or None,
            changed_files=changed_files,
            interpretation=explanation,
            review=review,
            review_dialogue=dialogue,
            ci_analysis=ci_analysis,
            plan=plan,
            candidate=context.code_candidate,
            verification=context.verification,
            merge_readiness=str((readiness or {}).get("status", "")),
            execution_result=execution,
        )

    def _select_operation(self, context: AgentContext) -> None:
        if self.reasoner is None:
            context.operation = (
                PullRequestOperation.CI_ANALYZE.value
                if context.entity_type == "workflow_run"
                else PullRequestOperation.GET.value
                if self._entity_number(context) is not None
                else PullRequestOperation.LIST.value
            )
            return
        try:
            value = self.reasoner.complete_structured(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.pull_request_decide",
                    goal=context.goal,
                    entity=(
                        f"{context.entity_type} #{context.entity_id}"
                        if context.entity_id
                        else "no concrete Pull Request or workflow run selected"
                    ),
                    guidance=guidance_section(context.guidance),
                ),
                schema=_OPERATION_SCHEMA,
                tool_name="select_pull_request_operation",
            )
            operation = PullRequestOperation(str(value.get("operation", "")))
            review_event = str(value.get("review_event", ""))
        except (LLMProviderError, ValidationError, ValueError) as exc:
            self.harness.trace.emit(
                session_id=context.session_id,
                category=TraceCategory.AGENT,
                name="pull_requests.select_operation",
                status=TraceStatus.PROGRESS,
                message=f"operation selection failed; using entity fallback ({type(exc).__name__}: {exc})",
            )
            operation = (
                PullRequestOperation.CI_ANALYZE
                if context.entity_type == "workflow_run"
                else PullRequestOperation.GET
                if self._entity_number(context) is not None
                else PullRequestOperation.LIST
            )
            review_event = ""
        context.operation = operation.value
        context.requested_outcome = review_event

    def _llm_decide_after_failure(self, context: AgentContext) -> AgentAction:
        if self.reasoner is None:
            raise WorkflowError("capability failure recovery requires a reasoner")
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.pull_request_recovery_decide",
                goal=context.goal,
                repository=context.repository,
                entity=(
                    f"Pull Request #{context.entity_id}"
                    if context.entity_id
                    else "no concrete Pull Request selected"
                ),
                operation=context.operation or "",
                budget=str(max(0, context.max_steps - context.steps)),
                guidance=guidance_section(context.guidance),
                observations=render_context_observations(context),
            ),
            schema=AGENT_ACTION_SCHEMA,
            tool_name="decide_action",
            tools=self.harness.llm_tools(context),
        )
        if value.get("kind") == "capability":
            value["capability_id"] = self.harness.resolve_llm_name(
                str(value.get("capability_id", "")),
                context,
            )
        return parse_action(value, requires_candidate=False)

    def _list_step(self, context: AgentContext) -> AgentAction:
        if self._last_capability(context, "github.list_pull_requests") is None:
            return self._capability("github.list_pull_requests", {"state": "open", "limit": 20}, "列出 Pull Requests")
        return AgentAction(AgentActionKind.FINISH, summary="Pull Requests 已列出")

    def _get_step(self, context: AgentContext) -> AgentAction:
        required = self._pr_metadata_step(context)
        if required is not None:
            return required
        return AgentAction(AgentActionKind.FINISH, summary="Pull Request 已读取")

    def _explain_step(self, context: AgentContext) -> AgentAction:
        required = self._pr_code_evidence_step(context)
        if required is not None:
            return required
        explanation = self._ensure_explanation(context)
        return AgentAction(
            AgentActionKind.FINISH,
            summary="Pull Request 变更已解读",
            message=self._format_explanation(explanation),
        )

    def _review_step(self, context: AgentContext) -> AgentAction:
        required = self._pr_code_evidence_step(context)
        if required is not None:
            return required
        review = self._ensure_review(context, context.goal)
        return AgentAction(
            AgentActionKind.FINISH,
            summary="Pull Request 审阅完成",
            message=self._format_review(review),
        )

    def _dialogue_step(self, context: AgentContext) -> AgentAction:
        required = self._pr_code_evidence_step(context, comments=True, reviews=True)
        if required is not None:
            return required
        dialogue = self._domain_data(context, "review_dialogue")
        if dialogue is None:
            dialogue = self._build_review_dialogue(context)
            self._record_domain(context, "review_dialogue", dialogue)
        return AgentAction(
            AgentActionKind.FINISH,
            summary="Review 对话已汇总",
            message=self._format_dialogue(dialogue),
        )

    def _ci_step(self, context: AgentContext, *, fix: bool) -> AgentAction:
        required = self._ci_evidence_step(context)
        if required is not None:
            return required
        analysis = self._domain_data(context, "ci_analysis")
        if analysis is None:
            analysis = self._build_ci_analysis(context)
            self._record_domain(context, "ci_analysis", analysis)
        if fix:
            return self._modify_step(context, ci_fix=True)
        return AgentAction(
            AgentActionKind.FINISH,
            summary="CI 分析完成",
            message=self._format_ci(analysis),
        )

    def _plan_step(self, context: AgentContext) -> AgentAction:
        required = self._pr_code_evidence_step(context)
        if required is not None:
            return required
        plan = self._ensure_plan(context)
        return AgentAction(
            AgentActionKind.FINISH,
            summary="修改方案已生成",
            message=self._format_plan(plan),
        )

    def _modify_step(self, context: AgentContext, *, ci_fix: bool) -> AgentAction:
        if not ci_fix:
            required = self._pr_code_evidence_step(context)
            if required is not None:
                return required
        pull_request = self._last_capability(context, "github.get_pr")
        if pull_request is None:
            return AgentAction(AgentActionKind.ASK, question="请指定需要完善的 Pull Request 编号。")
        applied = self._last_capability(context, "github.commit")
        if applied is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="候选改动已应用",
                message=f"候选改动已写入 PR 分支 `{applied.get('branch', '')}`，涉及：{', '.join(applied.get('files', []))}。",
            )
        preparation = self._ensure_candidate(context, pull_request)
        if isinstance(preparation, AgentAction):
            return preparation
        if preparation:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="模型未生成文件内容",
                message=preparation,
            )
        if self._is_fork(context.repository, pull_request):
            candidate = context.code_candidate
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Fork PR 候选改动已生成",
                message=(
                    "这是 Fork Pull Request，未生成自动写入提案。请在来源仓库应用以下候选 Diff：\n\n"
                    + (candidate.patch if candidate is not None else "")
                ),
            )
        candidate = context.code_candidate
        if candidate is None:
            raise WorkflowError("Pull Request candidate generation returned no patch")
        branch = self._branch(pull_request.get("head"))
        if not branch:
            raise WorkflowError("Pull Request head branch is missing")
        return self._capability(
            "github.commit",
            {
                "branch": branch,
                "files": candidate.files,
                "deleted_files": candidate.deleted_files,
                "message": candidate.summary,
            },
            self._candidate_approval_summary(candidate, context.verification),
        )

    def _post_review_step(self, context: AgentContext) -> AgentAction:
        required = self._pr_code_evidence_step(context, comments=True, reviews=True)
        if required is not None:
            return required
        published = self._last_capability(context, "github.post_review")
        if published is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Review 已发布",
                message=f"已发布 {published.get('event', context.requested_outcome or 'COMMENT')} Review。",
            )
        review = self._ensure_review(context, context.goal)
        event = context.requested_outcome or "COMMENT"
        context.requested_outcome = event
        return self._capability(
            "github.post_review",
            {
                "pr_number": self._require_pr_number(context),
                "event": event,
                "body": self._review_body(review),
            },
            f"发布 {event} Review 到 PR #{self._require_pr_number(context)}",
        )

    def _merge_step(self, context: AgentContext, *, execute: bool) -> AgentAction:
        required = self._pr_code_evidence_step(context, reviews=True, ci=True)
        if required is not None:
            return required
        review = self._ensure_review(context, _MERGE_CODE_REVIEW_REQUEST)
        readiness = self._domain_data(context, "merge_readiness")
        if readiness is None:
            readiness = self._assess_merge_readiness(context, review)
            self._record_domain(context, "merge_readiness", readiness)
        if not execute:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="合并准备度已评估",
                message=self._format_readiness(readiness),
            )
        merged = self._last_capability(context, "github.merge")
        if merged is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Pull Request 已合并",
                message=f"PR #{merged.get('pr_number')} 已按确认结果合并。",
            )
        if readiness.get("status") != "准备合并":
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Pull Request 尚未满足合并条件",
                message=self._format_readiness(readiness),
            )
        pull_request = self._last_capability(context, "github.get_pr") or {}
        head = pull_request.get("head") or {}
        expected_sha = str(head.get("sha") or "") if isinstance(head, dict) else ""
        if not expected_sha:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="缺少可确认的 PR head SHA",
                message="当前证据缺少 PR head SHA，不能形成合并提案。",
            )
        return self._capability(
            "github.merge",
            {"pr_number": self._require_pr_number(context), "expected_head_sha": expected_sha},
            f"合并 PR #{self._require_pr_number(context)}（head {expected_sha}）",
        )

    def _pr_metadata_step(self, context: AgentContext) -> AgentAction | None:
        pr_number = self._pr_number(context)
        if pr_number is None:
            return AgentAction(AgentActionKind.ASK, question="请指定 Pull Request 编号。")
        arguments = {"pr_number": pr_number}
        if not capability_attempted(context, "github.get_pr", arguments=arguments):
            return self._capability("github.get_pr", arguments, "读取 Pull Request")
        return None

    def _pr_code_evidence_step(
        self,
        context: AgentContext,
        *,
        comments: bool = False,
        reviews: bool = False,
        ci: bool = False,
    ) -> AgentAction | None:
        required = self._pr_metadata_step(context)
        if required is not None:
            return required
        pr_number = self._require_pr_number(context)
        changed_files_arguments = {"pr_number": pr_number}
        if not capability_attempted(
            context,
            "repository.get_changed_files",
            arguments=changed_files_arguments,
        ):
            return self._capability(
                "repository.get_changed_files",
                changed_files_arguments,
                "读取 Pull Request 变更文件",
            )
        diff_arguments = {"pr_number": pr_number}
        if not capability_attempted(context, "repository.get_pr_diff", arguments=diff_arguments):
            return self._capability("repository.get_pr_diff", diff_arguments, "读取 Pull Request Diff")
        tree_arguments = {
            "depth": 4,
            "ref": self._head_ref(self._last_capability(context, "github.get_pr") or {}),
        }
        if not capability_attempted(context, "repository.get_repo_tree", arguments=tree_arguments):
            return self._capability(
                "repository.get_repo_tree",
                tree_arguments,
                "读取 PR head 代码树",
            )
        readable = self._readable_changed_files(context)
        read_arguments = {
            "requests": [{"path": path, "limit": 180} for path in readable[:12]],
            "ref": self._head_ref(self._last_capability(context, "github.get_pr") or {}),
        }
        if readable and not capability_attempted(context, "repository.read_files", arguments=read_arguments):
            return self._capability(
                "repository.read_files",
                read_arguments,
                "读取与本次变更相关的代码",
            )
        comments_arguments = {"pr_number": pr_number}
        if comments and not capability_attempted(
            context,
            "github.get_pr_comments",
            arguments=comments_arguments,
        ):
            return self._capability("github.get_pr_comments", comments_arguments, "读取 Pull Request 评论")
        reviews_arguments = {"pr_number": pr_number}
        if reviews and not capability_attempted(
            context,
            "github.get_pr_reviews",
            arguments=reviews_arguments,
        ):
            return self._capability("github.get_pr_reviews", reviews_arguments, "读取 Pull Request Reviews")
        ci_arguments = {"pr_number": pr_number}
        if ci and not capability_attempted(
            context,
            "github.get_workflow_runs",
            arguments=ci_arguments,
        ):
            return self._capability("github.get_workflow_runs", ci_arguments, "读取 Pull Request CI 状态")
        return None

    def _ci_evidence_step(self, context: AgentContext) -> AgentAction | None:
        run_arguments: dict[str, Any] = {}
        if self._workflow_run_id(context) is not None:
            run_arguments["workflow_run_id"] = self._workflow_run_id(context)
        elif self._entity_number(context) is not None:
            run_arguments["pr_number"] = self._entity_number(context)
        if not capability_attempted(context, "github.get_workflow_runs", arguments=run_arguments):
            return self._capability("github.get_workflow_runs", run_arguments, "读取 CI workflow runs")
        runs_result = self._last_capability(context, "github.get_workflow_runs")
        if runs_result is None:
            return None
        for run in self._current_workflow_runs(runs_result.get("runs", [])):
            if self._failed(run) and not self._has_capability_arguments(
                context,
                "github.get_job_logs",
                run_id=int(run["id"]),
            ):
                return self._capability(
                    "github.get_job_logs",
                    {"run_id": int(run["id"])},
                    f"读取失败 workflow run #{run.get('id')} 的 job 日志",
                )
        pr_number = self._pr_number(context)
        if pr_number is None:
            return None
        pr_arguments = {"pr_number": pr_number}
        if not capability_attempted(context, "github.get_pr", arguments=pr_arguments):
            return self._capability("github.get_pr", pr_arguments, "读取 CI 对应的 Pull Request")
        if not capability_attempted(context, "repository.get_changed_files", arguments=pr_arguments):
            return self._capability(
                "repository.get_changed_files",
                pr_arguments,
                "读取 CI 对应的变更文件",
            )
        if not capability_attempted(context, "repository.get_pr_diff", arguments=pr_arguments):
            return self._capability("repository.get_pr_diff", pr_arguments, "读取 CI 对应的 Diff")
        return self._pr_code_evidence_step(context)

    def _ensure_explanation(self, context: AgentContext) -> CodeExplanationResult:
        existing = self._explanation(context)
        if existing is not None:
            return existing
        result = self.coding.explain(
            context.repository,
            context.goal,
            self._code_evidence(context),
            session_id=context.session_id,
            guidance=context.guidance,
        )
        self._record_coding(context, "explain", result)
        return result

    def _ensure_review(self, context: AgentContext, request: str) -> CodeReviewResult:
        existing = self._review(context)
        if existing is not None:
            return existing
        result = self.coding.review(
            context.repository,
            request,
            self._code_evidence(context),
            session_id=context.session_id,
            guidance=context.guidance,
        )
        self._record_coding(context, "review", result)
        return result

    def _ensure_plan(self, context: AgentContext) -> CodePlanResult:
        existing = self._plan(context)
        if existing is not None:
            return existing
        request = context.goal
        ci = self._domain_data(context, "ci_analysis")
        if ci is not None:
            request += "\n\nCI analysis: " + json.dumps(ci, ensure_ascii=False)
        result = self.coding.plan(
            context.repository,
            request,
            self._code_evidence(context),
            session_id=context.session_id,
            guidance=context.guidance,
        )
        self._record_coding(context, "plan", result)
        return result

    def _ensure_candidate(self, context: AgentContext, pull_request: dict[str, Any]) -> str | AgentAction:
        if context.code_candidate is not None:
            return ""
        plan = self._ensure_plan(context)
        head = pull_request.get("head") or {}
        source_ref = str(head.get("sha") or head.get("ref") or "") if isinstance(head, dict) else str(head)
        request = ChangeRequest(
            repository=context.repository,
            description=plan.direction,
            base_branch=self._branch(pull_request.get("base")) or "main",
            target_files=plan.files or self._changed_files(context),
            suggested_title=f"Improve PR #{self._require_pr_number(context)}",
            source_ref=source_ref or None,
        )
        prepared = prepare_verified_candidate(
            self.coding,
            self.verifier,
            request,
            session_id=context.session_id,
            guidance=context.guidance,
        )
        if record_candidate_capability_error(context, prepared):
            return AgentAction(
                AgentActionKind.CONTINUE,
                summary="Coding capability 失败，重新评估下一步",
            )
        if prepared.candidate is None:
            return prepared.message
        candidate = prepared.candidate
        report = prepared.verification
        context.change_request = request
        context.code_candidate = candidate
        context.verification = report
        if report is None:
            raise WorkflowError("candidate verification returned no report")
        package = code_change_review_package(request, candidate, report)
        self._record_domain(context, "code_review_package", to_plain(package))
        if not report.passed:
            raise WorkflowError("静态验证失败；未生成 Pull Request 写入提案")
        return ""

    def _build_review_dialogue(self, context: AgentContext) -> dict[str, list[str] | str]:
        reviews = (self._last_capability(context, "github.get_pr_reviews") or {}).get("reviews", [])
        comments = (self._last_capability(context, "github.get_pr_comments") or {}).get("comments", [])
        evidence = {"reviews": reviews, "comments": comments, **self._code_evidence(context)}
        if self.reasoner is not None:
            value = self.reasoner.complete_structured(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.pull_request_dialogue",
                    request=context.goal,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(context.guidance),
                ),
                schema=_DIALOGUE_SCHEMA,
                tool_name="summarize_review_dialogue",
            )
            return {
                "resolved": [str(item) for item in value.get("resolved", [])],
                "explained": [str(item) for item in value.get("explained", [])],
                "needs_changes": [str(item) for item in value.get("needs_changes", [])],
                "discussion": [str(item) for item in value.get("discussion", [])],
                "conflicts": [str(item) for item in value.get("conflicts", [])],
                "reply_draft": str(value.get("reply_draft", "")),
            }
        return {
            "resolved": [
                str(item.get("body") or "已批准")
                for item in reviews
                if canonical_review_event(item) == "APPROVE"
            ],
            "explained": [],
            "needs_changes": [
                str(item.get("body") or "Review 要求修改")
                for item in reviews
                if canonical_review_event(item) == "REQUEST_CHANGES"
            ],
            "discussion": [str(item.get("body") or "") for item in [*reviews, *comments] if item.get("body")],
            "conflicts": [],
            "reply_draft": "已查看现有 Review；请确认待处理意见后再发布回复。",
        }

    def _build_ci_analysis(self, context: AgentContext) -> dict[str, list[str]]:
        evidence = self._ci_evidence(context)
        run_facts = [
            f"workflow run #{run.get('id', '?')}：{run.get('conclusion') or run.get('status') or 'unknown'}"
            for run in evidence["workflow_runs"]
        ]
        jobs = [job for result in evidence["job_logs"] for job in result.get("jobs", [])]
        unavailable_facts = [
            f"job {job.get('name', job.get('id', '?'))}："
            f"{job.get('conclusion') or job.get('status') or 'unknown'}，日志暂不可用。"
            for job in jobs
            if job.get("log_unavailable")
        ]
        if self.reasoner is not None:
            value = self.reasoner.complete_structured(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.pull_request_ci",
                    request=context.goal,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(context.guidance),
                ),
                schema=_CI_SCHEMA,
                tool_name="analyze_pull_request_ci",
            )
            analysis = {key: [str(item) for item in value.get(key, [])] for key in _CI_SCHEMA["required"]}
            analysis["facts"].extend(
                fact for fact in [*run_facts, *unavailable_facts] if fact not in analysis["facts"]
            )
            return analysis
        facts = run_facts
        for job in jobs:
            if not job.get("log_unavailable"):
                facts.append(f"job {job.get('name', job.get('id', '?'))}：{str(job.get('log') or '').strip()}")
        facts.extend(unavailable_facts)
        changed = [str(path) for path in evidence.get("changed_files", [])]
        return {
            "facts": facts or ["没有找到符合条件的 workflow run。"],
            "suspected_causes": ["需要结合失败日志与本次 Diff 验证根因。"] if facts else [],
            "related_changes": changed,
            "actions": ["针对失败 job 运行对应检查，并验证相关变更文件。"] if facts else [],
        }

    def _assess_merge_readiness(self, context: AgentContext, review: CodeReviewResult) -> dict[str, Any]:
        pull_request = self._last_capability(context, "github.get_pr") or {}
        reviews = (self._last_capability(context, "github.get_pr_reviews") or {}).get("reviews", [])
        review_events = effective_review_events(reviews)
        runs = self._current_workflow_runs(
            (self._last_capability(context, "github.get_workflow_runs") or {}).get("runs", [])
        )
        satisfied: list[str] = []
        blockers: list[str] = []
        remaining: list[str] = []
        if str(pull_request.get("state", "open")).casefold() != "open":
            blockers.append("Pull Request 不是 open 状态。")
        else:
            satisfied.append("Pull Request 处于 open 状态。")
        if bool(pull_request.get("draft")):
            blockers.append("Pull Request 仍是 Draft。")
        else:
            satisfied.append("Pull Request 不是 Draft。")
        if review.recommendation == Recommendation.REQUEST_CHANGES:
            blockers.extend(review.blocking_issues or ["代码审阅建议继续修改。"])
        elif review.recommendation == Recommendation.NEEDS_HUMAN_REVIEW:
            remaining.append("代码审阅结论需要人工确认。")
        else:
            satisfied.append("代码审阅未发现阻塞性问题。")
        if review.goal_alignment == "MISMATCH":
            blockers.append("PR 目标与实际实现不一致。")
        elif review.goal_alignment in {"PARTIAL", "UNKNOWN"}:
            remaining.append("继续确认 PR 描述与实际实现的一致性。")
        else:
            satisfied.append("PR 目标与实际实现一致。")
        if "REQUEST_CHANGES" in review_events:
            blockers.append("已有 Review 要求修改。")
        elif "APPROVE" in review_events:
            satisfied.append("已观察到有效的 APPROVE Review。")
        else:
            remaining.append("尚未观察到 APPROVE Review。")
        if any(self._failed(run) for run in runs):
            blockers.append("CI 仍有失败 workflow run。")
        elif not runs:
            remaining.append("尚未观察到 PR CI 结果。")
        elif any(str(run.get("status") or "").casefold() != "completed" for run in runs):
            remaining.append("CI 尚未全部完成。")
        else:
            satisfied.append("当前 CI workflow runs 已完成且未失败。")
        status = "需要继续修改" if blockers else "处理少量事项后合并" if remaining else "准备合并"
        return {"status": status, "satisfied": satisfied, "blockers": blockers, "remaining": remaining}

    def _code_evidence(self, context: AgentContext) -> dict[str, Any]:
        pull_request = self._last_capability(context, "github.get_pr") or {}
        return {
            "change_goal": {
                "title": str(pull_request.get("title") or "")[:500],
                "body": str(pull_request.get("body") or "")[:4000],
            },
            "changed_files": self._changed_files(context)[:300],
            "diff": str((self._last_capability(context, "repository.get_pr_diff") or {}).get("diff", ""))[:160_000],
            "files": list((self._last_capability(context, "repository.read_files") or {}).get("files", [])),
        }

    def _ci_evidence(self, context: AgentContext) -> dict[str, Any]:
        return {
            "workflow_runs": self._current_workflow_runs(
                (self._last_capability(context, "github.get_workflow_runs") or {}).get("runs", [])
            ),
            "job_logs": self._capability_results(context, "github.get_job_logs"),
            **self._code_evidence(context),
        }

    def _explanation(self, context: AgentContext) -> CodeExplanationResult | None:
        data = self._coding_data(context, "explain")
        if data is None:
            return None
        return CodeExplanationResult(
            behavior_changes=[str(item) for item in data.get("behavior_changes", [])],
            key_symbols=[str(item) for item in data.get("key_symbols", [])],
            call_relationships=[str(item) for item in data.get("call_relationships", [])],
            impact_scope=[str(item) for item in data.get("impact_scope", [])],
        )

    def _review(self, context: AgentContext) -> CodeReviewResult | None:
        data = self._coding_data(context, "review")
        if data is None:
            return None
        return CodeReviewResult(
            summary=str(data.get("summary", "")),
            blocking_issues=[str(item) for item in data.get("blocking_issues", [])],
            impacts=[str(item) for item in data.get("impacts", [])],
            suggestions=[str(item) for item in data.get("suggestions", [])],
            test_assessment=str(data.get("test_assessment", "")),
            risk_level=str(data.get("risk_level", "MEDIUM")),
            recommendation=Recommendation(str(data.get("recommendation", "NEEDS_HUMAN_REVIEW"))),
            goal_alignment=str(data.get("goal_alignment", "UNKNOWN")),
        )

    def _plan(self, context: AgentContext) -> CodePlanResult | None:
        data = self._coding_data(context, "plan")
        if data is None:
            return None
        return CodePlanResult(
            direction=str(data.get("direction", "")),
            files=[str(item) for item in data.get("files", [])],
            tradeoffs=[str(item) for item in data.get("tradeoffs", [])],
            tests=[str(item) for item in data.get("tests", [])],
        )

    @staticmethod
    def _record_coding(context: AgentContext, capability: str, result: Any) -> None:
        context.observations.append(
            {
                "kind": "agent",
                "payload": {"agent": "coding", "capability": capability, "data": to_plain(result)},
            }
        )

    @staticmethod
    def _record_domain(context: AgentContext, capability: str, result: Any) -> None:
        context.observations.append(
            {
                "kind": "agent",
                "payload": {"agent": "pull_requests", "capability": capability, "data": to_plain(result)},
            }
        )

    @staticmethod
    def _coding_data(context: AgentContext, capability: str) -> dict[str, Any] | None:
        for observation in reversed(context.observations):
            payload = observation.get("payload") or {}
            if (
                observation.get("kind") == "agent"
                and payload.get("agent") == "coding"
                and payload.get("capability") == capability
            ):
                return dict(payload.get("data") or {})
        return None

    @staticmethod
    def _domain_data(context: AgentContext, capability: str) -> dict[str, Any] | None:
        for observation in reversed(context.observations):
            payload = observation.get("payload") or {}
            if (
                observation.get("kind") == "agent"
                and payload.get("agent") == "pull_requests"
                and payload.get("capability") == capability
            ):
                return dict(payload.get("data") or {})
        return None

    def _list_answer(
        self,
        context: AgentContext,
        raw_pull_requests: list[dict[str, Any]],
        pull_requests: list[PullRequestSummary],
    ) -> str:
        if not pull_requests:
            return "没有找到符合当前条件的 Pull Request。"
        if self.reasoner is not None:
            evidence = [self._bounded_pull_request(item) for item in raw_pull_requests]
            return self.reasoner.complete_text(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.pull_request_list_summarize",
                    request=context.goal,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(context.guidance),
                ),
            )
        return f"找到 {len(pull_requests)} 个符合条件的 Pull Request。"

    def _detail_answer(
        self,
        context: AgentContext,
        pull_request: dict[str, Any],
        comments: list[dict[str, Any]],
        changed_files: list[str],
        diff: str,
    ) -> str:
        if self.reasoner is not None:
            evidence = {
                "pull_request": self._bounded_pull_request(pull_request),
                "comments": self._bounded_comments(comments),
                "changed_files": changed_files[:100],
                "diff": diff[:20_000],
            }
            return self.reasoner.complete_text(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.pull_request_detail_answer",
                    request=context.goal,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(context.guidance),
                ),
            )
        body = str(pull_request.get("body") or "暂无正文").strip()
        return f"#{pull_request.get('number')} {pull_request.get('title', '')}\n\n{body[:4000]}"

    @staticmethod
    def _format_explanation(result: CodeExplanationResult) -> str:
        return "\n\n".join(
            (
                "行为变化\n" + PullRequestAgent._items(result.behavior_changes),
                "关键符号\n" + PullRequestAgent._items(result.key_symbols),
                "调用关系\n" + PullRequestAgent._items(result.call_relationships),
                "影响范围\n" + PullRequestAgent._items(result.impact_scope),
            )
        )

    @staticmethod
    def _format_review(result: CodeReviewResult) -> str:
        return (
            f"摘要\n{result.summary}\n\n阻塞性问题\n{PullRequestAgent._items(result.blocking_issues)}\n\n"
            f"影响\n{PullRequestAgent._items(result.impacts)}\n\n修改建议\n"
            f"{PullRequestAgent._items(result.suggestions)}\n\n测试判断\n{result.test_assessment}\n\n"
            f"风险：{result.risk_level}；建议：{result.recommendation.value}；目标一致性：{result.goal_alignment}。"
        )

    @staticmethod
    def _format_plan(result: CodePlanResult) -> str:
        return (
            f"修改方向\n{result.direction}\n\n涉及文件\n{PullRequestAgent._items(result.files)}\n\n"
            f"取舍\n{PullRequestAgent._items(result.tradeoffs)}\n\n测试范围\n{PullRequestAgent._items(result.tests)}"
        )

    @staticmethod
    def _format_dialogue(result: dict[str, Any]) -> str:
        parts = []
        for key, title in (
            ("resolved", "已解决"),
            ("explained", "已解释"),
            ("needs_changes", "仍需修改"),
            ("discussion", "需要讨论"),
            ("conflicts", "存在冲突"),
        ):
            parts.append(f"{title}\n{PullRequestAgent._items(result.get(key, []))}")
        if result.get("reply_draft"):
            parts.append("回复草稿\n" + str(result["reply_draft"]))
        return "\n\n".join(parts)

    @staticmethod
    def _format_ci(result: dict[str, Any]) -> str:
        return "\n\n".join(
            (
                "日志事实\n" + PullRequestAgent._items(result.get("facts", [])),
                "原因推测\n" + PullRequestAgent._items(result.get("suspected_causes", [])),
                "关联变更\n" + PullRequestAgent._items(result.get("related_changes", [])),
                "建议动作\n" + PullRequestAgent._items(result.get("actions", [])),
            )
        )

    @staticmethod
    def _format_readiness(result: dict[str, Any]) -> str:
        return (
            f"合并准备度：{result['status']}\n\n"
            f"已满足条件\n{PullRequestAgent._items(result['satisfied'])}\n\n"
            f"阻塞项\n{PullRequestAgent._items(result['blockers'])}\n\n"
            f"剩余事项\n{PullRequestAgent._items(result['remaining'])}"
        )

    @staticmethod
    def _review_body(review: CodeReviewResult) -> str:
        return PullRequestAgent._format_review(review)

    @staticmethod
    def _candidate_approval_summary(candidate: CandidatePatch, report: VerificationReport | None) -> str:
        checks = ", ".join(
            f"{check.name}={check.status}" for check in (report.checks if report is not None else [])
        ) or "无静态检查结果"
        return (
            f"将候选改动提交到当前 PR 分支。\n文件：{', '.join(candidate.changed_files)}\n"
            f"摘要：{candidate.summary}\n静态验证：{checks}\n风险：{'; '.join(candidate.risks) or '未识别具体风险'}"
        )

    @staticmethod
    def _items(values: Any) -> str:
        items = [str(item) for item in values or [] if str(item)]
        return "\n".join(f"- {item}" for item in items) or "- 无"

    @staticmethod
    def _capability(capability_id: str, arguments: dict[str, Any], summary: str) -> AgentAction:
        return AgentAction(
            AgentActionKind.CAPABILITY,
            capability_id=capability_id,
            arguments=arguments,
            summary=summary,
        )

    @staticmethod
    def _failed(run: dict[str, Any]) -> bool:
        return str(run.get("conclusion") or run.get("status") or "").casefold() in {"failure", "failed"}

    @staticmethod
    def _current_workflow_runs(runs: Any) -> list[dict[str, Any]]:
        """Return the newest run for each workflow while preserving unrelated runs."""
        latest: dict[str, tuple[tuple[str, int, int, int], dict[str, Any]]] = {}
        for run in runs or []:
            if not isinstance(run, dict):
                continue
            identity = next(
                (
                    f"{field}:{run[field]}"
                    for field in ("workflow_id", "path", "name")
                    if run.get(field) not in (None, "")
                ),
                f"run:{run.get('id', id(run))}",
            )
            rank = (
                str(run.get("run_started_at") or run.get("created_at") or run.get("updated_at") or ""),
                int(run.get("run_number") or 0),
                int(run.get("run_attempt") or 0),
                int(run.get("id") or 0),
            )
            previous = latest.get(identity)
            if previous is None or rank > previous[0]:
                latest[identity] = (rank, run)
        return [run for _, run in sorted(latest.values(), key=lambda item: item[0], reverse=True)]

    @staticmethod
    def _entity_number(context: AgentContext) -> int | None:
        if context.entity_type not in {None, "pull_request"}:
            return None
        if context.entity_id is None or not str(context.entity_id).isdigit():
            return None
        return int(context.entity_id)

    @staticmethod
    def _workflow_run_id(context: AgentContext) -> int | None:
        if context.entity_type != "workflow_run" or context.entity_id is None or not str(context.entity_id).isdigit():
            return None
        return int(context.entity_id)

    def _pr_number(self, context: AgentContext) -> int | None:
        direct = self._entity_number(context)
        if direct is not None:
            return direct
        runs = (self._last_capability(context, "github.get_workflow_runs") or {}).get("runs", [])
        if not runs:
            return None
        run = runs[0]
        if run.get("pr_number") is not None:
            return int(run["pr_number"])
        pull_requests = run.get("pull_requests") or []
        if pull_requests and pull_requests[0].get("number") is not None:
            return int(pull_requests[0]["number"])
        return None

    def _require_pr_number(self, context: AgentContext) -> int:
        number = self._pr_number(context)
        if number is None:
            raise WorkflowError("Pull Request operation requires a PR number")
        return number

    def _changed_files(self, context: AgentContext) -> list[str]:
        return [str(path) for path in (self._last_capability(context, "repository.get_changed_files") or {}).get("files", [])]

    def _readable_changed_files(self, context: AgentContext) -> list[str]:
        entries = {str(path) for path in (self._last_capability(context, "repository.get_repo_tree") or {}).get("entries", [])}
        return [path for path in self._changed_files(context) if path in entries]

    @staticmethod
    def _last_capability(context: AgentContext, capability_id: str) -> Any:
        for observation in reversed(context.observations):
            if (
                observation.get("kind") == "capability"
                and (observation.get("payload") or {}).get("capability_id") == capability_id
            ):
                return (observation.get("payload") or {}).get("data")
        return None

    @staticmethod
    def _capability_results(context: AgentContext, capability_id: str) -> list[dict[str, Any]]:
        return [
            dict((observation.get("payload") or {}).get("data") or {})
            for observation in context.observations
            if observation.get("kind") == "capability"
            and (observation.get("payload") or {}).get("capability_id") == capability_id
        ]

    @staticmethod
    def _has_capability_arguments(
        context: AgentContext,
        capability_id: str,
        **arguments: Any,
    ) -> bool:
        for observation in context.observations:
            if observation.get("kind") not in {"capability", "capability_error"}:
                continue
            payload = observation.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            observed = payload.get("arguments") or {}
            if payload.get("capability_id") == capability_id and all(
                observed.get(key) == value for key, value in arguments.items()
            ):
                return True
        return False

    @staticmethod
    def _last_write(context: AgentContext) -> dict[str, Any] | None:
        for capability_id in ("github.merge", "github.commit", "github.post_review"):
            result = PullRequestAgent._last_capability(context, capability_id)
            if result is not None:
                return dict(result)
        return None

    @staticmethod
    def _is_fork(repository: str, pull_request: dict[str, Any]) -> bool:
        head = pull_request.get("head") or {}
        if not isinstance(head, dict):
            return False
        source = head.get("repo") or {}
        full_name = str(source.get("full_name") or "") if isinstance(source, dict) else ""
        return bool(full_name and full_name != repository)

    @staticmethod
    def _head_ref(pull_request: dict[str, Any]) -> str | None:
        head = pull_request.get("head") or {}
        if isinstance(head, dict):
            return str(head.get("sha") or head.get("ref") or "") or None
        return str(head) or None

    @classmethod
    def _summary(cls, pull_request: dict[str, Any]) -> PullRequestSummary:
        user = pull_request.get("user") or pull_request.get("author") or ""
        author = str(user.get("login", "")) if isinstance(user, dict) else str(user)
        return PullRequestSummary(
            number=int(pull_request.get("number", 0)),
            title=str(pull_request.get("title", "")),
            state=str(pull_request.get("state", "open")),
            author=author,
            head=cls._branch(pull_request.get("head")),
            base=cls._branch(pull_request.get("base")),
            draft=bool(pull_request.get("draft")),
            updated_at=str(pull_request.get("updated_at", "")),
            url=str(pull_request.get("html_url") or pull_request.get("url") or ""),
        )

    @classmethod
    def _bounded_pull_request(cls, pull_request: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": pull_request.get("number"),
            "title": str(pull_request.get("title", ""))[:500],
            "state": pull_request.get("state", "open"),
            "body": str(pull_request.get("body") or "")[:4000],
            "author": cls._summary(pull_request).author,
            "head": cls._branch(pull_request.get("head")),
            "base": cls._branch(pull_request.get("base")),
            "draft": bool(pull_request.get("draft")),
            "updated_at": pull_request.get("updated_at", ""),
        }

    @staticmethod
    def _bounded_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "author": (comment.get("user") or {}).get("login", "")
                if isinstance(comment.get("user"), dict)
                else "",
                "body": str(comment.get("body") or "")[:2000],
                "created_at": comment.get("created_at", ""),
            }
            for comment in comments[:30]
        ]

    @staticmethod
    def _branch(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("ref", ""))
        return str(value or "")
