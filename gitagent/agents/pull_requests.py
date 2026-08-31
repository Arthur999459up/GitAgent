"""Intent-driven Pull Request domain orchestration."""

from __future__ import annotations

import json
import re
from typing import Any

from gitagent.agent_loop import AgentAction, AgentActionKind, rejection_feedback
from gitagent.domain.errors import ValidationError, WorkflowError
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
from gitagent.domain.reviews import effective_review_events
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.mutation_plans import code_change_review_package
from gitagent.harness.validation.static import StaticVerifier
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .coding import (
    CodingAgent,
    prepare_verified_candidate,
    record_candidate_capability_error,
)
from .decide import decide_action

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
        "operation": {
            "type": "string",
            "enum": [item.value for item in PullRequestOperation],
        },
        "review_event": {
            "type": "string",
            "enum": ["", "COMMENT", "APPROVE", "REQUEST_CHANGES"],
        },
    },
    "required": ["operation", "review_event"],
    "additionalProperties": False,
}

_ANALYSES_BY_OPERATION = {
    PullRequestOperation.EXPLAIN: frozenset({"explain"}),
    PullRequestOperation.REVIEW: frozenset({"review"}),
    PullRequestOperation.REVIEW_DIALOGUE: frozenset({"review_dialogue"}),
    PullRequestOperation.CI_ANALYZE: frozenset({"ci"}),
    PullRequestOperation.PLAN: frozenset({"plan"}),
    PullRequestOperation.MODIFY: frozenset({"plan"}),
    PullRequestOperation.CI_FIX: frozenset({"ci", "plan"}),
    PullRequestOperation.MERGE_READINESS: frozenset({"review"}),
    PullRequestOperation.MERGE: frozenset({"review"}),
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

        if (
            self._pr_number(context) is not None
            or _PR_NUMBER_QUESTION_FRAGMENT not in context.question
        ):
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
        operation = PullRequestOperation(context.operation)
        feedback = rejection_feedback(context)
        if feedback is not None and not feedback:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="已取消待确认操作",
                message="已按你的要求取消，未执行 Pull Request 写操作。",
            )
        if (
            feedback
            and context.code_candidate is not None
            and self._last_capability(context, "github.commit") is None
        ):
            context.goal += f"\n\nUser revision: {feedback}"
            context.change_request = None
            context.code_candidate = None
            context.verification = None

        merged = self._last_capability(context, "github.merge")
        if merged is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Pull Request 已合并",
                message=f"PR #{merged.get('pr_number', self._pr_number(context))} 已按确认结果合并。",
            )
        committed = self._last_capability(context, "github.commit")
        if committed is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="候选改动已应用",
                message=(
                    f"候选改动已写入 PR 分支 `{committed.get('branch', '')}`，涉及："
                    f"{', '.join(committed.get('files', []))}。"
                ),
            )
        published = self._last_capability(context, "github.post_review")
        if published is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Review 已发布",
                message=f"已发布 {published.get('event', context.requested_outcome or 'COMMENT')} Review。",
            )

        if self.reasoner is None:
            return self._minimal_fallback(context, operation)
        protected = list(
            (AgentActionKind.COMPLETE_ANALYSIS,)
            if operation in _ANALYSES_BY_OPERATION
            else ()
        )
        if operation in {PullRequestOperation.MODIFY, PullRequestOperation.CI_FIX}:
            protected.append(AgentActionKind.PREPARE_CODE_CHANGE)
        action = decide_action(
            context,
            self.harness,
            self.reasoner,
            protected_kinds=tuple(protected),
        )
        if action.kind == AgentActionKind.PREPARE_CODE_CHANGE:
            return self._prepare_change_action(context)
        if action.kind == AgentActionKind.CAPABILITY:
            if action.capability_id == "github.commit":
                raise WorkflowError(
                    "PR branch writes require a verified CandidatePatch and protected code-change action"
                )
            if action.capability_id == "github.merge":
                return self._protected_merge_action(context, execute=True)
            if action.capability_id == "github.post_review":
                return self._protected_review_action(context, action)
        if action.kind == AgentActionKind.FINISH and operation in {
            PullRequestOperation.MERGE_READINESS,
            PullRequestOperation.MERGE,
        }:
            return self._protected_merge_action(
                context,
                execute=operation == PullRequestOperation.MERGE,
            )
        return action

    def build_result(self, context: AgentContext) -> PullRequestAgentResult:
        operation = (
            PullRequestOperation(context.operation) if context.operation else None
        )
        pull_request = self._last_capability(context, "github.get_pr")
        raw_list = self._last_capability(context, "github.list_pull_requests")
        changed_files = self._changed_files(context)
        pull_requests: list[PullRequestSummary] = []
        if raw_list is not None:
            pull_requests = [
                self._summary(item) for item in raw_list.get("pull_requests", [])
            ]
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
        if not answer and raw_list is not None:
            answer = (
                f"找到 {len(pull_requests)} 个符合条件的 Pull Request。"
                if pull_requests
                else "没有找到符合当前条件的 Pull Request。"
            )
        if not answer and pull_request is not None:
            body = str(pull_request.get("body") or "暂无正文").strip()
            answer = (
                f"#{pull_request.get('number')} {pull_request.get('title', '')}\n\n"
                f"{body[:4000]}"
            )
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
        value = context.reason_structured(
            self.reasoner,
            schema=_OPERATION_SCHEMA,
            tool_name="select_pull_request_operation",
        )
        context.record_model_response(value, tool_name="select_pull_request_operation")
        try:
            operation = PullRequestOperation(str(value.get("operation", "")))
        except ValueError as exc:
            raise ValidationError(
                "PullRequestAgent selected an unknown operation"
            ) from exc
        review_event = str(value.get("review_event", ""))
        context.operation = operation.value
        context.requested_outcome = review_event
        context.complete_control_call(
            {"operation": operation.value, "review_event": review_event}
        )

    def complete_analysis(
        self, context: AgentContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Complete one model-selected typed analysis during the active AgentLoop."""

        if set(arguments) != {"analysis"}:
            raise ValidationError(
                "complete_analysis requires exactly one 'analysis' argument"
            )
        operation = PullRequestOperation(context.operation)
        analysis = str(arguments.get("analysis") or "")
        if analysis not in _ANALYSES_BY_OPERATION.get(operation, frozenset()):
            raise ValidationError(
                f"analysis {analysis or '<empty>'} is not valid for {operation.value}"
            )
        if analysis == "explain":
            artifact: Any = self._ensure_explanation(context)
        elif analysis == "review":
            request = (
                _MERGE_CODE_REVIEW_REQUEST
                if operation
                in {PullRequestOperation.MERGE, PullRequestOperation.MERGE_READINESS}
                else context.goal
            )
            artifact = self._ensure_review(context, request)
        elif analysis == "plan":
            artifact = self._ensure_plan(context)
        elif analysis == "review_dialogue":
            artifact = self._ensure_review_dialogue(context)
        elif analysis == "ci":
            artifact = self._ensure_ci_analysis(context)
        else:  # pragma: no cover - the operation map is the closed allowlist
            raise ValidationError(f"unknown Pull Request analysis: {analysis}")
        return {"analysis": analysis, "artifact": to_plain(artifact)}

    def _prepare_change_action(self, context: AgentContext) -> AgentAction:
        pull_request = self._last_capability(context, "github.get_pr")
        if pull_request is None:
            return AgentAction(
                AgentActionKind.ASK,
                summary="缺少 Pull Request",
                question="请指定并读取需要完善的 Pull Request。",
            )
        if PullRequestOperation(context.operation) == PullRequestOperation.CI_FIX:
            self._ensure_ci_analysis(context)
        preparation = self._ensure_candidate(context, pull_request)
        if isinstance(preparation, AgentAction):
            return preparation
        if preparation:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="模型未生成文件内容",
                message=preparation,
            )
        candidate = context.code_candidate
        if candidate is None:
            raise WorkflowError("Pull Request candidate generation returned no patch")
        if context.verification is None or not context.verification.passed:
            raise WorkflowError("静态验证失败；未生成 Pull Request 写入提案")
        if self._is_fork(context.repository, pull_request):
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Fork PR 候选改动已生成",
                message=(
                    "这是 Fork Pull Request，未生成自动写入提案。"
                    "请在来源仓库应用以下候选 Diff：\n\n" + candidate.patch
                ),
            )
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

    def _protected_review_action(
        self,
        context: AgentContext,
        action: AgentAction,
    ) -> AgentAction:
        if PullRequestOperation(context.operation) != PullRequestOperation.POST_REVIEW:
            raise WorkflowError(
                "github.post_review is only valid for an explicit POST_REVIEW goal"
            )
        pr_number = self._require_pr_number(context)
        arguments = dict(action.arguments)
        event = str(
            arguments.get("event") or context.requested_outcome or "COMMENT"
        ).upper()
        if event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValidationError("Pull Request review event is invalid")
        if context.requested_outcome and event != context.requested_outcome:
            raise ValidationError(
                "Pull Request review event differs from the selected outcome"
            )
        body = str(arguments.get("body") or "").strip()
        if not body:
            raise ValidationError("Pull Request review body cannot be empty")
        context.requested_outcome = event
        context.complete_control_call(
            {"status": "validated", "action": "protected_post_review"}
        )
        return self._capability(
            "github.post_review",
            {"pr_number": pr_number, "event": event, "body": body},
            action.summary or f"发布 {event} Review 到 PR #{pr_number}",
        )

    def _protected_merge_action(
        self,
        context: AgentContext,
        *,
        execute: bool,
    ) -> AgentAction:
        missing = [
            capability_id
            for capability_id in (
                "github.get_pr",
                "github.get_pr_reviews",
                "github.get_workflow_runs",
                "repository.get_changed_files",
                "repository.get_pr_diff",
            )
            if self._last_capability(context, capability_id) is None
        ]
        if missing:
            readiness = {
                "status": "证据不足",
                "satisfied": [],
                "blockers": [],
                "remaining": ["尚未完成必要的合并检查：" + ", ".join(missing)],
            }
            self._record_domain(context, "merge_readiness", readiness)
            return AgentAction(
                AgentActionKind.FINISH,
                summary="合并证据不足",
                message=self._format_readiness(readiness),
            )

        review = self._ensure_review(context, _MERGE_CODE_REVIEW_REQUEST)
        readiness = self._assess_merge_readiness(context, review)
        self._record_domain(context, "merge_readiness", readiness)
        if not execute or readiness.get("status") != "准备合并":
            return AgentAction(
                AgentActionKind.FINISH,
                summary=(
                    "合并准备度已评估"
                    if not execute
                    else "Pull Request 尚未满足合并条件"
                ),
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
        pr_number = self._require_pr_number(context)
        context.complete_control_call(
            {"status": "validated", "action": "protected_merge"}
        )
        return self._capability(
            "github.merge",
            {"pr_number": pr_number, "expected_head_sha": expected_sha},
            f"合并 PR #{pr_number}（head {expected_sha}）",
        )

    def _minimal_fallback(
        self,
        context: AgentContext,
        operation: PullRequestOperation,
    ) -> AgentAction:
        """A small provider-less degradation, never a parallel analysis workflow."""

        if operation in {
            PullRequestOperation.LIST,
            PullRequestOperation.SEARCH,
            PullRequestOperation.SUMMARIZE,
        }:
            if self._last_capability(context, "github.list_pull_requests") is None:
                return self._capability(
                    "github.list_pull_requests",
                    {"state": "open", "limit": 20},
                    "列出 Pull Requests",
                )
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Pull Requests 已列出",
                message="Pull Requests 已列出；当前未配置可继续自主分析的模型。",
            )
        pr_number = self._pr_number(context)
        if pr_number is None:
            return AgentAction(
                AgentActionKind.ASK,
                summary="缺少 Pull Request 编号",
                question="请指定 Pull Request 编号。",
            )
        if self._last_capability(context, "github.get_pr") is None:
            return self._capability(
                "github.get_pr",
                {"pr_number": pr_number},
                "读取 Pull Request",
            )
        return AgentAction(
            AgentActionKind.FINISH,
            summary="Pull Request 已读取",
            message="Pull Request 元数据已读取；当前未配置可继续自主分析的模型。",
        )

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

    def _ensure_candidate(
        self, context: AgentContext, pull_request: dict[str, Any]
    ) -> str | AgentAction:
        if context.code_candidate is not None:
            return ""
        plan = self._ensure_plan(context)
        head = pull_request.get("head") or {}
        source_ref = (
            str(head.get("sha") or head.get("ref") or "")
            if isinstance(head, dict)
            else str(head)
        )
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
            context.complete_control_call(
                {"status": "failed", "capability_error": prepared.capability_error}
            )
            if self.reasoner is None:
                return AgentAction(
                    AgentActionKind.FINISH,
                    summary="候选补丁准备失败",
                    message=str(
                        (prepared.capability_error or {}).get("message")
                        or "候选补丁准备失败。"
                    ),
                )
            action = decide_action(
                context,
                self.harness,
                self.reasoner,
                protected_kinds=(AgentActionKind.PREPARE_CODE_CHANGE,),
            )
            if action.kind == AgentActionKind.PREPARE_CODE_CHANGE:
                return AgentAction(
                    AgentActionKind.FINISH,
                    summary="候选补丁准备失败",
                    message="Coding capability 失败后不能在没有新证据时立即重复相同候选生成。",
                )
            return action
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

    def _ensure_review_dialogue(self, context: AgentContext) -> dict[str, Any]:
        existing = self._domain_data(context, "review_dialogue")
        if existing is not None:
            return existing
        reviews = (self._last_capability(context, "github.get_pr_reviews") or {}).get(
            "reviews", []
        )
        comments = (self._last_capability(context, "github.get_pr_comments") or {}).get(
            "comments", []
        )
        result = self.coding.summarize_review_dialogue(
            context.repository,
            context.goal,
            {
                "reviews": reviews,
                "comments": comments,
                **self._code_evidence(context),
            },
            session_id=context.session_id,
            guidance=context.guidance,
        )
        self._record_domain(context, "review_dialogue", result)
        return result

    def _ensure_ci_analysis(self, context: AgentContext) -> dict[str, Any]:
        existing = self._domain_data(context, "ci_analysis")
        if existing is not None:
            return existing
        result = self.coding.analyze_ci(
            context.repository,
            context.goal,
            self._ci_evidence(context),
            session_id=context.session_id,
            guidance=context.guidance,
        )
        self._record_domain(context, "ci_analysis", result)
        return result

    def _assess_merge_readiness(
        self, context: AgentContext, review: CodeReviewResult
    ) -> dict[str, Any]:
        pull_request = self._last_capability(context, "github.get_pr") or {}
        reviews = (self._last_capability(context, "github.get_pr_reviews") or {}).get(
            "reviews", []
        )
        review_events = effective_review_events(reviews)
        runs = self._current_workflow_runs(
            (self._last_capability(context, "github.get_workflow_runs") or {}).get(
                "runs", []
            )
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
        elif any(
            str(run.get("status") or "").casefold() != "completed" for run in runs
        ):
            remaining.append("CI 尚未全部完成。")
        else:
            satisfied.append("当前 CI workflow runs 已完成且未失败。")
        status = (
            "需要继续修改"
            if blockers
            else "处理少量事项后合并"
            if remaining
            else "准备合并"
        )
        return {
            "status": status,
            "satisfied": satisfied,
            "blockers": blockers,
            "remaining": remaining,
        }

    def _code_evidence(self, context: AgentContext) -> dict[str, Any]:
        pull_request = self._last_capability(context, "github.get_pr") or {}
        reads = [
            item
            for result in self._capability_results(context, "repository.read_files")
            for item in result.get("files", [])
        ]
        capability_evidence = [
            dict(observation.get("payload") or {})
            for observation in context.observations
            if observation.get("kind") in {"capability", "capability_error"}
        ][-40:]
        return {
            "change_goal": {
                "title": str(pull_request.get("title") or "")[:500],
                "body": str(pull_request.get("body") or "")[:4000],
            },
            "changed_files": self._changed_files(context)[:300],
            "diff": str(
                (self._last_capability(context, "repository.get_pr_diff") or {}).get(
                    "diff", ""
                )
            )[:160_000],
            "files": reads,
            "capability_evidence": capability_evidence,
        }

    def _ci_evidence(self, context: AgentContext) -> dict[str, Any]:
        return {
            "workflow_runs": self._current_workflow_runs(
                (self._last_capability(context, "github.get_workflow_runs") or {}).get(
                    "runs", []
                )
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
            call_relationships=[
                str(item) for item in data.get("call_relationships", [])
            ],
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
            recommendation=Recommendation(
                str(data.get("recommendation", "NEEDS_HUMAN_REVIEW"))
            ),
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
                "payload": {
                    "agent": "coding",
                    "capability": capability,
                    "data": to_plain(result),
                },
            }
        )

    @staticmethod
    def _record_domain(context: AgentContext, capability: str, result: Any) -> None:
        context.observations.append(
            {
                "kind": "agent",
                "payload": {
                    "agent": "pull_requests",
                    "capability": capability,
                    "data": to_plain(result),
                },
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

    @staticmethod
    def _format_readiness(result: dict[str, Any]) -> str:
        return (
            f"合并准备度：{result['status']}\n\n"
            f"已满足条件\n{PullRequestAgent._items(result['satisfied'])}\n\n"
            f"阻塞项\n{PullRequestAgent._items(result['blockers'])}\n\n"
            f"剩余事项\n{PullRequestAgent._items(result['remaining'])}"
        )

    @staticmethod
    def _candidate_approval_summary(
        candidate: CandidatePatch, report: VerificationReport | None
    ) -> str:
        checks = (
            ", ".join(
                f"{check.name}={check.status}"
                for check in (report.checks if report is not None else [])
            )
            or "无静态检查结果"
        )
        return (
            f"将候选改动提交到当前 PR 分支。\n文件：{', '.join(candidate.changed_files)}\n"
            f"摘要：{candidate.summary}\n静态验证：{checks}\n风险：{'; '.join(candidate.risks) or '未识别具体风险'}"
        )

    @staticmethod
    def _items(values: Any) -> str:
        items = [str(item) for item in values or [] if str(item)]
        return "\n".join(f"- {item}" for item in items) or "- 无"

    @staticmethod
    def _capability(
        capability_id: str, arguments: dict[str, Any], summary: str
    ) -> AgentAction:
        return AgentAction(
            AgentActionKind.CAPABILITY,
            capability_id=capability_id,
            arguments=arguments,
            summary=summary,
        )

    @staticmethod
    def _failed(run: dict[str, Any]) -> bool:
        return str(run.get("conclusion") or run.get("status") or "").casefold() in {
            "failure",
            "failed",
        }

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
                str(
                    run.get("run_started_at")
                    or run.get("created_at")
                    or run.get("updated_at")
                    or ""
                ),
                int(run.get("run_number") or 0),
                int(run.get("run_attempt") or 0),
                int(run.get("id") or 0),
            )
            previous = latest.get(identity)
            if previous is None or rank > previous[0]:
                latest[identity] = (rank, run)
        return [
            run
            for _, run in sorted(
                latest.values(), key=lambda item: item[0], reverse=True
            )
        ]

    @staticmethod
    def _entity_number(context: AgentContext) -> int | None:
        if context.entity_type not in {None, "pull_request"}:
            return None
        if context.entity_id is None or not str(context.entity_id).isdigit():
            return None
        return int(context.entity_id)

    def _pr_number(self, context: AgentContext) -> int | None:
        direct = self._entity_number(context)
        if direct is not None:
            return direct
        runs = (self._last_capability(context, "github.get_workflow_runs") or {}).get(
            "runs", []
        )
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
        return [
            str(path)
            for path in (
                self._last_capability(context, "repository.get_changed_files") or {}
            ).get("files", [])
        ]

    @staticmethod
    def _last_capability(context: AgentContext, capability_id: str) -> Any:
        for observation in reversed(context.observations):
            if (
                observation.get("kind") == "capability"
                and (observation.get("payload") or {}).get("capability_id")
                == capability_id
            ):
                return (observation.get("payload") or {}).get("data")
        return None

    @staticmethod
    def _capability_results(
        context: AgentContext, capability_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict((observation.get("payload") or {}).get("data") or {})
            for observation in context.observations
            if observation.get("kind") == "capability"
            and (observation.get("payload") or {}).get("capability_id") == capability_id
        ]

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
        full_name = (
            str(source.get("full_name") or "") if isinstance(source, dict) else ""
        )
        return bool(full_name and full_name != repository)

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

    @staticmethod
    def _branch(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("ref", ""))
        return str(value or "")
