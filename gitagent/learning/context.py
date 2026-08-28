"""Budgeted construction of temporary MainAgent reflection contexts."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from gitagent.domain.learning import DomainInteractionRecord, ReflectionContext
from gitagent.domain.models import SessionScope, to_plain
from gitagent.harness.context import estimate_tokens, render_agent_observations
from gitagent.infra.persistence import KnowledgeStore, SessionManager


class ReflectionContextBuilder:
    """Select learning evidence without modifying Main conversation context."""

    def __init__(
        self,
        sessions: SessionManager,
        knowledge: KnowledgeStore,
        *,
        input_budget_tokens: int,
    ) -> None:
        if not isinstance(input_budget_tokens, int) or isinstance(input_budget_tokens, bool):
            raise TypeError("reflection input budget must be an integer")
        if input_budget_tokens < 4096:
            raise ValueError("reflection input budget must be at least 4096 tokens")
        self.sessions = sessions
        self.knowledge = knowledge
        # Leave room for the reflection system prompt and structured-call definition.
        self.input_budget_tokens = max(4096, input_budget_tokens - 1024)

    def for_domain(self, record: DomainInteractionRecord) -> ReflectionContext:
        scope = record.scope
        conversation = self._conversation(
            scope,
            first_seq=max(1, record.origin_turn_seq - 1),
            last_seq=record.completed_turn_seq,
        )
        evidence = dict(record.evidence)
        observations = evidence.pop("observations", [])
        if not isinstance(observations, list):
            observations = []
        file_coverage = evidence.get("file_coverage", [])
        if not isinstance(file_coverage, list):
            file_coverage = []
        existing = self.knowledge.relevant(
            record.account_key,
            record.repository_key,
            self._retrieval_query(record.goal, evidence),
            user_limit=8,
            repository_limit=12,
            recent_repository=4,
        )
        interaction_base = {
            "interaction_id": record.interaction_id,
            "agent": record.agent,
            "entity_type": record.entity_type,
            "entity_id": record.entity_id,
            "goal": record.goal,
            "origin_turn_seq": record.origin_turn_seq,
            "completed_turn_seq": record.completed_turn_seq,
            "created_at": record.created_at,
            "evidence": evidence,
        }
        fixed_tokens = estimate_tokens(
            json.dumps(
                {
                    "repository": record.repository_full_name,
                    "conversation": conversation,
                    "existing_knowledge": [to_plain(item) for item in existing],
                    "interaction": interaction_base,
                },
                ensure_ascii=False,
                default=str,
            )
        )
        projected_observations = json.loads(
            render_agent_observations(
                observations,
                file_coverage=file_coverage,
                effective_input_budget=self.input_budget_tokens,
                prompt_overhead=fixed_tokens,
            )
        )
        interaction = dict(interaction_base)
        interaction["observations"] = projected_observations
        context = ReflectionContext(
            scope=scope,
            repository_full_name=record.repository_full_name,
            trigger="domain_interaction",
            conversation_units=conversation,
            interaction=interaction,
            existing_knowledge=existing,
            selection_metadata={
                "source": "high_fidelity_domain_record",
                "stored_observation_count": len(observations),
                "knowledge_candidates": len(existing),
            },
        )
        return self._fit(context)

    def for_conversation(
        self,
        scope: SessionScope,
        repository_full_name: str,
        *,
        turn_seq: int,
        user_input: str,
        assistant_text: str,
    ) -> ReflectionContext:
        conversation = self._conversation(scope, first_seq=max(1, turn_seq - 3), last_seq=turn_seq)
        existing = self.knowledge.relevant(
            scope.account_key,
            scope.repository_key,
            f"{user_input}\n{assistant_text}",
            user_limit=8,
            repository_limit=8,
            recent_repository=4,
        )
        context = ReflectionContext(
            scope=scope,
            repository_full_name=repository_full_name,
            trigger="main_conversation_signal",
            conversation_units=conversation,
            existing_knowledge=existing,
            selection_metadata={
                "source": "main_conversation",
                "turn_seq": turn_seq,
                "knowledge_candidates": len(existing),
            },
        )
        return self._fit(context)

    def _conversation(
        self,
        scope: SessionScope,
        *,
        first_seq: int,
        last_seq: int,
    ) -> tuple[dict[str, Any], ...]:
        session = self.sessions.get_session(scope.account_key, scope.repository_key, scope.session_id)
        if session is None:
            return ()
        return tuple(
            {
                "seq": turn.seq,
                "status": turn.status,
                "user": turn.user_text,
                "assistant": turn.assistant_text,
                "route": turn.route_summary,
            }
            for turn in self.sessions.list_turns(
                scope.account_key,
                scope.repository_key,
                scope.session_id,
                after_seq=max(0, first_seq - 1),
            )
            if turn.seq <= last_seq
        )

    def _fit(self, context: ReflectionContext) -> ReflectionContext:
        conversation = list(context.conversation_units)
        knowledge = list(context.existing_knowledge)
        decisions: list[dict[str, Any]] = []
        candidate = context
        while self._tokens(candidate) > self.input_budget_tokens and conversation:
            removed = conversation.pop(0)
            decisions.append({"kind": "turn", "id": removed.get("seq"), "reason": "budget_excluded"})
            candidate = replace(candidate, conversation_units=tuple(conversation))
        while self._tokens(candidate) > self.input_budget_tokens and knowledge:
            removed = knowledge.pop()
            decisions.append(
                {"kind": "knowledge", "id": removed.knowledge_id, "reason": "budget_excluded"}
            )
            candidate = replace(candidate, existing_knowledge=tuple(knowledge))
        if self._tokens(candidate) > self.input_budget_tokens and candidate.interaction is not None:
            for string_limit, item_limit in ((4_000, 20), (2_000, 12), (1_000, 8), (500, 5)):
                projected = _bounded_value(
                    candidate.interaction,
                    string_limit=string_limit,
                    item_limit=item_limit,
                )
                candidate = replace(candidate, interaction=projected)
                decisions.append(
                    {
                        "kind": "domain_interaction",
                        "id": str(projected.get("interaction_id") or ""),
                        "reason": f"budget_projection_{string_limit}",
                    }
                )
                if self._tokens(candidate) <= self.input_budget_tokens:
                    break
        metadata = dict(candidate.selection_metadata)
        metadata["input_tokens"] = self._tokens(candidate)
        metadata["input_budget_tokens"] = self.input_budget_tokens
        metadata["excluded"] = decisions
        return replace(candidate, selection_metadata=metadata)

    @staticmethod
    def _retrieval_query(goal: str, evidence: dict[str, Any]) -> str:
        parts = [goal]
        for key in ("operation", "requested_outcome", "final_message", "error"):
            value = evidence.get(key)
            if value:
                parts.append(str(value))
        return "\n".join(parts)

    @staticmethod
    def _tokens(context: ReflectionContext) -> int:
        return estimate_tokens(json.dumps(to_plain(context), ensure_ascii=False, default=str))


def _bounded_value(value: Any, *, string_limit: int, item_limit: int, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        half = string_limit // 2
        return value[:half] + f"... <{len(value) - string_limit} chars omitted> ..." + value[-half:]
    if isinstance(value, dict):
        if depth >= 5:
            return f"<{len(value)} keys omitted>"
        priority = (
            "interaction_id",
            "agent",
            "goal",
            "evidence",
            "observations",
            "operation",
            "requested_outcome",
            "final_message",
            "error",
            "verification",
        )
        ordered_keys = [key for key in priority if key in value]
        ordered_keys.extend(key for key in value if key not in ordered_keys)
        items = [(key, value[key]) for key in ordered_keys]
        result = {
            str(key): _bounded_value(
                item,
                string_limit=string_limit,
                item_limit=item_limit,
                depth=depth + 1,
            )
            for key, item in items[:item_limit]
        }
        if len(items) > item_limit:
            result["__omitted__"] = f"{len(items) - item_limit} keys"
        return result
    if isinstance(value, (list, tuple)):
        if depth >= 5:
            return f"<{len(value)} items omitted>"
        result = [
            _bounded_value(
                item,
                string_limit=string_limit,
                item_limit=item_limit,
                depth=depth + 1,
            )
            for item in value[:item_limit]
        ]
        if len(value) > item_limit:
            result.append(f"<{len(value) - item_limit} items omitted>")
        return result
    return value


__all__ = ["ReflectionContextBuilder"]
