from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from AGENT.GitAgent.gitagent.context import (
    ContextBudgetExceeded,
    ContextBuilder,
    ContextBuildError,
    DeterministicCompactor,
    estimate_tokens,
    merge_summary_records,
    render_summary_record,
)
from AGENT.GitAgent.gitagent.core.models import SessionScope
from AGENT.GitAgent.gitagent.state import default_working_state


@dataclass
class SessionRecord:
    session_id: str = "session-1"
    account_key: str = "api#user:1"
    repository_key: str = "api#repo:2"
    repository_full_name: str = "sample/widgets"
    context_boundary_seq: int = 0
    summary: str = ""
    summary_through_seq: int = 0
    working_state: dict[str, Any] = field(default_factory=default_working_state)


@dataclass
class TurnRecord:
    seq: int
    status: str = "completed"
    history_text: str = "handled safely"
    assistant_text: str = "visible result"
    route_summary: list[dict[str, Any]] = field(default_factory=list)
    entity_manifests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def user_text(self) -> str:
        raise AssertionError("context and summary code must never read user_text")


@dataclass
class MemoryRecord:
    memory_id: str
    scope: str
    kind: str
    content: str
    account_key: str = "api#user:1"
    repository_key: str | None = None
    updated_at: str = "2026-08-13T00:00:00Z"


class FakeSessionManager:
    def __init__(
        self,
        *,
        session: SessionRecord | None = None,
        turns: list[TurnRecord] | None = None,
        memories: list[MemoryRecord] | None = None,
    ) -> None:
        self.session = session or SessionRecord()
        self.turns = list(turns or [])
        self.memories = list(memories or [])
        self.summary_saves: list[tuple[str, int]] = []

    def get_session(self, account_key: str, repository_key: str, session_id: str) -> SessionRecord | None:
        if (account_key, repository_key, session_id) != (
            self.session.account_key,
            self.session.repository_key,
            self.session.session_id,
        ):
            return None
        return self.session

    def list_turns(
        self,
        account_key: str,
        repository_key: str,
        session_id: str,
        after_seq: int = 0,
    ) -> tuple[TurnRecord, ...]:
        return tuple(turn for turn in self.turns if turn.seq > after_seq)

    def list_memories(
        self,
        account_key: str,
        repository_key: str,
        scope: str | None = None,
    ) -> tuple[MemoryRecord, ...]:
        records = tuple(self.memories)
        return tuple(record for record in records if scope is None or record.scope == scope)

    def save_summary(
        self,
        account_key: str,
        repository_key: str,
        session_id: str,
        summary: str,
        through_seq: int,
    ) -> SessionRecord:
        self.summary_saves.append((summary, through_seq))
        self.session = replace(self.session, summary=summary, summary_through_seq=through_seq)
        return self.session


SCOPE = SessionScope("api#user:1", "api#repo:2", "session-1")


def _turn(seq: int, *, text: str = "handled safely") -> TurnRecord:
    return TurnRecord(
        seq=seq,
        history_text=text,
        route_summary=[
            {
                "route": "PULL_REQUEST",
                "session_goal": f"review PR #{seq}",
                "resolved_references": [{"type": "pull_request", "id": str(seq)}],
                "workflow_type": None,
                "workflow_status": None,
                # The compactor must ignore even allowlisted-looking neighbouring data.
                "turn_constraints": ["never persist me"],
            }
        ],
    )


def test_token_estimation_uses_ceil_utf8_bytes_divided_by_three() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("你") == 1
    assert estimate_tokens("你a") == 2


def test_recent_history_preserves_all_four_ordered_task_manifests() -> None:
    manifests = [
        {
            "turn_seq": 1,
            "entity_type": "issue",
            "items": [{"position": 1, "entity_id": str(index), "short_label": f"Issue {index}"}],
        }
        for index in range(1, 5)
    ]
    manager = FakeSessionManager(turns=[TurnRecord(seq=1, entity_manifests=manifests)])

    context = ContextBuilder(manager).build(SCOPE, "sample/widgets", "继续分析")

    assert [manifest["items"][0]["entity_id"] for manifest in context.history_units[0]["entity_manifests"]] == [
        "1",
        "2",
        "3",
        "4",
    ]


def test_context_builder_requires_the_versioned_working_state_contract() -> None:
    session = replace(SessionRecord(), working_state={})

    with pytest.raises(ContextBuildError, match="Working State"):
        ContextBuilder(FakeSessionManager(session=session)).build(
            SCOPE,
            "sample/widgets",
            "继续检查",
        )


def test_summary_is_deduplicated_bounded_and_never_reads_user_text() -> None:
    turn = _turn(3, text="diagnosis complete")
    record = render_summary_record(turn)
    assert record.startswith("[turn:3] PULL_REQUEST/completed")
    assert "pull_request:3" in record
    assert "never persist me" not in record

    records = [f"[turn:{seq}] ISSUE/completed | goal={'很长的安全结果' * 45}" for seq in range(1, 30)]
    summary = merge_summary_records(
        "[turn:3] OLD/completed | result=stale",
        records,
    )
    assert estimate_tokens(summary) <= 1500
    assert summary.count("[turn:3]") <= 1
    assert summary.startswith("[older turns omitted through:")


def test_history_count_alone_does_not_trigger_a_rolling_summary() -> None:
    manager = FakeSessionManager(turns=[_turn(seq) for seq in range(1, 14)])
    context = ContextBuilder(manager).build(SCOPE, "sample/widgets", "继续检查")

    assert context.selection_metadata["compression_level"] == "none"
    assert manager.summary_saves == []
    assert [unit["seq"] for unit in context.history_units] == list(range(1, 14))
    assert context.selection_metadata["final_projection_size"] <= 26112


def test_history_fields_are_not_hard_truncated_below_the_light_threshold() -> None:
    history = "history-" + "x" * 5_000
    assistant = "assistant-" + "y" * 5_000
    manager = FakeSessionManager(
        turns=[replace(_turn(1), history_text=history, assistant_text=assistant)]
    )

    context = ContextBuilder(manager).build(SCOPE, "sample/widgets", "继续检查")

    assert context.selection_metadata["compression_level"] == "none"
    assert context.history_units[0]["history_text"] == history
    assert context.history_units[0]["assistant_text"] == assistant


def test_light_and_emergency_projections_keep_the_latest_two_units() -> None:
    large = "x" * 1500
    light_manager = FakeSessionManager(
        turns=[replace(_turn(seq), history_text=large, assistant_text=large) for seq in range(1, 5)]
    )
    light = ContextBuilder(
        light_manager,
        context_window_tokens=7168,
        max_output_tokens=2048,
        safety_tokens=512,
    ).build(SCOPE, "sample/widgets", "继续")
    assert light.selection_metadata["compression_level"] == "light"
    assert [unit["seq"] for unit in light.history_units][-2:] == [3, 4]

    memories = [MemoryRecord(f"u-{index}", "user", "preference", "偏" * 500) for index in range(1, 7)]
    emergency_manager = FakeSessionManager(
        turns=[replace(_turn(seq), history_text="x" * 2000, assistant_text="x" * 2000) for seq in range(1, 14)],
        memories=memories,
    )
    emergency = ContextBuilder(
        emergency_manager,
        context_window_tokens=7168,
        max_output_tokens=2048,
        safety_tokens=512,
    ).build(SCOPE, "sample/widgets", "继续")
    assert emergency.selection_metadata["compression_level"] == "emergency"
    assert [unit["seq"] for unit in emergency.history_units] == [12, 13]
    assert all(unit["projection"] == "minimal" for unit in emergency.history_units)
    assert emergency.user_memories == ()
    assert emergency.repository_memories == ()
    assert emergency.selection_metadata["final_projection_size"] <= 4096


def test_required_partitions_are_never_truncated_to_force_a_call() -> None:
    builder = ContextBuilder(
        FakeSessionManager(),
        context_window_tokens=7168,
        max_output_tokens=2048,
        safety_tokens=512,
    )
    with pytest.raises(ContextBudgetExceeded, match="shorten the latest input|reset"):
        builder.build(SCOPE, "sample/widgets", "z" * 20000)


def test_manual_compaction_uses_the_same_tail_and_is_idempotent() -> None:
    manager = FakeSessionManager(turns=[_turn(seq) for seq in range(1, 10)])
    builder = ContextBuilder(manager)

    first = builder.compact(SCOPE)
    second = builder.compact(SCOPE)

    assert first.changed is True
    assert (first.covered_from_seq, first.covered_to_seq) == (1, 3)
    assert first.summary_through_seq == 3
    assert second.changed is False
    assert len(manager.summary_saves) == 1


def test_compactor_ignores_interrupted_rows() -> None:
    turns = [_turn(seq) for seq in range(1, 9)]
    turns.insert(2, TurnRecord(seq=20, status="interrupted"))
    compactor = DeterministicCompactor()
    plan = compactor.plan(turns, context_boundary_seq=0, summary_through_seq=0)

    assert all(turn.status != "interrupted" for turn in plan.turns)
