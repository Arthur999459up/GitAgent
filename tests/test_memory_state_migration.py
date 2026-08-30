from __future__ import annotations

import sqlite3
from pathlib import Path

from gitagent.infra.persistence import SCHEMA_VERSION, StateStore


def test_v8_state_database_is_migrated_without_recreating_sessions(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "state.db").resolve()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, account_key TEXT NOT NULL,
            repository_key TEXT NOT NULL, repository_full_name TEXT NOT NULL,
            title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            context_boundary_seq INTEGER NOT NULL DEFAULT 0 CHECK(context_boundary_seq >= 0),
            summary TEXT NOT NULL DEFAULT '',
            summary_through_seq INTEGER NOT NULL DEFAULT 0 CHECK(summary_through_seq >= 0),
            working_state TEXT NOT NULL, agent_context TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE turns (
            session_id TEXT NOT NULL, seq INTEGER NOT NULL CHECK(seq >= 1),
            status TEXT NOT NULL CHECK(status IN ('started','completed','failed','interrupted')),
            created_at TEXT NOT NULL, completed_at TEXT,
            PRIMARY KEY(session_id, seq),
            FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );
        CREATE INDEX turns_by_session_status ON turns(session_id, status, seq);
        CREATE INDEX sessions_by_scope_updated
            ON sessions(account_key, repository_key, updated_at DESC);
        CREATE INDEX sessions_by_account_updated
            ON sessions(account_key, updated_at DESC);
        INSERT INTO schema_metadata(key,value) VALUES('schema_version','8');
        INSERT INTO sessions(
            session_id,account_key,repository_key,repository_full_name,title,
            created_at,updated_at,working_state
        ) VALUES(
            'session-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','account','repository',
            'owner/repository','kept','2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00',
            '{"version":4,"goal":"","focus":null,"manifests":[],"open_question":""}'
        );
        """
    )
    connection.commit()
    connection.close()

    StateStore(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone() == (str(SCHEMA_VERSION),)
        assert connection.execute("SELECT title FROM sessions").fetchone() == ("kept",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "memory_extraction_state" in tables
        assert "memory_dream_state" in tables
    finally:
        connection.close()
