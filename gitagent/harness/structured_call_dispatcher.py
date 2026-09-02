"""Harness-side structured-call execution, approval, and safety transitions."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop.models import AgentCall, CapabilityCall, PendingCall
from gitagent.capability import CapabilityResult
from gitagent.capability.errors import CapabilityErrorType, capability_error
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

    def preflight_capability(
        self,
        context: Any,
        call: CapabilityCall,
    ) -> Any | None:
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
            if already_corrected:
                raise WorkflowError(
                    "agent repeated an identical capability call after correction"
                )
            error = capability_error(
                CapabilityErrorType.DUPLICATE_CALL,
                "The identical capability call was just attempted; choose another call.",
            )
            from gitagent.harness.context.state import CapabilityCallRecord

            return CapabilityCallRecord(
                call.call_id,
                arguments,
                None,
                CapabilityResult(
                    call.capability_id,
                    "failed",
                    "none",
                    error=error,
                    attempts=0,
                ),
            )
        return None

    def execute_capability_batch(
        self,
        context: Any,
        calls: list[CapabilityCall],
        *,
        summary: str = "",
    ) -> bool:
        """Execute one provider-ordered Capability batch through the shared Runtime."""

        prepared: dict[str, Any] = {}
        preflight_results: dict[str, Any] = {}
        failure_guard_preflight: dict[str, bool] = {}
        from gitagent.harness.execution import ExecutionProfile

        profiles = []
        for call in calls:
            profile = self.harness.describe_capability_execution(context, call)
            if self.harness.capability_permission_decision(context, call) in {
                "ASK",
                "DENY",
            }:
                profile = ExecutionProfile.exclusive(
                    read=profile.resource_claims.read,
                    write=profile.resource_claims.write,
                )
            profiles.append(profile)

        def prepare_call(call: CapabilityCall | AgentCall) -> None:
            if not isinstance(call, CapabilityCall):  # pragma: no cover - contract
                raise ValidationError("Capability batch contains an Agent call")
            preflight = self.preflight_capability(context, call)
            if preflight is not None:
                preflight_results[call.call_id] = preflight
            else:
                prepared[call.call_id] = context.prepare_capability_call(
                    call.call_id,
                    call.capability_id,
                    call.arguments,
                )
            invocation = prepared.get(call.call_id)
            guard_arguments = (
                invocation.execution_arguments
                if invocation is not None
                and invocation.execution_arguments is not None
                else call.arguments
            )
            failure_guard_preflight[call.call_id] = not (
                self.harness.capability_failure_blocked(
                    context,
                    call,
                    arguments=guard_arguments,
                )
            )

        def run_call(call: CapabilityCall | AgentCall) -> Any:
            if not isinstance(call, CapabilityCall):  # pragma: no cover - contract
                raise ValidationError("Capability batch contains an Agent call")
            if call.call_id in preflight_results:
                return preflight_results[call.call_id]
            return context.execute_capability_call(
                prepared[call.call_id],
                preflighted=failure_guard_preflight[call.call_id],
            )

        def commit_call(call: CapabilityCall | AgentCall, outcome: Any) -> str:
            if not isinstance(call, CapabilityCall):  # pragma: no cover - contract
                raise ValidationError("Capability batch contains an Agent call")
            return self.commit_capability(
                context,
                call,
                self.capability_record(call, outcome),
                summary=summary,
            )

        def suspend_call(call: CapabilityCall | AgentCall, outcome: Any) -> None:
            if not isinstance(call, CapabilityCall):  # pragma: no cover - contract
                raise ValidationError("Capability batch contains an Agent call")
            context.uncommitted_capability_results[call.call_id] = (
                self.capability_record(call, outcome)
            )

        def cancel_call(call: CapabilityCall | AgentCall, reason: str) -> None:
            context.uncommitted_capability_results.pop(call.call_id, None)
            if (
                context.pending is not None
                and context.pending.provider_call_id == call.call_id
            ):
                context.pending = None
            if context.unresolved_tool_call(call.call_id) is None:
                return
            context.append_tool_result(
                {"status": "cancelled", "reason": reason},
                call_id=call.call_id,
            )
            self.observe(
                context,
                "call_cancelled",
                {"call_id": call.call_id, "reason": reason},
            )

        return self.harness.coordinator.execute(
            calls,
            profiles,
            prepare_call=prepare_call,
            run_call=run_call,
            commit_call=commit_call,
            suspend_call=suspend_call,
            cancel_call=cancel_call,
            lane_for=lambda call: "capability",
            provider_for=lambda call: self.harness.provider_id(call.capability_id),
            owner=context,
        )

    @staticmethod
    def capability_record(call: CapabilityCall, outcome: Any) -> Any:
        """Normalize one Runtime outcome into a Capability call record."""

        if (
            getattr(outcome, "call_id", None) == call.call_id
            and hasattr(outcome, "result")
        ):
            return outcome
        from gitagent.harness.context.state import CapabilityCallRecord

        message = str(outcome) if isinstance(outcome, Exception) else "capability failed"
        error = capability_error(CapabilityErrorType.EXECUTION_FAILED, message)
        return CapabilityCallRecord(
            call.call_id,
            dict(call.arguments),
            None,
            CapabilityResult(
                call.capability_id,
                "failed",
                "none",
                error=error,
                attempts=0,
            ),
        )

    def commit_capability(
        self,
        context: Any,
        call: CapabilityCall,
        invocation: Any,
        *,
        summary: str = "",
    ) -> str:
        arguments = dict(call.arguments)
        if invocation.call_id != call.call_id:
            raise ValidationError("Capability result call_id correlation is invalid")
        invocation = context.commit_capability_call(invocation)
        if invocation.result.status == "approval_required":
            self.queue(
                context,
                summary or f"执行 {call.capability_id}",
                [PlannedCapabilityCall(call.capability_id, arguments)],
                provider_call_id=call.call_id,
            )
            return "waiting"
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
            return "failed"

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
        return "continue"

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
            invocation = context.invoke_approved(
                call.capability_id,
                call.arguments,
                approval_id=pending.approval_id,
                call_id=call_id,
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
                    "data": invocation.observation_data,
                },
            )
            context.append_tool_result(invocation.observation_data, call_id=call_id)
        if not self.harness.approvals.complete(pending.approval_id):
            raise WorkflowError("approved mutation plan was not fully consumed")

    def _validate_open_call(self, context: Any, call: CapabilityCall) -> None:
        open_call = context.unresolved_tool_call(call.call_id)
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
        if capability_id == "github.post_comment" and context.issue_reply is not None:
            workflow = context.issue_reply
            if context.entity_id is None or not str(context.entity_id).isdigit():
                raise WorkflowError("Issue reply workflow is missing its Issue number")
            expected = {
                "issue_number": int(context.entity_id),
                "body": workflow.draft,
            }
            if workflow.stage.value != "publish" or arguments != expected:
                raise WorkflowError(
                    "Issue reply publication must use the reviewed draft workflow"
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
            if context.change_request is None or not context.change_request.source_ref:
                raise WorkflowError("PR commit requires the candidate head SHA")
            expected = {
                "files": candidate.files,
                "deleted_files": candidate.deleted_files,
                "message": candidate.summary,
                "expected_head_sha": context.change_request.source_ref,
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

    def emit(
        self,
        context: Any,
        status: str,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        event_details = {"steps": context.steps}
        if details:
            event_details.update(details)
        if status == "progress" and "phase" not in event_details:
            open_calls = context.open_tool_calls()
            if open_calls:
                call_names: set[str] = set()
                for call in open_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if isinstance(function, dict):
                        call_names.add(str(function.get("name") or ""))
                event_details["phase"] = "thinking"
                event_details["has_thinking_text"] = message not in call_names
            else:
                event_details["phase"] = "final"
        self.harness.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.AGENT,
            name=context.agent,
            status=TraceStatus(status),
            message=message,
            details=event_details,
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
