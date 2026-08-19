"""Pull Request agent driven by the generic observe -> decide -> act loop."""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import LLMProviderError, ValidationError, WorkflowError
from ..core.models import (
    AgentSpec,
    DomainAction,
    PRReviewResult,
    PullRequestAgentResult,
    PullRequestOperation,
    PullRequestSummary,
    Route,
)
from ..core.trace import TraceCategory, TraceStatus
from ..prompts import get_prompt_library
from ..reasoning import Reasoner
from ..runtime import (
    AgentAction,
    AgentActionKind,
    AgentContext,
    AgentHarness,
    render_observations,
)
from .decide import AGENT_ACTION_SCHEMA, parse_action
from .guidance import guidance_section
from .pr_review import PRReviewAgent

_PROMPTS = get_prompt_library()

PULL_REQUEST_AGENT_SPEC = AgentSpec(
    name="pull_requests",
    role=(
        "Observe Pull Requests, diffs, CI runs, and reviews, then decide the next action. "
        "Every GitHub write and every formal review invocation is proposed to the loop and "
        "requires explicit user approval before execution."
    ),
    system_prompt=_PROMPTS.text("system.pull_requests"),
    allowed_tools=frozenset(
        {
            "github.list_pull_requests",
            "github.get_pr",
            "github.get_pr_comments",
            "github.get_pr_reviews",
            "github.get_workflow_runs",
            "github.get_job_logs",
            "github.post_review",
            "github.merge",
            "repository.get_repo_tree",
            "repository.search_code",
            "repository.read_file",
            "repository.read_files",
            "repository.find_symbol",
            "repository.find_references",
            "repository.get_pr_diff",
            "repository.get_changed_files",
        }
    ),
    output_schema=(
        "action",
        "operation",
        "answer",
        "pull_requests",
        "pr_number",
        "requested_outcome",
        "changed_files",
        "question",
    ),
    capabilities=frozenset({Route.PULL_REQUEST}),
    required_context=("repository",),
    routing_examples=(
        "看看有哪些开放的 PR",
        "列出合并到 main 的 Pull Requests",
        "总结最近的 PR",
        "查看 PR #17 的讨论和变更",
        "审查 PR #17",
        "批准 PR #17",
        "要求修改 PR #17",
        "合并 PR #17",
    ),
)


class PullRequestAgent:
    def __init__(
        self,
        harness: AgentHarness,
        reviewer: PRReviewAgent,
        reasoner: Reasoner | None = None,
    ) -> None:
        self.harness = harness
        self.reviewer = reviewer
        self.reasoner = reasoner
        harness.register(PULL_REQUEST_AGENT_SPEC)

    def decide(self, context: AgentContext) -> AgentAction:
        required = self._required_entity_evidence(context)
        if required is not None:
            return required
        if self.reasoner is not None:
            try:
                return self._llm_decide(context)
            except (LLMProviderError, ValidationError) as exc:
                self.harness.trace.emit(
                    session_id=context.session_id,
                    category=TraceCategory.AGENT,
                    name="pull_requests.decide",
                    status=TraceStatus.PROGRESS,
                    message=f"LLM decide failed; using minimal fallback ({type(exc).__name__}: {exc})",
                )
        return self._fallback_decide(context)

    def build_result(self, context: AgentContext) -> PullRequestAgentResult:
        review_data = self._last_tool(context, "specialist:pr_review")
        raw_list = self._last_tool(context, "github.list_pull_requests")
        pull_request = self._last_tool(context, "github.get_pr")
        pr_number = self._entity_number(context)
        if review_data is not None:
            return PullRequestAgentResult(
                action=DomainAction.ANSWER,
                operation=None,
                answer=context.final_message or str(review_data.get("body", "")),
                pull_requests=[self._summary(pull_request)] if pull_request is not None else [],
                pr_number=pr_number,
            )
        if raw_list is not None:
            pull_requests = [self._summary(item) for item in raw_list.get("pull_requests", [])]
            return PullRequestAgentResult(
                action=DomainAction.ANSWER,
                operation=PullRequestOperation.LIST,
                answer=context.final_message or self._list_answer(context, raw_list.get("pull_requests", []), pull_requests),
                pull_requests=pull_requests,
            )
        if pull_request is not None:
            comments = (self._last_tool(context, "github.get_pr_comments") or {}).get("comments", [])
            changed_files = (self._last_tool(context, "repository.get_changed_files") or {}).get("files", [])
            diff = (self._last_tool(context, "repository.get_pr_diff") or {}).get("diff", "")
            return PullRequestAgentResult(
                action=DomainAction.ANSWER,
                operation=PullRequestOperation.GET,
                answer=context.final_message or self._detail_answer(context, pull_request, comments, changed_files, diff),
                pull_requests=[self._summary(pull_request)],
                pr_number=int(pull_request.get("number", 0)),
                changed_files=changed_files,
            )
        return PullRequestAgentResult(
            action=DomainAction.ANSWER,
            operation=None,
            answer=context.final_message or "Pull Request 操作已完成。",
            pull_requests=[],
            pr_number=pr_number,
        )

    def run_specialist(self, context: AgentContext, specialist: str) -> dict[str, Any]:
        if specialist != "pr_review":
            raise WorkflowError(f"pull_requests agent has no approved specialist: {specialist}")
        pr_number = self._entity_number(context)
        if pr_number is None:
            raise WorkflowError("pr_review specialist requires a pull-request number")
        review = self.reviewer.review(
            context.repository,
            pr_number,
            session_id=context.session_id,
            guidance=context.guidance,
        )
        return {
            "recommendation": review.recommendation.value,
            "risk_level": review.risk_level,
            "body": self._review_body_from_result(review),
        }

    def _llm_decide(self, context: AgentContext) -> AgentAction:
        allowed_tools = self.harness.spec("pull_requests").allowed_tools
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.pull_request_decide",
                goal=context.goal,
                entity=f"Pull Request #{context.entity_id}" if context.entity_id else "no specific Pull Request selected",
                observations=render_observations(context),
                budget=str(max(0, context.max_steps - context.steps)),
                guidance=guidance_section(context.guidance),
            ),
            schema=AGENT_ACTION_SCHEMA,
            tool_name="decide_action",
            tools=self.harness.client.llm_tools(allowed_tools),
        )
        if value.get("kind") == "tool":
            value["tool"] = self.harness.client.resolve_llm_tool_name(str(value.get("tool", "")), allowed_tools)
        action = parse_action(value, requires_candidate=False)
        if action.kind == AgentActionKind.APPLY_CODE_CHANGE:
            raise ValidationError("pull_requests agent cannot apply code changes directly")
        return action

    def _required_entity_evidence(self, context: AgentContext) -> AgentAction | None:
        pr_number = self._entity_number(context)
        if pr_number is not None and self._last_tool(context, "github.get_pr") is None:
            return AgentAction(
                AgentActionKind.TOOL,
                tool="github.get_pr",
                arguments={"pr_number": pr_number},
                summary="读取 Pull Request",
            )
        return None

    def _fallback_decide(self, context: AgentContext) -> AgentAction:
        pr_number = self._entity_number(context)
        if pr_number is not None:
            if self._last_tool(context, "github.get_pr") is None:
                return AgentAction(
                    AgentActionKind.TOOL,
                    tool="github.get_pr",
                    arguments={"pr_number": pr_number},
                    summary="读取 Pull Request",
                )
            return AgentAction(AgentActionKind.FINISH, summary="Pull Request 已读取", message="")
        if self._last_tool(context, "github.list_pull_requests") is None:
            return AgentAction(
                AgentActionKind.TOOL,
                tool="github.list_pull_requests",
                arguments={"state": "open", "limit": 20},
                summary="列出 Pull Requests",
            )
        return AgentAction(AgentActionKind.FINISH, summary="Pull Requests 已列出", message="")

    def _list_answer(
        self,
        context: AgentContext,
        raw_pull_requests: list[dict[str, Any]],
        pull_requests: list[PullRequestSummary],
    ) -> str:
        if not pull_requests:
            return "没有找到符合当前条件的 Pull Request。"
        if self.reasoner:
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
        if self.reasoner:
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
        details = []
        if comments:
            details.append(f"{len(comments)} 条评论")
        if changed_files:
            details.append(f"{len(changed_files)} 个变更文件")
        suffix = f"\n\n包含 {'、'.join(details)}。" if details else ""
        return f"#{pull_request.get('number')} {pull_request.get('title', '')}\n\n{body[:4000]}{suffix}"

    @staticmethod
    def _review_body_from_result(review: PRReviewResult) -> str:
        changes = "\n".join(f"- {item}" for item in review.important_changes) or "- None"
        issues = "\n".join(f"- {item}" for item in review.potential_issues) or "- No concrete issue found"
        return (
            f"## Summary\n{review.summary}\n\n## Important changes\n{changes}\n\n"
            f"## Potential issues\n{issues}\n\n## Test assessment\n{review.test_assessment}\n\n"
            f"Recommendation: {review.recommendation.value}; risk: {review.risk_level}."
        )

    @staticmethod
    def _require_mergeable(pull_request: dict[str, Any]) -> None:
        if str(pull_request.get("state", "open")).casefold() != "open":
            raise WorkflowError("only an open Pull Request can be merged")
        if bool(pull_request.get("draft")):
            raise WorkflowError("a draft Pull Request cannot be merged")

    @staticmethod
    def _entity_number(context: AgentContext) -> int | None:
        if context.entity_id is None or not str(context.entity_id).isdigit():
            return None
        return int(context.entity_id)

    @staticmethod
    def _last_tool(context: AgentContext, tool: str) -> Any:
        for observation in reversed(context.observations):
            if observation["kind"] == "tool" and observation["payload"].get("tool") == tool:
                return observation["payload"].get("data")
        return None

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
                "author": (comment.get("user") or {}).get("login", "") if isinstance(comment.get("user"), dict) else "",
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
