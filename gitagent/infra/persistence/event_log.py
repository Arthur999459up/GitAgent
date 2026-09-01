"""Secure append-only JSONL history for observable Session events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from gitagent.domain.errors import StateError, ValidationError
from gitagent.domain.models import SessionEvent, SessionScope

EVENT_SCHEMA_VERSION = 1
DEFAULT_MAX_EVENT_BYTES = 4 * 1024 * 1024
DEFAULT_ARGUMENT_BYTES = 16 * 1024
DEFAULT_CONTENT_BYTES = 16 * 1024
_OMITTED = "\n… content omitted …\n"
_SESSION_ID = re.compile(r"^session-[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class SessionEventLog:
    """Own paths, sequencing, redaction, bounds, durability, and replay."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        redactor: Callable[[Any], Any],
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        fsync: bool = True,
    ) -> None:
        self.root = Path(root).expanduser()
        if not self.root.is_absolute():
            raise ValidationError("Session event root must be an absolute path")
        if not callable(redactor):
            raise ValidationError("Session event redactor must be callable")
        if (
            not isinstance(max_event_bytes, int)
            or isinstance(max_event_bytes, bool)
            or max_event_bytes < 1024
        ):
            raise ValidationError("max_event_bytes must be an integer of at least 1024")
        if not isinstance(fsync, bool):
            raise ValidationError("fsync must be a boolean")
        self._redact = redactor
        self.max_event_bytes = max_event_bytes
        self.fsync = fsync
        self._locks: dict[Path, Lock] = {}
        self._heads: dict[Path, tuple[int, tuple[int, int]]] = {}
        self._locks_guard = Lock()
        self._secure_directory(self.root)

    def path_for(self, scope: SessionScope) -> Path:
        scope = _scope(scope)
        return (
            self.root
            / _key_hash(scope.account_key)
            / _key_hash(scope.repository_key)
            / f"{scope.session_id}.jsonl"
        )

    def append(
        self,
        scope: SessionScope,
        event_type: str,
        *,
        turn_seq: int | None = None,
        agent: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> SessionEvent:
        scope = _scope(scope)
        event_type = _non_empty_text(event_type, "event type", maximum=80)
        if turn_seq is not None and (
            not isinstance(turn_seq, int) or isinstance(turn_seq, bool) or turn_seq < 1
        ):
            raise ValidationError("event turn_seq must be a positive integer or null")
        if agent is not None:
            agent = _non_empty_text(agent, "event agent", maximum=120)
        if data is not None and not isinstance(data, Mapping):
            raise ValidationError("event data must be an object")

        safe_data = self._redact(dict(data or {}))
        if not isinstance(safe_data, dict) or any(
            not isinstance(key, str) for key in safe_data
        ):
            raise ValidationError(
                "event redactor must return an object with string keys"
            )
        safe_data = _bound_payload(event_type, safe_data)
        path = self.path_for(scope)
        lock = self._lock_for(path)
        with lock:
            self._secure_directory(path.parent)
            signature = self._file_signature(path)
            cached = self._heads.get(path)
            if cached is not None and cached[1] == signature:
                last_seq = cached[0]
            else:
                events, _ = self._read_locked(scope, repair_tail=True)
                last_seq = events[-1].seq if events else 0
            event = SessionEvent(
                version=EVENT_SCHEMA_VERSION,
                seq=last_seq + 1,
                type=event_type,
                time=_utc_now(),
                session_id=scope.session_id,
                turn_seq=turn_seq,
                agent=agent,
                data=safe_data,
            )
            line = _encode_event(event)
            if len(line) > self.max_event_bytes:
                raise ValidationError(
                    f"Session event exceeds the {self.max_event_bytes}-byte storage boundary"
                )
            self._append_line(path, line)
            signature = self._file_signature(path)
            if signature is None:  # pragma: no cover - append just created it
                raise StateError("Session event file disappeared after append")
            self._heads[path] = (event.seq, signature)
            return event

    def iter_events(
        self, scope: SessionScope, *, after_seq: int = 0
    ) -> Iterator[SessionEvent]:
        scope = _scope(scope)
        if (
            not isinstance(after_seq, int)
            or isinstance(after_seq, bool)
            or after_seq < 0
        ):
            raise ValidationError("after_seq must be a non-negative integer")
        path = self.path_for(scope)
        with self._lock_for(path):
            events, damaged = self._read_locked(scope, repair_tail=False)
            signature = self._file_signature(path)
            if signature is not None and not damaged:
                self._heads[path] = (events[-1].seq if events else 0, signature)
            else:
                self._heads.pop(path, None)
        return iter(tuple(event for event in events if event.seq > after_seq))

    def last_seq(self, scope: SessionScope) -> int:
        scope = _scope(scope)
        path = self.path_for(scope)
        with self._lock_for(path):
            events, damaged = self._read_locked(scope, repair_tail=False)
            signature = self._file_signature(path)
            if signature is not None and not damaged:
                self._heads[path] = (events[-1].seq if events else 0, signature)
            else:
                self._heads.pop(path, None)
        return events[-1].seq if events else 0

    def delete(self, scope: SessionScope) -> bool:
        scope = _scope(scope)
        path = self.path_for(scope)
        with self._lock_for(path):
            self._reject_symlink(path, "Session event file")
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise StateError("cannot delete the Session event log") from exc
            self._heads.pop(path, None)
            self._prune_empty_parents(path.parent)
            return True

    def collect_garbage(
        self,
        active_scopes: Iterable[SessionScope],
        *,
        retention_days: int = 30,
        now: datetime | None = None,
    ) -> tuple[Path, ...]:
        if (
            not isinstance(retention_days, int)
            or isinstance(retention_days, bool)
            or retention_days < 0
        ):
            raise ValidationError("retention_days must be a non-negative integer")
        active = {self.path_for(scope) for scope in active_scopes}
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValidationError("GC timestamp must be timezone-aware")
        cutoff = current.timestamp() - timedelta(days=retention_days).total_seconds()
        removed: list[Path] = []
        self._secure_directory(self.root)
        for path in self.root.glob("*/*/session-*.jsonl"):
            if path in active or not _managed_path(self.root, path):
                continue
            self._reject_symlink(path, "Session event file")
            try:
                expired = path.stat(follow_symlinks=False).st_mtime <= cutoff
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StateError("cannot inspect a Session event log for GC") from exc
            if not expired:
                continue
            with self._lock_for(path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise StateError(
                        "cannot remove an expired Session event log"
                    ) from exc
                self._heads.pop(path, None)
                removed.append(path)
                self._prune_empty_parents(path.parent)
        return tuple(sorted(removed))

    def _read_locked(
        self, scope: SessionScope, *, repair_tail: bool
    ) -> tuple[list[SessionEvent], bool]:
        path = self.path_for(scope)
        self._reject_symlink(path, "Session event file")
        try:
            descriptor = os.open(path, os.O_RDONLY | _no_follow())
        except FileNotFoundError:
            return [], False
        except OSError as exc:
            raise StateError("cannot open the Session event log") from exc

        events: list[SessionEvent] = []
        valid_bytes = 0
        damaged_tail = False
        try:
            size = os.fstat(descriptor).st_size
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                expected = 1
                while handle.tell() < size:
                    start = handle.tell()
                    line = handle.readline(self.max_event_bytes + 1)
                    at_end = handle.tell() == size
                    if len(line) > self.max_event_bytes:
                        raise StateError(
                            "Session event log contains an oversized event"
                        )
                    if not line.endswith(b"\n"):
                        if at_end:
                            damaged_tail = True
                            break
                        raise StateError(
                            "Session event log contains an unterminated middle line"
                        )
                    try:
                        raw = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        if at_end:
                            damaged_tail = True
                            break
                        raise StateError(
                            "Session event log contains a corrupt middle line"
                        ) from exc
                    event = _decode_event(raw, scope, expected)
                    events.append(event)
                    expected += 1
                    valid_bytes = handle.tell()
                    if valid_bytes <= start:
                        raise StateError("Session event reader made no progress")
        except OSError as exc:
            raise StateError("cannot read the Session event log") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if damaged_tail:
            warnings.warn(
                f"ignored interrupted final event in {path}",
                RuntimeWarning,
                stacklevel=3,
            )
            if repair_tail:
                self._truncate(path, valid_bytes)
        return events, damaged_tail

    def _append_line(self, path: Path, line: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _no_follow()
        try:
            descriptor = os.open(path, flags, 0o600)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab", closefd=True) as handle:
                handle.write(line)
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise StateError("cannot append the Session event log") from exc

    def _truncate(self, path: Path, size: int) -> None:
        try:
            descriptor = os.open(path, os.O_WRONLY | _no_follow())
            try:
                os.ftruncate(descriptor, size)
                if self.fsync:
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise StateError("cannot repair the interrupted Session event log") from exc

    def _lock_for(self, path: Path) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(path, Lock())

    def _file_signature(self, path: Path) -> tuple[int, int] | None:
        self._reject_symlink(path, "Session event file")
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateError("cannot inspect the Session event file") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise StateError("Session event path is not a regular file")
        if os.name == "posix" and metadata.st_uid != os.getuid():
            raise StateError("Session event file is not owned by the current user")
        return metadata.st_size, metadata.st_mtime_ns

    def _secure_directory(self, path: Path) -> None:
        if os.name != "posix":  # pragma: no cover - POSIX is exercised in CI
            try:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise StateError("cannot create the Session event directory") from exc
            return
        for candidate in (path, *path.parents):
            self._reject_symlink(candidate, "Session event path")
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.stat(follow_symlinks=False).st_uid != os.getuid():
                raise StateError(
                    "Session event directory is not owned by the current user"
                )
            os.chmod(path, 0o700, follow_symlinks=False)
        except StateError:
            raise
        except OSError as exc:
            raise StateError("cannot secure the Session event directory") from exc

    @staticmethod
    def _reject_symlink(path: Path, label: str) -> None:
        if path.is_symlink():
            raise StateError(f"{label} must not be a symbolic link")

    def _prune_empty_parents(self, path: Path) -> None:
        while path != self.root:
            try:
                path.rmdir()
            except OSError:
                return
            path = path.parent


class SessionEventRecorder:
    """Project the stable subset of live trace events into durable history."""

    def __init__(
        self,
        event_log: SessionEventLog,
        scope_resolver: Callable[[str], SessionScope | None],
    ) -> None:
        self.event_log = event_log
        self.scope_resolver = scope_resolver

    def __call__(self, trace_event: Any) -> None:
        scope = self.scope_resolver(str(trace_event.session_id))
        if scope is None:
            return
        category = _enum_value(trace_event.category)
        status = _enum_value(trace_event.status)
        details = dict(trace_event.details or {})
        turn_seq = trace_event.turn_seq

        if category == "capability":
            phase = str(details.get("event", ""))
            if phase == "call.started":
                self.event_log.append(
                    scope,
                    "tool_call",
                    turn_seq=turn_seq,
                    agent=_optional_text(details.get("agent")),
                    data={
                        "tool": str(trace_event.name),
                        "call_id": str(details.get("call_id", "")),
                        "arguments": _json_compatible(details.get("arguments", {})),
                    },
                )
            elif phase in {"call.succeeded", "call.failed"}:
                self.event_log.append(
                    scope,
                    "tool_result",
                    turn_seq=turn_seq,
                    agent=_optional_text(details.get("agent")),
                    data={
                        "tool": str(trace_event.name),
                        "call_id": str(details.get("call_id", "")),
                        "status": str(details.get("status") or status),
                        "content": _json_compatible(details.get("content", "")),
                        "error": details.get("error"),
                        "attempts": details.get("attempts", 0),
                    },
                )
            return

        common = {
            "name": str(trace_event.name),
            "status": status,
            "message": str(trace_event.message or ""),
            "details": _json_compatible(details),
        }
        if trace_event.duration_ms is not None:
            common["duration_ms"] = trace_event.duration_ms
        if category == "agent" and status == "started":
            self.event_log.append(
                scope,
                "agent_started",
                turn_seq=turn_seq,
                agent=str(trace_event.name),
                data=common,
            )
        elif category == "agent" and status in {"completed", "failed", "cancelled"}:
            self.event_log.append(
                scope,
                "agent_completed",
                turn_seq=turn_seq,
                agent=str(trace_event.name),
                data=common,
            )
        elif category == "agent":
            self.event_log.append(
                scope,
                "workflow_step",
                turn_seq=turn_seq,
                agent=str(trace_event.name),
                data=common,
            )
        elif category == "workflow" and str(trace_event.name) == "verification":
            self.event_log.append(
                scope,
                "verification",
                turn_seq=turn_seq,
                agent=_optional_text(details.get("agent")),
                data=common,
            )
        elif category == "workflow":
            event_type = (
                "workflow_outcome"
                if status in {"completed", "failed", "denied", "cancelled"}
                else "workflow_step"
            )
            self.event_log.append(
                scope,
                event_type,
                turn_seq=turn_seq,
                agent=_optional_text(details.get("agent")),
                data=common,
            )


def _bound_payload(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    if event_type == "tool_call" and "arguments" in result:
        encoded = _json_text(result["arguments"])
        preview, truncated, original_bytes = _bounded_text(
            encoded, DEFAULT_ARGUMENT_BYTES
        )
        if truncated:
            result["arguments"] = {"preview": preview}
            result["arguments_truncated"] = True
            result["arguments_original_bytes"] = original_bytes
    if event_type == "tool_result" and "content" in result:
        content = (
            result["content"]
            if isinstance(result["content"], str)
            else _json_text(result["content"])
        )
        content, truncated, original_bytes = _bounded_text(
            content, DEFAULT_CONTENT_BYTES
        )
        result["content"] = content
        result["truncated"] = truncated
        result["original_bytes"] = original_bytes
    return result


def _bounded_text(value: str, max_bytes: int) -> tuple[str, bool, int]:
    encoded = value.encode("utf-8")
    original = len(encoded)
    if original <= max_bytes:
        return value, False, original
    marker = _OMITTED.encode("utf-8")
    available = max_bytes - len(marker)
    head = available // 2
    tail = available - head
    prefix = encoded[:head].decode("utf-8", errors="ignore")
    suffix = encoded[-tail:].decode("utf-8", errors="ignore")
    return prefix + _OMITTED + suffix, True, original


def _encode_event(event: SessionEvent) -> bytes:
    envelope = {
        "v": event.version,
        "seq": event.seq,
        "type": event.type,
        "time": event.time,
        "session_id": event.session_id,
        "turn_seq": event.turn_seq,
        "agent": event.agent,
        "data": event.data,
    }
    try:
        return (
            json.dumps(
                envelope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("Session event data must be JSON-compatible") from exc


def _decode_event(raw: Any, scope: SessionScope, expected_seq: int) -> SessionEvent:
    if not isinstance(raw, dict):
        raise StateError("Session event line must contain a JSON object")
    try:
        version = raw["v"]
        seq = raw["seq"]
        event_type = raw["type"]
        timestamp = raw["time"]
        session_id = raw["session_id"]
        turn_seq = raw["turn_seq"]
        agent = raw["agent"]
        data = raw["data"]
    except KeyError as exc:
        raise StateError("Session event is missing a core envelope field") from exc
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise StateError("Session event has an invalid schema version")
    if seq != expected_seq:
        raise StateError(
            f"Session event sequence is not contiguous: expected {expected_seq}, got {seq!r}"
        )
    if session_id != scope.session_id:
        raise StateError("Session event belongs to a different Session")
    if not isinstance(event_type, str) or not event_type:
        raise StateError("Session event has an invalid type")
    if not isinstance(timestamp, str) or not timestamp:
        raise StateError("Session event has an invalid timestamp")
    try:
        parsed_time = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise StateError("Session event has an invalid timestamp") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() != UTC.utcoffset(
        parsed_time
    ):
        raise StateError("Session event timestamp must be UTC")
    if turn_seq is not None and (
        not isinstance(turn_seq, int) or isinstance(turn_seq, bool) or turn_seq < 1
    ):
        raise StateError("Session event has an invalid turn_seq")
    if agent is not None and (not isinstance(agent, str) or not agent):
        raise StateError("Session event has an invalid agent")
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise StateError("Session event data must be an object")
    return SessionEvent(
        version=version,
        seq=seq,
        type=event_type,
        time=timestamp,
        session_id=session_id,
        turn_seq=turn_seq,
        agent=agent,
        data=data,
    )


def _scope(value: Any) -> SessionScope:
    if not isinstance(value, SessionScope):
        raise ValidationError("Session event scope has an invalid shape")
    account = _non_empty_text(value.account_key, "account key", maximum=500)
    repository = _non_empty_text(value.repository_key, "repository key", maximum=500)
    session_id = _non_empty_text(value.session_id, "session ID", maximum=80)
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValidationError("Session event scope has an invalid Session ID")
    return SessionScope(account, repository, session_id)


def _non_empty_text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(
            f"{label} must be a non-empty string of at most {maximum} characters"
        )
    return value


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _managed_path(root: Path, path: Path) -> bool:
    try:
        account, repository, filename = path.relative_to(root).parts
    except (ValueError, TypeError):
        return False
    return (
        _HASH.fullmatch(account) is not None
        and _HASH.fullmatch(repository) is not None
        and _SESSION_ID.fullmatch(path.stem) is not None
        and filename == f"{path.stem}.jsonl"
    )


def _json_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Session event payload cannot be represented as JSON"
        ) from exc


def _json_compatible(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "observable event data cannot be represented as JSON"
        ) from exc


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _optional_text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _no_follow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
