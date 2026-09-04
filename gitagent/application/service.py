"""Session service around the unified Agent Runtime lifecycle."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gitagent.agent_loop import AgentLoop, WaitForUser
from gitagent.agents import (
    CodingAgent,
    IssueAgent,
    MainAgent,
    PullRequestAgent,
    RepositoryAgent,
)
from gitagent.capability import CapabilityLayer, CapabilityResult
from gitagent.capability.errors import CapabilityErrorType, capability_error
from gitagent.domain.errors import RoutingError, ValidationError, WorkflowError
from gitagent.domain.models import (
    AgentGuidance,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    CodingTask,
    DraftResult,
    IssueReplyStage,
    IssueReplyWorkflow,
    PlannedCapabilityCall,
    Recommendation,
    Replacement,
    RepositoryRef,
    ResolvedReference,
    SessionScope,
    VerificationCheck,
    VerificationReport,
    WorkflowTurnDecision,
    to_plain,
)
from gitagent.harness.context import (
    MessageCompactionPlan,
    canonical_message,
    compact_messages,
    derive_domain_messages,
    derive_main_messages,
)
from gitagent.harness.context.state import AgentContext, CapabilityCallRecord
from gitagent.harness.execution import AgentHarness
from gitagent.harness.file_reads import FileReadLedger, PreparedFileRead
from gitagent.harness.mutation_plans import (
    code_change_review_package,
    issue_fix_mutation_plan,
    repository_change_mutation_plan,
)
from gitagent.infra.observability import TraceBus
from gitagent.infra.persistence import SessionManager
from gitagent.memory import MemoryPageStore, MemorySearch, MemoryStopHooks
from gitagent.model import Reasoner

from .approval_intent import ApprovalIntentClassifier


@dataclass
class ServiceResult:
    output: Any = None
    agent: str | None = None
    goal: str = ""
    entity_type: str | None = None
    entity_id: str | None = None
    domain_output: Any = None
    workflow_summary: str = ""
    output_agent: str = "main"


class GitAgentService:
    """Run and persist one Main-rooted Agent Runtime tree per Session."""

    def __init__(
        self,
        capabilities: CapabilityLayer,
        *,
        github: Any,
        main_reasoner: Reasoner,
        agent_reasoner: Reasoner | None = None,
        session_manager: SessionManager | None = None,
        memory_store: MemoryPageStore | None = None,
        memory_search: MemorySearch | None = None,
        memory_hooks: MemoryStopHooks | None = None,
        trace: TraceBus | None = None,
        session_scope: SessionScope | None = None,
        context_window_tokens: Mapping[str, int] | None = None,
        execution: Mapping[str, Any],
    ) -> None:
        self.harness = AgentHarness(
            capabilities,
            trace=trace,
            context_window_tokens=context_window_tokens,
            execution=execution,
        )
        self.harness.message_sink = self._persist_agent_message
        self.harness.compaction_sink = self._persist_agent_compaction
        domain_reasoner = agent_reasoner or main_reasoner
        self.coding = CodingAgent(self.harness, domain_reasoner, github)
        self.repository_agent = RepositoryAgent(self.harness, domain_reasoner)
        self.pull_request_agent = PullRequestAgent(self.harness, domain_reasoner)
        self.issue_agent = IssueAgent(self.harness, domain_reasoner)
        self.main_agent = MainAgent(
            self.harness,
            main_reasoner,
            guidance_resolver=self._guidance,
        )
        self.loop = AgentLoop(
            self.harness,
            child_agents={
                "issues": self.issue_agent,
                "pull_requests": self.pull_request_agent,
                "repository": self.repository_agent,
                "coding": self.coding,
            },
        )
        self.classifier = ApprovalIntentClassifier(
            main_reasoner,
            context_window_tokens=self.harness.context_window_for("main"),
        )
        self.reasoner = domain_reasoner
        self.session_manager = session_manager
        self.memory_store = memory_store
        self.memory_search = memory_search or (
            MemorySearch(memory_store) if memory_store is not None else None
        )
        self.memory_hooks = memory_hooks
        self.session_scope = session_scope
        self.dispatch_started = False
        self._active_turn_seq = 0
        self._invalidated = False

    def handle(
        self,
        user_input: str,
        *,
        repository: str,
        main_messages: list[dict[str, Any]],
        main_tools: list[dict[str, Any]] | None = None,
        session_scope: SessionScope | None = None,
        turn_seq: int | None = None,
    ) -> ServiceResult:
        self._require_live()
        self._require_scope(session_scope)
        repository = self._repository(repository)
        self.dispatch_started = False
        self._active_turn_seq = int(turn_seq or 0)
        if self._active_turn_seq > 0:
            self.harness.trace.bind_turn(self._scope().session_id, self._active_turn_seq)

        current = self._load_context(main_messages=main_messages)
        if current is None and (
            not main_messages or main_messages[-1].get("role") != "user"
        ):
            raise RoutingError("GitAgentService requires the current Main message thread")
        if current is not None:
            if current.agent != "main":
                raise RoutingError("stored Runtime root is not the Main Agent")
            if current.finished or current.error:
                self._clear_context()
            else:
                current.messages = main_messages
                current.model_tools = main_tools
                self.loop.validate_context_tree(current)
                self.dispatch_started = True
                output = self._resume_context(current, user_input)
                if current.waiting:
                    self._save_context(current)
                    return self._result_for_context(current, output=output)
                self._clear_context()
                return self._result_for_main(current)

        return self._run_main(main_messages, repository, main_tools)

    def _run_main(
        self,
        main_messages: list[dict[str, Any]],
        repository: str,
        main_tools: list[dict[str, Any]] | None,
    ) -> ServiceResult:
        tools = main_tools or self.main_agent.provider_tools(
            session_id=self._scope().session_id,
            repository=repository,
            goal=str(main_messages[-1].get("content") or ""),
        )
        context = self.harness.context(
            "main",
            self._scope().session_id,
            repository=repository,
            goal=str(main_messages[-1].get("content") or ""),
        )
        context.messages = main_messages
        context.model_tools = tools
        self._bind_context_turn(context)
        self.dispatch_started = True
        self.loop.start(context, self.main_agent)
        if context.waiting:
            self._save_context(context)
            return self._result_for_context(context)
        self._clear_context()
        return self._result_for_main(context)

    def _resume_context(self, context: AgentContext, user_input: str) -> Any:
        target = self.loop.waiting_context(context)
        if (
            target.issue_reply is not None
            and target.issue_reply.stage == IssueReplyStage.DRAFT
            and bool(target.issue_reply.draft)
            and target.user_input_request is not None
            and not target.active_children
        ):
            decision = self.classifier.classify(
                user_input=user_input,
                proposal_context={
                    "workflow_type": "issue_reply_draft",
                    "entity": {"type": "issue", "id": target.entity_id or ""},
                    "draft": target.issue_reply.draft,
                    "state": "reviewing_draft",
                },
            )
            self._observe_service_decision(target, decision, user_input)
            target.issue_reply.decision = WorkflowTurnDecision(
                decision.action,
                instruction=decision.instruction.strip() or user_input.strip(),
                message=decision.message,
            )
            self.loop.resume_user_input(context, self.main_agent, user_input)
            return self._runtime_output(context)
        if target.pending is not None:
            decision = self.classifier.classify(
                user_input=user_input,
                proposal_context=self._context_description(target),
            )
            return self._continue_approval(context, target, decision, user_input)
        if target.user_input_request is not None:
            self.loop.resume_user_input(context, self.main_agent, user_input)
            return self._runtime_output(context)
        raise RoutingError("stored Agent Runtime is not resumable")

    def _continue_approval(
        self,
        root: AgentContext,
        context: AgentContext,
        decision: WorkflowTurnDecision,
        user_input: str,
    ) -> Any:
        if context.pending is None:
            raise RoutingError("当前 Session 没有待审批提案")
        if decision.action == ApprovalIntent.REVISE and not decision.instruction.strip():
            decision = WorkflowTurnDecision(
                decision.action,
                instruction=user_input.strip(),
                message=decision.message,
            )
        if decision.action == ApprovalIntent.AMBIGUOUS:
            self._observe_service_decision(context, decision, user_input)
            return decision.message
        if decision.action == ApprovalIntent.QUESTION:
            self._observe_service_decision(context, decision, user_input)
            return self._proposal_description(context)

        if (
            context.agent == "pull_requests"
            and decision.action == ApprovalIntent.REVISE
            and len(context.pending.calls) == 1
            and context.pending.calls[0].capability_id == "github.post_review"
        ):
            self._observe_service_decision(context, decision, user_input)
            self._revise_pr_review(
                context, decision.instruction.strip() or user_input.strip()
            )
            return self._runtime_output(root)

        if (
            context.agent == "repository"
            and decision.action == ApprovalIntent.REVISE
            and context.change_request is not None
        ):
            instruction = decision.instruction.strip() or user_input.strip()
            context.change_request.description += f"\n\nUser revision: {instruction}"
            context.goal = context.change_request.description
            context.code_candidate = None
            context.verification = None

        self.loop.resume(root, self.main_agent, decision)
        return self._runtime_output(root)

    def _revise_pr_review(self, context: AgentContext, instruction: str) -> AgentContext:
        pending = context.pending
        if pending is None:
            raise RoutingError("当前没有可修改的 Review 提案")
        call = pending.calls[0]
        arguments = dict(call.arguments)
        arguments["body"] = self._revise_text(
            context,
            "GitHub Pull Request review body",
            str(arguments.get("body") or ""),
            instruction,
        )
        self.harness.approvals.supersede(pending.approval_id)
        if pending.provider_call_id:
            context.append_tool_result(
                {
                    "status": "superseded",
                    "capability_id": call.capability_id,
                    "reason": "user revised the proposed Review",
                },
                call_id=pending.provider_call_id,
            )
        revised_call_id = context.ensure_capability_tool_call(
            call.capability_id, arguments
        )
        self.loop.dispatcher.queue(
            context,
            pending.summary,
            [PlannedCapabilityCall(call.capability_id, arguments)],
            provider_call_id=revised_call_id,
        )
        return context

    def _revise_text(
        self, context: AgentContext, artifact: str, current: str, instruction: str
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    f"You revise a {artifact}. Follow the user's editing instruction exactly. "
                    "Return only the revised text. Do not claim it was posted."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"current_draft": current, "instruction": instruction},
                    ensure_ascii=False,
                ),
            },
        ]
        messages = compact_messages(
            messages, None, context_window_tokens=context.context_window_tokens
        ).messages
        revised = self.reasoner.complete_text_messages(
            messages=messages,
            context_window_tokens=context.context_window_tokens,
        ).strip()
        if not revised:
            raise RoutingError(f"{artifact} revision returned empty text")
        return revised

    def _result_for_main(self, context: AgentContext) -> ServiceResult:
        child = context.last_completed_child
        domain_output = self._after_loop(child) if child is not None else None
        return ServiceResult(
            output=context.error or context.final_message,
            agent=child.agent if child is not None else None,
            goal=child.goal if child is not None else "",
            entity_type=child.entity_type if child is not None else None,
            entity_id=child.entity_id if child is not None else None,
            domain_output=domain_output,
            workflow_summary=child.final_message if child is not None else "",
        )

    def _after_loop(self, context: AgentContext) -> Any:
        if context.waiting:
            return context
        if context.error:
            return context
        return context.result if context.result is not None else context.final_message

    @staticmethod
    def _domain_context(context: AgentContext) -> AgentContext | None:
        child = context.first_waiting_child() or context.last_completed_child
        return child if child is not None and child.agent != "coding" else None

    def _runtime_output(self, context: AgentContext) -> Any:
        domain = self._domain_context(context)
        if domain is not None:
            return (
                self._waiting_output(domain)
                if domain.waiting
                else self._after_loop(domain)
            )
        return context.error or context.final_message

    def _waiting_output(self, context: AgentContext) -> Any:
        if (
            context.issue_reply is not None
            and context.issue_reply.stage == IssueReplyStage.DRAFT
            and context.issue_reply.draft
        ):
            return self._issue_reply_draft_output(context)
        return context

    @staticmethod
    def _issue_reply_draft_output(context: AgentContext) -> DraftResult:
        workflow = context.issue_reply
        if workflow is None or not workflow.draft:
            raise RoutingError("Issue reply draft is missing")
        return DraftResult(
            "issue",
            context.entity_id,
            "Issue 回复草稿",
            workflow.draft,
            context.waiting_question
            or "草稿尚未发布；确认后才会创建发布审批。",
        )

    def _result_for_context(
        self, context: AgentContext, *, output: Any = None
    ) -> ServiceResult:
        domain = self._domain_context(context)
        if domain is None:
            raise RoutingError("waiting Main Runtime is missing its Domain child")
        domain_output = self._waiting_output(domain)
        return ServiceResult(
            output=domain_output if output is None else output,
            agent=domain.agent,
            goal=domain.goal,
            entity_type=domain.entity_type,
            entity_id=domain.entity_id,
            domain_output=domain_output,
            workflow_summary=domain.final_message,
            output_agent=domain.agent,
        )

    def memory_after_turn(self, *, turn_seq: int) -> Any:
        if self.memory_hooks is None:
            return None
        try:
            session = self._require_session_manager().get_session(
                self._scope().account_key,
                self._scope().repository_key,
                self._scope().session_id,
            )
            if session is not None:
                self.memory_hooks.handle_turn_stop(
                    self._scope(), session.repository_full_name, through_seq=turn_seq
                )
        except Exception:  # noqa: BLE001 - business turn is already durable
            return None
        return None

    def invalidate(self) -> None:
        if not self._invalidated:
            self.harness.approvals.invalidate_all()
            self.harness.close()
            self._invalidated = True

    def _serialize_context(self, context: AgentContext) -> dict[str, Any]:
        pending = None
        if context.pending is not None:
            pending = {
                "approval_id": context.pending.approval_id,
                "summary": context.pending.summary,
                "provider_call_id": context.pending.provider_call_id,
                "calls": [
                    {
                        "capability_id": call.capability_id,
                        "arguments": to_plain(call.arguments),
                    }
                    for call in context.pending.calls
                ],
            }
        return {
            "agent": context.agent,
            "run_id": context.run_id,
            "origin_turn_seq": context.origin_turn_seq,
            "parent_run_id": context.parent_run_id,
            "parent_call_id": context.parent_call_id,
            "parent_call_name": context.parent_call_name,
            "parent_call_arguments": to_plain(context.parent_call_arguments),
            "repository": context.repository,
            "goal": context.goal,
            "entity_type": context.entity_type,
            "entity_id": context.entity_id,
            "steps": context.steps,
            "observations": to_plain(context.observations),
            "pending": pending,
            "waiting_for_user": (
                {
                    "question": context.user_input_request.question,
                    "call_id": context.user_input_request.call_id,
                }
                if context.user_input_request is not None
                else None
            ),
            "active_children": {
                call_id: self._serialize_context(child)
                for call_id, child in context.active_children.items()
            },
            "uncommitted_capability_results": {
                call_id: self._serialize_capability_record(record)
                for call_id, record in context.uncommitted_capability_results.items()
            },
            "final_message": context.final_message,
            "code_candidate": to_plain(context.code_candidate),
            "change_request": to_plain(context.change_request),
            "verification": to_plain(context.verification),
            "issue_reply": to_plain(context.issue_reply),
            "coding_task": to_plain(context.coding_task),
            "coding_task_completed": context.coding_task_completed,
            "code_explanation": to_plain(context.code_explanation),
            "code_review": to_plain(context.code_review),
            "code_plan": to_plain(context.code_plan),
            "review_dialogue": to_plain(context.review_dialogue),
            "ci_analysis": to_plain(context.ci_analysis),
            "merge_readiness": to_plain(context.merge_readiness),
            "read_cache": to_plain(context.read_cache),
            "file_reads": context.file_reads.to_plain(),
            "error": context.error,
            "finished": context.finished,
        }

    @staticmethod
    def _serialize_capability_record(
        record: CapabilityCallRecord,
    ) -> dict[str, Any]:
        error = record.result.error
        return {
            "call_id": record.call_id,
            "arguments": to_plain(record.arguments),
            "observation_data": to_plain(record.observation_data),
            "result": {
                "capability_id": record.result.capability_id,
                "status": record.result.status,
                "type": record.result.type,
                "content": to_plain(record.result.content),
                "error": (
                    {
                        "type": error.type.value,
                        "message": error.message,
                        "details": to_plain(error.details),
                    }
                    if error is not None
                    else None
                ),
                "attempts": record.result.attempts,
            },
            "cached": record.cached,
            "covered": record.covered,
            "execution_arguments": to_plain(record.execution_arguments),
            "prepared_file_read": (
                record.prepared_file_read.to_plain()
                if record.prepared_file_read is not None
                else None
            ),
        }

    @staticmethod
    def _restore_capability_record(
        call_id: str, raw: dict[str, Any]
    ) -> CapabilityCallRecord:
        expected = {
            "call_id",
            "arguments",
            "observation_data",
            "result",
            "cached",
            "covered",
            "execution_arguments",
            "prepared_file_read",
        }
        if set(raw) != expected or not isinstance(raw.get("arguments"), dict):
            raise RoutingError("stored Capability result is invalid")
        if str(raw.get("call_id") or "") != call_id:
            raise RoutingError("stored Capability result call_id does not match")
        result_raw = raw.get("result")
        if not isinstance(result_raw, dict) or set(result_raw) != {
            "capability_id",
            "status",
            "type",
            "content",
            "error",
            "attempts",
        }:
            raise RoutingError("stored Capability result is invalid")
        capability_id = result_raw.get("capability_id")
        status = result_raw.get("status")
        result_type = result_raw.get("type")
        attempts = result_raw.get("attempts")
        if not isinstance(capability_id, str) or not capability_id:
            raise RoutingError("stored Capability result is invalid")
        if status not in {"success", "failed", "approval_required"}:
            raise RoutingError("stored Capability result status is invalid")
        if (
            not isinstance(result_type, str)
            or (status == "success" and result_type not in {"data", "context", "retrieval"})
            or (status != "success" and result_type != "none")
        ):
            raise RoutingError("stored Capability result type is invalid")
        if (
            not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 0
        ):
            raise RoutingError("stored Capability result attempts are invalid")
        error_raw = result_raw.get("error")
        error = None
        if isinstance(error_raw, dict):
            if set(error_raw) != {"type", "message", "details"}:
                raise RoutingError("stored Capability error is invalid")
            try:
                error_type = CapabilityErrorType(str(error_raw.get("type") or ""))
            except ValueError as exc:
                raise RoutingError("stored Capability error type is invalid") from exc
            error = capability_error(
                error_type,
                str(error_raw.get("message") or ""),
                details=(
                    dict(error_raw["details"])
                    if isinstance(error_raw.get("details"), dict)
                    else None
                ),
            )
        elif error_raw is not None:
            raise RoutingError("stored Capability error is invalid")
        if (status == "failed") != (error is not None):
            raise RoutingError("stored Capability error/status pair is invalid")
        if not isinstance(raw.get("cached"), bool) or not isinstance(
            raw.get("covered"), bool
        ):
            raise RoutingError("stored Capability cache flags are invalid")
        execution_arguments = raw.get("execution_arguments")
        if execution_arguments is not None and not isinstance(
            execution_arguments, dict
        ):
            raise RoutingError("stored Capability execution arguments are invalid")
        prepared_raw = raw.get("prepared_file_read")
        if prepared_raw is not None and not isinstance(prepared_raw, dict):
            raise RoutingError("stored prepared file read is invalid")
        try:
            prepared = (
                PreparedFileRead.from_plain(prepared_raw)
                if isinstance(prepared_raw, dict)
                else None
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise RoutingError("stored prepared file read is invalid") from exc
        return CapabilityCallRecord(
            call_id,
            dict(raw["arguments"]),
            raw.get("observation_data"),
            CapabilityResult(
                capability_id,
                status,
                result_type,
                result_raw.get("content"),
                error=error,
                attempts=attempts,
            ),
            cached=raw["cached"],
            covered=raw["covered"],
            execution_arguments=(
                dict(execution_arguments)
                if isinstance(execution_arguments, dict)
                else None
            ),
            prepared_file_read=prepared,
        )

    def _restore_context(
        self,
        raw: dict[str, Any],
        *,
        repository: str,
        main_messages: list[dict[str, Any]] | None = None,
    ) -> AgentContext:
        context = self._restore_agent_context(
            raw,
            repository=repository,
            parent_agent=None,
            parent_run_id="",
            main_messages=main_messages,
        )
        if (
            context.finished
            or context.error is not None
            or context.pending is not None
            or context.user_input_request is not None
            or context.first_waiting_child() is None
        ):
            raise RoutingError("stored Main Runtime state is not paused in a child call")
        try:
            self.loop.validate_context_tree(context)
        except ValidationError as exc:
            raise RoutingError("stored Agent child correlation is invalid") from exc
        return context

    def _restore_agent_context(
        self,
        raw: dict[str, Any],
        *,
        repository: str,
        parent_agent: str | None,
        parent_run_id: str,
        main_messages: list[dict[str, Any]] | None = None,
    ) -> AgentContext:
        agent = str(raw.get("agent") or "")
        allowed = {
            None: {"main"},
            "main": {"issues", "pull_requests", "repository"},
            "issues": {"coding"},
            "pull_requests": {"coding"},
            "repository": {"coding"},
            "coding": set(),
        }.get(parent_agent, set())
        if agent not in allowed:
            raise RoutingError("stored Session agent context is invalid")
        if str(raw.get("repository") or "") != repository:
            raise RoutingError("stored child Agent belongs to a different repository")
        context = self.harness.context(
            agent,
            self._scope().session_id,
            repository=repository,
            goal=str(raw.get("goal") or ""),
            entity_type=str(raw.get("entity_type") or "") or None,
            entity_id=str(raw.get("entity_id") or "") or None,
        )
        context.run_id = str(raw.get("run_id") or context.run_id)
        context.origin_turn_seq = int(raw.get("origin_turn_seq") or 0)
        context.parent_run_id = str(raw.get("parent_run_id") or parent_run_id)
        context.parent_call_id = str(raw.get("parent_call_id") or "")
        context.parent_call_name = str(raw.get("parent_call_name") or "")
        context.parent_call_arguments = dict(raw.get("parent_call_arguments") or {})
        context.steps = int(raw.get("steps") or 0)
        context.observations = list(raw.get("observations") or [])
        events = self._require_session_manager().event_log.iter_events(self._scope())
        persisted = (
            list(main_messages)
            if agent == "main" and main_messages is not None
            else derive_main_messages(events)
            if agent == "main"
            else derive_domain_messages(events, agent=agent, run_id=context.run_id)
        )
        if agent == "main" and main_messages is None:
            persisted.insert(
                0,
                canonical_message(
                    {
                        "role": "system",
                        "content": self.main_agent.current_system(
                            repository=repository,
                            memory_context=self._memory_context(context.goal),
                        ),
                    }
                ),
            )
        context.messages = [canonical_message(message) for message in persisted]
        if not context.messages:
            raise RoutingError("stored Agent message thread is missing")
        waiting = raw.get("waiting_for_user")
        if isinstance(waiting, dict):
            question = str(waiting.get("question") or "").strip()
            if not question:
                raise RoutingError("stored waiting-for-user request is invalid")
            context.user_input_request = WaitForUser(
                question,
                str(waiting.get("call_id") or "") or None,
            )
            if context.user_input_request.call_id:
                open_call = context.unresolved_tool_call(
                    context.user_input_request.call_id
                )
                function = (open_call or {}).get("function") or {}
                raw_arguments = (
                    function.get("arguments") if isinstance(function, dict) else None
                )
                try:
                    arguments = (
                        json.loads(raw_arguments)
                        if isinstance(raw_arguments, str)
                        else dict(raw_arguments or {})
                    )
                except (TypeError, ValueError) as exc:
                    raise RoutingError(
                        "stored waiting-for-user arguments are invalid"
                    ) from exc
                if (
                    open_call is None
                    or str(function.get("name") or "")
                    != "runtime__wait_for_user"
                    or set(arguments) != {"question"}
                    or str(arguments.get("question") or "").strip()
                    != context.user_input_request.question
                ):
                    raise RoutingError(
                        "stored waiting-for-user call correlation is invalid"
                    )
        context.final_message = str(raw.get("final_message") or "")
        context.code_candidate = self._restore_candidate(raw.get("code_candidate"))
        context.change_request = self._restore_change_request(raw.get("change_request"))
        context.verification = self._restore_verification(raw.get("verification"))
        issue_reply = raw.get("issue_reply")
        if isinstance(issue_reply, dict):
            raw_decision = issue_reply.get("decision")
            try:
                stage = IssueReplyStage(str(issue_reply.get("stage") or ""))
                reply_decision = (
                    WorkflowTurnDecision(
                        ApprovalIntent(str(raw_decision.get("action") or "")),
                        instruction=str(raw_decision.get("instruction") or ""),
                        message=str(raw_decision.get("message") or ""),
                    )
                    if isinstance(raw_decision, dict)
                    else None
                )
            except ValueError as exc:
                raise RoutingError("stored Issue reply workflow is invalid") from exc
            context.issue_reply = IssueReplyWorkflow(
                stage=stage,
                draft=str(issue_reply.get("draft") or ""),
                decision=reply_decision,
            )
        coding_task = raw.get("coding_task")
        if isinstance(coding_task, dict):
            context.coding_task = CodingTask(
                mode=str(coding_task.get("mode") or ""),
                task=str(coding_task.get("task") or ""),
                evidence=dict(coding_task.get("evidence") or {}),
                change_request=self._restore_change_request(
                    coding_task.get("change_request")
                ),
            )
        context.coding_task_completed = bool(raw.get("coding_task_completed", False))
        context.code_explanation = self._restore_explanation(raw.get("code_explanation"))
        context.code_review = self._restore_review(raw.get("code_review"))
        context.code_plan = self._restore_plan(raw.get("code_plan"))
        context.review_dialogue = _optional_dict(raw.get("review_dialogue"))
        context.ci_analysis = _optional_dict(raw.get("ci_analysis"))
        context.merge_readiness = _optional_dict(raw.get("merge_readiness"))
        context.read_cache = dict(raw.get("read_cache") or {})
        context.file_reads = FileReadLedger.from_plain(raw.get("file_reads"))
        context.error = str(raw.get("error")) if raw.get("error") is not None else None
        context.finished = bool(raw.get("finished", False))
        active_children = raw.get("active_children")
        if not isinstance(active_children, dict):
            raise RoutingError("stored active_children state is invalid")
        for call_id, child_raw in active_children.items():
            if not isinstance(call_id, str) or not isinstance(child_raw, dict):
                raise RoutingError("stored active child entry is invalid")
            child = self._restore_agent_context(
                child_raw,
                repository=repository,
                parent_agent=agent,
                parent_run_id=context.run_id,
            )
            if child.parent_call_id != call_id:
                raise RoutingError("stored active child call_id does not match")
            if child.parent_run_id != context.run_id:
                raise RoutingError("stored active child parent_run_id does not match")
            context.active_children[call_id] = child
        uncommitted = raw.get("uncommitted_capability_results")
        if not isinstance(uncommitted, dict):
            raise RoutingError(
                "stored uncommitted_capability_results state is invalid"
            )
        for call_id, value in uncommitted.items():
            if not isinstance(call_id, str) or not isinstance(value, dict):
                raise RoutingError("stored uncommitted Capability result is invalid")
            context.uncommitted_capability_results[call_id] = (
                self._restore_capability_record(call_id, value)
            )
        pending = raw.get("pending")
        if isinstance(pending, dict):
            calls = [
                PlannedCapabilityCall(
                    str(item["capability_id"]), dict(item.get("arguments") or {})
                )
                for item in pending.get("calls", [])
            ]
            self._validate_restored_mutation_plan(context, calls)
            self.loop.restore_pending(
                context,
                approval_id=str(pending.get("approval_id") or ""),
                summary=str(pending.get("summary") or ""),
                calls=calls,
                provider_call_id=str(pending.get("provider_call_id") or "") or None,
            )
        if context.issue_reply is not None and not context.finished and not context.error:
            if context.issue_reply.decision is not None:
                raise RoutingError("stored Issue reply contains an unconsumed decision")
            if (
                context.issue_reply.stage == IssueReplyStage.DRAFT
                and context.user_input_request is None
            ):
                raise RoutingError("stored Issue reply draft is not waiting for input")
            if (
                context.issue_reply.stage == IssueReplyStage.PUBLISH
                and context.pending is None
            ):
                raise RoutingError("stored Issue reply publish stage lacks approval")
        return context

    def _validate_restored_mutation_plan(
        self, context: AgentContext, calls: list[PlannedCapabilityCall]
    ) -> None:
        if not calls:
            raise RoutingError("stored mutation plan is empty")
        if any(
            "repository" in call.arguments
            and str(call.arguments["repository"]) != context.repository
            for call in calls
        ):
            raise RoutingError("stored mutation plan belongs to a different repository")
        if context.agent == "repository":
            if (
                context.change_request is None
                or context.code_candidate is None
                or context.verification is None
                or not context.verification.passed
                or calls
                != repository_change_mutation_plan(
                    context.change_request, context.code_candidate
                )
            ):
                raise RoutingError("stored Repository proposal is not the verified plan")
            return
        if context.agent == "issues":
            code_ids = {"github.create_branch", "github.commit", "github.push", "github.create_draft_pr"}
            if any(call.capability_id in code_ids for call in calls):
                if (
                    context.change_request is None
                    or context.code_candidate is None
                    or context.verification is None
                    or not context.verification.passed
                ):
                    raise RoutingError("stored Issue fix lacks verified artifacts")
                review = code_change_review_package(
                    context.change_request, context.code_candidate, context.verification
                )
                if calls != issue_fix_mutation_plan(
                    context.session_id,
                    context.change_request,
                    context.code_candidate,
                    review,
                ):
                    raise RoutingError("stored Issue fix plan changed")
                return
            if context.issue_reply is not None:
                expected = [
                    PlannedCapabilityCall(
                        "github.post_comment",
                        {
                            "issue_number": int(context.entity_id or 0),
                            "body": context.issue_reply.draft,
                        },
                    )
                ]
                if (
                    context.issue_reply.stage != IssueReplyStage.PUBLISH
                    or not context.issue_reply.draft
                    or context.entity_id is None
                    or not context.entity_id.isdigit()
                    or calls != expected
                ):
                    raise RoutingError("stored Issue reply proposal is invalid")
                return
            if len(calls) != 1 or calls[0].capability_id not in {
                "github.post_comment",
                "github.create_issue",
                "github.update_issue",
                "github.set_issue_lock",
            }:
                raise RoutingError("stored Issue proposal is invalid")
            return
        if context.agent == "pull_requests":
            if len(calls) != 1 or calls[0].capability_id not in {
                "github.post_review",
                "github.commit",
                "github.merge",
            }:
                raise RoutingError("stored Pull Request proposal is invalid")
            try:
                self.loop.dispatcher.validate_protected_capability(
                    context, calls[0].capability_id, calls[0].arguments
                )
            except WorkflowError as exc:
                raise RoutingError("stored Pull Request proposal violates safety") from exc

    @staticmethod
    def _restore_candidate(raw: Any) -> CandidatePatch | None:
        if not isinstance(raw, dict):
            return None
        return CandidatePatch(
            summary=str(raw.get("summary") or ""),
            root_cause=str(raw.get("root_cause") or ""),
            added_files=[str(item) for item in raw.get("added_files", [])],
            modified_files=[str(item) for item in raw.get("modified_files", [])],
            deleted_files=[str(item) for item in raw.get("deleted_files", [])],
            patch=str(raw.get("patch") or ""),
            files={str(key): str(value) for key, value in dict(raw.get("files") or {}).items()},
            risks=[str(item) for item in raw.get("risks", [])],
            verification_required=[str(item) for item in raw.get("verification_required", [])],
        )

    @staticmethod
    def _restore_change_request(raw: Any) -> ChangeRequest | None:
        if not isinstance(raw, dict):
            return None
        return ChangeRequest(
            repository=str(raw.get("repository") or ""),
            description=str(raw.get("description") or ""),
            base_branch=str(raw.get("base_branch") or "main"),
            target_files=[str(item) for item in raw.get("target_files", [])],
            replacements=[
                Replacement(
                    str(item.get("path") or ""),
                    str(item.get("old") or ""),
                    str(item.get("new") or ""),
                )
                for item in raw.get("replacements", [])
                if isinstance(item, dict)
            ],
            proposed_files={str(key): str(value) for key, value in dict(raw.get("proposed_files") or {}).items()},
            deleted_files=[str(item) for item in raw.get("deleted_files", [])],
            issue_number=int(raw["issue_number"]) if raw.get("issue_number") is not None else None,
            suggested_title=str(raw.get("suggested_title")) if raw.get("suggested_title") is not None else None,
            source_ref=str(raw.get("source_ref")) if raw.get("source_ref") is not None else None,
        )

    @staticmethod
    def _restore_verification(raw: Any) -> VerificationReport | None:
        if not isinstance(raw, dict):
            return None
        return VerificationReport(
            passed=bool(raw.get("passed")),
            checks=[
                VerificationCheck(
                    name=str(item.get("name") or ""),
                    status=str(item.get("status") or ""),
                    details=str(item.get("details") or ""),
                    files=[str(path) for path in item.get("files", [])],
                )
                for item in raw.get("checks", [])
                if isinstance(item, dict)
            ],
            skipped=[str(item) for item in raw.get("skipped", [])],
            attempts=int(raw.get("attempts") or 1),
        )

    @staticmethod
    def _restore_explanation(raw: Any) -> CodeExplanationResult | None:
        if not isinstance(raw, dict):
            return None
        return CodeExplanationResult(
            [str(item) for item in raw.get("behavior_changes", [])],
            [str(item) for item in raw.get("key_symbols", [])],
            [str(item) for item in raw.get("call_relationships", [])],
            [str(item) for item in raw.get("impact_scope", [])],
        )

    @staticmethod
    def _restore_review(raw: Any) -> CodeReviewResult | None:
        if not isinstance(raw, dict):
            return None
        return CodeReviewResult(
            summary=str(raw.get("summary") or ""),
            blocking_issues=[str(item) for item in raw.get("blocking_issues", [])],
            impacts=[str(item) for item in raw.get("impacts", [])],
            suggestions=[str(item) for item in raw.get("suggestions", [])],
            test_assessment=str(raw.get("test_assessment") or ""),
            risk_level=str(raw.get("risk_level") or "MEDIUM"),
            recommendation=Recommendation(str(raw.get("recommendation") or "NEEDS_HUMAN_REVIEW")),
            goal_alignment=str(raw.get("goal_alignment") or "UNKNOWN"),
        )

    @staticmethod
    def _restore_plan(raw: Any) -> CodePlanResult | None:
        if not isinstance(raw, dict):
            return None
        return CodePlanResult(
            direction=str(raw.get("direction") or ""),
            files=[str(item) for item in raw.get("files", [])],
            tradeoffs=[str(item) for item in raw.get("tradeoffs", [])],
            tests=[str(item) for item in raw.get("tests", [])],
        )

    def _guidance(
        self, query: str, entity_type: str | None, entity_id: str | None
    ) -> AgentGuidance | None:
        memory = self._memory_context(query)
        guidance = AgentGuidance(
            persistent_memory_index=memory.index,
            persistent_memory_pages=MemorySearch.render(memory.selected_pages),
            resolved_references=self._resolved_references(entity_type, entity_id),
        )
        return None if guidance.empty else guidance

    def _memory_context(self, query: str) -> Any:
        if self.memory_search is None:
            raise RoutingError("GitAgentService requires a MemorySearch")
        scope = self._scope()
        return self.memory_search.context(scope.account_key, scope.repository_key, query)

    @staticmethod
    def _resolved_references(
        entity_type: str | None, entity_id: str | None
    ) -> tuple[ResolvedReference, ...]:
        if entity_type in {"issue", "pull_request", "workflow_run"} and entity_id:
            return (ResolvedReference(entity_type, entity_id),)
        return ()

    @staticmethod
    def _context_description(context: AgentContext) -> dict[str, Any]:
        return {
            "workflow_type": context.agent,
            "entity": {
                "type": context.entity_type or "repository",
                "id": context.entity_id or context.repository,
            },
            "state": "awaiting_approval",
            "proposal_summary": context.pending.summary if context.pending else "",
            "mutation_plan": [
                {"capability_id": call.capability_id, "arguments": call.arguments}
                for call in (context.pending.calls if context.pending else [])
            ],
        }

    @staticmethod
    def _proposal_description(context: AgentContext) -> str:
        if context.pending is None:
            return "当前没有待确认的提案。"
        if context.issue_reply is not None and context.issue_reply.draft:
            return context.issue_reply.draft
        candidate = context.code_candidate
        if candidate is None:
            return context.pending.summary
        return "\n\n".join(
            [
                context.pending.summary,
                *(f"### `{path}`\n````\n{content}\n````" for path, content in candidate.files.items()),
                f"### Diff\n```diff\n{candidate.patch}\n```",
            ]
        )

    def _persist_agent_message(
        self, context: AgentContext, message: dict[str, Any]
    ) -> dict[str, Any]:
        turn_seq = self._active_turn_seq or context.origin_turn_seq
        if turn_seq < 1:
            raise RoutingError("Agent model message requires an active Turn")
        return self._require_session_manager().record_model_message(
            self._scope(),
            message,
            turn_seq=turn_seq,
            agent=context.agent,
            **({"run_id": context.run_id} if context.agent != "main" else {}),
        )

    def _persist_agent_compaction(
        self,
        context: AgentContext,
        plan: MessageCompactionPlan,
        level: str,
        before_tokens: int,
        after_tokens: int,
    ) -> None:
        self._require_session_manager().record_message_compaction(
            self._scope(),
            turn_seq=self._active_turn_seq or context.origin_turn_seq,
            agent=context.agent,
            checkpoint=plan.checkpoint,
            retain_message_indexes=plan.retain_message_indexes,
            tool_replacements=plan.tool_replacements,
            **({"run_id": context.run_id} if context.agent != "main" else {}),
        )
        self.harness.trace.emit_auto_compaction(
            session_id=context.session_id,
            agent=context.agent,
            level=level,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            context_window_tokens=context.context_window_tokens,
            turn_seq=self._active_turn_seq or context.origin_turn_seq,
        )

    def _save_context(self, context: AgentContext) -> None:
        self._require_session_manager().save_agent_context(
            self._scope(), self._serialize_context(context)
        )

    def _clear_context(self) -> None:
        self._require_session_manager().save_agent_context(self._scope(), None)

    def _load_context(
        self, *, main_messages: list[dict[str, Any]] | None = None
    ) -> AgentContext | None:
        session = self._require_session_manager().get_session(
            self._scope().account_key,
            self._scope().repository_key,
            self._scope().session_id,
        )
        if session is None:
            raise RoutingError("Session not found")
        context = (
            self._restore_context(
                session.agent_context,
                repository=session.repository_full_name,
                main_messages=main_messages,
            )
            if session.agent_context
            else None
        )
        if context is not None:
            self._restore_guidance(context)
        return context

    def _restore_guidance(
        self,
        context: AgentContext,
        inherited: AgentGuidance | None = None,
    ) -> None:
        if context.agent in {"issues", "pull_requests", "repository"}:
            context.guidance = self._guidance(
                context.goal, context.entity_type, context.entity_id
            )
        elif context.agent == "coding":
            context.guidance = inherited
        for child in context.active_children.values():
            self._restore_guidance(child, context.guidance)

    @staticmethod
    def _observe_service_decision(
        context: AgentContext,
        decision: WorkflowTurnDecision,
        user_input: str,
    ) -> None:
        context.observations.append(
            {
                "kind": "user_decision",
                "payload": {
                    "action": decision.action.value,
                    "instruction": decision.instruction or user_input,
                    "message": decision.message,
                },
            }
        )

    def _bind_context_turn(self, context: AgentContext) -> None:
        context.origin_turn_seq = self._active_turn_seq

    def _scope(self) -> SessionScope:
        return self._require_scope(self.session_scope)

    def _require_scope(self, supplied: SessionScope | None) -> SessionScope:
        if self.session_scope is None:
            raise RoutingError("GitAgentService requires an active Session scope")
        if supplied is not None and supplied != self.session_scope:
            raise RoutingError("request does not belong to this Service Session scope")
        return self.session_scope

    def _require_session_manager(self) -> SessionManager:
        if self.session_manager is None:
            raise RoutingError("GitAgentService requires a SessionManager")
        return self.session_manager

    def _require_live(self) -> None:
        if self._invalidated:
            raise RoutingError("this GitAgent Service is no longer active")

    @staticmethod
    def _repository(repository: str | None) -> str:
        if not repository:
            raise RoutingError("repository is ambiguous; provide owner/name explicitly")
        return str(RepositoryRef.parse(repository))


def _optional_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None
