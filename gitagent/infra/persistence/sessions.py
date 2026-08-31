"""Scoped Session, Turn, and Working State lifecycle."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from gitagent.domain.errors import StateError, ValidationError
from gitagent.domain.models import SessionScope

from .event_log import SessionEventLog
from .store import StateStore

OPEN_QUESTION_CHARACTER_LIMIT = 50_000

_ACCOUNT_KEY = re.compile(r"^(?P<api>.+)#user:(?P<id>[1-9][0-9]*)$")
_REPOSITORY_KEY = re.compile(r"^(?P<api>.+)#repo:(?P<id>[1-9][0-9]*)$")
_SESSION_ID = re.compile(r"^session-[0-9a-f]{32}$")


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    account_key: str
    repository_key: str
    repository_full_name: str
    title: str
    created_at: str
    updated_at: str
    context_boundary_seq: int
    summary: str
    summary_through_seq: int
    working_state: dict[str, Any]
    agent_context: dict[str, Any]

    @property
    def scope(self) -> SessionScope:
        return SessionScope(self.account_key, self.repository_key, self.session_id)


@dataclass(frozen=True)
class TurnRecord:
    session_id: str
    seq: int
    status: str
    user_text: str
    assistant_text: str
    entity_manifests: list[dict[str, Any]]
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class MemoryExtractionState:
    session_id: str
    extracted_through_seq: int = 0
    pending_through_seq: int = 0
    updated_at: str = ""


@dataclass(frozen=True)
class MemoryDreamState:
    account_key: str
    repository_key: str
    last_dream_at: str = ""
    last_dream_session_marker: str = ""
    updated_at: str = ""


def default_working_state() -> dict[str, Any]:
    return {
        "version": 4,
        "goal": "",
        "focus": None,
        "manifests": [],
        "open_question": "",
    }


def normalize_api_url(api_url: str) -> str:
    if not isinstance(api_url, str):
        raise ValidationError("GitHub API URL must be a string")
    value = api_url.strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("GitHub API URL must be HTTP(S) with a host and without credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))


def build_account_key(api_url: str, authenticated_user_id: int) -> str:
    return f"{normalize_api_url(api_url)}#user:{_positive_decimal(authenticated_user_id, 'authenticated user ID')}"


def build_repository_key(api_url: str, repository_id: int) -> str:
    return f"{normalize_api_url(api_url)}#repo:{_positive_decimal(repository_id, 'repository ID')}"


class SessionManager:
    """The application boundary for Session and Turn state."""

    def __init__(
        self,
        store: StateStore,
        event_log: SessionEventLog,
    ) -> None:
        self.store = store
        self.event_log = event_log
        self.recover_event_log()
        self.recover_interrupted()

    def create_session(
        self,
        account_key: str,
        repository_key: str,
        repository_full_name: str,
        *,
        session_id: str | None = None,
    ) -> SessionRecord:
        _validate_scope_keys(account_key, repository_key)
        if session_id is not None:
            _validate_session_id(session_id)
        full_name = self.store.text(
            _require_string(repository_full_name, "repository_full_name", maximum=240, allow_empty=False)
        )
        with self.store.transaction() as connection:
            created = self._insert_session_tx(
                connection,
                account_key,
                repository_key,
                full_name,
                session_id=session_id,
            )
        self._record_session_started(created)
        return created

    def get_account_session(self, account_key: str, session_id: str) -> SessionRecord | None:
        account_key, _ = _validate_account_key(account_key)
        _validate_session_id(session_id)
        connection = self.store.read()
        try:
            row = connection.execute(
                "SELECT * FROM sessions WHERE account_key=? AND session_id=?",
                (account_key, session_id),
            ).fetchone()
            return _session(row) if row is not None else None
        finally:
            connection.close()

    def get_session(
        self,
        account_key: str,
        repository_key: str,
        session_id: str,
    ) -> SessionRecord | None:
        _validate_scope_keys(account_key, repository_key)
        _validate_session_id(session_id)
        connection = self.store.read()
        try:
            row = connection.execute(
                "SELECT * FROM sessions WHERE account_key=? AND repository_key=? AND session_id=?",
                (account_key, repository_key, session_id),
            ).fetchone()
            return _session(row) if row is not None else None
        finally:
            connection.close()

    def save_agent_context(self, scope: SessionScope, value: Mapping[str, Any] | None) -> SessionRecord:
        _validate_session_scope(scope)
        context = {} if value is None else dict(value)
        if "messages" in context:
            raise ValidationError(
                "agent_context stores runtime state only; model messages belong in the Session event log"
            )
        encoded = self.store.json(context, max_bytes=512 * 1024)
        with self.store.transaction() as connection:
            self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id)
            now = _utc_now()
            connection.execute(
                "UPDATE sessions SET agent_context=?,updated_at=? WHERE session_id=?",
                (encoded, now, scope.session_id),
            )
            return _session(self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id))

    def list_sessions(self, account_key: str, repository_key: str) -> tuple[SessionRecord, ...]:
        _validate_scope_keys(account_key, repository_key)
        connection = self.store.read()
        try:
            rows = connection.execute(
                """
                SELECT * FROM sessions WHERE account_key=? AND repository_key=?
                ORDER BY updated_at DESC, session_id ASC
                """,
                (account_key, repository_key),
            ).fetchall()
            return tuple(_session(row) for row in rows)
        finally:
            connection.close()

    def list_account_sessions(self, account_key: str) -> tuple[SessionRecord, ...]:
        account_key, _ = _validate_account_key(account_key)
        connection = self.store.read()
        try:
            rows = connection.execute(
                """
                SELECT * FROM sessions WHERE account_key=?
                ORDER BY updated_at DESC, session_id ASC
                """,
                (account_key,),
            ).fetchall()
            return tuple(_session(row) for row in rows)
        finally:
            connection.close()

    def reset_session(self, scope: SessionScope) -> SessionRecord:
        _validate_session_scope(scope)
        with self.store.transaction() as connection:
            self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id)
            last_seq = _require_non_negative_integer(
                connection.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM turns WHERE session_id=?",
                    (scope.session_id,),
                ).fetchone()[0],
                "last Turn sequence",
            )
            now = _utc_now()
            working_state = self.store.json(_validate_working_state(default_working_state()), max_bytes=32 * 1024)
            connection.execute(
                """
                UPDATE sessions SET context_boundary_seq=?, summary='', summary_through_seq=?,
                    working_state=?, agent_context='{}', updated_at=? WHERE session_id=?
                """,
                (last_seq, last_seq, working_state, now, scope.session_id),
            )
            reset = _session(
                self._session_row_tx(
                    connection,
                    scope.account_key,
                    scope.repository_key,
                    scope.session_id,
                )
            )
        self.event_log.append(
            scope,
            "workflow_step",
            data={"operation": "session_reset", "through_turn_seq": last_seq},
        )
        return reset

    def delete_session(
        self,
        scope: SessionScope,
    ) -> SessionRecord:
        _validate_session_scope(scope)
        with self.store.transaction() as connection:
            target = self._session_row_tx(
                connection,
                scope.account_key,
                scope.repository_key,
                scope.session_id,
                required=False,
            )
            if target is None:
                raise StateError("Session not found")
            connection.execute("DELETE FROM sessions WHERE session_id=?", (scope.session_id,))
            deleted = _session(target)
        self.event_log.delete(scope)
        return deleted

    def replace_session(self, scope: SessionScope, replacement_session_id: str) -> SessionRecord:
        _validate_session_scope(scope)
        _validate_session_id(replacement_session_id)
        if replacement_session_id == scope.session_id:
            raise ValidationError("replacement Session must use a new ID")
        with self.store.transaction() as connection:
            target_row = self._session_row_tx(
                connection,
                scope.account_key,
                scope.repository_key,
                scope.session_id,
            )
            target = _session(target_row)
            replacement = self._insert_session_tx(
                connection,
                target.account_key,
                target.repository_key,
                target.repository_full_name,
                session_id=replacement_session_id,
            )
            connection.execute("DELETE FROM sessions WHERE session_id=?", (scope.session_id,))
        self.event_log.delete(scope)
        self._record_session_started(replacement)
        return replacement

    def start_turn(
        self,
        scope: SessionScope,
        user_text: str,
        *,
        agent: str | None = None,
    ) -> TurnRecord:
        _validate_session_scope(scope)
        safe_user = self.store.text(_require_string(user_text, "user_text", allow_empty=False))
        with self.store.transaction() as connection:
            self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id)
            seq = _require_positive_integer(
                connection.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
                    (scope.session_id,),
                ).fetchone()[0],
                "next Turn sequence",
            )
            now = _utc_now()
            first_conversation = connection.execute(
                "SELECT 1 FROM turns WHERE session_id=? LIMIT 1",
                (scope.session_id,),
            ).fetchone() is None
            connection.execute(
                """
                INSERT INTO turns(session_id,seq,status,created_at)
                VALUES(?,?,'started',?)
                """,
                (scope.session_id, seq, now),
            )
            connection.execute(
                "UPDATE sessions SET title=CASE WHEN ? THEN ? ELSE title END,updated_at=? WHERE session_id=?",
                (first_conversation, _title_from_user_text(safe_user), now, scope.session_id),
            )
            row = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, seq),
            ).fetchone()
        self.event_log.append(scope, "turn_started", turn_seq=seq)
        self.event_log.append(
            scope,
            "user_message",
            turn_seq=seq,
            agent=agent,
            data={"content": safe_user},
        )
        return _turn(
            row,
            user_text=safe_user,
        )

    def complete_turn(
        self,
        scope: SessionScope,
        seq: int,
        *,
        assistant_text: str,
        assistant_agent: str | None = None,
        workflow_summary: str,
        route: Mapping[str, Any] | None,
        entity_manifests: Sequence[Mapping[str, Any]],
        working_state: Mapping[str, Any],
    ) -> TurnRecord:
        _validate_session_scope(scope)
        _require_positive_integer(seq, "Turn sequence")
        manifests = _validate_manifests(entity_manifests, allow_empty=True)
        if any(manifest["turn_seq"] != seq for manifest in manifests):
            raise ValidationError("entity manifest turn_seq must match the completed Turn")
        state = _validate_working_state(working_state)
        safe_assistant = self.store.text(_require_string(assistant_text, "assistant_text"))
        safe_summary = self.store.text(
            _require_string(workflow_summary, "workflow_summary"), max_bytes=8 * 1024
        )
        safe_route = (
            json.loads(self.store.json(dict(route), max_bytes=8 * 1024))
            if route is not None
            else None
        )
        safe_manifests = self.store.json(manifests, max_bytes=16 * 1024)
        safe_state = self.store.json(state, max_bytes=32 * 1024)
        projected_manifests = json.loads(safe_manifests)
        self._require_started_turn(scope, seq)
        self.event_log.append(
            scope,
            "assistant_message",
            turn_seq=seq,
            agent=assistant_agent,
            data={"content": safe_assistant},
        )
        if safe_route is not None:
            self.event_log.append(
                scope,
                "route_selected",
                turn_seq=seq,
                data={"route": safe_route},
            )
        self.event_log.append(
            scope,
            "workflow_outcome",
            turn_seq=seq,
            data={
                "summary": safe_summary,
                "entity_manifests": projected_manifests,
            },
        )
        with self.store.transaction() as connection:
            self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id)
            row = connection.execute(
                "SELECT status FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, seq),
            ).fetchone()
            if row is None or row["status"] != "started":
                raise StateError("Turn is missing or no longer in started state")
            now = _utc_now()
            connection.execute(
                """
                UPDATE turns SET status='completed',completed_at=?
                WHERE session_id=? AND seq=?
                """,
                (now, scope.session_id, seq),
            )
            connection.execute(
                "UPDATE sessions SET working_state=?,updated_at=? WHERE session_id=?",
                (safe_state, now, scope.session_id),
            )
            row = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, seq),
            ).fetchone()
        self.event_log.append(scope, "turn_completed", turn_seq=seq)
        projection = _event_projection(self.event_log.iter_events(scope))
        return _turn(row, **projection.get(seq, {}))

    def record_model_message(
        self,
        scope: SessionScope,
        message: Mapping[str, Any],
        *,
        turn_seq: int,
        agent: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the same bounded canonical message appended to a live thread."""

        from gitagent.harness.context.messages import canonical_message

        _validate_session_scope(scope)
        _require_positive_integer(turn_seq, "Turn sequence")
        safe = canonical_message(self.store.redact(canonical_message(message)))
        data: dict[str, Any] = {"message": safe}
        if run_id:
            data["run_id"] = _require_string(run_id, "run_id", allow_empty=False)
        self.event_log.append(
            scope,
            "model_message",
            turn_seq=turn_seq,
            agent=_require_string(agent, "agent", allow_empty=False),
            data=data,
        )
        return safe

    def record_message_compaction(
        self,
        scope: SessionScope,
        *,
        turn_seq: int,
        agent: str,
        checkpoint: str = "",
        retain_message_indexes: Sequence[int] | None = None,
        tool_replacements: Sequence[tuple[str, str]] = (),
        run_id: str | None = None,
    ) -> None:
        """Persist the deterministic delta that produced a compacted provider thread."""

        _validate_session_scope(scope)
        _require_positive_integer(turn_seq, "Turn sequence")
        safe_agent = _require_string(agent, "agent", allow_empty=False)
        data: dict[str, Any] = {}
        if checkpoint:
            data["content"] = self.store.text(
                _require_string(checkpoint, "checkpoint", allow_empty=False)
            )
        if retain_message_indexes is not None:
            indexes = []
            previous = -1
            for index in retain_message_indexes:
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or index <= previous
                ):
                    raise ValidationError(
                        "retained message indexes must be unique ascending non-negative integers"
                    )
                indexes.append(index)
                previous = index
            data["retain_message_indexes"] = indexes
        if tool_replacements:
            replacements: list[dict[str, str]] = []
            for call_id, content in tool_replacements:
                replacements.append(
                    {
                        "tool_call_id": _require_string(
                            call_id, "tool_call_id", allow_empty=False
                        ),
                        "content": self.store.text(str(content), max_bytes=4 * 1024),
                    }
                )
            data["tool_replacements"] = replacements
        if run_id:
            data["run_id"] = _require_string(run_id, "run_id", allow_empty=False)
        if not data or set(data) == {"run_id"}:
            return
        self.event_log.append(
            scope,
            "compaction_checkpoint",
            turn_seq=turn_seq,
            agent=safe_agent,
            data=data,
        )

    def fail_turn(self, scope: SessionScope, seq: int, error: str) -> TurnRecord:
        _validate_session_scope(scope)
        _require_positive_integer(seq, "Turn sequence")
        raw_error = _require_string(error, "error", allow_empty=False)
        message = _bounded_characters(self.store.text(raw_error), 500)
        category = _error_category(message)
        self._require_started_turn(scope, seq)
        with self.store.transaction() as connection:
            self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id)
            row = connection.execute(
                "SELECT status FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, seq),
            ).fetchone()
            if row is None or row["status"] != "started":
                raise StateError("Turn is missing or no longer in started state")
            now = _utc_now()
            connection.execute(
                """
                UPDATE turns SET status='failed',completed_at=?
                WHERE session_id=? AND seq=?
                """,
                (now, scope.session_id, seq),
            )
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?",
                (now, scope.session_id),
            )
            row = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, seq),
            ).fetchone()
        self.event_log.append(
            scope,
            "turn_failed",
            turn_seq=seq,
            data={"error_type": category, "message": message},
        )
        projection = _event_projection(self.event_log.iter_events(scope))
        return _turn(row, **projection.get(seq, {}))

    def recover_interrupted(self) -> int:
        connection = self.store.read()
        try:
            interrupted = connection.execute(
                """
                SELECT s.account_key,s.repository_key,t.session_id,t.seq
                FROM turns AS t
                JOIN sessions AS s ON s.session_id=t.session_id
                WHERE t.status='started'
                ORDER BY t.session_id,t.seq
                """
            ).fetchall()
        finally:
            connection.close()
        with self.store.transaction() as connection:
            now = _utc_now()
            cursor = connection.execute(
                "UPDATE turns SET status='interrupted',completed_at=? WHERE status='started'",
                (now,),
            )
            count = _require_non_negative_integer(
                cursor.rowcount, "interrupted Turn count"
            )
        for row in interrupted:
            scope = SessionScope(
                str(row["account_key"]),
                str(row["repository_key"]),
                str(row["session_id"]),
            )
            turn_seq = _require_positive_integer(row["seq"], "Turn sequence")
            self.event_log.append(
                scope,
                "turn_failed",
                turn_seq=turn_seq,
                data={
                    "error_type": "interrupted",
                    "message": "Turn was interrupted before completion",
                    "recovered": True,
                },
            )
        return count

    def recover_event_log(self) -> int:
        """Repair terminal markers inferable from authoritative SQLite state."""

        connection = self.store.read()
        try:
            sessions = connection.execute(
                "SELECT * FROM sessions ORDER BY session_id"
            ).fetchall()
            turns = connection.execute(
                "SELECT * FROM turns ORDER BY session_id,seq"
            ).fetchall()
        finally:
            connection.close()
        turns_by_session: dict[str, list[Any]] = {}
        for row in turns:
            turns_by_session.setdefault(str(row["session_id"]), []).append(row)

        repaired = 0
        for row in sessions:
            session = _session(row)
            scope = session.scope
            events = tuple(self.event_log.iter_events(scope))
            if not events:
                self._record_session_started(session)
                repaired += 1
                events = tuple(self.event_log.iter_events(scope))
            terminal = {
                (event.turn_seq, event.type)
                for event in events
                if event.turn_seq is not None
                and event.type in {"turn_completed", "turn_failed"}
            }
            for turn_row in turns_by_session.get(session.session_id, ()):
                status = str(turn_row["status"])
                seq = _require_positive_integer(turn_row["seq"], "Turn sequence")
                event_type = "turn_completed" if status == "completed" else "turn_failed"
                if status == "started" or (seq, event_type) in terminal:
                    continue
                data: dict[str, Any] = {"recovered": True}
                if event_type == "turn_failed":
                    data.update(
                        {
                            "error_type": status,
                            "message": f"Turn state recovered as {status}",
                        }
                    )
                self.event_log.append(
                    scope,
                    event_type,
                    turn_seq=seq,
                    data=data,
                )
                repaired += 1
        return repaired

    def scope_for_session(self, session_id: str) -> SessionScope | None:
        _validate_session_id(session_id)
        connection = self.store.read()
        try:
            row = connection.execute(
                "SELECT account_key,repository_key FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return SessionScope(
            str(row["account_key"]), str(row["repository_key"]), session_id
        )

    def collect_event_logs(
        self, retention_days: int = 30, *, now: datetime | None = None
    ) -> tuple[Path, ...]:
        connection = self.store.read()
        try:
            scopes = tuple(
                SessionScope(
                    str(row["account_key"]),
                    str(row["repository_key"]),
                    str(row["session_id"]),
                )
                for row in connection.execute(
                    "SELECT account_key,repository_key,session_id FROM sessions"
                )
            )
        finally:
            connection.close()
        return self.event_log.collect_garbage(
            scopes, retention_days=retention_days, now=now
        )

    def list_turns(
        self,
        account_key: str,
        repository_key: str,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> tuple[TurnRecord, ...]:
        account_key, repository_key = _validate_scope_keys(
            account_key, repository_key
        )
        _validate_session_id(session_id)
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
            raise ValidationError("after_seq must be a non-negative integer")
        connection = self.store.read()
        try:
            row = self._session_row_tx(connection, account_key, repository_key, session_id, required=False)
            if row is None:
                return ()
            rows = connection.execute(
                "SELECT * FROM turns WHERE session_id=? AND seq>? ORDER BY seq ASC",
                (session_id, after_seq),
            ).fetchall()
        finally:
            connection.close()
        scope = SessionScope(account_key, repository_key, session_id)
        projection = _event_projection(self.event_log.iter_events(scope))
        return tuple(_turn(item, **projection.get(int(item["seq"]), {})) for item in rows)

    def get_memory_extraction_state(
        self, scope: SessionScope
    ) -> MemoryExtractionState:
        _validate_session_scope(scope)
        connection = self.store.read()
        try:
            self._session_row_tx(
                connection,
                scope.account_key,
                scope.repository_key,
                scope.session_id,
            )
            row = connection.execute(
                "SELECT * FROM memory_extraction_state WHERE session_id=?",
                (scope.session_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return MemoryExtractionState(scope.session_id)
        return MemoryExtractionState(
            session_id=str(row["session_id"]),
            extracted_through_seq=int(row["extracted_through_seq"]),
            pending_through_seq=int(row["pending_through_seq"]),
            updated_at=str(row["updated_at"]),
        )

    def mark_memory_extraction_pending(
        self, scope: SessionScope, through_seq: int
    ) -> MemoryExtractionState:
        _validate_session_scope(scope)
        _require_positive_integer(through_seq, "Memory extraction target")
        with self.store.transaction() as connection:
            self._session_row_tx(
                connection,
                scope.account_key,
                scope.repository_key,
                scope.session_id,
            )
            turn = connection.execute(
                "SELECT status FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, through_seq),
            ).fetchone()
            if turn is None or str(turn["status"]) != "completed":
                raise StateError("Memory extraction target must be a completed Turn")
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO memory_extraction_state(
                    session_id,extracted_through_seq,pending_through_seq,updated_at
                ) VALUES(?,0,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    pending_through_seq=MAX(pending_through_seq,excluded.pending_through_seq),
                    updated_at=excluded.updated_at
                """,
                (scope.session_id, through_seq, now),
            )
            row = connection.execute(
                "SELECT * FROM memory_extraction_state WHERE session_id=?",
                (scope.session_id,),
            ).fetchone()
        return MemoryExtractionState(
            str(row["session_id"]),
            int(row["extracted_through_seq"]),
            int(row["pending_through_seq"]),
            str(row["updated_at"]),
        )

    def complete_memory_extraction(
        self, scope: SessionScope, through_seq: int
    ) -> MemoryExtractionState:
        _validate_session_scope(scope)
        _require_positive_integer(through_seq, "Memory extraction cursor")
        with self.store.transaction() as connection:
            self._session_row_tx(
                connection,
                scope.account_key,
                scope.repository_key,
                scope.session_id,
            )
            row = connection.execute(
                "SELECT * FROM memory_extraction_state WHERE session_id=?",
                (scope.session_id,),
            ).fetchone()
            if row is None or through_seq > int(row["pending_through_seq"]):
                raise StateError("Memory extraction cursor exceeds its pending target")
            now = _utc_now()
            connection.execute(
                """
                UPDATE memory_extraction_state
                SET extracted_through_seq=MAX(extracted_through_seq,?),updated_at=?
                WHERE session_id=?
                """,
                (through_seq, now, scope.session_id),
            )
            row = connection.execute(
                "SELECT * FROM memory_extraction_state WHERE session_id=?",
                (scope.session_id,),
            ).fetchone()
        return MemoryExtractionState(
            str(row["session_id"]),
            int(row["extracted_through_seq"]),
            int(row["pending_through_seq"]),
            str(row["updated_at"]),
        )

    def get_memory_dream_state(
        self, account_key: str, repository_key: str
    ) -> MemoryDreamState:
        account_key, repository_key = _validate_scope_keys(account_key, repository_key)
        connection = self.store.read()
        try:
            row = connection.execute(
                """
                SELECT * FROM memory_dream_state
                WHERE account_key=? AND repository_key=?
                """,
                (account_key, repository_key),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return MemoryDreamState(account_key, repository_key)
        return MemoryDreamState(
            account_key=str(row["account_key"]),
            repository_key=str(row["repository_key"]),
            last_dream_at=str(row["last_dream_at"]),
            last_dream_session_marker=str(row["last_dream_session_marker"]),
            updated_at=str(row["updated_at"]),
        )

    def count_memory_sessions_since(
        self,
        account_key: str,
        repository_key: str,
        marker: str,
    ) -> int:
        account_key, repository_key = _validate_scope_keys(account_key, repository_key)
        marker = _require_string(marker, "Dream session marker")
        connection = self.store.read()
        try:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT s.session_id)
                FROM sessions AS s
                JOIN turns AS t ON t.session_id=s.session_id
                WHERE s.account_key=? AND s.repository_key=?
                  AND t.status='completed' AND (?='' OR t.completed_at>?)
                """,
                (account_key, repository_key, marker, marker),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def complete_memory_dream(
        self,
        account_key: str,
        repository_key: str,
        *,
        completed_at: str,
        session_marker: str,
    ) -> MemoryDreamState:
        account_key, repository_key = _validate_scope_keys(account_key, repository_key)
        completed_at = _require_string(completed_at, "Dream completion time", allow_empty=False)
        session_marker = _require_string(session_marker, "Dream session marker", allow_empty=False)
        try:
            if datetime.fromisoformat(completed_at).tzinfo is None or datetime.fromisoformat(session_marker).tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise ValidationError("Dream timestamps must be timezone-aware ISO-8601 values") from exc
        with self.store.transaction() as connection:
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO memory_dream_state(
                    account_key,repository_key,last_dream_at,
                    last_dream_session_marker,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(account_key,repository_key) DO UPDATE SET
                    last_dream_at=excluded.last_dream_at,
                    last_dream_session_marker=excluded.last_dream_session_marker,
                    updated_at=excluded.updated_at
                """,
                (account_key, repository_key, completed_at, session_marker, now),
            )
        return self.get_memory_dream_state(account_key, repository_key)

    def _insert_session_tx(
        self,
        connection: Any,
        account_key: str,
        repository_key: str,
        repository_full_name: str,
        *,
        session_id: str | None = None,
    ) -> SessionRecord:
        account_key, repository_key = _validate_scope_keys(account_key, repository_key)
        repository_full_name = _require_string(
            repository_full_name,
            "repository_full_name",
            maximum=240,
            allow_empty=False,
        )
        now = _utc_now()
        session_id = session_id or f"session-{uuid.uuid4().hex}"
        session_id = _validate_session_id(session_id)
        working_state = self.store.json(_validate_working_state(default_working_state()), max_bytes=32 * 1024)
        connection.execute(
            """
            INSERT INTO sessions(
                session_id,account_key,repository_key,repository_full_name,title,created_at,updated_at,
                context_boundary_seq,summary,summary_through_seq,working_state,agent_context
            ) VALUES(?,?,?,?,?,?,?,0,'',0,?,'{}')
            """,
            (
                session_id,
                self.store.text(account_key, max_characters=500),
                self.store.text(repository_key, max_characters=500),
                self.store.text(repository_full_name, max_characters=240),
                _default_title(),
                now,
                now,
                working_state,
            ),
        )
        return _session(connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())

    def _record_session_started(self, session: SessionRecord) -> None:
        self.event_log.append(
            session.scope,
            "session_started",
            data={
                "repository_full_name": session.repository_full_name,
                "created_at": session.created_at,
            },
        )

    def _require_started_turn(self, scope: SessionScope, seq: int) -> None:
        connection = self.store.read()
        try:
            self._session_row_tx(
                connection,
                scope.account_key,
                scope.repository_key,
                scope.session_id,
            )
            row = connection.execute(
                "SELECT status FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, seq),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row["status"] != "started":
            raise StateError("Turn is missing or no longer in started state")

    @staticmethod
    def _session_row_tx(
        connection: Any,
        account_key: str,
        repository_key: str,
        session_id: str,
        *,
        required: bool = True,
    ) -> Any:
        row = connection.execute(
            "SELECT * FROM sessions WHERE account_key=? AND repository_key=? AND session_id=?",
            (account_key, repository_key, session_id),
        ).fetchone()
        if row is None and required:
            raise StateError("Session not found")
        return row


def _validate_manifests(value: Sequence[Mapping[str, Any]], *, allow_empty: bool) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationError("entity_manifests must be a list")
    if len(value) > 4:
        raise ValidationError("a Turn can contain at most four entity manifests")
    result: list[dict[str, Any]] = []
    for manifest in value:
        if not isinstance(manifest, Mapping) or set(manifest) != {"turn_seq", "entity_type", "items"}:
            raise ValidationError("entity manifest has an invalid shape")
        items = manifest["items"]
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or len(items) > 20:
            raise ValidationError("entity manifest allows at most 20 items")
        projected = []
        for expected_position, item in enumerate(items, 1):
            if not isinstance(item, Mapping) or set(item) != {"position", "entity_id", "short_label"}:
                raise ValidationError("entity manifest item has an invalid shape")
            position = _require_positive_integer(item["position"], "manifest position")
            if position != expected_position:
                raise ValidationError("manifest positions must be contiguous and match presentation order")
            projected.append(
                {
                    "position": position,
                    "entity_id": _require_string(item["entity_id"], "manifest entity_id", allow_empty=False),
                    "short_label": _require_string(item["short_label"], "manifest short_label", maximum=120),
                }
            )
        result.append(
            {
                "turn_seq": _require_positive_integer(manifest["turn_seq"], "manifest turn_seq"),
                "entity_type": _require_string(manifest["entity_type"], "manifest entity_type", allow_empty=False),
                "items": projected,
            }
        )
    if not allow_empty and not result:
        raise ValidationError("at least one manifest is required")
    return result


def _validate_working_state(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {"version", "goal", "focus", "manifests", "open_question"}
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or not isinstance(value["version"], int)
        or isinstance(value["version"], bool)
        or value["version"] != 4
    ):
        raise ValidationError("working_state has an invalid versioned shape")
    goal = _require_string(value["goal"], "working_state goal", maximum=1000)
    question = _require_string(
        value["open_question"],
        "working_state open_question",
        maximum=OPEN_QUESTION_CHARACTER_LIMIT,
    )
    focus = value["focus"]
    if focus is not None:
        if not isinstance(focus, Mapping) or set(focus) != {"type", "id", "short_label"}:
            raise ValidationError("working_state focus has an invalid shape")
        focus = {
            "type": _require_string(focus["type"], "focus type", allow_empty=False),
            "id": _require_string(focus["id"], "focus id", allow_empty=False),
            "short_label": _require_string(focus["short_label"], "focus short_label", maximum=120),
        }
    manifests = _validate_manifests(value["manifests"], allow_empty=True)
    if len(manifests) > 3:
        raise ValidationError("working_state keeps at most three manifests")
    return {
        "version": 4,
        "goal": goal,
        "focus": focus,
        "manifests": manifests,
        "open_question": question,
    }


def merge_working_state(current: Mapping[str, Any], *, projection: Any) -> dict[str, Any]:
    """Apply one successful Turn projection in dispatch order."""
    state = _validate_working_state(current)
    raw_goals = getattr(projection, "goals", ())
    if not isinstance(raw_goals, Sequence) or isinstance(raw_goals, (str, bytes)):
        raise ValidationError("projection goals must be a sequence")
    goals = [_require_string(goal, "projection goal", maximum=1000, allow_empty=False) for goal in raw_goals]
    if goals:
        combined = "\n".join(f"- {goal}" for goal in goals)
        if len(combined) > 1000:
            raise ValidationError("combined projection goals exceed 1000 characters")
        state["goal"] = combined
    manifests = list(state["manifests"])
    manifests.extend(getattr(projection, "entity_manifests", ()))
    state["manifests"] = manifests[-3:]
    focus = getattr(projection, "focus", None)
    if focus is not None:
        state["focus"] = focus
    raw_question = _require_string(
        getattr(projection, "open_question", None) or "",
        "projection open_question",
    )
    state["open_question"] = _bounded_characters(raw_question, OPEN_QUESTION_CHARACTER_LIMIT)
    return _validate_working_state(state)


def _session(row: Any) -> SessionRecord:
    try:
        session_id = _validate_session_id(row["session_id"])
        account_key, repository_key = _validate_scope_keys(row["account_key"], row["repository_key"])
        repository_full_name = _require_string(
            row["repository_full_name"], "repository_full_name", maximum=240, allow_empty=False
        )
        title = _require_string(row["title"], "title", maximum=80, allow_empty=False)
        created_at = _require_utc_timestamp(row["created_at"], "created_at")
        updated_at = _require_utc_timestamp(row["updated_at"], "updated_at")
        boundary = _require_non_negative_integer(row["context_boundary_seq"], "context_boundary_seq")
        summary = _require_string(row["summary"], "summary")
        if estimate_tokens(summary) > 1500:
            raise ValidationError("stored summary exceeds 1500 estimated tokens")
        through = _require_non_negative_integer(row["summary_through_seq"], "summary_through_seq")
        if through < boundary:
            raise ValidationError("summary_through_seq precedes context_boundary_seq")
        parsed_state = json.loads(_require_string(row["working_state"], "working_state"))
        working_state = _validate_working_state(parsed_state)
        parsed_agent_context = json.loads(_require_string(row["agent_context"], "agent_context"))
        if not isinstance(parsed_agent_context, dict):
            raise ValidationError("stored agent_context must be an object")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise StateError("stored Session record is invalid") from exc
    return SessionRecord(
        session_id=session_id,
        account_key=account_key,
        repository_key=repository_key,
        repository_full_name=repository_full_name,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        context_boundary_seq=boundary,
        summary=summary,
        summary_through_seq=through,
        working_state=working_state,
        agent_context=parsed_agent_context,
    )


def _turn(
    row: Any,
    *,
    user_text: str = "",
    assistant_text: str = "",
    entity_manifests: Sequence[Mapping[str, Any]] = (),
) -> TurnRecord:
    try:
        session_id = _validate_session_id(row["session_id"])
        seq = _require_positive_integer(row["seq"], "Turn sequence")
        status = _require_string(row["status"], "Turn status", allow_empty=False)
        if status not in {"started", "completed", "failed", "interrupted"}:
            raise ValidationError("stored Turn status is invalid")
        user_text = _bounded_stored_text(user_text, "user_text", 32 * 1024)
        assistant_text = _bounded_stored_text(
            assistant_text, "assistant_text", 32 * 1024
        )
        entity_manifests = _validate_manifests(
            entity_manifests, allow_empty=True
        )
        if any(manifest["turn_seq"] != seq for manifest in entity_manifests):
            raise ValidationError("stored entity manifest belongs to another Turn")
        created_at = _require_utc_timestamp(row["created_at"], "created_at")
        completed_at = None
        if row["completed_at"] is not None:
            completed_at = _require_utc_timestamp(row["completed_at"], "completed_at")
        if (status == "started") != (completed_at is None):
            raise ValidationError("stored Turn completion timestamp is inconsistent")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise StateError("stored Turn record is invalid") from exc
    return TurnRecord(
        session_id=session_id,
        seq=seq,
        status=status,
        user_text=user_text,
        assistant_text=assistant_text,
        entity_manifests=entity_manifests,
        created_at=created_at,
        completed_at=completed_at,
    )


def _event_projection(events: Any) -> dict[int, dict[str, Any]]:
    projected: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.turn_seq is None:
            continue
        turn = projected.setdefault(
            event.turn_seq,
            {
                "user_text": "",
                "assistant_text": "",
                "entity_manifests": [],
            },
        )
        if event.type == "user_message":
            content = event.data.get("content", "")
            if isinstance(content, str):
                turn["user_text"] = content
        elif event.type == "assistant_message":
            content = event.data.get("content", "")
            if isinstance(content, str):
                turn["assistant_text"] = content
        elif event.type == "workflow_outcome":
            if "entity_manifests" in event.data:
                manifests = event.data["entity_manifests"]
                if isinstance(manifests, list):
                    turn["entity_manifests"] = manifests
        elif event.type == "turn_failed":
            message = event.data.get("message", "")
            if isinstance(message, str) and message:
                turn["assistant_text"] = message
    return projected


def _validate_scope_keys(account_key: Any, repository_key: Any) -> tuple[str, str]:
    account, account_api = _validate_account_key(account_key)
    repository, repository_api = _validate_repository_key(repository_key)
    if account_api != repository_api:
        raise ValidationError("account_key and repository_key must use the same normalized API URL")
    return account, repository


def _validate_account_key(value: Any) -> tuple[str, str]:
    key = _require_string(value, "account_key", maximum=500, allow_empty=False)
    match = _ACCOUNT_KEY.fullmatch(key)
    if match is None or normalize_api_url(match.group("api")) != match.group("api"):
        raise ValidationError("account_key must use the canonical '<api>#user:<decimal-id>' format")
    return key, match.group("api")


def _validate_repository_key(value: Any) -> tuple[str, str]:
    key = _require_string(value, "repository_key", maximum=500, allow_empty=False)
    match = _REPOSITORY_KEY.fullmatch(key)
    if match is None or normalize_api_url(match.group("api")) != match.group("api"):
        raise ValidationError("repository_key must use the canonical '<api>#repo:<decimal-id>' format")
    return key, match.group("api")


def _validate_session_scope(scope: Any) -> SessionScope:
    if not isinstance(scope, SessionScope):
        raise ValidationError("Session scope has an invalid shape")
    account_key, repository_key = _validate_scope_keys(scope.account_key, scope.repository_key)
    session_id = _validate_session_id(scope.session_id)
    return SessionScope(account_key, repository_key, session_id)


def _validate_session_id(value: Any) -> str:
    session_id = _require_string(value, "session_id", maximum=80, allow_empty=False)
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValidationError("session_id must be an unguessable local ID")
    return session_id


def _require_string(
    value: Any,
    label: str,
    *,
    maximum: int | None = None,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValidationError(f"{label} cannot be empty")
    if maximum is not None and len(value) > maximum:
        raise ValidationError(f"{label} must be at most {maximum} Unicode characters")
    return value


def _require_non_negative_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _require_positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _require_utc_timestamp(value: Any, label: str) -> str:
    timestamp = _require_string(value, label, allow_empty=False)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))  # noqa: FURB162
    except ValueError as exc:
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    return timestamp


def _bounded_stored_text(value: Any, label: str, max_bytes: int) -> str:
    text = _require_string(value, label)
    if len(text.encode("utf-8")) > max_bytes:
        raise ValidationError(f"stored {label} exceeds its byte boundary")
    return text


def _bounded_characters(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    marker = "[TRUNCATED]"
    available = maximum - len(marker)
    head = available // 2
    return value[:head] + marker + value[-(available - head) :]


def _error_category(value: str) -> str:
    candidate = value.partition(":")[0].strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,119}", candidate):
        return candidate
    return "error"


def _positive_decimal(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise ValidationError(f"{label} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        parsed = int(value)
    else:
        raise ValidationError(f"{label} must be a positive integer")
    if parsed < 1:
        raise ValidationError(f"{label} must be a positive integer")
    return str(parsed)


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _default_title() -> str:
    return "新会话（等待首条消息）"


def _title_from_user_text(value: str) -> str:
    title = _normalize_whitespace(value)
    if len(title) <= 60:
        return title
    return f"{title[:59].rstrip()}…"


def estimate_tokens(value: str) -> int:
    return (len(value.encode("utf-8")) + 2) // 3
