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
from gitagent.harness.mutation_plans import (
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

    def restore_pending(
        self, context: Any, *, summary: str, calls: list[PlannedCapabilityCall]
    ) -> None:
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
        )
        context.pending = PendingAction(approval.approval_id, summary, calls)

    def apply_user_decision(self, context: Any, decision: WorkflowTurnDecision) -> None:
        self.observe(
            context,
            "user_decision",
            {
                "action": decision.action.value,
                "instruction": decision.instruction,
                "message": decision.message,
            },
        )
        user_content = decision.instruction or decision.message or decision.action.value
        context.append_message({"role": "user", "content": user_content})
        if context.question:
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
        self.observe(
            context, "rejection", {"instruction": (decision.instruction or "").strip()}
        )
        context.pending = None

    def handle(self, context: Any, agent: AgentLoopAgent, action: AgentAction) -> bool:
        if action.kind == AgentActionKind.CAPABILITY:
            return self._handle_capability(context, agent, action)
        if action.kind == AgentActionKind.COMPLETE_ANALYSIS:
            handler = getattr(agent, "complete_analysis", None)
            if not callable(handler):
                raise ValidationError(
                    f"{context.agent} does not support explicit typed analysis"
                )
            result = handler(context, dict(action.arguments))
            context.complete_control_call(
                {
                    "status": "accepted",
                    "action": AgentActionKind.COMPLETE_ANALYSIS.value,
                    "result": result,
                }
            )
            return True
        if action.kind == AgentActionKind.APPLY_ISSUE_FIX:
            return self._handle_issue_fix(context)
        if action.kind == AgentActionKind.APPLY_REPOSITORY_CHANGE:
            return self._handle_repository_change(context)
        if action.kind == AgentActionKind.ASK:
            context.complete_control_call(
                {"status": "accepted", "action": AgentActionKind.ASK.value}
            )
            context.question = action.question or action.summary
            context.append_message({"role": "assistant", "content": context.question})
            return False
        if action.kind == AgentActionKind.FINISH:
            context.complete_control_call(
                {"status": "accepted", "action": AgentActionKind.FINISH.value}
            )
            context.final_message = action.message or context.final_message
            if context.result_required:
                context.result = agent.build_result(context)
            final = context.final_message or action.summary or "任务已完成。"
            context.append_message({"role": "assistant", "content": final})
            context.finished = True
            return False
        raise ValidationError(f"unknown action kind: {action.kind}")

    def _handle_capability(
        self, context: Any, agent: AgentLoopAgent, action: AgentAction
    ) -> bool:
        if not action.capability_id:
            raise ValidationError("capability action requires a capability ID")
        arguments = dict(action.arguments)
        self.validate_protected_capability(context, action.capability_id, arguments)

        expected_name = self.harness.function_name(action.capability_id)
        open_call = context.open_tool_call()
        if open_call is not None:
            function = open_call.get("function") or {}
            open_name = (
                str(function.get("name") or "") if isinstance(function, dict) else ""
            )
            if open_name == "decide_action":
                context.complete_control_call(
                    {
                        "status": "accepted",
                        "action": AgentActionKind.CAPABILITY.value,
                        "capability_id": action.capability_id,
                    }
                )
            elif open_name != expected_name:
                raise ValidationError(
                    f"open tool call {open_name or '<unnamed>'} does not match capability {expected_name}"
                )

        context.ensure_capability_tool_call(action.capability_id, arguments)
        previous = self._latest_capability_attempt(context)
        previous_payload = (previous or {}).get("payload") or {}
        if (
            previous is not None
            and previous_payload.get("capability_id") == action.capability_id
            and previous_payload.get("arguments") == arguments
        ):
            already_corrected = previous_payload.get("error") == "duplicate_call"
            duplicate = {
                "status": "failed",
                "capability_id": action.capability_id,
                "error": "duplicate_call",
                "message": "The identical capability call was just attempted; choose another action.",
            }
            context.append_tool_result(duplicate)
            self.observe(
                context,
                "capability_error",
                {
                    "capability_id": action.capability_id,
                    "arguments": arguments,
                    "error": "duplicate_call",
                    "message": duplicate["message"],
                    "details": {},
                    "attempts": 0,
                },
            )
            if already_corrected:
                raise WorkflowError(
                    "agent repeated an identical capability call after correction"
                )
            return True
        context.invoke(action.capability_id, **arguments)
        call = context.last_capability_call
        if call is None:
            raise WorkflowError("capability invocation did not record its result")
        if call.result.status == "approval_required":
            context.append_tool_result(
                {"status": "approval_required", "capability_id": action.capability_id}
            )
            call = PlannedCapabilityCall(action.capability_id, arguments)
            approval = self.harness.approvals.create(
                session_id=context.session_id,
                repository=context.repository,
                summary=action.summary or f"执行 {action.capability_id}",
                calls=[call],
            )
            context.pending = PendingAction(
                approval.approval_id, approval.summary, [call]
            )
            context.append_message(
                {
                    "role": "assistant",
                    "content": f"{approval.summary}\n\n请确认是否执行。",
                }
            )
            return False
        if call.result.status == "failed":
            error = call.result.error
            self.observe(
                context,
                "capability_error",
                {
                    "capability_id": call.result.capability_id,
                    "arguments": dict(arguments),
                    "error": error.type.value if error is not None else "unknown",
                    "message": error.message
                    if error is not None
                    else "capability failed",
                    "details": error.details if error is not None else {},
                    "attempts": call.result.attempts,
                },
            )
            context.append_tool_result(
                {
                    "status": "failed",
                    "capability_id": call.result.capability_id,
                    "error": error.type.value if error is not None else "unknown",
                    "message": error.message
                    if error is not None
                    else "capability failed",
                }
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
        context.append_tool_result(call.observation_data)
        return True

    def _handle_issue_fix(self, context: Any) -> bool:
        if context.code_candidate is None or context.change_request is None:
            raise WorkflowError(
                "apply_issue_fix requires a verified candidate and change request"
            )
        if context.verification is None or not context.verification.passed:
            raise WorkflowError(
                "static verification failed; GitHub mutation is forbidden"
            )
        review = code_change_review_package(
            context.change_request, context.code_candidate, context.verification
        )
        calls = issue_fix_mutation_plan(
            context.session_id, context.change_request, context.code_candidate, review
        )
        summary = issue_fix_approval_summary(context.change_request, review)
        self._queue(context, summary, calls)
        return False

    def _handle_repository_change(self, context: Any) -> bool:
        if context.code_candidate is None or context.change_request is None:
            raise WorkflowError(
                "apply_repository_change requires a verified candidate and change request"
            )
        if context.verification is None or not context.verification.passed:
            raise WorkflowError(
                "static verification failed; default-branch mutation is forbidden"
            )
        calls = repository_change_mutation_plan(
            context.change_request, context.code_candidate
        )
        summary = repository_change_approval_summary(
            context.change_request,
            context.code_candidate,
            context.verification,
        )
        self._queue(context, summary, calls)
        return False

    def _queue(
        self, context: Any, summary: str, calls: list[PlannedCapabilityCall]
    ) -> None:
        context.complete_control_call({"status": "awaiting_approval"})
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
        )
        context.pending = PendingAction(approval.approval_id, approval.summary, calls)
        context.append_message(
            {"role": "assistant", "content": f"{approval.summary}\n\n请确认是否执行。"}
        )

    def _execute_pending(self, context: Any, pending: PendingAction) -> None:
        for call in pending.calls:
            context.ensure_capability_tool_call(call.capability_id, call.arguments)
            result = context.invoke_approved(
                call.capability_id,
                call.arguments,
                approval_id=pending.approval_id,
            )
            invocation = context.last_capability_call
            if invocation is None:
                raise WorkflowError(
                    "approved capability invocation did not record its result"
                )
            if invocation.result.status != "success":
                error = invocation.result.error
                if error is None:
                    raise WorkflowError(
                        "approved capability failed without a structured error"
                    )
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
                context.append_tool_result(
                    {
                        "status": "failed",
                        "capability_id": invocation.result.capability_id,
                        "error": error.type.value,
                        "message": error.message,
                    }
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
            context.append_tool_result(result)
        if not self.harness.approvals.complete(pending.approval_id):
            raise WorkflowError("approved mutation plan was not fully consumed")

    @staticmethod
    def validate_protected_capability(
        context: Any,
        capability_id: str,
        arguments: dict[str, Any],
    ) -> None:
        """Reject model-authored calls that would bypass deterministic safety gates."""

        if capability_id == "github.commit_to_default_branch":
            raise WorkflowError(
                "default-branch commits may only come from the verified repository mutation plan"
            )
        if capability_id == "github.commit":
            candidate = context.code_candidate
            report = context.verification
            if (
                context.agent != "pull_requests"
                or candidate is None
                or report is None
                or not report.passed
            ):
                raise WorkflowError(
                    "PR commits require a verified CandidatePatch prepared by CodingAgent"
                )
            expected = {
                "files": candidate.files,
                "deleted_files": candidate.deleted_files,
                "message": candidate.summary,
            }
            if any(arguments.get(key) != value for key, value in expected.items()):
                raise WorkflowError(
                    "PR commit arguments do not match the verified CandidatePatch"
                )
            pull_request = next(
                (
                    (observation.get("payload") or {}).get("data") or {}
                    for observation in reversed(context.observations)
                    if observation.get("kind") == "capability"
                    and (observation.get("payload") or {}).get("capability_id")
                    == "github.get_pr"
                ),
                {},
            )
            head = pull_request.get("head") or {}
            if not isinstance(head, dict):
                raise WorkflowError(
                    "PR commit requires current head repository evidence"
                )
            source = head.get("repo") or {}
            source_name = (
                str(source.get("full_name") or "") if isinstance(source, dict) else ""
            )
            if source_name and source_name != context.repository:
                raise WorkflowError(
                    "Fork Pull Requests cannot receive an automatic source-branch commit"
                )
            if not str(head.get("ref") or "") or arguments.get("branch") != str(
                head.get("ref")
            ):
                raise WorkflowError(
                    "PR commit branch does not match the observed Pull Request head"
                )
        if capability_id == "github.merge":
            if context.agent != "pull_requests":
                raise WorkflowError("only PullRequestAgent may propose a merge")
            readiness = next(
                (
                    (observation.get("payload") or {}).get("data") or {}
                    for observation in reversed(context.observations)
                    if observation.get("kind") == "agent"
                    and (observation.get("payload") or {}).get("agent")
                    == "pull_requests"
                    and (observation.get("payload") or {}).get("capability")
                    == "merge_readiness"
                ),
                {},
            )
            if readiness.get("status") != "准备合并":
                raise WorkflowError("merge readiness has not passed")
            pull_request = next(
                (
                    (observation.get("payload") or {}).get("data") or {}
                    for observation in reversed(context.observations)
                    if observation.get("kind") == "capability"
                    and (observation.get("payload") or {}).get("capability_id")
                    == "github.get_pr"
                ),
                {},
            )
            head = pull_request.get("head") or {}
            expected_sha = str(head.get("sha") or "") if isinstance(head, dict) else ""
            if not expected_sha or arguments.get("expected_head_sha") != expected_sha:
                raise WorkflowError(
                    "merge proposal is not bound to the reviewed PR head SHA"
                )
            observed_number = pull_request.get("number")
            if observed_number is not None and arguments.get("pr_number") != int(
                observed_number
            ):
                raise WorkflowError("merge proposal targets a different Pull Request")

    @staticmethod
    def _latest_capability_attempt(context: Any) -> dict[str, Any] | None:
        return next(
            (
                observation
                for observation in reversed(context.observations)
                if observation.get("kind") in {"capability", "capability_error"}
            ),
            None,
        )

    def emit(self, context: Any, status: str, message: str = "") -> None:
        self.harness.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.AGENT,
            name=context.agent,
            status=TraceStatus(status),
            message=message,
            details={"steps": context.steps},
        )

    @staticmethod
    def observe(context: Any, kind: str, payload: Any) -> None:
        context.observations.append({"kind": kind, "payload": payload})
