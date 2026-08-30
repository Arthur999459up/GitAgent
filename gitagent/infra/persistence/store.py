"""Secure single-process SQLite boundary for transactional Session state."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gitagent.domain.errors import StateError, ValidationError

SCHEMA_VERSION = 9
REDACTED = "[REDACTED]"

_SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"-----BEGIN (?P<kind>(?:(?:RSA|DSA|EC|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----.*?"
        r"(?:-----END (?P=kind)-----|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<key_quote>['\"]?)(?P<key>api_key|access_token|secret|password)"
    r"(?P=key_quote)\s*(?P<separator>[:=])\s*"
    r"(?:(?P<quote>['\"])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\r\n]){8,})(?P=quote)"
    r"|(?P<bare>[^\s,;'\"]{8,}))"
)
_SECRET_FIELD_NAMES = {"api_key", "access_token", "secret", "password"}

_TABLE_DEFINITIONS = {
    "schema_metadata": """
        CREATE TABLE schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """,
    "sessions": """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            account_key TEXT NOT NULL,
            repository_key TEXT NOT NULL,
            repository_full_name TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            context_boundary_seq INTEGER NOT NULL DEFAULT 0 CHECK(context_boundary_seq >= 0),
            summary TEXT NOT NULL DEFAULT '',
            summary_through_seq INTEGER NOT NULL DEFAULT 0 CHECK(summary_through_seq >= 0),
            working_state TEXT NOT NULL,
            agent_context TEXT NOT NULL DEFAULT '{}'
        )
    """,
    "turns": """
        CREATE TABLE turns (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL CHECK(seq >= 1),
            status TEXT NOT NULL CHECK(status IN ('started','completed','failed','interrupted')),
            created_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY(session_id, seq),
            FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """,
    "memory_extraction_state": """
        CREATE TABLE memory_extraction_state (
            session_id TEXT PRIMARY KEY,
            extracted_through_seq INTEGER NOT NULL DEFAULT 0 CHECK(extracted_through_seq >= 0),
            pending_through_seq INTEGER NOT NULL DEFAULT 0 CHECK(pending_through_seq >= extracted_through_seq),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """,
    "memory_dream_state": """
        CREATE TABLE memory_dream_state (
            account_key TEXT NOT NULL,
            repository_key TEXT NOT NULL,
            last_dream_at TEXT NOT NULL DEFAULT '',
            last_dream_session_marker TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key, repository_key)
        )
    """,
}

_INDEX_DEFINITIONS = {
    "turns_by_session_status": "CREATE INDEX turns_by_session_status ON turns(session_id, status, seq)",
    "sessions_by_scope_updated": (
        "CREATE INDEX sessions_by_scope_updated ON sessions(account_key, repository_key, updated_at DESC)"
    ),
    "sessions_by_account_updated": (
        "CREATE INDEX sessions_by_account_updated ON sessions(account_key, updated_at DESC)"
    ),
}


class StateStore:
    """Own the database path, schema, transactions, and final redaction boundary."""

    def __init__(
        self, path: str | os.PathLike[str], *, secret_values: Sequence[str] = ()
    ) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise ValidationError("GITAGENT_STATE_PATH must be an absolute path")
        if isinstance(secret_values, (str, bytes)) or not isinstance(
            secret_values, Sequence
        ):
            raise ValidationError("secret_values must be a sequence of strings")
        if any(not isinstance(value, str) for value in secret_values):
            raise ValidationError("secret_values must contain only strings")
        self.secret_values = tuple(
            sorted(
                {value for value in secret_values if len(value) >= 8},
                key=len,
                reverse=True,
            )
        )
        self._secure_path()
        self._initialize()

    @contextmanager
    def transaction(self) -> Iterator[_SanitizedTransaction]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield _SanitizedTransaction(self, connection)
            connection.commit()
        except sqlite3.Error as exc:
            self._rollback(connection)
            raise StateError("state transaction failed") from exc
        except BaseException:
            self._rollback(connection)
            raise
        finally:
            connection.close()

    def read(self) -> sqlite3.Connection:
        """Return a configured read connection; callers must close it explicitly."""
        self._verify_existing_database()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=30.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA query_only=ON")
            self._verify_existing_database()
            return connection
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise StateError("cannot open the state connection read-only") from exc

    def redact(self, value: Any, *, reject_secrets: bool = False) -> Any:
        """Recursively sanitize every string before it crosses the SQL write boundary."""
        if not isinstance(reject_secrets, bool):
            raise ValidationError("reject_secrets must be a boolean")
        if isinstance(value, str):
            redacted, found = self._redact_text(value)
            if reject_secrets and found:
                raise ValidationError(
                    "Memory content appears to contain a credential and was not saved"
                )
            return redacted
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValidationError("state values cannot contain non-finite numbers")
            return value
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValidationError("state object keys must be strings")
                safe_key = self.redact(key, reject_secrets=reject_secrets)
                if safe_key in result:
                    raise ValidationError(
                        "redaction produced duplicate state object keys"
                    )
                if (
                    key.strip().casefold() in _SECRET_FIELD_NAMES
                    and _contains_secret_field_value(item)
                ):
                    if reject_secrets:
                        raise ValidationError(
                            "Memory content appears to contain a credential and was not saved"
                        )
                    result[safe_key] = REDACTED
                else:
                    result[safe_key] = self.redact(item, reject_secrets=reject_secrets)
            return result
        if isinstance(value, tuple):
            return tuple(
                self.redact(item, reject_secrets=reject_secrets) for item in value
            )
        if isinstance(value, list):
            return [self.redact(item, reject_secrets=reject_secrets) for item in value]
        raise ValidationError(f"unsupported state value type: {type(value).__name__}")

    def text(
        self,
        value: str,
        *,
        max_bytes: int | None = None,
        max_characters: int | None = None,
        reject_secrets: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise ValidationError("state text values must be strings")
        if max_bytes is not None and (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ValidationError("max_bytes must be a positive integer")
        if max_characters is not None and (
            isinstance(max_characters, bool)
            or not isinstance(max_characters, int)
            or max_characters < 0
        ):
            raise ValidationError("max_characters must be a non-negative integer")
        text = self.redact(value, reject_secrets=reject_secrets)
        if max_characters is not None and len(text) > max_characters:
            raise ValidationError(
                f"text must be at most {max_characters} Unicode characters"
            )
        return truncate_utf8(text, max_bytes) if max_bytes is not None else text

    def json(
        self,
        value: Any,
        *,
        max_bytes: int,
        reject_secrets: bool = False,
    ) -> str:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ValidationError("max_bytes must be a positive integer")
        sanitized = self.redact(value, reject_secrets=reject_secrets)
        _validate_json_value(sanitized)
        try:
            encoded = json.dumps(
                sanitized,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "state projection must contain only JSON-compatible values"
            ) from exc
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValidationError(
                f"JSON projection exceeds the {max_bytes}-byte storage boundary"
            )
        return encoded

    def _redact_text(self, value: str) -> tuple[str, bool]:
        text = value
        found = False
        for secret in self.secret_values:
            if secret in text:
                text = text.replace(secret, REDACTED)
                found = True
        for pattern in _SECRET_PATTERNS:
            text, count = pattern.subn(REDACTED, text)
            found = found or count > 0

        def replace_assignment(match: re.Match[str]) -> str:
            nonlocal found
            found = True
            key_quote = match.group("key_quote")
            return f"{key_quote}{match.group('key')}{key_quote}{match.group('separator')}{REDACTED}"

        text = _SECRET_ASSIGNMENT.sub(replace_assignment, text)
        return text, found

    def _secure_path(self) -> None:
        parent = self.path.parent
        if os.name == "posix":
            for candidate in (parent, *parent.parents):
                self._reject_symlink(candidate, "state path")
            self._reject_symlink(parent, "state directory")
            if parent.exists():
                self._require_owned(parent, "state directory")
            else:
                try:
                    parent.mkdir(mode=0o700, parents=True)
                except OSError as exc:
                    raise StateError(
                        f"cannot create state directory: {parent}"
                    ) from exc
            self._reject_symlink(parent, "state directory")
            self._require_owned(parent, "state directory")
            self._enforce_mode(parent, 0o700, "state directory")
            if self.path.exists() or self.path.is_symlink():
                self._reject_symlink(self.path, "state database")
                self._require_owned(self.path, "state database")
                if not self.path.is_file():
                    raise StateError("state database path is not a regular file")
                self._enforce_mode(self.path, 0o600, "state database")
        else:  # pragma: no cover - POSIX is exercised in CI
            try:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise StateError(f"cannot create state directory: {parent}") from exc

    @staticmethod
    def _reject_symlink(path: Path, label: str) -> None:
        if path.is_symlink():
            raise StateError(f"{label} must not be a symbolic link")

    @staticmethod
    def _require_owned(path: Path, label: str) -> None:
        try:
            owner = path.stat(follow_symlinks=False).st_uid
        except OSError as exc:
            raise StateError(f"cannot inspect {label}") from exc
        if owner != os.getuid():
            raise StateError(f"{label} is not owned by the current user")

    @staticmethod
    def _enforce_mode(path: Path, mode: int, label: str) -> None:
        try:
            os.chmod(path, mode, follow_symlinks=False)
            actual = path.stat(follow_symlinks=False).st_mode & 0o777
        except OSError as exc:
            raise StateError(
                f"cannot enforce {mode:04o} permissions on {label}"
            ) from exc
        if actual != mode:
            raise StateError(f"cannot enforce {mode:04o} permissions on {label}")

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            self._verify_existing_database()
            connection = sqlite3.connect(self.path, timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).casefold() != "delete":
                raise StateError("SQLite journal_mode=DELETE could not be enforced")
            if os.name == "posix":
                self._reject_symlink(self.path, "state database")
                self._require_owned(self.path, "state database")
                if not self.path.is_file():
                    raise StateError("state database path is not a regular file")
                self._enforce_mode(self.path, 0o600, "state database")
            return connection
        except StateError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise StateError(f"cannot open state database: {self.path}") from exc

    def _verify_existing_database(self) -> None:
        if os.name != "posix" or not (self.path.exists() or self.path.is_symlink()):
            return
        self._reject_symlink(self.path, "state database")
        self._require_owned(self.path, "state database")
        if not self.path.is_file():
            raise StateError("state database path is not a regular file")

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise StateError(f"state database integrity check failed: {integrity}")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if not str(row[0]).startswith("sqlite_")
            }
            if not tables:
                connection.execute("BEGIN IMMEDIATE")
                for definition in _TABLE_DEFINITIONS.values():
                    connection.execute(definition)
                for definition in _INDEX_DEFINITIONS.values():
                    connection.execute(definition)
                connection.execute(
                    "INSERT INTO schema_metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                if "schema_metadata" not in tables:
                    raise StateError("state database has no recognized schema metadata")
                row = connection.execute(
                    "SELECT value FROM schema_metadata WHERE key='schema_version'"
                ).fetchone()
                if row is not None and str(row[0]) == "8":
                    self._migrate_v8(connection, tables)
                elif row is None or str(row[0]) != str(SCHEMA_VERSION):
                    raise StateError(
                        "state database schema is incompatible with this GitAgent version; recreate the state database"
                    )
            self._validate_schema(connection)
            if connection.in_transaction:
                connection.commit()
        except (sqlite3.DatabaseError, OSError) as exc:
            if connection.in_transaction:
                connection.rollback()
            raise StateError(
                "state database is corrupt or cannot be initialized"
            ) from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_v8(connection: sqlite3.Connection, tables: set[str]) -> None:
        """Add Memory coordinator state without discarding existing Sessions."""

        legacy_tables = {"schema_metadata", "sessions", "turns"}
        if tables != legacy_tables:
            raise StateError("state database v8 schema contains unexpected tables")
        indexes = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'
                """
            )
        }
        if indexes != set(_INDEX_DEFINITIONS):
            raise StateError("state database v8 schema indexes are invalid")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_TABLE_DEFINITIONS["memory_extraction_state"])
        connection.execute(_TABLE_DEFINITIONS["memory_dream_state"])
        connection.execute(
            "UPDATE schema_metadata SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        table_rows = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table'"
            )
            if not str(row[0]).startswith("sqlite_")
        }
        tables = set(table_rows)
        expected_tables = set(_TABLE_DEFINITIONS)
        if tables != expected_tables:
            missing = expected_tables - tables
            extra = tables - expected_tables
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unexpected {', '.join(sorted(extra))}")
            label = "incomplete" if missing else "invalid"
            raise StateError(f"state database schema is {label}: {'; '.join(details)}")
        unexpected_objects = connection.execute(
            """
            SELECT type,name FROM sqlite_master
            WHERE type IN ('trigger','view')
            ORDER BY type,name
            """
        ).fetchall()
        if unexpected_objects:
            raise StateError(
                "state database schema contains unsupported triggers or views"
            )
        for table, definition in _TABLE_DEFINITIONS.items():
            if _canonical_sql(table_rows[table]) != _canonical_sql(definition):
                raise StateError(
                    f"state database schema definition for {table} is invalid"
                )
        index_rows = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                """
                SELECT name,sql FROM sqlite_master
                WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'
                """
            )
        }
        if set(index_rows) != set(_INDEX_DEFINITIONS):
            raise StateError("state database schema indexes are invalid")
        for index, definition in _INDEX_DEFINITIONS.items():
            if _canonical_sql(index_rows[index]) != _canonical_sql(definition):
                raise StateError(f"state database schema index {index} is invalid")
        metadata = connection.execute(
            "SELECT key,value FROM schema_metadata ORDER BY key"
        ).fetchall()
        if [tuple(row) for row in metadata] != [
            ("schema_version", str(SCHEMA_VERSION))
        ]:
            raise StateError("state database schema metadata is invalid")
        foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise StateError("state database contains invalid foreign-key references")


class _SanitizedTransaction:
    """Minimal SQL facade that redacts every bound string before a write can occur."""

    __slots__ = ("__connection", "__store")

    def __init__(self, store: StateStore, connection: sqlite3.Connection) -> None:
        self.__store = store
        self.__connection = connection

    def execute(self, sql: str, parameters: Any = ()) -> _SanitizedCursor:
        if not isinstance(sql, str):
            raise ValidationError("SQL statements must be strings")
        _, contains_secret = self.__store._redact_text(sql)
        if contains_secret:
            raise ValidationError(
                "SQL text must not contain credentials; use bound parameters"
            )
        cursor = self.__connection.execute(sql, self.__store.redact(parameters))
        return _SanitizedCursor(cursor)

    def executemany(self, sql: str, parameters: Any) -> _SanitizedCursor:
        if not isinstance(sql, str):
            raise ValidationError("SQL statements must be strings")
        _, contains_secret = self.__store._redact_text(sql)
        if contains_secret:
            raise ValidationError(
                "SQL text must not contain credentials; use bound parameters"
            )
        if isinstance(parameters, (str, bytes)) or not isinstance(parameters, Sequence):
            raise ValidationError("SQL parameter batches must be a sequence")
        sanitized = [self.__store.redact(item) for item in parameters]
        return _SanitizedCursor(self.__connection.executemany(sql, sanitized))


class _SanitizedCursor:
    """Read-only cursor view that cannot issue unsanitized follow-up writes."""

    __slots__ = ("__cursor",)

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.__cursor = cursor

    @property
    def rowcount(self) -> int:
        return self.__cursor.rowcount

    def fetchone(self) -> sqlite3.Row | None:
        return self.__cursor.fetchone()

    def fetchall(self) -> list[sqlite3.Row]:
        return self.__cursor.fetchall()

    def __iter__(self) -> Iterator[sqlite3.Row]:
        return iter(self.__cursor)


def _contains_secret_field_value(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) >= 8
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return len(str(value)) >= 8
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return bool(value)
    return True


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("state JSON cannot contain non-finite numbers")
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValidationError("state JSON object keys must be strings")
        for item in value.values():
            _validate_json_value(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    raise ValidationError(f"unsupported state JSON value type: {type(value).__name__}")


def _canonical_sql(value: str) -> str:
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is None:
            if character in {"'", '"', "`"}:
                quote = character
                result.append(character)
            elif character == "[":
                quote = "]"
                result.append(character)
            elif not character.isspace():
                result.append(character)
            index += 1
            continue
        result.append(character)
        if character == quote:
            if index + 1 < len(value) and value[index + 1] == quote:
                result.append(value[index + 1])
                index += 2
                continue
            quote = None
        index += 1
    return "".join(result)


def truncate_utf8(value: str, max_bytes: int | None) -> str:
    if not isinstance(value, str):
        raise ValidationError("truncated state values must be strings")
    if max_bytes is None:
        return value
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValidationError("max_bytes must be a positive integer")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker_template = "[TRUNCATED redacted_bytes={}]"
    if max_bytes < len(marker_template.format(0).encode("utf-8")):
        raise ValidationError("max_bytes is too small for the truncation marker")
    removed = len(encoded) - max_bytes
    while True:
        marker = marker_template.format(max(0, removed)).encode("utf-8")
        available = max(0, max_bytes - len(marker))
        head_budget = available // 2
        tail_budget = available - head_budget
        head = encoded[:head_budget].decode("utf-8", errors="ignore").encode("utf-8")
        tail = (
            encoded[-tail_budget:].decode("utf-8", errors="ignore").encode("utf-8")
            if tail_budget
            else b""
        )
        actual_removed = len(encoded) - len(head) - len(tail)
        if actual_removed == removed:
            return (head + marker + tail).decode("utf-8")
        removed = actual_removed
