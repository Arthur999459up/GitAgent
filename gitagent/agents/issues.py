"""GitHub Issues Agent using native Capability and Coding Agent calls."""

from __future__ import annotations

import json
from typing import Any

from gitagent.agent_loop import (
    AgentCall,
    AgentResult,
    ModelResponse,
    rejection_feedback,
)
from gitagent.domain.errors import WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    CandidatePreparationResult,
    ChangeRequest,
    DomainAction,
    IssueAgentResult,
    IssueOperation,
    IssueSummary,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.validation.static import StaticVerifier
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .coding import CodingAgent
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
        "action",
        "operation",
        "answer",
        "issues",
        "issue_number",
        "question",
    ),
)


class IssueAgent:
    def __init__(
        self,
        harness: AgentHarness,
        coding: CodingAgent,
        verifier: StaticVerifier,
        reasoner: Reasoner,
    ) -> None:
        self.harness = harness
        self.coding = coding
        self.verifier = verifier
        self.reasoner = reasoner
        harness.register(ISSUE_AGENT_SPEC)

    def agent_schemas(self) -> dict[str, dict[str, Any]]:
        return {"coding": _CODING_SCHEMA}

    def step(self, context: AgentContext) -> ModelResponse:
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
        ]
        return context.reason(self.reasoner, tools=tools)

    def invoke_child(
        self, context: AgentContext, call: AgentCall
    ) -> AgentResult:
        if call.agent_id != "coding":
            raise WorkflowError(f"IssueAgent cannot call {call.agent_id}")
        issue = self._last_capability(context, "github.get_issue")
        if issue is None:
            raise WorkflowError("Issue code fix requires observed Issue evidence")
        task = str(call.arguments["task"])
        request = self._change_request(context, issue, task)
        context.change_request = request
        result, artifact = self.coding.run_call(
            context,
            call_id=call.call_id,
            mode="patch",
            task=task,
            evidence={
                "issue": self._bounded_issue(issue),
                "observations": _capability_evidence(context),
            },
            change_request=request,
            verifier=self.verifier,
        )
        if isinstance(artifact, CandidatePreparationResult):
            context.code_candidate = artifact.candidate
            context.verification = artifact.verification
            if artifact.capability_error:
                context.observations.append(
                    {"kind": "capability_error", "payload": artifact.capability_error}
                )
        return result

    @staticmethod
    def after_agent_result(
        context: AgentContext,
        call: AgentCall,
        result: AgentResult,
        dispatcher: Any,
    ) -> None:
        del call
        if result.status != "completed" or context.code_candidate is None:
            return
        if context.verification is None or not context.verification.passed:
            raise WorkflowError(
                "static verification failed; refusing an Issue fix proposal"
            )
        dispatcher.queue_issue_fix(context)

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
            action=DomainAction.ANSWER,
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
        return ModelResponse(content, None, message)

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
