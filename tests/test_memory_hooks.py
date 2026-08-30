from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock

from gitagent.infra.observability import TraceBus
from gitagent.infra.persistence import (
    SessionEventLog,
    SessionManager,
    StateStore,
    build_account_key,
    build_repository_key,
)
from gitagent.memory import (
    AutoDream,
    MemoryExtractionContextBuilder,
    MemoryExtractor,
    MemoryPageStore,
    MemoryStopHooks,
)


class _Reasoner:
    def __init__(self, *, block: bool = False, fail_once: bool = False) -> None:
        self.started = Event()
        self.release = Event()
        self.block = block
        self.fail_once = fail_once
        self.calls = 0
        self._lock = Lock()

    def complete_structured_messages(self, **_: object) -> dict[str, object]:
        with self._lock:
            self.calls += 1
            call = self.calls
        self.started.set()
        if self.block and call == 1:
            assert self.release.wait(5)
        if self.fail_once and call == 1:
            raise RuntimeError("extract failed")
        return {"candidates": []}

    def complete_text_messages(self, **_: object) -> str:
        return ""


def _runtime(tmp_path: Path, reasoner: _Reasoner):
    store = StateStore((tmp_path / "state.db").resolve())
    sessions = SessionManager(
        store,
        SessionEventLog((tmp_path / "events").resolve(), redactor=store.redact),
    )
    scope = sessions.create_session(
        build_account_key("https://api.github.com", 1),
        build_repository_key("https://api.github.com", 2),
        "owner/repository",
    ).scope
    memory = MemoryPageStore((tmp_path / "memory").resolve())
    trace = TraceBus()
    hooks = MemoryStopHooks(
        sessions,
        MemoryExtractor(reasoner, memory),
        MemoryExtractionContextBuilder(sessions, memory, input_budget_tokens=8_000),
        AutoDream(sessions, memory),
        trace,
    )
    return sessions, scope, hooks, trace


def _complete(sessions: SessionManager, scope, text: str) -> int:
    turn = sessions.start_turn(scope, text)
    sessions.complete_turn(
        scope,
        turn.seq,
        assistant_text="done",
        workflow_summary="bounded domain summary",
        route=None,
        entity_manifests=[],
        working_state={
            "version": 4,
            "goal": "",
            "focus": None,
            "manifests": [],
            "open_question": "",
        },
    )
    return turn.seq


def test_running_extractor_coalesces_new_turns_into_one_trailing_run(
    tmp_path: Path,
) -> None:
    reasoner = _Reasoner(block=True)
    sessions, scope, hooks, trace = _runtime(tmp_path, reasoner)
    first = _complete(sessions, scope, "first")
    hooks.handle_turn_stop(scope, "owner/repository", through_seq=first)
    assert reasoner.started.wait(5)

    second = _complete(sessions, scope, "second")
    hooks.handle_turn_stop(scope, "owner/repository", through_seq=second)
    reasoner.release.set()
    hooks.wait_for_idle(5)

    state = sessions.get_memory_extraction_state(scope)
    assert state.extracted_through_seq == second
    assert state.pending_through_seq == second
    assert reasoner.calls == 2
    completed = [
        event
        for event in trace.events(scope.session_id)
        if event.name == "memory_extract" and event.status.value == "completed"
    ]
    assert completed[-1].display_message == "本轮未发现需要保存的长期记忆。"
    hooks.close()


def test_failed_extraction_keeps_cursor_until_a_later_turn_retries(tmp_path: Path) -> None:
    reasoner = _Reasoner(fail_once=True)
    sessions, scope, hooks, _ = _runtime(tmp_path, reasoner)
    first = _complete(sessions, scope, "first")
    hooks.handle_turn_stop(scope, "owner/repository", through_seq=first)
    hooks.wait_for_idle(5)
    assert sessions.get_memory_extraction_state(scope).extracted_through_seq == 0

    second = _complete(sessions, scope, "second")
    hooks.handle_turn_stop(scope, "owner/repository", through_seq=second)
    hooks.wait_for_idle(5)
    assert sessions.get_memory_extraction_state(scope).extracted_through_seq == second
    assert reasoner.calls == 2
    hooks.close()


def test_dream_gate_uses_time_and_distinct_sessions(tmp_path: Path) -> None:
    store = StateStore((tmp_path / "state.db").resolve())
    sessions = SessionManager(
        store,
        SessionEventLog((tmp_path / "events").resolve(), redactor=store.redact),
    )
    account = build_account_key("https://api.github.com", 1)
    repository = build_repository_key("https://api.github.com", 2)
    last_scope = None
    for index in range(5):
        last_scope = sessions.create_session(
            account,
            repository,
            "owner/repository",
        ).scope
        _complete(sessions, last_scope, f"session {index}")
    assert last_scope is not None
    clock = [datetime.now().astimezone()]
    dream = AutoDream(
        sessions,
        MemoryPageStore((tmp_path / "memory").resolve()),
        now=lambda: clock[0],
    )
    assert dream.eligible(last_scope).eligible is True
    dream.run(last_scope)
    clock[0] += timedelta(minutes=11)
    assert dream.eligible(last_scope).reason == "minimum_interval"
