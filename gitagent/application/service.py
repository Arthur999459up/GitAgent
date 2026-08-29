"""Session-scoped application service coordinating MainAgent and child AgentContext execution."""

from __future__ import annotations

import json
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
from gitagent.domain.errors import RoutingError
from gitagent.domain.learning import LearningTrace, TraceStep
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
    RepositoryOperation,
    RepositoryRef,
    ResolvedReference,
    RoutingContext,
    SessionScope,
    VerificationCheck,
    VerificationReport,
    WorkflowTurnDecision,
    to_plain,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.file_reads import FileReadLedger
from gitagent.harness.validation.static import StaticVerifier
from gitagent.infra.observability import TraceBus, TraceCategory, TraceStatus
from gitagent.infra.persistence import SessionManager
from gitagent.learning import LearningCoordinator
from gitagent.memory import MemoryAccessTracker, MemoryStore
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
    learning_trace: LearningTrace | None = None


class GitAgentService:
    """Run one MainAgent context per Session and one isolated child context at a time."""

    def __init__(
        self,
        capabilities: CapabilityLayer,
        *,
        main_reasoner: Reasoner,
        agent_reasoner: Reasoner | None = None,
        session_manager: SessionManager | None = None,
        memory_store: MemoryStore | None = None,
        memory_accesses: MemoryAccessTracker | None = None,
        trace: TraceBus | None = None,
        session_scope: SessionScope | None = None,
        input_budget_tokens: int = 26_112,
        auto_learning: bool = True,
    ) -> None:
        self.harness = AgentHarness(
            capabilities, trace=trace, context_budget=input_budget_tokens
        )
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
        self.main_agent = MainAgent(self.harness, main_reasoner)
        self.classifier = ApprovalIntentClassifier(main_reasoner)
        self.reasoner = agent_reasoner or main_reasoner
        self.session_manager = session_manager
        self.memory_store = memory_store
        self.memory_accesses = memory_accesses or MemoryAccessTracker()
        self.session_scope = session_scope
        self.auto_learning = auto_learning
        self.learning = (
            LearningCoordinator(
                self.main_agent,
                session_manager,
                self.memory_store,
                self.harness.trace,
                input_budget_tokens=input_budget_tokens,
                enabled=auto_learning,
            )
            if session_manager is not None and self.memory_store is not None
            else None
        )
        self.dispatch_started = False
        self.completed_learning_trace: LearningTrace | None = None
        self.completed_learning_turn_seq = 0
        self._active_turn_seq = 0
        self._invalidated = False

    def handle(
        self,
        user_input: str,
        *,
        repository: str,
        routing_context: RoutingContext | None = None,
        session_scope: SessionScope | None = None,
        turn_seq: int | None = None,
    ) -> ServiceResult:
        self._require_live()
        self._require_scope(session_scope)
        repository = self._repository(repository)
        if routing_context is None:
            raise RoutingError("GitAgentService requires Session routing context")
        if routing_context.scope != self._scope():
            raise RoutingError("routing context belongs to a different Session scope")
        if routing_context.repository_full_name != repository:
            raise RoutingError("routing context belongs to a different repository")
        self.dispatch_started = False
        self.completed_learning_trace = None
        self.completed_learning_turn_seq = 0
        self._active_turn_seq = int(turn_seq or 0)

        current = self._load_context(routing_context)
        if current is not None:
            if (
                current.reply_draft is not None
                and current.pending is None
                and not current.question
            ):
                self.dispatch_started = True
                output = self._continue_draft(current, user_input)
                return self._result_for_context(current, output)
            if current.pending is not None:
                self.dispatch_started = True
                decision = self.classifier.classify(
                    user_input=user_input,
                    proposal_context=self._context_description(current),
                )
                output = self._continue_approval(current, decision, user_input)
                return self._result_for_context(current, output)
            if current.question and (
                current.agent != "pull_requests"
                or self.pull_request_agent.accept_question_reply(current, user_input)
            ):
                self.dispatch_started = True
                self.loop.resume(
                    current,
                    self._agent_for(current.agent),
                    WorkflowTurnDecision(
                        ApprovalIntent.APPROVE, instruction=user_input
                    ),
                )
                output = self._after_loop(current)
                return self._result_for_context(current, output)
            self._clear_context()

        decision = self.main_agent.decide(
            user_input,
            repository=repository,
            context=routing_context,
        )
        if not decision.target_agent:
            return ServiceResult(decision, decision.message or None)

        self.dispatch_started = True
        output = self._start_child(decision, repository, routing_context)
        return ServiceResult(
            decision,
            output,
            agent=decision.target_agent,
            goal=decision.request or user_input,
            entity_type=decision.entity_type,
            entity_id=decision.entity_id,
            learning_trace=self.completed_learning_trace,
        )

    def reflect_after_turn(
        self,
        *,
        turn_seq: int,
        user_input: str,
        assistant_text: str,
        learning_trace: LearningTrace | None,
    ) -> Any:
        """Run optional learning only after the successful Turn is durable."""

        if self.learning is None:
            self.memory_accesses.clear()
            self.completed_learning_trace = None
            self.completed_learning_turn_seq = 0
            return None
        accessed_paths = self.memory_accesses.snapshot()
        try:
            session = self._require_session_manager().get_session(
                self._scope().account_key,
                self._scope().repository_key,
                self._scope().session_id,
            )
            if session is None:
                return None
            del user_input, assistant_text
            if learning_trace is not None:
                return self.learning.reflect_domain(
                    self._scope(),
                    session.repository_full_name,
                    learning_trace,
                    turn_seq=turn_seq,
                    accessed_paths=accessed_paths,
                )
            return self.learning.reflect_conversation(
                self._scope(),
                session.repository_full_name,
                turn_seq=turn_seq,
                accessed_paths=accessed_paths,
            )
        except Exception as exc:  # noqa: BLE001 - the successful Turn is already durable
            self.harness.trace.emit(
                session_id=self._scope().session_id,
                category=TraceCategory.WORKFLOW,
                name="long_term_learning",
                status=TraceStatus.FAILED,
                message=str(exc),
                details={"error_type": type(exc).__name__},
            )
            return None
        finally:
            self.memory_accesses.clear()
            self.completed_learning_trace = None
            self.completed_learning_turn_seq = 0

    def approve(self) -> Any:
        context = self._require_pending_context()
        return self._continue_approval(
            context,
            WorkflowTurnDecision(ApprovalIntent.APPROVE),
            "",
        )

    def compact_memory(
        self, repository_full_name: str
    ) -> dict[str, tuple[str, ...]] | None:
        """Run explicit maintenance without counting management reads as access."""

        if self.learning is None:
            return None
        pending_accesses = self.memory_accesses.snapshot()
        self.memory_accesses.clear()
        try:
            return self.learning.compact(self._scope(), repository_full_name)
        finally:
            self.memory_accesses.clear()
            for root, path in pending_accesses:
                self.memory_accesses.record(root, path)

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
        self.discard_turn_learning()
        self._invalidated = True

    def discard_turn_learning(self) -> None:
        """Drop ephemeral reads and trace evidence for a failed business turn."""

        self.memory_accesses.clear()
        self.completed_learning_trace = None
        self.completed_learning_turn_seq = 0

    def _start_child(
        self,
        decision: MainDecision,
        repository: str,
        routing_context: RoutingContext,
    ) -> Any:
        scope = self._scope()
        goal = decision.request.strip()
        guidance = self._guidance(
            decision.entity_type, decision.entity_id, routing_context
        )
        if decision.target_agent == "repository":
            operation = self.repository_agent.operation_for(goal)
            context = self.harness.context(
                "repository",
                scope.session_id,
                repository=repository,
                goal=goal,
                entity_type="repository",
                guidance=guidance,
            )
            self.repository_agent.prepare(context, operation)
            self._start_learning_trace(context)
            self.loop.start(context, self.repository_agent)
            return self._after_loop(context)
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
            self._start_learning_trace(context)
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
                    )
                return self._after_loop(context)
            self.loop.start(context, self.issue_agent)
            return self._after_loop(context)
        if decision.target_agent == "pull_requests":
            self._start_learning_trace(context)
            self.loop.start(context, self.pull_request_agent)
            return self._after_loop(context)
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
        self._capture_learning_trace(context)
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
        revised = self._revise_text("GitHub Issue reply draft", draft, instruction)
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
            self._observe_service_decision(context, decision, user_input)
            self.harness.approvals.decide(context.pending.approval_id, "Reject")
            request = context.change_request
            if request is None:
                raise RoutingError(
                    "repository modification revision has no change request"
                )
            instruction = decision.instruction.strip() or user_input.strip()
            revised = self.harness.context(
                "repository",
                context.session_id,
                repository=context.repository,
                goal=f"{request.description}\n\nUser revision: {instruction}",
                entity_type="repository",
                guidance=context.guidance,
            )
            revised.operation = RepositoryOperation.MODIFY.value
            revised.change_request = ChangeRequest(
                repository=request.repository,
                description=f"{request.description}\n\nUser revision: {instruction}",
            )
            revised.origin_turn_seq = context.origin_turn_seq
            revised.observations = list(context.observations)
            self.loop.start(revised, self.repository_agent)
            return self._after_loop(revised)

        if (
            context.agent == "pull_requests"
            and decision.action == ApprovalIntent.REVISE
        ):
            self._observe_service_decision(context, decision, user_input)
            return self._revise_pr_review(
                context, decision.instruction.strip() or user_input.strip()
            )

        self.loop.resume(context, self._agent_for(context.agent), decision)
        return self._after_loop(context)

    def _revise_draft(self, context: AgentContext, instruction: str) -> DraftResult:
        current = str(context.reply_draft or "")
        revised = self._revise_text("GitHub Issue reply draft", current, instruction)
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

    def _revise_text(self, artifact: str, current: str, instruction: str) -> str:
        revised = self.reasoner.complete_text(
            system=(
                f"You revise a {artifact}. Follow the user's editing instruction exactly. "
                "Return only the revised text. Do not claim it was posted and do not add meta commentary."
            ),
            prompt=json.dumps(
                {"current_draft": current, "instruction": instruction},
                ensure_ascii=False,
            ),
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
            learning_trace=self.completed_learning_trace,
        )

    def _save_context(self, context: AgentContext) -> None:
        self._require_session_manager().save_agent_context(
            self._scope(), self._serialize_context(context)
        )

    def _clear_context(self) -> None:
        self._require_session_manager().save_agent_context(self._scope(), None)

    def _load_context(
        self, routing_context: RoutingContext | None = None
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
                session.agent_context, repository=session.repository_full_name
            )
            if session.agent_context
            else None
        )
        if context is not None and routing_context is not None:
            context.guidance = self._guidance(
                context.entity_type, context.entity_id, routing_context
            )
        elif context is not None:
            context.guidance = self._stored_guidance(context)
        return context

    def _stored_guidance(self, context: AgentContext) -> AgentGuidance | None:
        scope = self._scope()
        guidance = AgentGuidance(
            memory_index=self._require_memory_store().read_index(
                scope.account_key,
                scope.repository_key,
            ),
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
        context.origin_turn_seq = int(raw.get("origin_turn_seq") or 0)
        context.steps = int(raw.get("steps") or 0)
        context.observations = list(raw.get("observations") or [])
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
            if agent == "repository" and (
                len(calls) != 1
                or calls[0].capability_id != "github.commit_to_default_branch"
            ):
                raise RoutingError(
                    "stored RepositoryAgent proposal contains an invalid mutation plan"
                )
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
        return context

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

    @staticmethod
    def _guidance(
        entity_type: str | None, entity_id: str | None, context: RoutingContext
    ) -> AgentGuidance | None:
        guidance = AgentGuidance(
            memory_index=context.memory_index,
            resolved_references=GitAgentService._resolved_references(
                entity_type, entity_id
            ),
        )
        return None if guidance.empty else guidance

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

    def _require_memory_store(self) -> MemoryStore:
        if self.memory_store is None:
            raise RoutingError("GitAgentService requires a MemoryStore")
        return self.memory_store

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

    def _start_learning_trace(self, context: AgentContext) -> None:
        context.origin_turn_seq = self._active_turn_seq

    def _capture_learning_trace(self, context: AgentContext) -> None:
        if context.origin_turn_seq < 1:
            return
        steps = tuple(
            step
            for step in (_trace_step(item) for item in context.observations)
            if step is not None
        )
        outcome = (
            context.final_message
            or _trace_result(to_plain(context.result))
            or "任务成功完成"
        )
        self.completed_learning_trace = LearningTrace(
            goal=context.goal[:1000],
            outcome=outcome[:1200],
            trajectory=steps,
        )
        self.completed_learning_turn_seq = max(
            context.origin_turn_seq, self._active_turn_seq
        )

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


def _trace_step(observation: Any) -> TraceStep | None:
    """Project one observation to a short causal step without raw tool output."""

    if not isinstance(observation, dict):
        return None
    kind = str(observation.get("kind") or "")
    payload = observation.get("payload")
    if not isinstance(payload, dict):
        return None
    if kind == "capability":
        capability_id = str(payload.get("capability_id") or "capability")
        arguments = payload.get("arguments")
        identity = _trace_arguments(arguments) if isinstance(arguments, dict) else ""
        action = f"{capability_id}{identity}"
        return TraceStep(action[:400], _trace_result(payload.get("data"))[:800])
    if kind == "capability_error":
        capability_id = str(payload.get("capability_id") or "capability")
        message = str(payload.get("message") or payload.get("error") or "调用失败")
        return TraceStep(f"{capability_id} 失败"[:400], message[:800])
    if kind in {"user_decision", "rejection"}:
        action = str(payload.get("action") or kind)
        result = str(payload.get("instruction") or payload.get("message") or "")
        return TraceStep(action[:400], result[:800])
    return None


def _trace_arguments(arguments: dict[str, Any]) -> str:
    keys = ("path", "root", "issue_number", "pull_number", "run_id", "ref")
    selected = [
        f"{key}={arguments[key]}"
        for key in keys
        if arguments.get(key) not in (None, "")
    ]
    return f" ({', '.join(selected)})" if selected else ""


def _trace_result(value: Any) -> str:
    if value is None:
        return "完成"
    if isinstance(value, str):
        return " ".join(value.split())[:800]
    if isinstance(value, bool):
        return "成功" if value else "未成功"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return f"返回 {len(value)} 项"
    if isinstance(value, dict):
        for key in (
            "summary",
            "conclusion",
            "status",
            "message",
            "state",
            "result",
            "answer",
            "path",
        ):
            scalar = value.get(key)
            if isinstance(scalar, (str, int, float, bool)) and str(scalar).strip():
                return f"{key}: {' '.join(str(scalar).split())}"[:800]
        for key in ("files", "items", "matches", "checks", "pull_requests", "issues"):
            items = value.get(key)
            if isinstance(items, list):
                return f"{key}: {len(items)} 项"
        return "成功返回结构化结果"
    return type(value).__name__
