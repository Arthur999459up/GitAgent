"""Scoped Session, Turn, Working State, and explicit Memory lifecycle."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from gitagent.domain.errors import StateError, ValidationError
from gitagent.domain.models import SessionScope
from .store import StateStore

USER_MEMORY_LIMIT = 16
USER_MEMORY_TOKEN_LIMIT = 1000
REPOSITORY_MEMORY_LIMIT = 24
REPOSITORY_MEMORY_TOKEN_LIMIT = 1600
OPEN_QUESTION_CHARACTER_LIMIT = 50_000

_ACCOUNT_KEY = re.compile(r"^(?P<api>.+)#user:(?P<id>[1-9][0-9]*)$")
_REPOSITORY_KEY = re.compile(r"^(?P<api>.+)#repo:(?P<id>[1-9][0-9]*)$")
_SESSION_ID = re.compile(r"^session-[0-9a-f]{32}$")
_MEMORY_ID = re.compile(r"^mem-[0-9a-f]{16}$")


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
    history_text: str
    route_summary: list[dict[str, Any]]
    entity_manifests: list[dict[str, Any]]
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    account_key: str
    repository_key: str | None
    scope: str
    kind: str
    content: str
    created_at: str
    updated_at: str


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
    """The only application component allowed to write Session or Memory state."""

    def __init__(self, store: StateStore, *, token_counter: Any = None) -> None:
        self.store = store
        self.token_counter = token_counter or estimate_tokens
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
            return self._insert_session_tx(
                connection,
                account_key,
                repository_key,
                full_name,
                session_id=session_id,
            )

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
            return _session(self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id))

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
            return _session(target)

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
            return replacement

    def start_turn(
        self,
        scope: SessionScope,
        user_text: str,
    ) -> TurnRecord:
        _validate_session_scope(scope)
        safe_user = self.store.text(_require_string(user_text, "user_text", allow_empty=False), max_bytes=8 * 1024)
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
                INSERT INTO turns(session_id,seq,status,user_text,created_at)
                VALUES(?,?,'started',?,?)
                """,
                (scope.session_id, seq, safe_user, now),
            )
            connection.execute(
                "UPDATE sessions SET title=CASE WHEN ? THEN ? ELSE title END,updated_at=? WHERE session_id=?",
                (first_conversation, _title_from_user_text(safe_user), now, scope.session_id),
            )
            return _turn(
                connection.execute(
                    "SELECT * FROM turns WHERE session_id=? AND seq=?",
                    (scope.session_id, seq),
                ).fetchone()
            )

    def complete_turn(
        self,
        scope: SessionScope,
        seq: int,
        *,
        assistant_text: str,
        history_text: str,
        route_summary: Sequence[Mapping[str, Any]],
        entity_manifests: Sequence[Mapping[str, Any]],
        working_state: Mapping[str, Any],
    ) -> TurnRecord:
        _validate_session_scope(scope)
        _require_positive_integer(seq, "Turn sequence")
        routes = _validate_route_summary(route_summary)
        manifests = _validate_manifests(entity_manifests, allow_empty=True)
        if any(manifest["turn_seq"] != seq for manifest in manifests):
            raise ValidationError("entity manifest turn_seq must match the completed Turn")
        state = _validate_working_state(working_state)
        safe_assistant = self.store.text(_require_string(assistant_text, "assistant_text"), max_bytes=8 * 1024)
        safe_history = self.store.text(_require_string(history_text, "history_text"), max_bytes=2 * 1024)
        safe_routes = self.store.json(routes, max_bytes=8 * 1024)
        safe_manifests = self.store.json(manifests, max_bytes=16 * 1024)
        safe_state = self.store.json(state, max_bytes=32 * 1024)
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
                UPDATE turns SET status='completed',assistant_text=?,history_text=?,route_summary=?,
                    entity_manifests=?,completed_at=? WHERE session_id=? AND seq=?
                """,
                (safe_assistant, safe_history, safe_routes, safe_manifests, now, scope.session_id, seq),
            )
            connection.execute(
                "UPDATE sessions SET working_state=?,updated_at=? WHERE session_id=?",
                (safe_state, now, scope.session_id),
            )
            return _turn(
                connection.execute(
                    "SELECT * FROM turns WHERE session_id=? AND seq=?",
                    (scope.session_id, seq),
                ).fetchone()
            )

    def fail_turn(self, scope: SessionScope, seq: int, error: str) -> TurnRecord:
        _validate_session_scope(scope)
        _require_positive_integer(seq, "Turn sequence")
        raw_error = _require_string(error, "error", allow_empty=False)
        message = _bounded_characters(self.store.text(raw_error), 500)
        category = _error_category(message)
        with self.store.transaction() as connection:
            self._session_row_tx(connection, scope.account_key, scope.repository_key, scope.session_id)
            row = connection.execute(
                "SELECT status FROM turns WHERE session_id=? AND seq=?",
                (scope.session_id, seq),
            ).fetchone()
            if row is None or row["status"] != "started":
                raise StateError("Turn is missing or no longer in started state")
            now = _utc_now()
            history = f"failed: {category}"
            connection.execute(
                """
                UPDATE turns SET status='failed',assistant_text=?,history_text=?,completed_at=?
                WHERE session_id=? AND seq=?
                """,
                (message, history, now, scope.session_id, seq),
            )
            connection.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?",
                (now, scope.session_id),
            )
            return _turn(
                connection.execute(
                    "SELECT * FROM turns WHERE session_id=? AND seq=?",
                    (scope.session_id, seq),
                ).fetchone()
            )

    def recover_interrupted(self) -> int:
        with self.store.transaction() as connection:
            now = _utc_now()
            cursor = connection.execute(
                "UPDATE turns SET status='interrupted',completed_at=? WHERE status='started'",
                (now,),
            )
            return _require_non_negative_integer(cursor.rowcount, "interrupted Turn count")

    def list_turns(
        self,
        account_key: str,
        repository_key: str,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> tuple[TurnRecord, ...]:
        _validate_scope_keys(account_key, repository_key)
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
            return tuple(_turn(item) for item in rows)
        finally:
            connection.close()

    def save_summary(
        self,
        account_key: str,
        repository_key: str,
        session_id: str,
        summary: str,
        through_seq: int,
    ) -> SessionRecord:
        _validate_scope_keys(account_key, repository_key)
        _validate_session_id(session_id)
        if not isinstance(through_seq, int) or isinstance(through_seq, bool) or through_seq < 0:
            raise ValidationError("summary_through_seq must be a non-negative integer")
        safe_summary = self.store.text(_require_string(summary, "summary"))
        if self.token_counter(safe_summary) > 1500:
            raise ValidationError("rolling summary exceeds 1500 estimated tokens")
        with self.store.transaction() as connection:
            row = self._session_row_tx(connection, account_key, repository_key, session_id)
            current_through = _require_non_negative_integer(row["summary_through_seq"], "stored summary_through_seq")
            boundary = _require_non_negative_integer(row["context_boundary_seq"], "stored context_boundary_seq")
            if through_seq < current_through:
                raise StateError("summary_through_seq cannot move backwards")
            if through_seq < boundary:
                raise StateError("summary_through_seq cannot precede the Context boundary")
            max_seq = _require_non_negative_integer(
                connection.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM turns WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0],
                "last Turn sequence",
            )
            if through_seq > max_seq:
                raise StateError("summary_through_seq exceeds the last Turn")
            connection.execute(
                "UPDATE sessions SET summary=?,summary_through_seq=?,updated_at=? WHERE session_id=?",
                (safe_summary, through_seq, _utc_now(), session_id),
            )
            return _session(self._session_row_tx(connection, account_key, repository_key, session_id))

    def remember(
        self,
        account_key: str,
        repository_key: str,
        *,
        scope: str,
        kind: str,
        content: str,
    ) -> tuple[MemoryRecord, bool]:
        _validate_scope_keys(account_key, repository_key)
        _validate_memory_type(scope, kind)
        raw_content = _require_string(content, "Memory content", maximum=500, allow_empty=False)
        safe_content = self.store.text(
            raw_content.strip(),
            max_characters=500,
            reject_secrets=True,
        )
        if not safe_content:
            raise ValidationError("Memory content cannot be empty")
        actual_repository = None if scope == "user" else repository_key
        normalized = _normalize_whitespace(safe_content)
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories WHERE account_key=? AND scope=?
                    AND ((? IS NULL AND repository_key IS NULL) OR repository_key=?)
                ORDER BY updated_at DESC, memory_id ASC
                """,
                (account_key, scope, actual_repository, actual_repository),
            ).fetchall()
            for row in rows:
                record = _memory(row)
                if _normalize_whitespace(record.content) == normalized:
                    return record, False
            count_limit = USER_MEMORY_LIMIT if scope == "user" else REPOSITORY_MEMORY_LIMIT
            token_limit = USER_MEMORY_TOKEN_LIMIT if scope == "user" else REPOSITORY_MEMORY_TOKEN_LIMIT
            existing_tokens = sum(self.token_counter(_memory(row).content) for row in rows)
            if len(rows) >= count_limit or existing_tokens + self.token_counter(safe_content) > token_limit:
                raise StateError("Memory capacity reached; use /memory and /forget before adding another item")
            now = _utc_now()
            memory_id = f"mem-{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO memories(memory_id,account_key,repository_key,scope,kind,content,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (memory_id, account_key, actual_repository, scope, kind, safe_content, now, now),
            )
            row = connection.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            return _memory(row), True

    def list_memories(
        self,
        account_key: str,
        repository_key: str,
        scope: str | None = None,
    ) -> tuple[MemoryRecord, ...]:
        _validate_scope_keys(account_key, repository_key)
        if scope not in {None, "user", "repository"}:
            raise ValidationError("Memory scope must be user or repository")
        connection = self.store.read()
        try:
            groups: list[MemoryRecord] = []
            requested = (scope,) if scope else ("user", "repository")
            for item_scope in requested:
                if item_scope == "user":
                    rows = connection.execute(
                        """
                        SELECT * FROM memories WHERE account_key=? AND scope='user' AND repository_key IS NULL
                        ORDER BY updated_at DESC, memory_id ASC
                        """,
                        (account_key,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT * FROM memories WHERE account_key=? AND scope='repository' AND repository_key=?
                        ORDER BY updated_at DESC, memory_id ASC
                        """,
                        (account_key, repository_key),
                    ).fetchall()
                groups.extend(_memory(row) for row in rows)
            return tuple(groups)
        finally:
            connection.close()

    def forget(self, account_key: str, repository_key: str, memory_id: str) -> MemoryRecord | None:
        _validate_scope_keys(account_key, repository_key)
        _validate_memory_id(memory_id)
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM memories WHERE memory_id=? AND account_key=?
                    AND (scope='user' OR (scope='repository' AND repository_key=?))
                """,
                (memory_id, account_key, repository_key),
            ).fetchone()
            if row is None:
                return None
            record = _memory(row)
            connection.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
            return record

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


def _validate_route_summary(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > 4:
        raise ValidationError("route_summary must contain at most four entries")
    keys = {"route", "session_goal", "resolved_references", "workflow_type", "workflow_status"}
    result: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != keys:
            raise ValidationError("route_summary entry has an invalid shape")
        references = entry["resolved_references"]
        if not isinstance(references, Sequence) or isinstance(references, (str, bytes)) or len(references) > 8:
            raise ValidationError("route_summary resolved_references is invalid")
        normalized_references = []
        for reference in references:
            if not isinstance(reference, Mapping) or set(reference) != {"type", "id"}:
                raise ValidationError("route_summary reference has an invalid shape")
            normalized_references.append({
                "type": _require_string(reference["type"], "reference type", allow_empty=False),
                "id": _require_string(reference["id"], "reference id", allow_empty=False),
            })
        workflow_type = entry["workflow_type"]
        workflow_status = entry["workflow_status"]
        if (workflow_type is None) != (workflow_status is None):
            raise ValidationError("workflow_type and workflow_status must both be strings or both be null")
        if workflow_type is not None:
            workflow_type = _require_string(workflow_type, "workflow_type", allow_empty=False)
            workflow_status = _require_string(workflow_status, "workflow_status", allow_empty=False)
        result.append({
            "route": _require_string(entry["route"], "route", allow_empty=False),
            "session_goal": _require_string(entry["session_goal"], "session_goal", maximum=1000),
            "resolved_references": normalized_references,
            "workflow_type": workflow_type,
            "workflow_status": workflow_status,
        })
    return result


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


def _turn(row: Any) -> TurnRecord:
    try:
        session_id = _validate_session_id(row["session_id"])
        seq = _require_positive_integer(row["seq"], "Turn sequence")
        status = _require_string(row["status"], "Turn status", allow_empty=False)
        if status not in {"started", "completed", "failed", "interrupted"}:
            raise ValidationError("stored Turn status is invalid")
        user_text = _bounded_stored_text(row["user_text"], "user_text", 8 * 1024)
        assistant_text = _bounded_stored_text(row["assistant_text"], "assistant_text", 8 * 1024)
        history_text = _bounded_stored_text(row["history_text"], "history_text", 2 * 1024)
        route_text = _bounded_stored_text(row["route_summary"], "route_summary", 8 * 1024)
        manifest_text = _bounded_stored_text(row["entity_manifests"], "entity_manifests", 16 * 1024)
        route_summary = _validate_route_summary(json.loads(route_text))
        entity_manifests = _validate_manifests(json.loads(manifest_text), allow_empty=True)
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
        history_text=history_text,
        route_summary=route_summary,
        entity_manifests=entity_manifests,
        created_at=created_at,
        completed_at=completed_at,
    )


def _memory(row: Any) -> MemoryRecord:
    try:
        memory_id = _validate_memory_id(row["memory_id"])
        account_key, account_api = _validate_account_key(row["account_key"])
        scope = _require_string(row["scope"], "Memory scope", allow_empty=False)
        kind = _require_string(row["kind"], "Memory kind", allow_empty=False)
        _validate_memory_type(scope, kind)
        repository_key = None
        if row["repository_key"] is not None:
            repository_key, repository_api = _validate_repository_key(row["repository_key"])
            if repository_api != account_api:
                raise ValidationError("stored Memory crosses API scope")
        if (scope == "user") != (repository_key is None):
            raise ValidationError("stored Memory scope is inconsistent")
        content = _require_string(row["content"], "Memory content", maximum=500, allow_empty=False)
        created_at = _require_utc_timestamp(row["created_at"], "created_at")
        updated_at = _require_utc_timestamp(row["updated_at"], "updated_at")
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise StateError("stored Memory record is invalid") from exc
    return MemoryRecord(
        memory_id=memory_id,
        account_key=account_key,
        repository_key=repository_key,
        scope=scope,
        kind=kind,
        content=content,
        created_at=created_at,
        updated_at=updated_at,
    )


def _validate_memory_type(scope: str, kind: str) -> None:
    if (scope, kind) == ("user", "preference"):
        return
    if scope == "repository" and kind in {"decision", "constraint", "reference"}:
        return
    raise ValidationError("invalid Memory scope/kind combination")


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


def _validate_memory_id(value: Any) -> str:
    memory_id = _require_string(value, "memory_id", allow_empty=False)
    if _MEMORY_ID.fullmatch(memory_id) is None:
        raise ValidationError("memory_id has an invalid shape")
    return memory_id


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
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
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
    return datetime.now(timezone.utc).isoformat()


def _default_title() -> str:
    return "新会话（等待首条消息）"


def _title_from_user_text(value: str) -> str:
    title = _normalize_whitespace(value)
    if len(title) <= 60:
        return title
    return f"{title[:59].rstrip()}…"


def estimate_tokens(value: str) -> int:
    return (len(value.encode("utf-8")) + 2) // 3
