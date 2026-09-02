"""GitHub Issues Agent using native Capability and Coding Agent calls."""

from __future__ import annotations

import json
from typing import Any

from gitagent.agent_loop import (
    AgentCall,
    AgentResult,
    ModelResponse,
    StructuredCall,
    WaitForUser,
    explicit_wait,
    rejection_feedback,
    wait_for_user_tool,
)
from gitagent.domain.errors import WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    ApprovalIntent,
    ChangeRequest,
    CodingTask,
    IssueAgentResult,
    IssueOperation,
    IssueReplyStage,
    IssueSummary,
    WorkflowTurnDecision,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness, ExecutionProfile
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .guidance import guidance_section

_PROMPTS = get_prompt_library()
_CODING_SCHEMA = {
    "type": "object",
    "properties": {"task": {"type": "string", "minLength": 1}},
    "required": ["task"],
    "additionalProperties": False,
}


ISSUE_AGENT_SPEC = AgentSpec(
    name="issues",
    role=(
        "Observe GitHub Issues and repository evidence, call CodingAgent for code "
        "changes, and submit exact writes through approval."
    ),
    system_prompt=_PROMPTS.text("system.issues"),
    output_schema=(
        "operation",
        "answer",
        "issues",
        "issue_number",
    ),
    agent_depth=1,
    execution_profile=ExecutionProfile.concurrent(),
)


class IssueAgent:
    def __init__(
        self,
        harness: AgentHarness,
        reasoner: Reasoner,
    ) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(ISSUE_AGENT_SPEC)

    def agent_schemas(self) -> dict[str, dict[str, Any]]:
        return {"coding": _CODING_SCHEMA}

    def step(self, context: AgentContext) -> ModelResponse | WaitForUser:
        if context.issue_reply is not None:
            return self._reply_step(context)
        feedback = rejection_feedback(context)
        if feedback is not None and not feedback:
            return self._text(
                context,
                "已按你的要求放弃，未执行任何 Issue 写入。",
            )
        tools = [
            *self.harness.llm_tools(context),
            self.harness.agent_tool(
                "coding",
                (
                    "Delegate one self-contained Issue-scoped code-fix task to a fresh "
                    "Coding Agent. Use only after the required Issue/repository evidence is observed."
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
            raise WorkflowError(f"IssueAgent cannot call {call.agent_id}")
        issue = self._last_capability(context, "github.get_issue")
        if issue is None:
            raise WorkflowError("Issue code fix requires observed Issue evidence")
        task = str(call.arguments["task"])
        request = self._change_request(context, issue, task)
        context.change_request = request
        child.coding_task = CodingTask(
            mode="patch",
            task=task,
            evidence={
                "issue": self._bounded_issue(issue),
                "observations": _capability_evidence(context),
            },
            change_request=request,
        )

    @staticmethod
    def after_agent_result(
        context: AgentContext,
        call: AgentCall,
        result: AgentResult,
        child: AgentContext,
        dispatcher: Any,
    ) -> None:
        del call
        context.change_request = child.change_request
        context.code_candidate = child.code_candidate
        context.verification = child.verification
        context.observations.extend(
            observation
            for observation in child.observations
            if observation.get("kind") == "capability_error"
        )
        if result.status != "completed" or context.code_candidate is None:
            return
        if context.verification is None or not context.verification.passed:
            raise WorkflowError(
                "verification failed; refusing an Issue fix proposal"
            )
        dispatcher.queue_issue_fix(context)

    def _reply_step(self, context: AgentContext) -> ModelResponse | WaitForUser:
        workflow = context.issue_reply
        if workflow is None:  # pragma: no cover - guarded by step
            raise WorkflowError("Issue reply workflow state is missing")
        if workflow.stage == IssueReplyStage.PUBLISH:
            decision = self._latest_user_decision(context)
            if decision is not None and decision.action == ApprovalIntent.REVISE:
                workflow.stage = IssueReplyStage.DRAFT
                workflow.draft = self._revise_reply(
                    context, workflow.draft, decision.instruction
                )
                return WaitForUser(
                    "草稿已修改；你可以继续修改、取消，或再次确认进入发布审批。"
                )
            if decision is not None and decision.action == ApprovalIntent.REJECT:
                return self._text(
                    context, "已取消 Issue 回复；没有发布评论。"
                )
            published = self._last_capability(context, "github.post_comment")
            return self._text(
                context,
                "Issue 回复已发布。" if published is not None else "Issue 回复未发布。",
            )
        if workflow.draft and workflow.decision is not None:
            decision = workflow.decision
            workflow.decision = None
            if decision.action == ApprovalIntent.AMBIGUOUS:
                return WaitForUser(
                    decision.message
                    or "你是想发布当前草稿、继续修改，还是取消？"
                )
            if decision.action == ApprovalIntent.QUESTION:
                return WaitForUser(
                    "你可以继续修改、取消，或确认进入发布审批。"
                )
            if decision.action == ApprovalIntent.REJECT:
                return self._text(
                    context, "已取消 Issue 回复；没有创建发布提案。"
                )
            if decision.action == ApprovalIntent.REVISE:
                workflow.draft = self._revise_reply(
                    context, workflow.draft, decision.instruction
                )
                return WaitForUser(
                    "草稿已修改；你可以继续修改、取消，或确认进入发布审批。"
                )
            if context.entity_id is None or not context.entity_id.isdigit():
                raise WorkflowError("Issue reply workflow is missing its Issue number")
            workflow.stage = IssueReplyStage.PUBLISH
            arguments = {
                "issue_number": int(context.entity_id),
                "body": workflow.draft,
            }
            call_id = context.ensure_capability_tool_call(
                "github.post_comment", arguments
            )
            call = StructuredCall(
                call_id,
                self.harness.function_name("github.post_comment"),
                arguments,
            )
            return ModelResponse(
                f"Publish the reviewed reply to Issue #{context.entity_id}.",
                [call],
                context.messages[-1],
            )
        response = context.reason(
            self.reasoner,
            tools=[
                *self.harness.llm_tools(context, read_only=True),
                wait_for_user_tool(),
            ],
        )
        explicit = explicit_wait(response)
        if isinstance(explicit, WaitForUser) or response.calls:
            return explicit
        workflow.draft = self.draft_reply(context, self.reasoner)
        return WaitForUser(
            "请查看 Issue 回复草稿；你可以提出修改、取消，或确认进入发布审批。"
        )

    def _revise_reply(
        self, context: AgentContext, draft: str, instruction: str
    ) -> str:
        revised = context.complete_text(
            self.reasoner,
            prompt=json.dumps(
                {
                    "task": (
                        "Revise this GitHub Issue reply draft exactly as instructed. "
                        "Return only the complete revised reply and do not claim it was posted."
                    ),
                    "current_draft": draft,
                    "instruction": instruction,
                },
                ensure_ascii=False,
            ),
        ).strip()
        if not revised:
            raise WorkflowError("Issue reply revision returned empty text")
        return revised

    @staticmethod
    def _latest_user_decision(
        context: AgentContext,
    ) -> WorkflowTurnDecision | None:
        for observation in reversed(context.observations):
            if observation.get("kind") != "user_decision":
                continue
            payload = observation.get("payload") or {}
            try:
                action = ApprovalIntent(str(payload.get("action") or ""))
            except ValueError:
                return None
            return WorkflowTurnDecision(
                action,
                instruction=str(payload.get("instruction") or ""),
                message=str(payload.get("message") or ""),
            )
        return None

    def build_result(self, context: AgentContext) -> IssueAgentResult:
        issue_number = self._entity_number(context)
        raw_issues = self._last_capability(context, "github.list_issues")
        issue = self._last_capability(context, "github.get_issue")
        issues: list[IssueSummary] = []
        operation: IssueOperation | None = None
        if raw_issues is not None:
            issues = [
                self._summary(item) for item in raw_issues.get("issues", [])
            ]
            operation = IssueOperation.LIST
        elif issue is not None:
            issues = [self._summary(issue)]
            issue_number = int(issue.get("number", 0))
            operation = IssueOperation.GET
        mutation = self._last_capability_id(
            context,
            {
                "github.create_issue": IssueOperation.CREATE,
                "github.update_issue": IssueOperation.UPDATE,
                "github.set_issue_lock": IssueOperation.UPDATE,
                "github.post_comment": IssueOperation.UPDATE,
            },
        )
        if mutation is not None:
            operation = mutation
        return IssueAgentResult(
            operation=operation,
            answer=context.final_message or "Issue 请求已处理。",
            issues=issues,
            issue_number=issue_number,
        )

    def draft_reply(self, context: AgentContext, reasoner: Reasoner) -> str:
        issue = self._last_capability(context, "github.get_issue")
        if issue is None:
            raise WorkflowError("reply drafting requires Issue evidence")
        comments = (
            self._last_capability(context, "github.get_issue_comments") or {}
        ).get("comments", [])
        return context.complete_text(
            reasoner,
            prompt=_PROMPTS.render(
                "agents.issue_reply_draft",
                request=context.goal,
                evidence=json.dumps(
                    {
                        "issue": self._bounded_issue(issue),
                        "comments": self._bounded_comments(comments),
                    },
                    ensure_ascii=False,
                ),
                guidance=guidance_section(context.guidance),
            ),
        )

    @staticmethod
    def _text(context: AgentContext, content: str) -> ModelResponse:
        message = context.append_message({"role": "assistant", "content": content})
        return ModelResponse(content, [], message)

    @staticmethod
    def _change_request(
        context: AgentContext,
        issue: dict[str, Any],
        task: str,
    ) -> ChangeRequest:
        return ChangeRequest(
            repository=context.repository,
            description=task,
            issue_number=int(issue.get("number", 0)),
            suggested_title=f"Fix #{issue.get('number')}: {issue.get('title', '')}"[:200],
        )

    @staticmethod
    def _entity_number(context: AgentContext) -> int | None:
        value = context.entity_id
        return int(value) if value is not None and str(value).isdigit() else None

    @staticmethod
    def _last_capability(
        context: AgentContext, capability_id: str
    ) -> dict[str, Any] | None:
        for observation in reversed(context.observations):
            payload = observation.get("payload") or {}
            if (
                observation.get("kind") == "capability"
                and payload.get("capability_id") == capability_id
            ):
                data = payload.get("data")
                return dict(data) if isinstance(data, dict) else None
        return None

    @staticmethod
    def _last_capability_id(
        context: AgentContext,
        operations: dict[str, IssueOperation],
    ) -> IssueOperation | None:
        for observation in reversed(context.observations):
            payload = observation.get("payload") or {}
            capability_id = str(payload.get("capability_id") or "")
            if observation.get("kind") == "capability" and capability_id in operations:
                return operations[capability_id]
        return None

    @classmethod
    def _summary(cls, issue: dict[str, Any]) -> IssueSummary:
        user = issue.get("user") or {}
        author = str(user.get("login", "")) if isinstance(user, dict) else ""
        return IssueSummary(
            number=int(issue.get("number", 0)),
            title=str(issue.get("title", "")),
            state=str(issue.get("state", "open")),
            locked=bool(issue.get("locked")),
            labels=cls._labels(issue),
            assignees=cls._assignees(issue),
            milestone=cls._milestone_title(issue),
            author=author,
            updated_at=str(issue.get("updated_at", "")),
            url=str(issue.get("html_url") or issue.get("url") or ""),
        )

    @staticmethod
    def _labels(issue: dict[str, Any]) -> list[str]:
        return [
            str(item.get("name", "")) if isinstance(item, dict) else str(item)
            for item in issue.get("labels", [])
        ]

    @staticmethod
    def _assignees(issue: dict[str, Any]) -> list[str]:
        return [
            str(item.get("login", "")) if isinstance(item, dict) else str(item)
            for item in issue.get("assignees", [])
        ]

    @staticmethod
    def _milestone_title(issue: dict[str, Any]) -> str | None:
        milestone = issue.get("milestone")
        if not milestone:
            return None
        return str(milestone.get("title", "")) if isinstance(milestone, dict) else str(milestone)

    @classmethod
    def _bounded_issue(cls, issue: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": issue.get("number"),
            "title": str(issue.get("title", ""))[:500],
            "state": issue.get("state", "open"),
            "locked": bool(issue.get("locked")),
            "labels": cls._labels(issue),
            "assignees": cls._assignees(issue),
            "milestone": cls._milestone_title(issue),
            "body": str(issue.get("body") or "")[:4000],
            "comments": issue.get("comments", 0),
            "updated_at": issue.get("updated_at", ""),
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


def _capability_evidence(context: AgentContext) -> list[Any]:
    return [
        item.get("payload")
        for item in context.observations
        if item.get("kind") in {"capability", "capability_error"}
    ]
