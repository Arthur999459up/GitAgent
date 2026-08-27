"""Harness-side action dispatch, constraints, approvals, and recovery."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop.actions import (
    AgentAction,
    AgentActionKind,
    AgentLoopAgent,
    PendingAction,
)
from gitagent.domain.errors import ValidationError, WorkflowError
from gitagent.domain.models import (
    ApprovalIntent,
    PlannedCapabilityCall,
    WorkflowTurnDecision,
)
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

    def restore_pending(self, context: Any, *, summary: str, calls: list[PlannedCapabilityCall]) -> None:
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
            self._execute_pending(context, pending)
            return

        self.harness.approvals.decide(pending.approval_id, "Reject")
        self.observe(context, "rejection", {"instruction": (decision.instruction or "").strip()})
        context.pending = None

    def handle(self, context: Any, agent: AgentLoopAgent, action: AgentAction) -> bool:
        if action.kind == AgentActionKind.CAPABILITY:
            return self._handle_capability(context, agent, action)
        if action.kind == AgentActionKind.APPLY_ISSUE_FIX:
            return self._handle_issue_fix(context)
        if action.kind == AgentActionKind.APPLY_REPOSITORY_CHANGE:
            return self._handle_repository_change(context)
        if action.kind == AgentActionKind.CONTINUE:
            return True
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

    def _handle_capability(self, context: Any, agent: AgentLoopAgent, action: AgentAction) -> bool:
        if not action.capability_id:
            raise ValidationError("capability action requires a capability ID")
        arguments = dict(action.arguments)

        context.invoke(action.capability_id, **arguments)
        call = context.last_capability_call
        if call is None:
            raise WorkflowError("capability invocation did not record its result")
        if call.result.status == "approval_required":
            call = PlannedCapabilityCall(action.capability_id, arguments)
            approval = self.harness.approvals.create(
                session_id=context.session_id,
                repository=context.repository,
                summary=action.summary or f"执行 {action.capability_id}",
                calls=[call],
            )
            context.pending = PendingAction(approval.approval_id, approval.summary, [call])
            return False
        if call.result.status == "failed":
            error = call.result.error
            self.observe(
                context,
                "capability_error",
                {
                    "capability_id": call.result.capability_id,
                    "arguments": dict(arguments),
                    "error": error.type.value,
                    "message": error.message,
                    "details": error.details,
                    "attempts": call.result.attempts,
                },
            )
            return True
        payload = {
            "capability_id": action.capability_id,
            "arguments": dict(arguments),
            "data": call.observation_data,
        }
        if call.cached:
            payload["cached"] = True
        if call.covered:
            payload["covered"] = True
        self.observe(context, "capability", payload)
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

    def _queue(self, context: Any, summary: str, calls: list[PlannedCapabilityCall]) -> None:
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
            executor = mutator if call.capability_id.startswith("github.") else context
            result = executor.invoke(
                call.capability_id,
                approval_id=pending.approval_id,
                **call.arguments,
            )
            invocation = executor.last_capability_call
            if invocation is None:
                raise WorkflowError("approved capability invocation did not record its result")
            if invocation.result.status != "success":
                error = invocation.result.error
                if error is None:
                    raise WorkflowError("approved capability failed without a structured error")
                self.harness.approvals.invalidate(pending.approval_id)
                self.observe(
                    context,
                    "capability_error",
                    {
                        "capability_id": invocation.result.capability_id,
                        "arguments": dict(call.arguments),
                        "error": error.type.value,
                        "message": error.message,
                        "details": error.details,
                        "attempts": invocation.result.attempts,
                    },
                )
                return
            self.observe(
                context,
                "capability",
                {
                    "capability_id": call.capability_id,
                    "arguments": dict(call.arguments),
                    "data": result,
                },
            )
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
