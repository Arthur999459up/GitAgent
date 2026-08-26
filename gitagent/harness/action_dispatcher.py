"""Harness-side action dispatch, constraints, approvals, and recovery."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop.actions import AgentAction, AgentActionKind, AgentLoopAgent, PendingAction
from gitagent.domain.errors import PermissionDenied, ToolExecutionError, ValidationError, WorkflowError
from gitagent.domain.models import AccessLevel, ApprovalIntent, MutationRejectedResult, PlannedToolCall, WorkflowTurnDecision
from gitagent.harness.recovery.github_mutations import (
    code_change_review_package,
    issue_fix_approval_summary,
    issue_fix_mutation_plan,
    repository_change_approval_summary,
    repository_change_mutation_plan,
)
from gitagent.infra.observability import TraceCategory, TraceStatus


class HarnessActionDispatcher:
    """Apply Harness policy to actions requested by the Agent Loop."""

    def __init__(self, harness: Any) -> None:
        self.harness = harness

    def restore_pending(self, context: Any, *, summary: str, calls: list[PlannedToolCall]) -> None:
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
        )
        context.pending = PendingAction(approval.approval_id, summary, calls)

    def apply_user_decision(self, context: Any, decision: WorkflowTurnDecision) -> None:
        if context.question:
            self.observe(context, "assistant", context.question)
            self.observe(context, "user", decision.instruction or decision.message or "")
            context.question = ""
            return

        pending = context.pending
        if pending is None:
            raise WorkflowError("agent context is not waiting for user input")

        if decision.action == ApprovalIntent.APPROVE:
            self.harness.approvals.decide(pending.approval_id, "Approve")
            context.pending = None
            try:
                self._execute_pending(context, pending)
            except ToolExecutionError as exc:
                context.result = MutationRejectedResult(
                    summary=pending.summary or "GitHub 操作",
                    reason=exc.user_message,
                )
                context.finished = True
            return

        self.harness.approvals.decide(pending.approval_id, "Reject")
        self.observe(context, "rejection", {"instruction": (decision.instruction or "").strip()})
        context.pending = None

    def handle(self, context: Any, agent: AgentLoopAgent, action: AgentAction) -> bool:
        if action.kind == AgentActionKind.TOOL:
            return self._handle_tool(context, agent, action)
        if action.kind == AgentActionKind.APPLY_ISSUE_FIX:
            return self._handle_issue_fix(context)
        if action.kind == AgentActionKind.APPLY_REPOSITORY_CHANGE:
            return self._handle_repository_change(context)
        if action.kind == AgentActionKind.ASK:
            context.question = action.question or action.summary
            return False
        if action.kind == AgentActionKind.FINISH:
            context.final_message = action.message or context.final_message
            if context.result_required:
                context.result = agent.build_result(context)
            context.finished = True
            return False
        raise ValidationError(f"unknown action kind: {action.kind}")

    def _handle_tool(self, context: Any, agent: AgentLoopAgent, action: AgentAction) -> bool:
        if not action.tool:
            raise ValidationError("tool action requires a tool name")
        spec = self.harness.spec(context.agent)
        if action.tool not in spec.allowed_tools:
            raise PermissionDenied(f"agent {context.agent} is not allowed to use {action.tool}")

        arguments = dict(action.arguments)
        if "repository" not in arguments and (
            action.tool.startswith("github.") or action.tool.startswith("repository.")
        ):
            arguments["repository"] = context.repository

        tool = self.harness.server.get_tool(action.tool)
        if tool.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}:
            if context.read_only:
                self.observe(
                    context,
                    "policy",
                    {"tool": action.tool, "blocked": "read_only context forbids mutation proposals"},
                )
                context.final_message = "只读取证阶段已完成；未生成或执行写操作。"
                context.result = agent.build_result(context)
                context.finished = True
                return False
            call = PlannedToolCall(action.tool, arguments)
            approval = self.harness.approvals.create(
                session_id=context.session_id,
                repository=context.repository,
                summary=action.summary or f"执行 {action.tool}",
                calls=[call],
            )
            context.pending = PendingAction(approval.approval_id, approval.summary, [call])
            return False

        context.tool(action.tool, **arguments)
        call = context.last_tool_call
        if call is None:
            raise WorkflowError("tool execution did not record its result")
        payload = {"tool": action.tool, "arguments": call.arguments, "data": call.observation_data}
        if call.cached:
            payload["cached"] = True
        if call.covered:
            payload["covered"] = True
        self.observe(context, "tool", payload)
        return True

    def _handle_issue_fix(self, context: Any) -> bool:
        if context.code_candidate is None or context.change_request is None:
            raise WorkflowError("apply_issue_fix requires a verified candidate and change request")
        if context.verification is not None and not context.verification.passed:
            raise WorkflowError("static verification failed; GitHub mutation is forbidden")
        review = code_change_review_package(context.change_request, context.code_candidate, context.verification)
        calls = issue_fix_mutation_plan(context.session_id, context.change_request, context.code_candidate, review)
        summary = issue_fix_approval_summary(context.change_request, review)
        self._queue(context, summary, calls)
        return False

    def _handle_repository_change(self, context: Any) -> bool:
        if context.code_candidate is None or context.change_request is None:
            raise WorkflowError("apply_repository_change requires a verified candidate and change request")
        if context.verification is None or not context.verification.passed:
            raise WorkflowError("static verification failed; default-branch mutation is forbidden")
        calls = repository_change_mutation_plan(context.change_request, context.code_candidate)
        summary = repository_change_approval_summary(
            context.change_request,
            context.code_candidate,
            context.verification,
        )
        self._queue(context, summary, calls)
        return False

    def _queue(self, context: Any, summary: str, calls: list[PlannedToolCall]) -> None:
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
        )
        context.pending = PendingAction(approval.approval_id, approval.summary, calls)

    def _execute_pending(self, context: Any, pending: PendingAction) -> None:
        mutator = self.harness.context(
            "github_mutator",
            context.session_id,
            repository=context.repository,
            goal=context.goal,
        )
        for call in pending.calls:
            result = mutator.tool(call.tool, approval_id=pending.approval_id, **call.arguments)
            self.observe(context, "tool", {"tool": call.tool, "arguments": dict(call.arguments), "data": result})
        if not self.harness.approvals.complete(pending.approval_id):
            raise WorkflowError("approved mutation plan was not fully consumed")

    def emit(self, context: Any, status: str, message: str = "") -> None:
        self.harness.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.AGENT,
            name=context.agent,
            status=TraceStatus(status),
            message=message,
            details={"steps": context.steps, "phase": context.phase},
        )

    @staticmethod
    def observe(context: Any, kind: str, payload: Any) -> None:
        context.observations.append({"kind": kind, "payload": payload})
