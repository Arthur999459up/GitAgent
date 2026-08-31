"""Harness-side structured-call execution, approval, and safety transitions."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop.models import CapabilityCall, PendingCall
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


class StructuredCallDispatcher:
    """Execute already-structured calls without interpreting natural language."""

    def __init__(self, harness: Any) -> None:
        self.harness = harness

    def restore_pending(
        self,
        context: Any,
        *,
        approval_id: str,
        summary: str,
        calls: list[PlannedCapabilityCall],
        provider_call_id: str | None = None,
    ) -> None:
        approval = self.harness.approvals.restore(
            approval_id=approval_id,
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
        )
        context.pending = PendingCall(
            approval.approval_id, summary, calls, provider_call_id
        )

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
        if context.question:
            user_content = (
                decision.instruction or decision.message or decision.action.value
            )
            context.append_message({"role": "user", "content": user_content})
            context.question = ""
            return

        pending = context.pending
        if pending is None:
            raise WorkflowError("agent context is not waiting for user input")
        if decision.action == ApprovalIntent.APPROVE:
            self.harness.approvals.decide(pending.approval_id, "Approve")
            context.pending = None
            self.execute_pending(context, pending)
            return

        self.harness.approvals.decide(pending.approval_id, "Reject")
        self.observe(
            context,
            "rejection",
            {"instruction": (decision.instruction or "").strip()},
        )
        context.pending = None
        if pending.provider_call_id:
            context.append_tool_result(
                {
                    "status": "rejected",
                    "capability_id": pending.calls[0].capability_id,
                    "instruction": (decision.instruction or "").strip(),
                },
                call_id=pending.provider_call_id,
            )
        elif decision.instruction.strip():
            context.append_message(
                {"role": "user", "content": decision.instruction.strip()}
            )

    def handle_capability(
        self,
        context: Any,
        call: CapabilityCall,
        *,
        summary: str = "",
    ) -> bool:
        arguments = dict(call.arguments)
        self.validate_protected_capability(context, call.capability_id, arguments)
        self._validate_open_call(context, call)

        previous = self._latest_capability_attempt(context)
        previous_payload = (previous or {}).get("payload") or {}
        if (
            previous is not None
            and previous_payload.get("capability_id") == call.capability_id
            and previous_payload.get("arguments") == arguments
        ):
            already_corrected = previous_payload.get("error") == "duplicate_call"
            duplicate = {
                "status": "failed",
                "capability_id": call.capability_id,
                "error": "duplicate_call",
                "message": "The identical capability call was just attempted; choose another call.",
            }
            context.append_tool_result(duplicate, call_id=call.call_id)
            self.observe(
                context,
                "capability_error",
                {
                    "capability_id": call.capability_id,
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

        context.invoke(call.capability_id, **arguments)
        invocation = context.last_capability_call
        if invocation is None:
            raise WorkflowError("capability invocation did not record its result")
        if invocation.result.status == "approval_required":
            self.queue(
                context,
                summary or f"执行 {call.capability_id}",
                [PlannedCapabilityCall(call.capability_id, arguments)],
                provider_call_id=call.call_id,
            )
            return False
        if invocation.result.status == "failed":
            error = invocation.result.error
            payload = {
                "status": "failed",
                "capability_id": invocation.result.capability_id,
                "error": error.type.value if error is not None else "unknown",
                "message": error.message if error is not None else "capability failed",
            }
            context.append_tool_result(payload, call_id=call.call_id)
            self.observe(
                context,
                "capability_error",
                {
                    "capability_id": invocation.result.capability_id,
                    "arguments": arguments,
                    "error": payload["error"],
                    "message": payload["message"],
                    "details": error.details if error is not None else {},
                    "attempts": invocation.result.attempts,
                },
            )
            return True

        payload = {
            "capability_id": call.capability_id,
            "arguments": arguments,
            "data": invocation.observation_data,
        }
        if invocation.cached:
            payload["cached"] = True
        if invocation.covered:
            payload["covered"] = True
        self.observe(context, "capability", payload)
        context.append_tool_result(invocation.observation_data, call_id=call.call_id)
        return True

    def queue_issue_fix(self, context: Any) -> None:
        if context.code_candidate is None or context.change_request is None:
            raise WorkflowError(
                "issue fix requires a verified candidate and change request"
            )
        if context.verification is None or not context.verification.passed:
            raise WorkflowError("static verification failed; GitHub mutation is forbidden")
        review = code_change_review_package(
            context.change_request, context.code_candidate, context.verification
        )
        self.queue(
            context,
            issue_fix_approval_summary(context.change_request, review),
            issue_fix_mutation_plan(
                context.session_id,
                context.change_request,
                context.code_candidate,
                review,
            ),
        )

    def queue_repository_change(self, context: Any) -> None:
        if context.code_candidate is None or context.change_request is None:
            raise WorkflowError(
                "repository change requires a verified candidate and change request"
            )
        if context.verification is None or not context.verification.passed:
            raise WorkflowError(
                "static verification failed; default-branch mutation is forbidden"
            )
        self.queue(
            context,
            repository_change_approval_summary(
                context.change_request, context.code_candidate, context.verification
            ),
            repository_change_mutation_plan(
                context.change_request, context.code_candidate
            ),
        )

    def queue(
        self,
        context: Any,
        summary: str,
        calls: list[PlannedCapabilityCall],
        *,
        provider_call_id: str | None = None,
    ) -> None:
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
        )
        context.pending = PendingCall(
            approval.approval_id,
            approval.summary,
            calls,
            provider_call_id,
        )

    def execute_pending(self, context: Any, pending: PendingCall) -> None:
        for index, call in enumerate(pending.calls):
            call_id = (
                pending.provider_call_id
                if index == 0 and pending.provider_call_id
                else context.ensure_capability_tool_call(
                    call.capability_id, call.arguments
                )
            )
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
                    },
                    call_id=call_id,
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
            context.append_tool_result(result, call_id=call_id)
        if not self.harness.approvals.complete(pending.approval_id):
            raise WorkflowError("approved mutation plan was not fully consumed")

    def _validate_open_call(self, context: Any, call: CapabilityCall) -> None:
        open_call = context.open_tool_call()
        if open_call is None:
            raise ValidationError("structured Capability call is missing its assistant message")
        function = open_call.get("function") or {}
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        expected = self.harness.function_name(call.capability_id)
        if str(open_call.get("id") or "") != call.call_id or name != expected:
            raise ValidationError(
                "structured Capability call does not match the open provider call"
            )

    @staticmethod
    def validate_protected_capability(
        context: Any, capability_id: str, arguments: dict[str, Any]
    ) -> None:
        """Reject calls that would bypass deterministic mutation safety gates."""

        if capability_id == "github.commit_to_default_branch":
            raise WorkflowError(
                "default-branch commits may only come from the verified repository mutation plan"
            )
        if capability_id == "github.post_review":
            if context.agent != "pull_requests":
                raise WorkflowError("only PullRequestAgent may post a Review")
            if (
                context.entity_id is None
                or not str(context.entity_id).isdigit()
                or arguments.get("pr_number") != int(context.entity_id)
            ):
                raise WorkflowError(
                    "Review proposal does not match the active Pull Request"
                )
            if str(arguments.get("event") or "") not in {
                "COMMENT",
                "APPROVE",
                "REQUEST_CHANGES",
            }:
                raise ValidationError("Pull Request review event is invalid")
            if not str(arguments.get("body") or "").strip():
                raise ValidationError("Pull Request review body cannot be empty")
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
            pull_request = _last_capability_data(context, "github.get_pr")
            head = pull_request.get("head") or {}
            if not isinstance(head, dict):
                raise WorkflowError("PR commit requires current head repository evidence")
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
            readiness = _last_agent_artifact(context, "merge_readiness")
            if readiness.get("status") != "准备合并":
                raise WorkflowError("merge readiness has not passed")
            pull_request = _last_capability_data(context, "github.get_pr")
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


def _last_capability_data(context: Any, capability_id: str) -> dict[str, Any]:
    for observation in reversed(context.observations):
        payload = observation.get("payload") or {}
        if (
            observation.get("kind") == "capability"
            and payload.get("capability_id") == capability_id
        ):
            data = payload.get("data")
            return dict(data) if isinstance(data, dict) else {}
    return {}


def _last_agent_artifact(context: Any, artifact: str) -> dict[str, Any]:
    for observation in reversed(context.observations):
        payload = observation.get("payload") or {}
        if (
            observation.get("kind") == "agent_artifact"
            and payload.get("name") == artifact
        ):
            data = payload.get("data")
            return dict(data) if isinstance(data, dict) else {}
    return {}
