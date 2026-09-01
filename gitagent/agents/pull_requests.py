"""Pull Request Agent using native Capability and Coding Agent calls."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop import (
    AgentCall,
    AgentResult,
    ModelResponse,
    WaitForUser,
    explicit_wait,
    rejection_feedback,
    wait_for_user_tool,
)
from gitagent.domain.errors import WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    ChangeRequest,
    CodeReviewResult,
    CodingTask,
    PlannedCapabilityCall,
    PullRequestAgentResult,
    PullRequestOperation,
    PullRequestSummary,
    Recommendation,
)
from gitagent.domain.reviews import effective_review_events
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

_PROMPTS = get_prompt_library()
_CODING_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "minLength": 1},
        "mode": {
            "type": "string",
            "enum": [
                "explain",
                "review",
                "plan",
                "review_dialogue",
                "ci",
                "patch",
            ],
        },
    },
    "required": ["task", "mode"],
    "additionalProperties": False,
}


PULL_REQUEST_AGENT_SPEC = AgentSpec(
    name="pull_requests",
    role=(
        "Own Pull Request browsing, analysis, review, CI, verified improvements, "
        "review publication, readiness, and merge orchestration."
    ),
    system_prompt=_PROMPTS.text("system.pull_requests"),
    output_schema=(
        "operation",
        "answer",
        "pull_requests",
        "pr_number",
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
    ),
)


class PullRequestAgent:
    def __init__(
        self,
        harness: AgentHarness,
        reasoner: Reasoner,
    ) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(PULL_REQUEST_AGENT_SPEC)

    def agent_schemas(self) -> dict[str, dict[str, Any]]:
        return {"coding": _CODING_SCHEMA}

    def step(self, context: AgentContext) -> ModelResponse | WaitForUser:
        feedback = rejection_feedback(context)
        if feedback is not None and not feedback:
            return self._text(
                context,
                "已按你的要求取消，未执行 Pull Request 写操作。",
            )
        tools = [
            *self.harness.llm_tools(context),
            self.harness.agent_tool(
                "coding",
                (
                    "Delegate one self-contained PR explanation, review, plan, Review-dialogue, "
                    "CI analysis, or verified patch task to a fresh Coding Agent."
                ),
                _CODING_SCHEMA,
            ),
            wait_for_user_tool(),
        ]
        return explicit_wait(context.reason(self.reasoner, tools=tools))

    def prepare_child(
        self,
        context: AgentContext,
        call: AgentCall,
        child: AgentContext,
    ) -> None:
        if call.agent_id != "coding":
            raise WorkflowError(f"PullRequestAgent cannot call {call.agent_id}")
        mode = str(call.arguments["mode"])
        task = str(call.arguments["task"])
        evidence = self._ci_evidence(context) if mode == "ci" else self._code_evidence(context)
        if mode == "review_dialogue":
            evidence.update(
                {
                    "reviews": (
                        self._last_capability(context, "github.get_pr_reviews") or {}
                    ).get("reviews", []),
                    "comments": (
                        self._last_capability(context, "github.get_pr_comments") or {}
                    ).get("comments", []),
                }
            )
        request = None
        if mode == "patch":
            pull_request = self._last_capability(context, "github.get_pr")
            if not isinstance(pull_request, dict):
                raise WorkflowError("PR patch requires observed Pull Request metadata")
            head = pull_request.get("head") or {}
            request = ChangeRequest(
                repository=context.repository,
                description=task,
                base_branch=self._branch(head),
                target_files=self._changed_files(context),
                source_ref=str(head.get("sha") or "") if isinstance(head, dict) else None,
                suggested_title=task[:200],
            )
            context.change_request = request
        child.coding_task = CodingTask(
            mode=mode,
            task=task,
            evidence=evidence,
            change_request=request,
        )

    def after_agent_result(
        self,
        context: AgentContext,
        call: AgentCall,
        result: AgentResult,
        child: AgentContext,
        dispatcher: Any,
    ) -> None:
        context.change_request = child.change_request
        context.code_candidate = child.code_candidate
        context.verification = child.verification
        context.code_explanation = child.code_explanation
        context.code_review = child.code_review
        context.code_plan = child.code_plan
        context.review_dialogue = child.review_dialogue
        context.ci_analysis = child.ci_analysis
        context.observations.extend(
            observation
            for observation in child.observations
            if observation.get("kind") == "capability_error"
        )
        if result.status != "completed":
            return
        mode = str(call.arguments.get("mode") or "")
        if mode == "review" and context.code_review is not None:
            context.merge_readiness = self._assess_merge_readiness(
                context, context.code_review
            )
            context.observations.append(
                {
                    "kind": "agent_artifact",
                    "payload": {
                        "name": "merge_readiness",
                        "data": context.merge_readiness,
                    },
                }
            )
        if mode != "patch" or context.code_candidate is None:
            return
        if context.verification is None or not context.verification.passed:
            raise WorkflowError("static verification failed; refusing a PR write proposal")
        if context.change_request is None or not context.change_request.source_ref:
            raise WorkflowError("PR write proposal is missing its candidate head SHA")
        pull_request = self._last_capability(context, "github.get_pr") or {}
        if self._is_fork(context.repository, pull_request):
            return
        branch = self._branch(pull_request.get("head"))
        if not branch:
            raise WorkflowError("Pull Request head branch is missing")
        dispatcher.queue(
            context,
            self._candidate_approval_summary(context),
            [
                PlannedCapabilityCall(
                    "github.commit",
                    {
                        "branch": branch,
                        "files": context.code_candidate.files,
                        "deleted_files": context.code_candidate.deleted_files,
                        "message": context.code_candidate.summary,
                        "expected_head_sha": context.change_request.source_ref,
                    },
                )
            ],
        )

    def build_result(self, context: AgentContext) -> PullRequestAgentResult:
        pull_request = self._last_capability(context, "github.get_pr")
        raw_list = self._last_capability(context, "github.list_pull_requests")
        pull_requests: list[PullRequestSummary] = []
        if isinstance(raw_list, dict):
            pull_requests = [
                self._summary(item) for item in raw_list.get("pull_requests", [])
            ]
        elif isinstance(pull_request, dict):
            pull_requests = [self._summary(pull_request)]
        return PullRequestAgentResult(
            operation=self._result_operation(context, raw_list, pull_request),
            answer=context.final_message or "Pull Request 请求已处理。",
            pull_requests=pull_requests,
            pr_number=self._pr_number(context),
            changed_files=self._changed_files(context),
            interpretation=context.code_explanation,
            review=context.code_review,
            review_dialogue=context.review_dialogue,
            ci_analysis=context.ci_analysis,
            plan=context.code_plan,
            candidate=context.code_candidate,
            verification=context.verification,
            merge_readiness=str((context.merge_readiness or {}).get("status", "")),
            execution_result=self._last_write(context),
        )

    @staticmethod
    def _text(context: AgentContext, content: str) -> ModelResponse:
        message = context.append_message({"role": "assistant", "content": content})
        return ModelResponse(content, None, message)

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

    @staticmethod
    def _result_operation(
        context: AgentContext, raw_list: Any, pull_request: Any
    ) -> PullRequestOperation | None:
        writes = {
            "github.merge": PullRequestOperation.MERGE,
            "github.commit": PullRequestOperation.MODIFY,
            "github.post_review": PullRequestOperation.POST_REVIEW,
        }
        for observation in reversed(context.observations):
            payload = observation.get("payload") or {}
            capability_id = str(payload.get("capability_id") or "")
            if observation.get("kind") == "capability" and capability_id in writes:
                return writes[capability_id]
        if context.code_candidate is not None:
            return PullRequestOperation.MODIFY
        if context.code_review is not None:
            return PullRequestOperation.REVIEW
        if context.code_explanation is not None:
            return PullRequestOperation.EXPLAIN
        if context.code_plan is not None:
            return PullRequestOperation.PLAN
        if context.review_dialogue is not None:
            return PullRequestOperation.REVIEW_DIALOGUE
        if context.ci_analysis is not None:
            return PullRequestOperation.CI_ANALYZE
        if raw_list is not None:
            return PullRequestOperation.LIST
        if pull_request is not None:
            return PullRequestOperation.GET
        return None

    def _candidate_approval_summary(self, context: AgentContext) -> str:
        candidate = context.code_candidate
        if candidate is None:
            raise WorkflowError("candidate approval requires CandidatePatch")
        checks = ", ".join(
            f"{check.name}={check.status}"
            for check in (
                context.verification.checks if context.verification is not None else []
            )
        ) or "无静态检查结果"
        return (
            f"将候选改动提交到当前 PR 分支。\n文件：{', '.join(candidate.changed_files)}\n"
            f"摘要：{candidate.summary}\n静态验证：{checks}\n"
            f"风险：{'; '.join(candidate.risks) or '未识别具体风险'}"
        )

    @staticmethod
    def _failed(run: dict[str, Any]) -> bool:
        return str(run.get("conclusion") or run.get("status") or "").casefold() in {
            "failure",
            "failed",
        }

    @staticmethod
    def _current_workflow_runs(runs: Any) -> list[dict[str, Any]]:
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
        full_name = str(source.get("full_name") or "") if isinstance(source, dict) else ""
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
        return str(value.get("ref", "")) if isinstance(value, dict) else str(value or "")
