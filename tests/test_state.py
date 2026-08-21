"""StateStore and Session-owned working-memory persistence tests."""

from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest
from AGENT.GitAgent.gitagent.context import ContextBuilder
from AGENT.GitAgent.gitagent.core.errors import StateError, ValidationError
from AGENT.GitAgent.gitagent.state import (
    REDACTED,
    SCHEMA_VERSION,
    SessionManager,
    StateStore,
    build_account_key,
    build_repository_key,
    default_working_state,
    merge_working_state,
    truncate_utf8,
)


def _store(tmp_path, *, sensitive_values=()):
    return StateStore(tmp_path / "state" / "state.db", secret_values=sensitive_values)


def _session(store):
    manager = SessionManager(store)
    account = build_account_key("https://api.github.test", 1)
    repo = build_repository_key("https://api.github.test", 2)
    return manager, manager.create_session(account, repo, "sample/widgets")


def test_stable_identity_uses_normalized_url_and_numeric_ids():
    assert build_account_key("HTTPS://API.GITHUB.COM/", 7) == "https://api.github.com#user:7"
    assert build_repository_key("https://API.GITHUB.COM/", 9) == "https://api.github.com#repo:9"
    with pytest.raises(ValidationError):
        build_account_key("https://api.github.com", True)
    with pytest.raises(ValidationError):
        build_repository_key("https://api.github.com", 0)


def test_store_permissions_schema_and_foreign_keys(tmp_path):
    store = _store(tmp_path)
    if os.name == "posix":
        assert store.path.parent.stat().st_mode & 0o777 == 0o700
        assert store.path.stat().st_mode & 0o777 == 0o600
    connection = store.read()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == str(
            SCHEMA_VERSION
        )
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not str(row[0]).startswith("sqlite_")
        }
        assert tables == {"schema_metadata", "sessions", "turns", "memories"}
    finally:
        connection.close()


def test_default_working_state_is_session_main_agent_memory():
    assert default_working_state() == {
        "version": 4,
        "goal": "",
        "focus": None,
        "manifests": [],
        "open_question": "",
    }


def test_first_conversation_sets_a_short_stable_session_title(tmp_path):
    manager, session = _session(_store(tmp_path))
    assert session.title == "新会话（等待首条消息）"

    first_text = "  请检查   Session 隔离，尤其是恢复时的记忆载入。  " + "补充" * 30
    first = manager.start_turn(session.scope, first_text)
    titled = manager.get_session(session.account_key, session.repository_key, session.session_id)
    assert titled is not None
    assert titled.title.startswith("请检查 Session 隔离，尤其是恢复时的记忆载入。")
    assert len(titled.title) == 60
    assert titled.title.endswith("…")

    manager.fail_turn(session.scope, first.seq, "test failure")
    second = manager.start_turn(session.scope, "这句话不能覆盖标题")
    manager.fail_turn(session.scope, second.seq, "test failure")
    restored = manager.get_session(session.account_key, session.repository_key, session.session_id)
    assert restored is not None and restored.title == titled.title


def test_account_session_listing_and_deletion_do_not_cross_accounts(tmp_path):
    manager = SessionManager(_store(tmp_path))
    account = build_account_key("https://api.github.test", 1)
    other_account = build_account_key("https://api.github.test", 2)
    first = manager.create_session(
        account,
        build_repository_key("https://api.github.test", 10),
        "sample/one",
    )
    second = manager.create_session(
        account,
        build_repository_key("https://api.github.test", 20),
        "sample/two",
    )
    hidden = manager.create_session(
        other_account,
        build_repository_key("https://api.github.test", 10),
        "other/private",
    )

    listed = manager.list_account_sessions(account)
    assert {session.session_id for session in listed} == {first.session_id, second.session_id}
    assert manager.get_account_session(account, hidden.session_id) is None

    manager.delete_session(first.scope)
    assert [session.session_id for session in manager.list_account_sessions(account)] == [second.session_id]
    assert manager.get_account_session(other_account, hidden.session_id) == hidden


def test_context_restore_is_session_isolated_and_loads_only_scoped_memories(tmp_path):
    manager = SessionManager(_store(tmp_path))
    account = build_account_key("https://api.github.test", 1)
    first_repo = build_repository_key("https://api.github.test", 10)
    second_repo = build_repository_key("https://api.github.test", 20)
    first = manager.create_session(account, first_repo, "sample/one")
    sibling = manager.create_session(account, first_repo, "sample/one")
    other_repo = manager.create_session(account, second_repo, "sample/two")

    turn = manager.start_turn(first.scope, "只属于第一个 Session")
    manager.complete_turn(
        first.scope,
        turn.seq,
        assistant_text="first answer",
        history_text="first-session-history",
        route_summary=[],
        entity_manifests=[],
        working_state=default_working_state(),
    )
    user_memory, _ = manager.remember(account, first_repo, scope="user", kind="preference", content="统一用中文")
    first_repo_memory, _ = manager.remember(
        account,
        first_repo,
        scope="repository",
        kind="constraint",
        content="仓库一约束",
    )
    second_repo_memory, _ = manager.remember(
        account,
        second_repo,
        scope="repository",
        kind="constraint",
        content="仓库二约束",
    )

    builder = ContextBuilder(manager)
    restored_first = builder.build(first.scope, "sample/one", "继续")
    restored_sibling = builder.build(sibling.scope, "sample/one", "开始")
    restored_other_repo = builder.build(other_repo.scope, "sample/two", "开始")

    assert [unit["history_text"] for unit in restored_first.history_units] == ["first-session-history"]
    assert restored_sibling.history_units == ()
    assert restored_other_repo.history_units == ()
    assert [memory.memory_id for memory in restored_first.user_memories] == [user_memory.memory_id]
    assert [memory.memory_id for memory in restored_sibling.repository_memories] == [first_repo_memory.memory_id]
    assert [memory.memory_id for memory in restored_other_repo.user_memories] == [user_memory.memory_id]
    assert [memory.memory_id for memory in restored_other_repo.repository_memories] == [second_repo_memory.memory_id]


def test_projection_open_question_allows_fifty_thousand_characters_and_truncates_larger_values():
    question = "问" * 60_000
    projection = SimpleNamespace(
        goals=(),
        entity_manifests=(),
        focus=None,
        open_question=question,
    )

    state = merge_working_state(default_working_state(), projection=projection)

    assert len(state["open_question"]) == 50_000
    assert "[TRUNCATED]" in state["open_question"]


def test_session_agent_context_survives_restart(tmp_path):
    store = _store(tmp_path)
    sessions, session = _session(store)
    value = {
        "agent": "issues",
        "repository": "sample/widgets",
        "goal": "review issue 1",
        "entity_type": "issue",
        "entity_id": "1",
        "observations": [{"kind": "tool", "payload": {"tool": "github.get_issue"}}],
        "pending": None,
    }
    sessions.save_agent_context(session.scope, value)

    restarted = SessionManager(StateStore(store.path))
    restored = restarted.get_session(session.account_key, session.repository_key, session.session_id)

    assert restored is not None
    assert restored.agent_context == value


def test_reset_session_clears_main_working_state_and_child_context(tmp_path):
    store = _store(tmp_path)
    sessions, session = _session(store)
    sessions.save_agent_context(session.scope, {"agent": "issues", "goal": "x"})
    turn = sessions.start_turn(session.scope, "hello")
    state = default_working_state()
    state["goal"] = "old goal"
    sessions.complete_turn(
        session.scope,
        turn.seq,
        assistant_text="done",
        history_text="history",
        route_summary=[],
        entity_manifests=[],
        working_state=state,
    )

    reset = sessions.reset_session(session.scope)

    assert reset.working_state == default_working_state()
    assert reset.agent_context == {}
    assert reset.context_boundary_seq == turn.seq


def test_store_boundary_redacts_session_turns_and_agent_context(tmp_path):
    marker = "marker-value-12345"
    store = _store(tmp_path, sensitive_values=(marker,))
    sessions, session = _session(store)
    turn = sessions.start_turn(session.scope, f"user {marker}")
    sessions.complete_turn(
        session.scope,
        turn.seq,
        assistant_text=f"assistant {marker}",
        history_text=f"history {marker}",
        route_summary=[],
        entity_manifests=[],
        working_state=default_working_state(),
    )
    sessions.save_agent_context(session.scope, {"agent": "issues", "goal": marker, "observations": [marker]})

    restored_turn = sessions.list_turns(session.account_key, session.repository_key, session.session_id)[0]
    restored_session = sessions.get_session(session.account_key, session.repository_key, session.session_id)
    assert restored_session is not None
    assert marker not in restored_turn.user_text
    assert REDACTED in restored_turn.user_text
    assert marker not in str(restored_session.agent_context)
    assert marker.encode() not in store.path.read_bytes()


def test_incompatible_schema_is_rejected_instead_of_keeping_legacy_state(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE schema_metadata SET value='3' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="incompatible"):
        StateStore(path)


def test_higher_schema_version_is_rejected(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE schema_metadata SET value='99' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="incompatible"):
        StateStore(path)


def test_store_rejects_symlink_database(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX symlink protection")
    target = tmp_path / "target.db"
    target.write_bytes(b"")
    link = tmp_path / "state.db"
    link.symlink_to(target)
    with pytest.raises(StateError, match="symbolic link"):
        StateStore(link)


def test_truncate_utf8_never_splits_multibyte_codepoint():
    value = "你" * 100
    truncated = truncate_utf8(value, 48)
    assert len(truncated.encode("utf-8")) <= 48
    assert truncated.encode("utf-8").decode("utf-8") == truncated
    assert "[TRUNCATED redacted_bytes=" in truncated
