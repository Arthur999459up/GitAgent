"""Session-scoped application service coordinating MainAgent and child AgentContext execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gitagent.agent_loop import AgentLoop
from gitagent.agents import (
    CodingAgent,
    IssueAgent,
    MainAgent,
    PullRequestAgent,
    RepositoryAgent,
)
from gitagent.capability import CapabilityLayer
from gitagent.domain.errors import RoutingError, WorkflowError
from gitagent.domain.models import (
    AgentGuidance,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    DraftResult,
    MainDecision,
    PlannedCapabilityCall,
    PullRequestOperation,
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
    assistant_tool_call,
    canonical_message,
    derive_domain_messages,
    fit_messages,
    tool_result_message,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.file_reads import FileReadLedger
from gitagent.harness.mutation_plans import (
    code_change_review_package,
    issue_fix_mutation_plan,
    repository_change_mutation_plan,
)
from gitagent.harness.validation.static import StaticVerifier
from gitagent.infra.observability import TraceBus
from gitagent.infra.persistence import SessionManager
from gitagent.memory import MemoryPageStore, MemorySearch, MemoryStopHooks
from gitagent.model import Reasoner

from .approval_intent import ApprovalIntentClassifier


@dataclass
class ServiceResult:
    decision: MainDecision
    output: Any = None
    agent: str | None = None
    goal: str = ""
    entity_type: str | None = None
    entity_id: str | None = None
    domain_output: Any = None
    workflow_summary: str = ""


class GitAgentService:
    """Run one MainAgent context per Session and one isolated child context at a time."""

    def __init__(
        self,
        capabilities: CapabilityLayer,
        *,
        main_reasoner: Reasoner,
        agent_reasoner: Reasoner | None = None,
        session_manager: SessionManager | None = None,
        memory_store: MemoryPageStore | None = None,
        memory_search: MemorySearch | None = None,
        memory_hooks: MemoryStopHooks | None = None,
        trace: TraceBus | None = None,
        session_scope: SessionScope | None = None,
        context_window_tokens: Mapping[str, int] | None = None,
    ) -> None:
        self.harness = AgentHarness(
            capabilities,
            trace=trace,
            context_window_tokens=context_window_tokens,
        )
        self.harness.message_sink = self._persist_domain_message
        self.harness.compaction_sink = self._persist_domain_compaction
        self.coding = CodingAgent(self.harness, agent_reasoner)
        self.verifier = StaticVerifier(self.harness)
        self.repository_agent = RepositoryAgent(
            self.harness,
            self.coding,
            self.verifier,
            agent_reasoner,
        )
        self.pull_request_agent = PullRequestAgent(
            self.harness, self.coding, self.verifier, agent_reasoner
        )
        self.issue_agent = IssueAgent(
            self.harness, self.coding, self.verifier, agent_reasoner
        )
        self.loop = AgentLoop(self.harness)
        self.main_agent = MainAgent(
            self.harness,
            main_reasoner,
            message_sink=self._persist_main_message,
            compaction_sink=self._persist_main_compaction,
        )
        self.classifier = ApprovalIntentClassifier(
            main_reasoner,
            context_window_tokens=self.harness.context_window_for("main"),
        )
        self.reasoner = agent_reasoner or main_reasoner
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
        if not main_messages or main_messages[-1].get("role") != "user":
            raise RoutingError(
                "GitAgentService requires the current Main message thread"
            )
        self.dispatch_started = False
        self._active_turn_seq = int(turn_seq or 0)
        if self._active_turn_seq > 0:
            self.harness.trace.bind_turn(
                self._scope().session_id, self._active_turn_seq
            )

        current = self._load_context()
        if current is not None:
            self._ensure_domain_thread_durable(current)
            if (
                current.reply_draft is not None
                and current.pending is None
                and not current.question
            ):
                self._append_resume_delegation(main_messages, current)
                self.dispatch_started = True
                domain_output = self._continue_draft(current, user_input)
                return self._finish_domain_turn(main_messages, current, domain_output)
            if current.pending is not None:
                self._append_resume_delegation(main_messages, current)
                self.dispatch_started = True
                decision = self.classifier.classify(
                    user_input=user_input,
                    proposal_context=self._context_description(current),
                )
                domain_output = self._continue_approval(current, decision, user_input)
                return self._finish_domain_turn(main_messages, current, domain_output)
            if current.question and (
                current.agent != "pull_requests"
                or self.pull_request_agent.accept_question_reply(current, user_input)
            ):
                self._append_resume_delegation(main_messages, current)
                self.dispatch_started = True
                self.loop.resume(
                    current,
                    self._agent_for(current.agent),
                    WorkflowTurnDecision(
                        ApprovalIntent.APPROVE, instruction=user_input
                    ),
                )
                domain_output = self._after_loop(current)
                return self._finish_domain_turn(main_messages, current, domain_output)
            self._clear_context()

        decision = self.main_agent.decide(
            main_messages,
            repository=repository,
            scope=self._scope(),
            tools=main_tools,
        )
        if not decision.target_agent:
            self._append_main_message(
                main_messages,
                tool_result_message(
                    self._main_tool_call_id(main_messages),
                    {"direct_answer": decision.message, "clarify": decision.clarify},
                ),
            )
            final = self.main_agent.finalize(main_messages)
            return ServiceResult(decision, final)

        self.dispatch_started = True
        domain_output, context = self._start_child(decision, repository)
        return self._finish_domain_turn(
            main_messages, context, domain_output, decision=decision
        )

    def memory_after_turn(
        self,
        *,
        turn_seq: int,
    ) -> Any:
        """Fire transcript-free stop hooks after the successful Turn is durable."""

        if self.memory_hooks is None:
            return None
        try:
            session = self._require_session_manager().get_session(
                self._scope().account_key,
                self._scope().repository_key,
                self._scope().session_id,
            )
            if session is None:
                return None
            self.memory_hooks.handle_turn_stop(
                self._scope(), session.repository_full_name, through_seq=turn_seq
            )
        except Exception:  # noqa: BLE001 - the business Turn is already durable
            return None

    def approve(self) -> Any:
        context = self._require_pending_context()
        return self._continue_approval(
            context,
            WorkflowTurnDecision(ApprovalIntent.APPROVE),
            "",
        )

    def dream_memory(self) -> dict[str, tuple[str, ...]] | None:
        """Run one explicit controlled Dream, bypassing only the automatic gate."""

        if self.memory_hooks is None:
            return None
        return self.memory_hooks.dream_now(self._scope())

    def reject(self) -> Any:
        context = self._require_pending_context()
        return self._continue_approval(
            context,
            WorkflowTurnDecision(ApprovalIntent.REJECT),
            "",
        )

    def revise_proposal(self, instruction: str) -> Any:
        context = self._require_pending_context()
        return self._continue_approval(
            context,
            WorkflowTurnDecision(
                ApprovalIntent.REVISE, instruction=instruction.strip()
            ),
            instruction,
        )

    def invalidate(self) -> None:
        if self._invalidated:
            return
        self.harness.approvals.invalidate_all()
        self._invalidated = True

    def _start_child(
        self,
        decision: MainDecision,
        repository: str,
    ) -> tuple[Any, AgentContext]:
        scope = self._scope()
        goal = decision.request.strip()
        guidance = self._guidance(goal, decision.entity_type, decision.entity_id)
        if decision.target_agent == "repository":
            context = self.harness.context(
                "repository",
                scope.session_id,
                repository=repository,
                goal=goal,
                entity_type="repository",
                guidance=guidance,
            )
            self._bind_context_turn(context)
            self.loop.start(context, self.repository_agent)
            return self._after_loop(context), context
        context = self.harness.context(
            decision.target_agent,
            scope.session_id,
            repository=repository,
            goal=goal,
            entity_type=decision.entity_type
            or self._default_entity_type(decision.target_agent),
            entity_id=decision.entity_id,
            guidance=guidance,
        )
        if decision.target_agent == "issues":
            self._bind_context_turn(context)
            if decision.requested_reply and context.entity_id:
                context.result_required = False
                self.loop.start(context, self.issue_agent)
                if context.finished and not context.error:
                    draft = self.issue_agent.draft_reply(context, self.reasoner)
                    context.reply_draft = draft
                    context.result_required = True
                    context.finished = False
                    context.result = None
                    context.final_message = ""
                    self._save_context(context)
                    return DraftResult(
                        "issue",
                        context.entity_id,
                        "Issue 回复草稿",
                        draft,
                        "草稿尚未发布。你可以直接提出修改，或确认发布。",
                    ), context
                return self._after_loop(context), context
            self.loop.start(context, self.issue_agent)
            return self._after_loop(context), context
        if decision.target_agent == "pull_requests":
            self._bind_context_turn(context)
            self.loop.start(context, self.pull_request_agent)
            return self._after_loop(context), context
        raise RoutingError(f"unsupported domain agent: {decision.target_agent}")

    def _after_loop(self, context: AgentContext) -> Any:
        if (
            context.pending is not None
            or context.question
            or (context.reply_draft is not None and not context.finished)
        ):
            self._save_context(context)
            return context
        self._clear_context()
        if context.error:
            return context
        if context.result is not None:
            return context.result
        return context.final_message or context

    def _continue_draft(self, context: AgentContext, user_input: str) -> Any:
        draft = str(context.reply_draft or "")
        if not draft or context.agent != "issues":
            raise RoutingError("current Session has no Issue reply draft")
        decision = self.classifier.classify(
            user_input=user_input,
            proposal_context={
                "workflow_type": "issue_reply_draft",
                "entity": {"type": "issue", "id": context.entity_id or ""},
                "draft": draft,
                "state": "reviewing_draft",
            },
        )
        self._observe_service_decision(context, decision, user_input)
        if decision.action == ApprovalIntent.AMBIGUOUS:
            self._save_context(context)
            return (
                decision.message or "你是想发布当前草稿、继续修改，还是查看草稿内容？"
            )
        if decision.action == ApprovalIntent.QUESTION:
            self._save_context(context)
            return DraftResult(
                "issue",
                context.entity_id,
                "Issue 回复草稿",
                draft,
                "这是当前草稿，尚未发布。",
            )
        if decision.action == ApprovalIntent.REJECT:
            self._save_context(context)
            return DraftResult(
                "issue",
                context.entity_id,
                "Issue 回复草稿",
                draft,
                "本次没有发布；草稿仍保留。",
            )
        if decision.action == ApprovalIntent.APPROVE:
            context.finished = False
            context.error = None
            self.loop.start(context, self.issue_agent)
            return self._after_loop(context)
        instruction = decision.instruction.strip() or user_input.strip()
        revised = self._revise_text(
            context, "GitHub Issue reply draft", draft, instruction
        )
        context.reply_draft = revised
        self._save_context(context)
        return DraftResult(
            "issue",
            context.entity_id,
            "Issue 回复草稿 · 已修改",
            revised,
            "仍未发布。继续提修改即可；确认后再发布。",
        )

    def _continue_approval(
        self,
        context: AgentContext,
        decision: WorkflowTurnDecision,
        user_input: str,
    ) -> Any:
        if context.pending is None:
            raise RoutingError("当前 Session 没有待审批提案")
        if decision.action == ApprovalIntent.AMBIGUOUS:
            self._observe_service_decision(context, decision, user_input)
            self._save_context(context)
            return decision.message
        if decision.action == ApprovalIntent.QUESTION:
            self._observe_service_decision(context, decision, user_input)
            self._save_context(context)
            return self._proposal_description(context)

        if (
            context.agent == "issues"
            and context.reply_draft is not None
            and decision.action
            in {
                ApprovalIntent.REJECT,
                ApprovalIntent.REVISE,
            }
        ):
            self._observe_service_decision(context, decision, user_input)
            self.harness.approvals.decide(context.pending.approval_id, "Reject")
            context.pending = None
            if decision.action == ApprovalIntent.REVISE:
                return self._revise_draft(
                    context, decision.instruction.strip() or user_input.strip()
                )
            self._save_context(context)
            return DraftResult(
                "issue",
                context.entity_id,
                "Issue 回复草稿",
                context.reply_draft,
                "已拒绝本次发布提案；草稿保留，可继续修改。",
            )

        if context.agent == "repository" and decision.action == ApprovalIntent.REVISE:
            request = context.change_request
            if request is None:
                raise RoutingError(
                    "repository modification revision has no change request"
                )
            instruction = decision.instruction.strip() or user_input.strip()
            revised_description = (
                f"{request.description}\n\nUser revision: {instruction}"
            )
            request.description = revised_description
            context.goal = revised_description
            context.code_candidate = None
            context.verification = None
            context.result = None
            context.final_message = ""
            self.loop.resume(context, self.repository_agent, decision)
            return self._after_loop(context)

        if (
            context.agent == "pull_requests"
            and decision.action == ApprovalIntent.REVISE
            and context.pending is not None
            and len(context.pending.calls) == 1
            and context.pending.calls[0].capability_id == "github.post_review"
        ):
            self._observe_service_decision(context, decision, user_input)
            return self._revise_pr_review(
                context, decision.instruction.strip() or user_input.strip()
            )

        self.loop.resume(context, self._agent_for(context.agent), decision)
        return self._after_loop(context)

    def _revise_draft(self, context: AgentContext, instruction: str) -> DraftResult:
        current = str(context.reply_draft or "")
        revised = self._revise_text(
            context, "GitHub Issue reply draft", current, instruction
        )
        context.reply_draft = revised
        self._save_context(context)
        return DraftResult(
            "issue", context.entity_id, "Issue 回复草稿 · 已修改", revised, "仍未发布。"
        )

    def _revise_pr_review(
        self, context: AgentContext, instruction: str
    ) -> AgentContext:
        pending = context.pending
        if (
            pending is None
            or context.operation != PullRequestOperation.POST_REVIEW.value
            or len(pending.calls) != 1
            or pending.calls[0].capability_id != "github.post_review"
        ):
            raise RoutingError("当前 Pull Request 提案不是可修改的 Review 正文")
        call = pending.calls[0]
        revised = self._revise_text(
            context,
            "GitHub Pull Request review body",
            str(call.arguments.get("body") or ""),
            instruction,
        )
        arguments = dict(call.arguments)
        arguments["body"] = revised

        self.harness.approvals.supersede(pending.approval_id)
        self.loop.restore_pending(
            context,
            summary=pending.summary,
            calls=[PlannedCapabilityCall(call.capability_id, arguments)],
        )
        self._save_context(context)
        return context

    def _revise_text(
        self,
        context: AgentContext,
        artifact: str,
        current: str,
        instruction: str,
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    f"You revise a {artifact}. Follow the user's editing instruction exactly. "
                    "Return only the revised text. Do not claim it was posted and do not add meta commentary."
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
        messages, _, _ = fit_messages(
            messages,
            None,
            context_window_tokens=context.context_window_tokens,
        )
        revised = self.reasoner.complete_text_messages(
            messages=messages,
            context_window_tokens=context.context_window_tokens,
        ).strip()
        if not revised:
            raise RoutingError(f"{artifact} revision returned empty text")
        return revised

    def _result_for_context(self, context: AgentContext, output: Any) -> ServiceResult:
        return ServiceResult(
            MainDecision(
                target_agent=context.agent,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                request=context.goal,
            ),
            output,
            agent=context.agent,
            goal=context.goal,
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            domain_output=output,
        )

    def _finish_domain_turn(
        self,
        main_messages: list[dict[str, Any]],
        context: AgentContext,
        domain_output: Any,
        *,
        decision: MainDecision | None = None,
    ) -> ServiceResult:
        from .projection import domain_summary

        effective_decision = decision or MainDecision(
            target_agent=context.agent,
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            request=context.goal,
        )
        summary = domain_summary(
            agent=context.agent,
            goal=context.goal,
            output=domain_output,
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            context=context,
        )
        self._append_main_message(
            main_messages,
            tool_result_message(self._main_tool_call_id(main_messages), summary),
        )
        final = self.main_agent.finalize(main_messages)
        return ServiceResult(
            effective_decision,
            final,
            agent=context.agent,
            goal=context.goal,
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            domain_output=domain_output,
            workflow_summary=summary,
        )

    def _append_resume_delegation(
        self, main_messages: list[dict[str, Any]], context: AgentContext
    ) -> None:
        self._append_main_message(
            main_messages,
            assistant_tool_call(
                f"call-resume-{context.run_id}",
                f"delegate_{context.agent}",
                {"task": context.goal, "resume": True},
            ),
        )

    def _append_main_message(
        self, messages: list[dict[str, Any]], message: dict[str, Any]
    ) -> dict[str, Any]:
        safe = self._persist_main_message(message)
        messages.append(safe)
        return safe

    def _persist_main_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if self._active_turn_seq < 1:
            raise RoutingError("Main model message requires an active Turn")
        return self._require_session_manager().record_model_message(
            self._scope(),
            message,
            turn_seq=self._active_turn_seq,
            agent="main",
        )

    def _persist_main_compaction(self, plan: MessageCompactionPlan) -> None:
        if self._active_turn_seq < 1:
            raise RoutingError("Main compaction requires an active Turn")
        self._require_session_manager().record_message_compaction(
            self._scope(),
            turn_seq=self._active_turn_seq,
            agent="main",
            checkpoint=plan.checkpoint,
            retain_message_indexes=plan.retain_message_indexes,
            tool_replacements=plan.tool_replacements,
        )

    @staticmethod
    def _main_tool_call_id(messages: list[dict[str, Any]]) -> str:
        resolved = {
            str(message.get("tool_call_id") or "")
            for message in messages
            if message.get("role") == "tool"
        }
        for message in reversed(messages):
            for call in reversed(message.get("tool_calls") or []):
                call_id = str(call.get("id") or "")
                if call_id and call_id not in resolved:
                    return call_id
        raise RoutingError("Main delegation is missing its assistant tool call")

    def _ensure_domain_thread_durable(self, context: AgentContext) -> None:
        """Migrate an old paused in-memory Domain thread only when a new Turn resumes it."""

        if self._active_turn_seq < 1 or not context.messages:
            return
        persisted = derive_domain_messages(
            self._require_session_manager().event_log.iter_events(self._scope()),
            agent=context.agent,
            run_id=context.run_id,
        )
        if persisted:
            context.messages = persisted
            return
        context.messages = [
            self._require_session_manager().record_model_message(
                self._scope(),
                message,
                turn_seq=self._active_turn_seq,
                agent=context.agent,
                run_id=context.run_id,
            )
            for message in context.messages
        ]

    def _persist_domain_message(
        self, context: AgentContext, message: dict[str, Any]
    ) -> dict[str, Any]:
        return self._require_session_manager().record_model_message(
            self._scope(),
            message,
            turn_seq=self._active_turn_seq or context.origin_turn_seq,
            agent=context.agent,
            run_id=context.run_id,
        )

    def _persist_domain_compaction(
        self, context: AgentContext, plan: MessageCompactionPlan
    ) -> None:
        self._require_session_manager().record_message_compaction(
            self._scope(),
            turn_seq=self._active_turn_seq or context.origin_turn_seq,
            agent=context.agent,
            checkpoint=plan.checkpoint,
            retain_message_indexes=plan.retain_message_indexes,
            tool_replacements=plan.tool_replacements,
            run_id=context.run_id,
        )

    def _save_context(self, context: AgentContext) -> None:
        self._require_session_manager().save_agent_context(
            self._scope(), self._serialize_context(context)
        )

    def _clear_context(self) -> None:
        self._require_session_manager().save_agent_context(self._scope(), None)

    def _load_context(self) -> AgentContext | None:
        session = self._require_session_manager().get_session(
            self._scope().account_key,
            self._scope().repository_key,
            self._scope().session_id,
        )
        if session is None:
            raise RoutingError("Session not found")
        context = (
            self._restore_context(
                session.agent_context, repository=session.repository_full_name
            )
            if session.agent_context
            else None
        )
        if context is not None:
            context.guidance = self._stored_guidance(context)
        return context

    def _stored_guidance(self, context: AgentContext) -> AgentGuidance | None:
        memory = self._memory_context(context.goal)
        guidance = AgentGuidance(
            persistent_memory_index=memory.index,
            persistent_memory_pages=MemorySearch.render(memory.selected_pages),
            resolved_references=self._resolved_references(
                context.entity_type, context.entity_id
            ),
        )
        return None if guidance.empty else guidance

    def _require_pending_context(self) -> AgentContext:
        self._require_live()
        context = self._load_context()
        if context is None or context.pending is None:
            raise RoutingError("当前 Session 没有待审批提案")
        return context

    def _serialize_context(self, context: AgentContext) -> dict[str, Any]:
        pending = None
        if context.pending is not None:
            pending = {
                "approval_id": context.pending.approval_id,
                "summary": context.pending.summary,
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
            "repository": context.repository,
            "goal": context.goal,
            "entity_type": context.entity_type,
            "entity_id": context.entity_id,
            "operation": context.operation,
            "requested_outcome": context.requested_outcome,
            "steps": context.steps,
            "max_steps": context.max_steps,
            "observations": to_plain(context.observations),
            "pending": pending,
            "question": context.question,
            "final_message": context.final_message,
            "code_candidate": to_plain(context.code_candidate),
            "change_request": to_plain(context.change_request),
            "verification": to_plain(context.verification),
            "reply_draft": context.reply_draft,
            "result_required": context.result_required,
            "read_cache": to_plain(context.read_cache),
            "file_reads": context.file_reads.to_plain(),
            "error": context.error,
            "finished": context.finished,
        }

    def _restore_context(self, raw: dict[str, Any], *, repository: str) -> AgentContext:
        agent = str(raw.get("agent") or "")
        if agent not in {"issues", "pull_requests", "repository"}:
            raise RoutingError("stored Session agent context is invalid")
        stored_repository = str(raw.get("repository") or "")
        if stored_repository != repository:
            raise RoutingError(
                "stored Session agent context belongs to a different repository"
            )
        context = self.harness.context(
            agent,
            self._scope().session_id,
            repository=repository,
            goal=str(raw.get("goal") or ""),
            entity_type=str(raw.get("entity_type") or "") or None,
            entity_id=str(raw.get("entity_id") or "") or None,
            guidance=None,
            max_steps=int(raw.get("max_steps") or 20),
        )
        context.run_id = str(raw.get("run_id") or context.run_id)
        context.origin_turn_seq = int(raw.get("origin_turn_seq") or 0)
        context.steps = int(raw.get("steps") or 0)
        context.observations = list(raw.get("observations") or [])
        persisted_messages = derive_domain_messages(
            self._require_session_manager().event_log.iter_events(self._scope()),
            agent=agent,
            run_id=context.run_id,
        )
        context.messages = (
            persisted_messages
            if persisted_messages
            else [
                canonical_message(message)
                for message in raw.get("messages", [])
                if isinstance(message, dict)
            ]
        )
        context.operation = str(raw.get("operation") or "")
        context.requested_outcome = str(raw.get("requested_outcome") or "")
        context.question = str(raw.get("question") or "")
        context.final_message = str(raw.get("final_message") or "")
        context.code_candidate = self._restore_candidate(raw.get("code_candidate"))
        context.change_request = self._restore_change_request(raw.get("change_request"))
        context.verification = self._restore_verification(raw.get("verification"))
        context.reply_draft = (
            str(raw.get("reply_draft")) if raw.get("reply_draft") is not None else None
        )
        context.result_required = bool(raw.get("result_required", True))
        context.read_cache = dict(raw.get("read_cache") or {})
        context.file_reads = FileReadLedger.from_plain(raw.get("file_reads"))
        context.error = str(raw.get("error")) if raw.get("error") is not None else None
        context.finished = bool(raw.get("finished", False))
        pending = raw.get("pending")
        if isinstance(pending, dict):
            if context.finished or context.error:
                raise RoutingError(
                    "stored Session agent context cannot be terminal and pending"
                )
            calls = [
                PlannedCapabilityCall(
                    str(item["capability_id"]),
                    dict(item.get("arguments") or {}),
                )
                for item in pending.get("calls", [])
            ]
            self._validate_restored_mutation_plan(context, calls)
            if any(
                "repository" in call.arguments
                and str(call.arguments["repository"]) != repository
                for call in calls
            ):
                raise RoutingError(
                    "stored Session mutation plan belongs to a different repository"
                )
            self.loop.restore_pending(
                context,
                summary=str(pending.get("summary") or ""),
                calls=calls,
            )
            if not context.messages:
                origin_turn_seq = context.origin_turn_seq
                context.origin_turn_seq = 0
                try:
                    context.start_message_thread()
                    context.append_message(
                        {
                            "role": "assistant",
                            "content": f"{context.pending.summary}\n\n请确认是否执行。",
                        }
                    )
                finally:
                    context.origin_turn_seq = origin_turn_seq
        return context

    def _validate_restored_mutation_plan(
        self,
        context: AgentContext,
        calls: list[PlannedCapabilityCall],
    ) -> None:
        if context.agent == "repository":
            if (
                context.change_request is None
                or context.code_candidate is None
                or context.verification is None
                or not context.verification.passed
            ):
                raise RoutingError(
                    "stored RepositoryAgent proposal lacks reviewed change artifacts"
                )
            expected = repository_change_mutation_plan(
                context.change_request,
                context.code_candidate,
            )
            if calls != expected:
                raise RoutingError(
                    "stored RepositoryAgent proposal contains an invalid mutation plan"
                )
            return
        if context.agent == "issues":
            code_capabilities = {
                "github.create_branch",
                "github.commit",
                "github.push",
                "github.create_draft_pr",
            }
            if any(call.capability_id in code_capabilities for call in calls):
                if (
                    context.change_request is None
                    or context.code_candidate is None
                    or context.verification is None
                    or not context.verification.passed
                ):
                    raise RoutingError(
                        "stored Issue code proposal lacks a verified CandidatePatch"
                    )
                review = code_change_review_package(
                    context.change_request,
                    context.code_candidate,
                    context.verification,
                )
                expected = issue_fix_mutation_plan(
                    context.session_id,
                    context.change_request,
                    context.code_candidate,
                    review,
                )
                if calls != expected:
                    raise RoutingError(
                        "stored Issue code proposal contains an invalid mutation plan"
                    )
                return
            allowed = {
                "github.post_comment",
                "github.create_issue",
                "github.update_issue",
                "github.set_issue_lock",
            }
            if len(calls) != 1 or calls[0].capability_id not in allowed:
                raise RoutingError(
                    "stored Issue proposal contains an invalid mutation plan"
                )
            return
        if context.agent == "pull_requests":
            if len(calls) != 1 or calls[0].capability_id not in {
                "github.post_review",
                "github.commit",
                "github.merge",
            }:
                raise RoutingError(
                    "stored PullRequestAgent proposal contains an invalid mutation plan"
                )
            call = calls[0]
            expected_operations = {
                "github.post_review": {PullRequestOperation.POST_REVIEW.value},
                "github.commit": {
                    PullRequestOperation.MODIFY.value,
                    PullRequestOperation.CI_FIX.value,
                },
                "github.merge": {PullRequestOperation.MERGE.value},
            }
            if context.operation not in expected_operations[call.capability_id]:
                raise RoutingError(
                    "stored PullRequestAgent proposal does not match its selected operation"
                )
            if call.capability_id == "github.post_review" and (
                context.entity_id is None
                or not str(context.entity_id).isdigit()
                or call.arguments.get("pr_number") != int(context.entity_id)
                or str(call.arguments.get("event") or "") != context.requested_outcome
            ):
                raise RoutingError(
                    "stored PullRequestAgent Review proposal changed its target or event"
                )
            if call.capability_id in {"github.commit", "github.merge"}:
                try:
                    self.loop.dispatcher.validate_protected_capability(
                        context,
                        call.capability_id,
                        call.arguments,
                    )
                except WorkflowError as exc:
                    raise RoutingError(
                        "stored PullRequestAgent proposal violates a protected mutation boundary"
                    ) from exc

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
            files={
                str(key): str(value)
                for key, value in dict(raw.get("files") or {}).items()
            },
            static_checks=[str(item) for item in raw.get("static_checks", [])],
            risks=[str(item) for item in raw.get("risks", [])],
            verification_required=[
                str(item) for item in raw.get("verification_required", [])
            ],
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
            proposed_files={
                str(key): str(value)
                for key, value in dict(raw.get("proposed_files") or {}).items()
            },
            deleted_files=[str(item) for item in raw.get("deleted_files", [])],
            issue_number=int(raw["issue_number"])
            if raw.get("issue_number") is not None
            else None,
            suggested_title=str(raw.get("suggested_title"))
            if raw.get("suggested_title") is not None
            else None,
            source_ref=str(raw.get("source_ref"))
            if raw.get("source_ref") is not None
            else None,
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

    def _guidance(
        self,
        query: str,
        entity_type: str | None,
        entity_id: str | None,
    ) -> AgentGuidance | None:
        memory = self._memory_context(query)
        guidance = AgentGuidance(
            persistent_memory_index=memory.index,
            persistent_memory_pages=MemorySearch.render(memory.selected_pages),
            resolved_references=self._resolved_references(entity_type, entity_id),
        )
        return None if guidance.empty else guidance

    def _memory_context(self, query: str) -> Any:
        scope = self._scope()
        if self.memory_search is None:
            raise RoutingError("GitAgentService requires a MemorySearch")
        return self.memory_search.context(
            scope.account_key, scope.repository_key, query
        )

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
            "state": "awaiting_approval" if context.pending is not None else "active",
            "proposal_summary": context.pending.summary
            if context.pending is not None
            else "",
            "mutation_plan": [
                {"capability_id": call.capability_id, "arguments": call.arguments}
                for call in (
                    context.pending.calls if context.pending is not None else []
                )
            ],
        }

    @staticmethod
    def _proposal_description(context: AgentContext) -> str:
        if context.pending is None:
            return "当前没有待确认的提案。"
        if context.reply_draft is not None:
            return context.reply_draft
        candidate = context.code_candidate
        if candidate is None:
            return context.pending.summary
        sections = [context.pending.summary]
        sections.extend(
            f"### `{path}`\n````\n{content}\n````"
            for path, content in candidate.files.items()
        )
        if candidate.deleted_files:
            sections.append("删除文件：" + ", ".join(candidate.deleted_files))
        sections.append(f"### Diff\n```diff\n{candidate.patch}\n```")
        return "\n\n".join(sections)

    def _agent_for(self, name: str) -> Any:
        agents = {
            "issues": self.issue_agent,
            "pull_requests": self.pull_request_agent,
            "repository": self.repository_agent,
        }
        try:
            return agents[name]
        except KeyError as exc:
            raise RoutingError(
                f"agent has no AgentLoop implementation: {name}"
            ) from exc

    def _scope(self) -> SessionScope:
        return self._require_scope(self.session_scope)

    def _require_scope(self, supplied_scope: SessionScope | None) -> SessionScope:
        if self.session_scope is None:
            raise RoutingError("GitAgentService requires an active Session scope")
        if supplied_scope is not None and supplied_scope != self.session_scope:
            raise RoutingError("request does not belong to this Service Session scope")
        return self.session_scope

    def _require_session_manager(self) -> SessionManager:
        if self.session_manager is None:
            raise RoutingError("GitAgentService requires a SessionManager")
        return self.session_manager

    def _require_memory_store(self) -> MemoryPageStore:
        if self.memory_store is None:
            raise RoutingError("GitAgentService requires a MemoryPageStore")
        return self.memory_store

    @staticmethod
    def _observe_service_decision(
        context: AgentContext,
        decision: WorkflowTurnDecision,
        user_input: str,
    ) -> None:
        context.start_message_thread()
        context.append_message(
            {
                "role": "user",
                "content": user_input
                or decision.instruction
                or decision.message
                or decision.action.value,
            }
        )
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

    def _require_live(self) -> None:
        if self._invalidated:
            raise RoutingError("this GitAgent Service is no longer active")

    @staticmethod
    def _repository(repository: str | None) -> str:
        if not repository:
            raise RoutingError("repository is ambiguous; provide owner/name explicitly")
        return str(RepositoryRef.parse(repository))

    @staticmethod
    def _default_entity_type(owner: str) -> str:
        return {
            "issues": "issue",
            "pull_requests": "pull_request",
        }.get(owner, "repository")
