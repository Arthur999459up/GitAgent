"""Session-scoped application service coordinating MainAgent and child AgentContext execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..agents import (
    CIDiagnosisAgent,
    CodeChangeController,
    CodingAgent,
    IssueAgent,
    MainAgent,
    PRReviewAgent,
    PullRequestAgent,
    RepoQAAgent,
)
from ..core.errors import RoutingError
from ..core.models import (
    AgentGuidance,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    ContextMemory,
    DraftResult,
    MainDecision,
    PlannedToolCall,
    Replacement,
    RepositoryRef,
    ResolvedReference,
    RoutingContext,
    SessionScope,
    VerificationCheck,
    VerificationReport,
    WorkflowTurnDecision,
    to_plain,
)
from ..core.trace import TraceBus
from ..mcp import MCPServer
from ..reasoning import Reasoner
from ..runtime import AgentContext, AgentHarness, AgentLoop, register_github_mutator
from ..state import SessionManager
from ..verification import StaticVerifier
from .approval import ApprovalIntentClassifier


@dataclass
class ServiceResult:
    decision: MainDecision
    output: Any = None
    agent: str | None = None
    goal: str = ""
    entity_type: str | None = None
    entity_id: str | None = None


class GitAgentService:
    """Run one MainAgent context per Session and one isolated child context at a time."""

    def __init__(
        self,
        server: MCPServer,
        *,
        main_reasoner: Reasoner,
        agent_reasoner: Reasoner | None = None,
        session_manager: SessionManager | None = None,
        trace: TraceBus | None = None,
        session_scope: SessionScope | None = None,
    ) -> None:
        self.harness = AgentHarness(server, trace=trace)
        register_github_mutator(self.harness)
        self.repo_qa = RepoQAAgent(self.harness, agent_reasoner)
        self.pr_review_agent = PRReviewAgent(self.harness, agent_reasoner)
        self.pull_request_agent = PullRequestAgent(self.harness, self.pr_review_agent, agent_reasoner)
        self.ci_diagnosis = CIDiagnosisAgent(self.harness, agent_reasoner)
        self.coding = CodingAgent(self.harness, agent_reasoner)
        self.verifier = StaticVerifier(self.harness)
        self.issue_agent = IssueAgent(self.harness, self.coding, self.verifier, agent_reasoner)
        self.code_change_controller = CodeChangeController(self.coding, self.verifier)
        self.loop = AgentLoop(self.harness)
        self.main_agent = MainAgent(self.harness, main_reasoner)
        self.classifier = ApprovalIntentClassifier(main_reasoner)
        self.reasoner = agent_reasoner or main_reasoner
        self.session_manager = session_manager
        self.session_scope = session_scope
        self.dispatch_started = False
        self._invalidated = False

    def handle(
        self,
        user_input: str,
        *,
        repository: str,
        routing_context: RoutingContext | None = None,
        session_scope: SessionScope | None = None,
    ) -> ServiceResult:
        self._require_live()
        self._require_scope(session_scope)
        repository = self._repository(repository)
        if routing_context is None:
            raise RoutingError("GitAgentService requires Session routing context")
        self.dispatch_started = False

        current = self._load_context()
        if current is not None:
            if current.reply_draft is not None and current.pending is None and not current.question:
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
            if current.question:
                self.dispatch_started = True
                self.loop.resume(
                    current,
                    self._agent_for(current.agent),
                    WorkflowTurnDecision(ApprovalIntent.APPROVE, instruction=user_input),
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
        )

    def approve(self) -> Any:
        context = self._require_pending_context()
        return self._continue_approval(
            context,
            WorkflowTurnDecision(ApprovalIntent.APPROVE),
            "",
        )

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
            WorkflowTurnDecision(ApprovalIntent.REVISE, instruction=instruction.strip()),
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
        routing_context: RoutingContext,
    ) -> Any:
        scope = self._scope()
        goal = decision.request.strip()
        guidance = self._guidance(decision.entity_type, decision.entity_id, routing_context)
        if decision.target_agent == "repo_qa":
            return self.repo_qa.answer(
                repository,
                goal,
                session_id=scope.session_id,
                guidance=guidance,
            )
        if decision.target_agent == "ci_diagnosis":
            diagnosis = self.ci_diagnosis.diagnose(
                repository,
                pr_number=int(decision.entity_id)
                if decision.entity_type == "pull_request" and decision.entity_id and decision.entity_id.isdigit()
                else None,
                workflow_run_id=int(decision.entity_id)
                if decision.entity_type == "workflow_run" and decision.entity_id and decision.entity_id.isdigit()
                else None,
                session_id=scope.session_id,
                guidance=guidance,
            )
            if not decision.requested_fix:
                return diagnosis
            context = self.harness.context(
                "code_change",
                scope.session_id,
                repository=repository,
                goal=diagnosis.suggested_fix,
                entity_type=decision.entity_type,
                entity_id=decision.entity_id,
                guidance=guidance,
            )
            context.change_request = ChangeRequest(
                repository=repository,
                description=diagnosis.suggested_fix,
                target_files=list(diagnosis.suspected_files),
            )
            self.loop.start(context, self.code_change_controller)
            return {"diagnosis": diagnosis, "code_change": self._after_loop(context)}

        context = self.harness.context(
            decision.target_agent,
            scope.session_id,
            repository=repository,
            goal=goal,
            entity_type=decision.entity_type or self._default_entity_type(decision.target_agent),
            entity_id=decision.entity_id,
            guidance=guidance,
        )
        if decision.target_agent == "code_change":
            context.change_request = ChangeRequest(repository=repository, description=goal)
            self.loop.start(context, self.code_change_controller)
            return self._after_loop(context)
        if decision.target_agent == "issues":
            if decision.requested_reply and context.entity_id:
                context.read_only = True
                context.result_required = False
                self.loop.start(context, self.issue_agent)
                if context.finished and not context.error:
                    draft = self.issue_agent.draft_reply(context, self.reasoner)
                    context.reply_draft = draft
                    context.read_only = False
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
            self.loop.start(context, self.pull_request_agent)
            return self._after_loop(context)
        raise RoutingError(f"unsupported domain agent: {decision.target_agent}")

    def _after_loop(self, context: AgentContext) -> Any:
        if context.pending is not None or context.question or (context.reply_draft is not None and not context.finished):
            self._save_context(context)
            return context
        self._clear_context()
        if context.result is not None:
            return context.result
        if context.error:
            return context
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
        if decision.action == ApprovalIntent.AMBIGUOUS:
            self._save_context(context)
            return decision.message or "你是想发布当前草稿、继续修改，还是查看草稿内容？"
        if decision.action == ApprovalIntent.QUESTION:
            self._save_context(context)
            return DraftResult("issue", context.entity_id, "Issue 回复草稿", draft, "这是当前草稿，尚未发布。")
        if decision.action == ApprovalIntent.REJECT:
            self._save_context(context)
            return DraftResult("issue", context.entity_id, "Issue 回复草稿", draft, "本次没有发布；草稿仍保留。")
        if decision.action == ApprovalIntent.APPROVE:
            context.finished = False
            context.error = None
            self.loop.start(context, self.issue_agent)
            return self._after_loop(context)
        instruction = decision.instruction.strip() or user_input.strip()
        revised = self.reasoner.complete_text(
            system=(
                "You revise a GitHub Issue reply draft. Follow the user's editing instruction exactly. "
                "Return only the revised draft text. Do not claim it was posted and do not add meta commentary."
            ),
            prompt=json.dumps({"current_draft": draft, "instruction": instruction}, ensure_ascii=False),
        ).strip()
        if not revised:
            raise RoutingError("draft revision returned empty text")
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
            self._save_context(context)
            return decision.message
        if decision.action == ApprovalIntent.QUESTION:
            self._save_context(context)
            return self._proposal_description(context)

        if context.agent == "issues" and context.reply_draft is not None and decision.action in {
            ApprovalIntent.REJECT,
            ApprovalIntent.REVISE,
        }:
            self.harness.approvals.decide(context.pending.approval_id, "Reject")
            context.pending = None
            if decision.action == ApprovalIntent.REVISE:
                return self._revise_draft(context, decision.instruction.strip() or user_input.strip())
            self._save_context(context)
            return DraftResult(
                "issue",
                context.entity_id,
                "Issue 回复草稿",
                context.reply_draft,
                "已拒绝本次发布提案；草稿保留，可继续修改。",
            )

        if context.agent == "code_change" and decision.action == ApprovalIntent.REVISE:
            self.harness.approvals.decide(context.pending.approval_id, "Reject")
            request = context.change_request
            if request is None:
                raise RoutingError("code-change revision has no change request")
            instruction = decision.instruction.strip() or user_input.strip()
            revised = self.harness.context(
                "code_change",
                context.session_id,
                repository=context.repository,
                goal=f"{request.description}\n\nUser revision: {instruction}",
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                guidance=context.guidance,
            )
            revised.change_request = ChangeRequest(
                repository=request.repository,
                description=f"{request.description}\n\nUser revision: {instruction}",
                base_branch=request.base_branch,
                target_files=list(request.target_files),
                replacements=list(request.replacements),
                proposed_files=dict(request.proposed_files),
                issue_number=request.issue_number,
                suggested_title=request.suggested_title,
            )
            self.loop.start(revised, self.code_change_controller)
            return self._after_loop(revised)

        self.loop.resume(context, self._agent_for(context.agent), decision)
        return self._after_loop(context)

    def _revise_draft(self, context: AgentContext, instruction: str) -> DraftResult:
        current = str(context.reply_draft or "")
        revised = self.reasoner.complete_text(
            system=(
                "You revise a GitHub Issue reply draft. Follow the user's editing instruction exactly. "
                "Return only the revised draft text. Do not claim it was posted and do not add meta commentary."
            ),
            prompt=json.dumps({"current_draft": current, "instruction": instruction}, ensure_ascii=False),
        ).strip()
        if not revised:
            raise RoutingError("draft revision returned empty text")
        context.reply_draft = revised
        self._save_context(context)
        return DraftResult("issue", context.entity_id, "Issue 回复草稿 · 已修改", revised, "仍未发布。")

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
        )

    def _save_context(self, context: AgentContext) -> None:
        self._require_session_manager().save_agent_context(self._scope(), self._serialize_context(context))

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
        return self._restore_context(session.agent_context) if session.agent_context else None

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
                "summary": context.pending.summary,
                "calls": [{"tool": call.tool, "arguments": to_plain(call.arguments)} for call in context.pending.calls],
                "specialist": context.pending.specialist,
            }
        return {
            "agent": context.agent,
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
            "guidance": to_plain(context.guidance),
            "reply_draft": context.reply_draft,
            "read_only": context.read_only,
            "result_required": context.result_required,
            "read_cache": to_plain(context.read_cache),
            "error": context.error,
            "finished": context.finished,
        }

    def _restore_context(self, raw: dict[str, Any]) -> AgentContext:
        agent = str(raw.get("agent") or "")
        if agent not in {"issues", "pull_requests", "code_change"}:
            raise RoutingError("stored Session agent context is invalid")
        context = self.harness.context(
            agent,
            self._scope().session_id,
            repository=str(raw.get("repository") or ""),
            goal=str(raw.get("goal") or ""),
            entity_type=str(raw.get("entity_type") or "") or None,
            entity_id=str(raw.get("entity_id") or "") or None,
            guidance=self._restore_guidance(raw.get("guidance")),
            max_steps=int(raw.get("max_steps") or 20),
        )
        context.steps = int(raw.get("steps") or 0)
        context.observations = list(raw.get("observations") or [])
        context.question = str(raw.get("question") or "")
        context.final_message = str(raw.get("final_message") or "")
        context.code_candidate = self._restore_candidate(raw.get("code_candidate"))
        context.change_request = self._restore_change_request(raw.get("change_request"))
        context.verification = self._restore_verification(raw.get("verification"))
        context.reply_draft = str(raw.get("reply_draft")) if raw.get("reply_draft") is not None else None
        context.read_only = bool(raw.get("read_only", False))
        context.result_required = bool(raw.get("result_required", True))
        context.read_cache = dict(raw.get("read_cache") or {})
        context.error = str(raw.get("error")) if raw.get("error") is not None else None
        context.finished = bool(raw.get("finished", False))
        pending = raw.get("pending")
        if isinstance(pending, dict):
            calls = [
                PlannedToolCall(str(item["tool"]), dict(item.get("arguments") or {}))
                for item in pending.get("calls", [])
            ]
            self.loop.restore_pending(
                context,
                summary=str(pending.get("summary") or ""),
                calls=calls,
                specialist=str(pending.get("specialist")) if pending.get("specialist") is not None else None,
            )
        return context

    @staticmethod
    def _restore_candidate(raw: Any) -> CandidatePatch | None:
        if not isinstance(raw, dict):
            return None
        return CandidatePatch(
            summary=str(raw.get("summary") or ""),
            root_cause=str(raw.get("root_cause") or ""),
            changed_files=[str(item) for item in raw.get("changed_files", [])],
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
                Replacement(str(item.get("path") or ""), str(item.get("old") or ""), str(item.get("new") or ""))
                for item in raw.get("replacements", [])
                if isinstance(item, dict)
            ],
            proposed_files={str(key): str(value) for key, value in dict(raw.get("proposed_files") or {}).items()},
            issue_number=int(raw["issue_number"]) if raw.get("issue_number") is not None else None,
            suggested_title=str(raw.get("suggested_title")) if raw.get("suggested_title") is not None else None,
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
    def _restore_guidance(raw: Any) -> AgentGuidance | None:
        if not isinstance(raw, dict):
            return None
        guidance = AgentGuidance(
            user_memories=tuple(
                ContextMemory(
                    str(item.get("memory_id") or ""),
                    str(item.get("scope") or ""),
                    str(item.get("kind") or ""),
                    str(item.get("content") or ""),
                )
                for item in raw.get("user_memories", [])
                if isinstance(item, dict)
            ),
            repository_memories=tuple(
                ContextMemory(
                    str(item.get("memory_id") or ""),
                    str(item.get("scope") or ""),
                    str(item.get("kind") or ""),
                    str(item.get("content") or ""),
                )
                for item in raw.get("repository_memories", [])
                if isinstance(item, dict)
            ),
            resolved_references=tuple(
                ResolvedReference(str(item.get("type") or ""), str(item.get("id") or ""))
                for item in raw.get("resolved_references", [])
                if isinstance(item, dict)
            ),
        )
        return None if guidance.empty else guidance

    @staticmethod
    def _guidance(entity_type: str | None, entity_id: str | None, context: RoutingContext) -> AgentGuidance | None:
        references = (
            (ResolvedReference(entity_type, entity_id),)
            if entity_type in {"issue", "pull_request", "workflow_run"} and entity_id
            else ()
        )
        guidance = AgentGuidance(
            user_memories=context.user_memories,
            repository_memories=context.repository_memories,
            resolved_references=references,
        )
        return None if guidance.empty else guidance

    @staticmethod
    def _context_description(context: AgentContext) -> dict[str, Any]:
        return {
            "workflow_type": context.agent,
            "entity": {"type": context.entity_type or "repository", "id": context.entity_id or context.repository},
            "state": "awaiting_approval" if context.pending is not None else "active",
            "proposal_summary": context.pending.summary if context.pending is not None else "",
            "mutation_plan": [
                {"tool": call.tool, "arguments": call.arguments}
                for call in (context.pending.calls if context.pending is not None else [])
            ],
        }

    @staticmethod
    def _proposal_description(context: AgentContext) -> str:
        if context.pending is None:
            return "当前没有待确认的提案。"
        return json.dumps(
            {
                "summary": context.pending.summary,
                "calls": [{"tool": call.tool, "arguments": call.arguments} for call in context.pending.calls],
            },
            ensure_ascii=False,
            indent=2,
        )

    def _agent_for(self, name: str) -> Any:
        agents = {
            "issues": self.issue_agent,
            "pull_requests": self.pull_request_agent,
            "code_change": self.code_change_controller,
        }
        try:
            return agents[name]
        except KeyError as exc:
            raise RoutingError(f"agent has no AgentLoop implementation: {name}") from exc

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
            "ci_diagnosis": "workflow_run",
        }.get(owner, "repository")
