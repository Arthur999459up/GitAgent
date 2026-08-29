from __future__ import annotations

import unittest
from types import SimpleNamespace

from gitagent.domain.learning import LearningTrace, TraceStep
from gitagent.domain.models import SessionScope
from gitagent.learning.context import ReflectionContextBuilder


class _Sessions:
    def __init__(self) -> None:
        self.turns = tuple(
            SimpleNamespace(
                seq=index,
                status="completed",
                user_text=f"user {index}",
                assistant_text=f"assistant {index}",
                route_summary=f"route {index}",
            )
            for index in range(1, 4)
        )

    def get_session(self, account_key: str, repository_key: str, session_id: str):
        del account_key, repository_key, session_id
        return SimpleNamespace(repository_full_name="owner/repository")

    def list_turns(
        self,
        account_key: str,
        repository_key: str,
        session_id: str,
        *,
        after_seq: int = 0,
    ):
        del account_key, repository_key, session_id
        return tuple(turn for turn in self.turns if turn.seq > after_seq)


class _Memory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def read_index(self, account_key: str, repository_key: str, *, full: bool = False) -> str:
        self.calls.append((account_key, repository_key, full))
        return "memory index"


class ReflectionContextBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = _Sessions()
        self.memory = _Memory()
        self.scope = SessionScope("account", "repository", "session")
        self.builder = ReflectionContextBuilder(
            self.sessions,
            self.memory,  # type: ignore[arg-type]
            input_budget_tokens=4096,
        )

    def test_conversation_reflection_reads_only_current_turn(self) -> None:
        context = self.builder.for_conversation(
            self.scope,
            "owner/repository",
            turn_seq=3,
        )

        self.assertEqual([unit["seq"] for unit in context.conversation_units], [3])
        self.assertEqual(self.memory.calls, [("account", "repository", False)])

    def test_domain_reflection_preserves_every_trajectory_step_when_compacting(self) -> None:
        trace = LearningTrace(
            goal="goal " + ("g" * 2000),
            outcome="outcome " + ("o" * 2000),
            trajectory=tuple(
                TraceStep(f"step-{index}", f"result-{index} " + ("x" * 2000))
                for index in range(10)
            ),
        )

        context = self.builder.for_domain(
            self.scope,
            "owner/repository",
            trace,
            turn_seq=3,
        )

        self.assertEqual([unit["seq"] for unit in context.conversation_units], [3])
        self.assertIsNotNone(context.learning_trace)
        assert context.learning_trace is not None
        self.assertEqual(len(context.learning_trace.trajectory), 10)
        self.assertEqual(
            [step.action for step in context.learning_trace.trajectory],
            [f"step-{index}" for index in range(10)],
        )


if __name__ == "__main__":
    unittest.main()
