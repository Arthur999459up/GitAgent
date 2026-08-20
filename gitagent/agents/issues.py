"""GitHub Issues agent driven by the generic observe -> decide -> act loop."""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import LLMProviderError, ValidationError, WorkflowError
from ..core.models import (
    AgentSpec,
    ChangeRequest,
    DomainAction,
    IssueAgentResult,
    IssueOperation,
    IssueSummary,
    Replacement,
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
    rejection_feedback,
    render_observations,
)
from ..verification import StaticVerifier
from .coding import CodingAgent, prepare_verified_candidate
from .decide import AGENT_ACTION_SCHEMA, parse_action
from .guidance import guidance_section

_PROMPTS = get_prompt_library()
_MAX_FILE_READS = 5
_ISSUE_ACTION_SCHEMA = {
    **AGENT_ACTION_SCHEMA,
    "properties": {
        **AGENT_ACTION_SCHEMA["properties"],
        "awaiting_user_confirmation": {
            "type": "boolean",
            "description": (
                "True only when repository evidence supports a code change but the agent must wait for the user's "
                "confirmation before preparing it."
            ),
        },
    },
    "required": [*AGENT_ACTION_SCHEMA["required"], "awaiting_user_confirmation"],
}

ISSUE_AGENT_SPEC = AgentSpec(
    name="issues",
    role=(
        "Observe GitHub Issues and repository evidence, then decide the next action. "
        "Every GitHub write is proposed to the loop and requires explicit user approval before execution."
    ),
    system_prompt=_PROMPTS.text("system.issues"),
    allowed_tools=frozenset(
        {
            "github.list_issues",
            "github.get_issue",
            "github.get_issue_comments",
            "github.post_comment",
            "repository.get_repo_tree",
            "repository.search_code",
            "repository.read_file",
            "repository.read_files",
            "repository.find_symbol",
            "repository.find_references",
        }
    ),
    output_schema=("action", "operation", "answer", "issues", "issue_number", "question"),
    capabilities=frozenset({Route.ISSUE}),
    required_context=("repository",),
    routing_examples=(
        "看看有哪些 Issues",
        "列出当前未关闭的 bug",
        "总结最近的 Issues",
        "查看 Issue #17 的讨论",
        "处理 Issue #17",
        "修复 Issue #17",
    ),
)


class IssueAgent:
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
        harness.register(ISSUE_AGENT_SPEC)

    def decide(self, context: AgentContext) -> AgentAction:
        if context.reply_draft is not None:
            return self._publish_draft_decide(context)
        draft_pr = self._last_tool(context, "github.create_draft_pr")
        if draft_pr is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="代码修复已发布为 Draft PR",
                message=f"已创建修复 Draft PR #{draft_pr.get('number')}。",
            )
        if context.code_candidate is not None:
            if rejection_feedback(context) is not None and not rejection_feedback(context):
                return self._abandon()
            return AgentAction(
                AgentActionKind.APPLY_CODE_CHANGE,
                summary="提交 Coding Agent 生成并验证的候选补丁供你审阅",
            )
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
                    name="issues.decide",
                    status=TraceStatus.PROGRESS,
                    message=f"LLM decide failed; using minimal fallback ({type(exc).__name__}: {exc})",
                )
        return self._fallback_decide(context)

    def build_result(self, context: AgentContext) -> IssueAgentResult:
        draft_pr = self._last_tool(context, "github.create_draft_pr")
        issue_number = self._entity_number(context)
        if draft_pr is not None:
            return IssueAgentResult(
                action=DomainAction.ANSWER,
                operation=IssueOperation.GET,
                answer=context.final_message or f"已创建修复 Draft PR #{draft_pr.get('number')}。",
                issues=[],
                issue_number=issue_number,
            )
        posted = self._last_tool(context, "github.post_comment")
        raw_issues = self._last_tool(context, "github.list_issues")
        issue = self._last_tool(context, "github.get_issue")
        latest_view = self._last_tool_name(
            context,
            {"github.post_comment", "github.list_issues", "github.get_issue", "github.get_issue_comments"},
        )
        if latest_view == "github.post_comment" and posted is not None:
            return IssueAgentResult(
                action=DomainAction.ANSWER,
                operation=None,
                answer=context.final_message
                or (f"已发布回复到 Issue #{issue_number}。" if issue_number is not None else "Issue 回复已发布。"),
                issues=[],
                issue_number=issue_number,
            )
        if latest_view in {"github.get_issue", "github.get_issue_comments"} and issue is not None:
            comments = (self._last_tool(context, "github.get_issue_comments") or {}).get("comments", [])
            return IssueAgentResult(
                action=DomainAction.ANSWER,
                operation=IssueOperation.GET,
                answer=context.final_message or self._detail_answer(context, issue, comments),
                issues=[self._summary(issue)],
                issue_number=int(issue.get("number", 0)),
            )
        if raw_issues is not None:
            issues = [self._summary(item) for item in raw_issues.get("issues", [])]
            return IssueAgentResult(
                action=DomainAction.ANSWER,
                operation=IssueOperation.LIST,
                answer=context.final_message or self._list_answer(context, raw_issues.get("issues", []), issues),
                issues=issues,
            )
        if issue is not None:
            comments = (self._last_tool(context, "github.get_issue_comments") or {}).get("comments", [])
            return IssueAgentResult(
                action=DomainAction.ANSWER,
                operation=IssueOperation.GET,
                answer=context.final_message or self._detail_answer(context, issue, comments),
                issues=[self._summary(issue)],
                issue_number=int(issue.get("number", 0)),
            )
        return IssueAgentResult(
            action=DomainAction.ANSWER,
            operation=None,
            answer=context.final_message or "Issue 操作已完成。",
            issues=[],
            issue_number=issue_number,
        )

    def run_specialist(self, context: AgentContext, specialist: str) -> dict[str, Any]:
        raise WorkflowError(f"issues agent has no approved specialist: {specialist}")

    def draft_reply(self, context: AgentContext, reasoner: Reasoner) -> str:
        if self._last_tool(context, "github.get_issue") is None:
            raise WorkflowError("reply drafting requires Issue evidence")
        return self._draft_reply(context, reasoner)

    def _publish_draft_decide(self, context: AgentContext) -> AgentAction:
        issue_number = self._entity_number(context)
        if issue_number is None:
            return AgentAction(AgentActionKind.ASK, question="发布回复前需要明确的 Issue 编号。")
        if self._last_tool(context, "github.post_comment") is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="Issue 回复已发布",
                message=f"已发布回复到 Issue #{issue_number}。",
            )
        return AgentAction(
            AgentActionKind.TOOL,
            tool="github.post_comment",
            arguments={"issue_number": issue_number, "body": context.reply_draft},
            summary=f"在 Issue #{issue_number} 发布已确认的回复草稿",
        )

    def _llm_decide(self, context: AgentContext) -> AgentAction:
        allowed_tools = self.harness.spec("issues").allowed_tools
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.issue_decide",
                goal=context.goal,
                repository=context.repository,
                entity=f"Issue #{context.entity_id}" if context.entity_id else "no specific Issue selected",
                observations=render_observations(context),
                budget=str(max(0, context.max_steps - context.steps)),
                guidance=guidance_section(context.guidance),
            ),
            schema=_ISSUE_ACTION_SCHEMA,
            tool_name="decide_action",
            tools=self.harness.client.llm_tools(allowed_tools),
        )
        if value.get("kind") == "tool":
            value["tool"] = self.harness.client.resolve_llm_tool_name(str(value.get("tool", "")), allowed_tools)
        if bool(value.get("awaiting_user_confirmation")):
            question = str(value.get("question") or value.get("message") or "").strip()
            if not question:
                raise ValidationError("Issue confirmation state requires a question")
            return AgentAction(
                AgentActionKind.ASK,
                summary=str(value.get("summary") or "等待用户确认是否继续修改"),
                question=question,
            )
        if value.get("kind") == AgentActionKind.FINISH.value and str(value.get("question") or "").strip():
            return AgentAction(
                AgentActionKind.ASK,
                summary=str(value.get("summary") or "等待用户补充信息"),
                question=str(value["question"]).strip(),
            )
        if value.get("kind") == AgentActionKind.APPLY_CODE_CHANGE.value:
            if context.code_candidate is None:
                try:
                    self._prepare_code_change(context)
                except (LLMProviderError, ValidationError) as exc:
                    raise WorkflowError(f"code candidate preparation failed: {exc}") from exc
            return AgentAction(
                AgentActionKind.APPLY_CODE_CHANGE,
                summary=str(value.get("summary") or "提交 Coding Agent 生成并验证的候选补丁供你审阅"),
            )
        action = parse_action(value, requires_candidate=False)
        if action.kind == AgentActionKind.SPECIALIST:
            raise ValidationError("issues agent does not expose a specialist action")
        if action.kind == AgentActionKind.TOOL and action.tool == "repository.read_file":
            return self._normalize_file_read(context, action)
        return action

    def _required_entity_evidence(self, context: AgentContext) -> AgentAction | None:
        issue_number = self._entity_number(context)
        if issue_number is not None and self._last_tool(context, "github.get_issue") is None:
            return AgentAction(
                AgentActionKind.TOOL,
                tool="github.get_issue",
                arguments={"issue_number": issue_number},
                summary="读取 Issue",
            )
        return None

    def _fallback_decide(self, context: AgentContext) -> AgentAction:
        issue_number = self._entity_number(context)
        if issue_number is not None:
            if self._last_tool(context, "github.get_issue") is None:
                return AgentAction(
                    AgentActionKind.TOOL,
                    tool="github.get_issue",
                    arguments={"issue_number": issue_number},
                    summary="读取 Issue",
                )
            return AgentAction(AgentActionKind.FINISH, summary="Issue 已读取", message="")
        if self._last_tool(context, "github.list_issues") is None:
            return AgentAction(
                AgentActionKind.TOOL,
                tool="github.list_issues",
                arguments={"state": "open", "labels": [], "limit": 20},
                summary="列出 Issues",
            )
        return AgentAction(AgentActionKind.FINISH, summary="Issues 已列出", message="")

    def _answer_from_observations(self, context: AgentContext) -> str:
        if self.reasoner is None:
            return "已有证据足够，请基于已读取的 Issue 信息继续。"
        return self.reasoner.complete_text(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.issue_detail_answer",
                request=context.goal,
                evidence=render_observations(context),
                guidance=guidance_section(context.guidance),
            ),
        )

    def _list_answer(
        self,
        context: AgentContext,
        raw_issues: list[dict[str, Any]],
        issues: list[IssueSummary],
    ) -> str:
        if not issues:
            return "没有找到符合当前条件的 Issue。"
        if self.reasoner:
            evidence = [self._bounded_issue(issue) for issue in raw_issues]
            return self.reasoner.complete_text(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.issue_list_summarize",
                    request=context.goal,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(context.guidance),
                ),
            )
        return f"找到 {len(issues)} 个符合条件的 Issue。"

    def _detail_answer(self, context: AgentContext, issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
        if self.reasoner:
            repository_evidence = [
                observation["payload"]
                for observation in context.observations
                if observation.get("kind") == "tool"
                and str((observation.get("payload") or {}).get("tool") or "").startswith("repository.")
            ][-12:]
            evidence = {
                "issue": self._bounded_issue(issue),
                "comments": self._bounded_comments(comments),
                "repository_evidence": repository_evidence,
            }
            return self.reasoner.complete_text(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.issue_detail_answer",
                    request=context.goal,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(context.guidance),
                ),
            )
        body = str(issue.get("body") or "暂无正文").strip()
        suffix = f"\n\n共有 {len(comments)} 条评论。" if comments else ""
        return f"#{issue.get('number')} {issue.get('title', '')}\n\n{body[:4000]}{suffix}"

    def _change_request(self, context: AgentContext, issue: dict[str, Any]) -> ChangeRequest:
        raw = issue.get("change_request") or {}
        if not raw and self.reasoner is not None:
            raw = self.reasoner.complete_structured(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.issue_fix_guide",
                    issue=json.dumps(self._bounded_issue(issue), ensure_ascii=False),
                    observations=render_observations(context),
                    guidance=guidance_section(context.guidance),
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "target_files": {"type": "array", "items": {"type": "string"}},
                        "suggested_title": {"type": "string"},
                    },
                    "required": ["description", "target_files", "suggested_title"],
                },
                tool_name="prepare_fix_guide",
            )
        replacements = [
            Replacement(path=str(item["path"]), old=str(item["old"]), new=str(item["new"]))
            for item in raw.get("replacements", [])
        ]
        target_files = [str(path) for path in raw.get("target_files", [])]
        proposed_files = {str(path): str(content) for path, content in raw.get("proposed_files", {}).items()}
        return ChangeRequest(
            repository=context.repository,
            description=str(raw.get("description") or f"Resolve issue #{issue.get('number')}: {issue.get('title', '')}"),
            base_branch=str(raw.get("base_branch") or "main"),
            target_files=target_files,
            replacements=replacements,
            proposed_files=proposed_files,
            issue_number=int(issue.get("number", 0)),
            suggested_title=raw.get("suggested_title"),
        )

    def _prepare_code_change(self, context: AgentContext) -> None:
        issue = self._last_tool(context, "github.get_issue")
        if issue is None:
            raise WorkflowError("code repair requires Issue evidence")
        request = self._change_request(context, issue)
        candidate, report = prepare_verified_candidate(
            self.coding,
            self.verifier,
            request,
            session_id=context.session_id,
            guidance=context.guidance,
        )
        if not report.passed:
            raise WorkflowError("静态验证失败；拒绝生成 GitHub 变更提案")
        context.change_request = request
        context.code_candidate = candidate
        context.verification = report
        context.observations.append(
            {
                "kind": "agent",
                "payload": {
                    "agent": "coding",
                    "summary": candidate.summary,
                    "changed_files": list(candidate.changed_files),
                    "verification_passed": report.passed,
                },
            }
        )

    def _draft_reply(self, context: AgentContext, reasoner: Reasoner) -> str:
        return reasoner.complete_text(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.issue_reply_draft",
                request=context.goal,
                evidence=render_observations(context),
                guidance=guidance_section(context.guidance),
            ),
        ).strip()

    def _normalize_file_read(self, context: AgentContext, action: AgentAction) -> AgentAction:
        path = str(action.arguments.get("path") or "")
        ref = action.arguments.get("ref")
        reads = [
            observation["payload"]
            for observation in context.observations
            if observation["kind"] == "tool"
            and observation["payload"].get("tool") == "repository.read_file"
            and observation["payload"].get("arguments", {}).get("path") == path
            and observation["payload"].get("arguments", {}).get("ref") == ref
        ]
        if not reads:
            return action
        if len(reads) >= _MAX_FILE_READS:
            return AgentAction(
                AgentActionKind.FINISH,
                summary=f"{path} 已达到 {_MAX_FILE_READS} 次读取上限，使用已有证据继续",
                message=self._answer_from_observations(context) if context.result_required else "",
            )
        data = reads[-1].get("data")
        if not isinstance(data, dict) or not isinstance(data.get("end_line"), int):
            return action
        if data.get("truncated") is not True:
            return action
        continuation = dict(action.arguments)
        continuation["start_line"] = data["end_line"] + 1
        return AgentAction(
            AgentActionKind.TOOL,
            tool="repository.read_file",
            arguments=continuation,
            summary=f"续读 {path}，从第 {continuation['start_line']} 行开始",
        )

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

    @staticmethod
    def _last_tool_name(context: AgentContext, tools: set[str]) -> str | None:
        for observation in reversed(context.observations):
            if observation["kind"] != "tool":
                continue
            tool = str(observation["payload"].get("tool") or "")
            if tool in tools:
                return tool
        return None

    @staticmethod
    def _abandon() -> AgentAction:
        return AgentAction(
            AgentActionKind.FINISH,
            summary="已放弃",
            message="已按你的要求放弃，未执行任何写入。",
        )

    @classmethod
    def _summary(cls, issue: dict[str, Any]) -> IssueSummary:
        user = issue.get("user") or issue.get("author") or ""
        author = str(user.get("login", "")) if isinstance(user, dict) else str(user)
        return IssueSummary(
            number=int(issue.get("number", 0)),
            title=str(issue.get("title", "")),
            state=str(issue.get("state", "open")),
            labels=cls._labels(issue),
            author=author,
            updated_at=str(issue.get("updated_at", "")),
            url=str(issue.get("html_url") or issue.get("url") or ""),
        )

    @staticmethod
    def _labels(issue: dict[str, Any]) -> list[str]:
        return [str(item.get("name", "")) if isinstance(item, dict) else str(item) for item in issue.get("labels", [])]

    @classmethod
    def _bounded_issue(cls, issue: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": issue.get("number"),
            "title": str(issue.get("title", ""))[:500],
            "state": issue.get("state", "open"),
            "labels": cls._labels(issue),
            "body": str(issue.get("body") or "")[:4000],
            "comments": issue.get("comments", 0),
            "created_at": issue.get("created_at", ""),
            "updated_at": issue.get("updated_at", ""),
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
