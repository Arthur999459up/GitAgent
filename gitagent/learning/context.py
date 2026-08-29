"""Budgeted construction of isolated reflection inputs."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from gitagent.domain.learning import LearningTrace, ReflectionInput, TraceStep
from gitagent.domain.models import SessionScope, to_plain
from gitagent.harness.context import estimate_tokens
from gitagent.infra.persistence import SessionManager
from gitagent.memory import MemoryStore


class ReflectionContextBuilder:
    """Build one-turn learning contexts without touching Main conversation state."""

    def __init__(
        self,
        sessions: SessionManager,
        memory: MemoryStore,
        *,
        input_budget_tokens: int,
    ) -> None:
        if not isinstance(input_budget_tokens, int) or isinstance(
            input_budget_tokens, bool
        ):
            raise TypeError("reflection input budget must be an integer")
        if input_budget_tokens < 4096:
            raise ValueError("reflection input budget must be at least 4096 tokens")
        self.sessions = sessions
        self.memory = memory
        self.input_budget_tokens = max(4096, input_budget_tokens - 1024)

    def for_domain(
        self,
        scope: SessionScope,
        repository_full_name: str,
        trace: LearningTrace,
        *,
        turn_seq: int,
    ) -> ReflectionInput:
        context = ReflectionInput(
            scope=scope,
            repository_full_name=repository_full_name,
            trigger="domain_trace",
            conversation_units=self._conversation(scope, turn_seq),
            learning_trace=trace,
            memory_index=self.memory.read_index(
                scope.account_key, scope.repository_key
            ),
        )
        return self._fit(context)

    def for_conversation(
        self,
        scope: SessionScope,
        repository_full_name: str,
        *,
        turn_seq: int,
    ) -> ReflectionInput:
        context = ReflectionInput(
            scope=scope,
            repository_full_name=repository_full_name,
            trigger="main_conversation",
            conversation_units=self._conversation(scope, turn_seq),
            memory_index=self.memory.read_index(
                scope.account_key, scope.repository_key
            ),
        )
        return self._fit(context)

    def for_compaction(
        self, scope: SessionScope, repository_full_name: str
    ) -> ReflectionInput:
        return ReflectionInput(
            scope=scope,
            repository_full_name=repository_full_name,
            trigger="explicit_memory_compact",
            memory_index=self.memory.read_index(
                scope.account_key, scope.repository_key, full=True
            ),
        )

    def _conversation(
        self, scope: SessionScope, turn_seq: int
    ) -> tuple[dict[str, Any], ...]:
        session = self.sessions.get_session(
            scope.account_key, scope.repository_key, scope.session_id
        )
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
                after_seq=max(0, turn_seq - 1),
            )
            if turn.seq == turn_seq
        )

    def _fit(self, context: ReflectionInput) -> ReflectionInput:
        """Fit required evidence by compacting fields, never by dropping trajectory steps."""

        candidate = context
        if self._tokens(candidate) <= self.input_budget_tokens:
            return candidate

        trace = candidate.learning_trace
        if trace is not None:
            candidate = replace(
                candidate,
                learning_trace=_compact_trace(
                    trace,
                    action_limit=300,
                    result_limit=600,
                    goal_limit=1000,
                    outcome_limit=1000,
                ),
            )

        if self._tokens(candidate) > self.input_budget_tokens:
            candidate = replace(
                candidate,
                conversation_units=_compact_conversation(
                    candidate.conversation_units, limit=800
                ),
            )

        trace = candidate.learning_trace
        if self._tokens(candidate) > self.input_budget_tokens and trace is not None:
            candidate = replace(
                candidate,
                learning_trace=_compact_trace(
                    trace,
                    action_limit=160,
                    result_limit=320,
                    goal_limit=600,
                    outcome_limit=600,
                ),
            )

        if self._tokens(candidate) > self.input_budget_tokens:
            candidate = replace(
                candidate,
                conversation_units=_compact_conversation(
                    candidate.conversation_units, limit=400
                ),
            )

        if self._tokens(candidate) > self.input_budget_tokens:
            raise ValueError(
                "required one-turn Memory index and learning evidence exceed the reflection budget"
            )
        return candidate

    @staticmethod
    def _tokens(context: ReflectionInput) -> int:
        return estimate_tokens(
            json.dumps(to_plain(context), ensure_ascii=False, default=str)
        )


def _compact_trace(
    trace: LearningTrace,
    *,
    action_limit: int,
    result_limit: int,
    goal_limit: int,
    outcome_limit: int,
) -> LearningTrace:
    return replace(
        trace,
        goal=_bounded_result(trace.goal, goal_limit),
        outcome=_bounded_result(trace.outcome, outcome_limit),
        trajectory=tuple(
            TraceStep(
                _bounded_result(step.action, action_limit),
                _bounded_result(step.result, result_limit),
            )
            for step in trace.trajectory
        ),
    )


def _compact_conversation(
    units: tuple[dict[str, Any], ...], *, limit: int
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            key: _bounded_result(value, limit) if isinstance(value, str) else value
            for key, value in unit.items()
        }
        for unit in units
    )


def _bounded_result(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, (limit - 20) // 2)
    return value[:half] + " … result omitted … " + value[-half:]


__all__ = ["ReflectionContextBuilder"]
