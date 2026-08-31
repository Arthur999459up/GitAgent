"""Session service for Main Agent calls and isolated child Agent lifecycles."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gitagent.agent_loop import AgentCall, AgentLoop, CapabilityCall
from gitagent.agents import (
    CodingAgent,
    IssueAgent,
    MainAgent,
    PullRequestAgent,
    RepositoryAgent,
)
from gitagent.capability import AccessLevel, CapabilityLayer
from gitagent.domain.errors import RoutingError, WorkflowError
from gitagent.domain.models import (
    AgentGuidance,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    DraftResult,
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
    output: Any = None
    agent: str | None = None
    goal: str = ""
    entity_type: str | None = None
    entity_id: str | None = None
    domain_output: Any = None
    workflow_summary: str = ""
    clarify: bool = False
    output_agent: str = "main"


class GitAgentService:
    """Run one Main context and at most one persisted child lifecycle per Session."""

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
        domain_reasoner = agent_reasoner or main_reasoner
        self.coding = CodingAgent(self.harness, domain_reasoner)
        self.verifier = StaticVerifier(self.harness)
        self.repository_agent = RepositoryAgent(
            self.harness, self.coding, self.verifier, domain_reasoner
        )
        self.pull_request_agent = PullRequestAgent(
            self.harness, self.coding, self.verifier, domain_reasoner
        )
        self.issue_agent = IssueAgent(
            self.harness, self.coding, self.verifier, domain_reasoner
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

        current = self._load_context()
        if current is None and (
            not main_messages or main_messages[-1].get("role") != "user"
        ):
            raise RoutingError("GitAgentService requires the current Main message thread")
        if current is not None:
            self._ensure_domain_thread_durable(current)
            self.dispatch_started = True
            paused_output = (
                self._resume_child(current, user_input)
                if not current.finished and not current.error
                else self._after_loop(current)
            )
            if self._child_waiting(current):
                self._save_context(current)
                return self._result_for_context(current, paused_output)
            domain_output = self._after_loop(current)
            self._complete_parent_agent_call(main_messages, current)
            self._clear_context()
            return self._run_main(
                main_messages,
                repository,
                main_tools,
                child_context=current,
                domain_output=domain_output,
            )

        return self._run_main(main_messages, repository, main_tools)

    def _run_main(
        self,
        main_messages: list[dict[str, Any]],
        repository: str,
        main_tools: list[dict[str, Any]] | None,
        *,
        child_context: AgentContext | None = None,
        domain_output: Any = None,
    ) -> ServiceResult:
        tools = main_tools or self.main_agent.provider_tools(
            session_id=self._scope().session_id,
            repository=repository,
            goal=str(main_messages[-1].get("content") or ""),
        )
        last_child = child_context
        last_domain_output = domain_output
        for _ in range(20):
            response = self.main_agent.step(
                main_messages,
                repository=repository,
                scope=self._scope(),
                tools=tools,
            )
            if response.call is None:
                return ServiceResult(
                    output=response.text,
                    agent=last_child.agent if last_child is not None else None,
                    goal=last_child.goal if last_child is not None else "",
                    entity_type=last_child.entity_type if last_child is not None else None,
                    entity_id=last_child.entity_id if last_child is not None else None,
                    domain_output=last_domain_output,
                    workflow_summary=(
                        last_child.final_message if last_child is not None else ""
                    ),
                )

            main_context = self.harness.context(
                "main",
                self._scope().session_id,
                repository=repository,
                goal=str(main_messages[-1].get("content") or ""),
            )
            resolved = self.harness.resolve_model_call(
                response.call,
                main_context,
                agent_schemas=self.main_agent.agent_schemas(),
            )
            if isinstance(resolved, CapabilityCall):
                self._execute_main_capability(main_messages, main_context, resolved)
                continue
            if not isinstance(resolved, AgentCall):
                raise RoutingError("Main Agent returned an unsupported structured call")

            self.dispatch_started = True
            context, reply_mode = self._start_child(resolved, repository)
            if (
                reply_mode
                and context.finished
                and not context.error
                and context.pending is None
            ):
                draft = self.issue_agent.draft_reply(context, self.reasoner)
                context.reply_draft = draft
                context.finished = False
                context.result = None
                context.final_message = ""
                self._save_context(context)
                return self._result_for_context(
                    context,
                    DraftResult(
                        "issue",
                        context.entity_id,
                        "Issue 回复草稿",
                        draft,
                        "草稿尚未发布。你可以直接提出修改，或确认发布。",
                    ),
                )
            if self._child_waiting(context):
                self._save_context(context)
                return self._result_for_context(context, context)

            last_domain_output = self._after_loop(context)
            self._complete_parent_agent_call(main_messages, context)
            last_child = context
        raise RoutingError("Main Agent exceeded the 20-step call limit")

    def _execute_main_capability(
        self,
        main_messages: list[dict[str, Any]],
        context: AgentContext,
        call: CapabilityCall,
    ) -> None:
        capability = next(
            (
                item
                for item in self.harness.discover(context)
                if item.id == call.capability_id
            ),
            None,
        )
        if capability is None or capability.access != AccessLevel.READ:
            raise RoutingError("Main Agent may only call visible READ capabilities")
        result = self.harness.invoke(context, call.capability_id, call.arguments)
        payload = (
            result.content
            if result.status == "success"
            else {
                "status": result.status,
                "error": result.error.type.value if result.error is not None else "unknown",
                "message": result.error.message if result.error is not None else "",
            }
        )
        self._append_main_message(
            main_messages, tool_result_message(call.call_id, payload)
        )

    def _start_child(
        self, call: AgentCall, repository: str
    ) -> tuple[AgentContext, bool]:
        arguments = call.arguments
        goal = str(arguments["task"])
        entity_type: str | None = None
        entity_id: str | None = None
        reply_mode = False
        if call.agent_id == "issues":
            entity_type = "issue"
            if arguments.get("issue_number") is not None:
                entity_id = str(arguments["issue_number"])
            reply_mode = arguments.get("mode") == "reply"
        elif call.agent_id == "pull_requests":
            if arguments.get("pr_number") is not None:
                entity_type = "pull_request"
                entity_id = str(arguments["pr_number"])
            elif arguments.get("workflow_run_id") is not None:
                entity_type = "workflow_run"
                entity_id = str(arguments["workflow_run_id"])
            else:
                entity_type = "pull_request"
        elif call.agent_id == "repository":
            entity_type = "repository"
        else:
            raise RoutingError(f"unsupported Domain Agent: {call.agent_id}")

        context = self.harness.context(
            call.agent_id,
            self._scope().session_id,
            repository=repository,
            goal=goal,
            entity_type=entity_type,
            entity_id=entity_id,
            guidance=self._guidance(goal, entity_type, entity_id),
        )
        context.parent_call_id = call.call_id
        context.parent_call_name = f"agent__{call.agent_id}"
        self._bind_context_turn(context)
        self.loop.start(context, self._agent_for(call.agent_id))
        return context, reply_mode

    def _resume_child(self, context: AgentContext, user_input: str) -> Any:
        if context.reply_draft is not None and context.pending is None:
            return self._continue_draft(context, user_input)
        if context.pending is not None:
            decision = self.classifier.classify(
                user_input=user_input,
                proposal_context=self._context_description(context),
            )
            return self._continue_approval(context, decision, user_input)
        if context.question:
            self.loop.resume(
                context,
                self._agent_for(context.agent),
                WorkflowTurnDecision(ApprovalIntent.APPROVE, instruction=user_input),
            )
            return self._after_loop(context)
        raise RoutingError("stored child Agent is not resumable")

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
            return decision.message or "你是想发布当前草稿、继续修改，还是查看草稿内容？"
        if decision.action == ApprovalIntent.QUESTION:
            return DraftResult(
                "issue", context.entity_id, "Issue 回复草稿", draft, "草稿尚未发布。"
            )
        if decision.action == ApprovalIntent.REJECT:
            return DraftResult(
                "issue",
                context.entity_id,
                "Issue 回复草稿",
                draft,
                "本次没有发布；草稿仍保留。",
            )
        if decision.action == ApprovalIntent.REVISE:
            instruction = decision.instruction.strip() or user_input.strip()
            context.reply_draft = self._revise_text(
                context, "GitHub Issue reply draft", draft, instruction
            )
            return DraftResult(
                "issue",
                context.entity_id,
                "Issue 回复草稿 · 已修改",
                context.reply_draft,
                "仍未发布。继续提修改即可；确认后再发布。",
            )

        context.append_message(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": "Publish the approved Issue reply draft.",
                        "issue_number": int(context.entity_id or 0),
                        "draft": context.reply_draft,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        context.finished = False
        context.error = None
        context.result = None
        context.final_message = ""
        self.loop.start(context, self.issue_agent)
        return self._after_loop(context)

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
            return decision.message
        if decision.action == ApprovalIntent.QUESTION:
            self._observe_service_decision(context, decision, user_input)
            return self._proposal_description(context)

        if (
            context.agent == "issues"
            and context.reply_draft is not None
            and decision.action in {ApprovalIntent.REJECT, ApprovalIntent.REVISE}
        ):
            self._observe_service_decision(context, decision, user_input)
            pending = context.pending
            self.harness.approvals.decide(pending.approval_id, "Reject")
            context.pending = None
            if pending.provider_call_id:
                context.append_tool_result(
                    {
                        "status": "rejected",
                        "instruction": decision.instruction or user_input,
                    },
                    call_id=pending.provider_call_id,
                )
            if decision.action == ApprovalIntent.REVISE:
                context.reply_draft = self._revise_text(
                    context,
                    "GitHub Issue reply draft",
                    str(context.reply_draft),
                    decision.instruction.strip() or user_input.strip(),
                )
            return DraftResult(
                "issue",
                context.entity_id,
                "Issue 回复草稿",
                str(context.reply_draft),
                "发布提案已拒绝；草稿仍保留。",
            )

        if (
            context.agent == "pull_requests"
            and decision.action == ApprovalIntent.REVISE
            and len(context.pending.calls) == 1
            and context.pending.calls[0].capability_id == "github.post_review"
        ):
            self._observe_service_decision(context, decision, user_input)
            return self._revise_pr_review(
                context, decision.instruction.strip() or user_input.strip()
            )

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

        self.loop.resume(context, self._agent_for(context.agent), decision)
        return self._after_loop(context)

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
        messages, _, _ = fit_messages(
            messages, None, context_window_tokens=context.context_window_tokens
        )
        revised = self.reasoner.complete_text_messages(
            messages=messages,
            context_window_tokens=context.context_window_tokens,
        ).strip()
        if not revised:
            raise RoutingError(f"{artifact} revision returned empty text")
        return revised

    def _complete_parent_agent_call(
        self, main_messages: list[dict[str, Any]], context: AgentContext
    ) -> None:
        if not context.parent_call_id:
            raise RoutingError("child Agent is missing its parent call correlation")
        status = "failed" if context.error else "completed"
        content = context.error or context.final_message
        if not content:
            raise RoutingError("completed child Agent returned empty final Text")
        self._append_main_message(
            main_messages,
            tool_result_message(
                context.parent_call_id,
                {
                    "status": status,
                    "agent": context.agent,
                    "content": content,
                },
            ),
        )

    def _after_loop(self, context: AgentContext) -> Any:
        if self._child_waiting(context):
            return context
        if context.error:
            return context
        return context.result if context.result is not None else context.final_message

    @staticmethod
    def _child_waiting(context: AgentContext) -> bool:
        return (
            context.pending is not None
            or bool(context.question)
            or (context.reply_draft is not None and not context.finished)
        )

    def _result_for_context(self, context: AgentContext, output: Any) -> ServiceResult:
        return ServiceResult(
            output=output,
            agent=context.agent,
            goal=context.goal,
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            domain_output=output,
            workflow_summary=context.final_message,
            clarify=bool(context.question),
            output_agent=context.agent,
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

    def approve(self) -> Any:
        context = self._require_pending_context()
        output = self._continue_approval(
            context, WorkflowTurnDecision(ApprovalIntent.APPROVE), ""
        )
        self._save_context(context)
        return output

    def reject(self) -> Any:
        context = self._require_pending_context()
        output = self._continue_approval(
            context, WorkflowTurnDecision(ApprovalIntent.REJECT), ""
        )
        self._save_context(context)
        return output

    def revise_proposal(self, instruction: str) -> Any:
        context = self._require_pending_context()
        output = self._continue_approval(
            context,
            WorkflowTurnDecision(ApprovalIntent.REVISE, instruction=instruction.strip()),
            instruction,
        )
        self._save_context(context)
        return output

    def dream_memory(self) -> dict[str, tuple[str, ...]] | None:
        return self.memory_hooks.dream_now(self._scope()) if self.memory_hooks else None

    def invalidate(self) -> None:
        if not self._invalidated:
            self.harness.approvals.invalidate_all()
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
            "parent_call_id": context.parent_call_id,
            "parent_call_name": context.parent_call_name,
            "repository": context.repository,
            "goal": context.goal,
            "entity_type": context.entity_type,
            "entity_id": context.entity_id,
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

    def _restore_context(self, raw: dict[str, Any], *, repository: str) -> AgentContext:
        agent = str(raw.get("agent") or "")
        if agent not in {"issues", "pull_requests", "repository"}:
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
            max_steps=int(raw.get("max_steps") or 20),
        )
        context.run_id = str(raw.get("run_id") or context.run_id)
        context.origin_turn_seq = int(raw.get("origin_turn_seq") or 0)
        context.parent_call_id = str(raw.get("parent_call_id") or "")
        context.parent_call_name = str(raw.get("parent_call_name") or "")
        if not context.parent_call_id or context.parent_call_name != f"agent__{agent}":
            raise RoutingError("stored child Agent correlation is invalid")
        context.steps = int(raw.get("steps") or 0)
        context.observations = list(raw.get("observations") or [])
        persisted = derive_domain_messages(
            self._require_session_manager().event_log.iter_events(self._scope()),
            agent=agent,
            run_id=context.run_id,
        )
        context.messages = persisted or [
            canonical_message(message)
            for message in raw.get("messages", [])
            if isinstance(message, dict)
        ]
        context.question = str(raw.get("question") or "")
        context.final_message = str(raw.get("final_message") or "")
        context.code_candidate = self._restore_candidate(raw.get("code_candidate"))
        context.change_request = self._restore_change_request(raw.get("change_request"))
        context.verification = self._restore_verification(raw.get("verification"))
        context.reply_draft = (
            str(raw.get("reply_draft")) if raw.get("reply_draft") is not None else None
        )
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
            static_checks=[str(item) for item in raw.get("static_checks", [])],
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
        if context.reply_draft is not None:
            return context.reply_draft
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

    def _agent_for(self, name: str) -> Any:
        try:
            return {
                "issues": self.issue_agent,
                "pull_requests": self.pull_request_agent,
                "repository": self.repository_agent,
            }[name]
        except KeyError as exc:
            raise RoutingError(f"unknown Domain Agent: {name}") from exc

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
            self._scope(), message, turn_seq=self._active_turn_seq, agent="main"
        )

    def _persist_main_compaction(self, plan: MessageCompactionPlan) -> None:
        self._require_session_manager().record_message_compaction(
            self._scope(),
            turn_seq=self._active_turn_seq,
            agent="main",
            checkpoint=plan.checkpoint,
            retain_message_indexes=plan.retain_message_indexes,
            tool_replacements=plan.tool_replacements,
        )

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

    def _ensure_domain_thread_durable(self, context: AgentContext) -> None:
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
            context.guidance = self._guidance(
                context.goal, context.entity_type, context.entity_id
            )
        return context

    def _require_pending_context(self) -> AgentContext:
        self._require_live()
        context = self._load_context()
        if context is None or context.pending is None:
            raise RoutingError("当前 Session 没有待审批提案")
        return context

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
