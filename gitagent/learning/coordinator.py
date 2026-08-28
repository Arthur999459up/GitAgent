"""Best-effort orchestration of MainAgent reflection and knowledge consolidation."""

from __future__ import annotations

from gitagent.agents.main import MainAgent
from gitagent.domain.learning import ConsolidationResult
from gitagent.domain.models import SessionScope
from gitagent.infra.observability import TraceBus, TraceCategory, TraceStatus
from gitagent.infra.persistence import (
    DomainEvidenceStore,
    KnowledgeStore,
    SessionManager,
)

from .context import ReflectionContextBuilder


class LearningCoordinator:
    """Keep the optional learning path outside the user-request success boundary."""

    def __init__(
        self,
        main_agent: MainAgent,
        sessions: SessionManager,
        knowledge: KnowledgeStore,
        evidence: DomainEvidenceStore,
        trace: TraceBus,
        *,
        input_budget_tokens: int,
        enabled: bool = True,
    ) -> None:
        self.main_agent = main_agent
        self.knowledge = knowledge
        self.evidence = evidence
        self.trace = trace
        self.enabled = enabled
        self.contexts = ReflectionContextBuilder(
            sessions,
            knowledge,
            input_budget_tokens=input_budget_tokens,
        )

    def reflect_domain(
        self,
        scope: SessionScope,
        interaction_id: str,
    ) -> ConsolidationResult | None:
        if not self.enabled:
            return None
        record = self.evidence.get(scope.account_key, scope.repository_key, interaction_id)
        if record is None or record.reflection_status != "pending":
            return None
        try:
            context = self.contexts.for_domain(record)
            proposal = self.main_agent.reflect(context)
            result = self.knowledge.consolidate(
                scope,
                proposal,
                turn_seq=record.completed_turn_seq,
                interaction=record,
            )
            self.evidence.mark_reflected(
                record,
                status="reflected" if result.changed else "skipped",
            )
            self._emit(
                scope,
                TraceStatus.COMPLETED,
                details=self._result_details(
                    proposal.candidates,
                    result,
                    interaction_id=interaction_id,
                ),
            )
            return result
        except Exception as exc:  # noqa: BLE001 - learning must not change Domain success semantics
            try:
                self.evidence.mark_reflected(record, status="reflection_failed", error=str(exc))
            except Exception:  # noqa: BLE001, S110 - preserve the original learning failure
                pass
            self._emit(
                scope,
                TraceStatus.FAILED,
                message=str(exc),
                details={"interaction_id": interaction_id, "error_type": type(exc).__name__},
            )
            return None

    def reflect_conversation(
        self,
        scope: SessionScope,
        repository_full_name: str,
        *,
        turn_seq: int,
        user_input: str,
        assistant_text: str,
    ) -> ConsolidationResult | None:
        if not self.enabled:
            return None
        try:
            context = self.contexts.for_conversation(
                scope,
                repository_full_name,
                turn_seq=turn_seq,
                user_input=user_input,
                assistant_text=assistant_text,
            )
            proposal = self.main_agent.reflect(context)
            result = self.knowledge.consolidate(scope, proposal, turn_seq=turn_seq)
            self._emit(
                scope,
                TraceStatus.COMPLETED,
                details=self._result_details(
                    proposal.candidates,
                    result,
                    turn_seq=turn_seq,
                ),
            )
            return result
        except Exception as exc:  # noqa: BLE001 - conversation response is already successful
            self._emit(
                scope,
                TraceStatus.FAILED,
                message=str(exc),
                details={"turn_seq": turn_seq, "error_type": type(exc).__name__},
            )
            return None

    @staticmethod
    def _result_details(
        candidates: tuple[object, ...],
        result: ConsolidationResult,
        **identity: object,
    ) -> dict[str, object]:
        details: dict[str, object] = {
            **identity,
            "triggered": True,
            "reason": (
                "stored"
                if result.changed
                else "no_candidates"
                if not candidates
                else "all_candidates_skipped"
            ),
            "candidates": len(candidates),
            "added": len(result.added),
            "updated": len(result.updated),
            "removed": len(result.removed),
            "skipped": len(result.skipped),
            "changes": [
                {
                    "action": change.action.value,
                    "knowledge_id": change.record.knowledge_id,
                    "scope": change.record.scope,
                    "kind": change.record.kind,
                    "topic": change.record.topic,
                    "content": change.record.content,
                    "conditions": change.record.conditions,
                    "source": change.record.source,
                }
                for change in result.changes
            ],
        }
        return details

    def _emit(
        self,
        scope: SessionScope,
        status: TraceStatus,
        *,
        message: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        self.trace.emit(
            session_id=scope.session_id,
            category=TraceCategory.WORKFLOW,
            name="long_term_learning",
            status=status,
            message=message,
            details=details,
        )
__all__ = ["LearningCoordinator"]
