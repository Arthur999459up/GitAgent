"""Auditable Markdown storage for user and repository long-term context."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from gitagent.domain.errors import PermissionDenied, StateError, ValidationError
from gitagent.domain.learning import MemoryItem, ReflectionChanges

INDEX_LINE_LIMIT = 200
INDEX_BYTE_LIMIT = 25 * 1024
_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
_ITEM_TYPES = {"memory", "experience"}
_SCOPES = {"user", "repository"}
_ROOT_TO_SCOPE = {"user_memory": "user", "repository_memory": "repository"}
_FRONTMATTER_FIELDS = {"type", "priority", "last_accessed_at", "pinned"}
_INDEX_ENTRY_RE = re.compile(
    r"^- \[(HIGH|NORMAL|LOW)\] \[([^\]]+)\]\((items/[A-Za-z0-9_-]+\.md)\) "
    r"(.*?) <!-- last_accessed_at=(.+) -->$"
)


@dataclass(frozen=True)
class _ScopedIndexItem:
    scope: str
    item: MemoryItem


class MemoryAccessTracker:
    """Keep body reads ephemeral until a successful business boundary."""

    def __init__(self) -> None:
        self._paths: set[tuple[str, str]] = set()

    def record(self, root: str, relative_path: str) -> None:
        if root in _ROOT_TO_SCOPE:
            self._paths.add((root, relative_path))

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._paths))

    def clear(self) -> None:
        self._paths.clear()


class MemoryStore:
    """Own deterministic storage, validation, ordering, and index rebuilding."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        text_sanitizer: Callable[[str], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        if not self.root.is_absolute():
            raise ValidationError("Memory root must be an absolute path")
        self._sanitize = text_sanitizer or (lambda value: value)
        self._now = now or (lambda: datetime.now().astimezone())
        if os.name == "posix":
            for candidate in (self.root, *self.root.parents):
                if candidate.is_symlink():
                    raise StateError(
                        f"Memory path must not contain a symbolic link: {candidate}"
                    )
        self._secure_directory(self.root)

    def roots(self, account_key: str, repository_key: str) -> dict[str, Path]:
        """Return and prepare the two roots authorized for direct item access."""

        roots = self._root_paths(account_key, repository_key)
        for path in roots.values():
            self._secure_directory(path / "items")
        return roots

    def _root_paths(self, account_key: str, repository_key: str) -> dict[str, Path]:
        account_root = self.root / "accounts" / _key_hash(account_key)
        return {
            "user_memory": account_root / "user",
            "repository_memory": account_root
            / "repositories"
            / _key_hash(repository_key),
        }

    def read_index(
        self,
        account_key: str,
        repository_key: str,
        *,
        full: bool = False,
    ) -> str:
        """Read persisted indexes only; normal context construction never scans items."""

        roots = self._root_paths(account_key, repository_key)
        with self._locked():
            entries = tuple(
                _ScopedIndexItem(scope, item)
                for root_name, scope in (
                    ("user_memory", "user"),
                    ("repository_memory", "repository"),
                )
                for item in self._read_scope_index(roots[root_name])
            )
        return _render_combined_index(entries, full=full)

    def list_items(
        self,
        account_key: str,
        repository_key: str,
        *,
        scope: str,
        item_type: str | None = None,
    ) -> tuple[MemoryItem, ...]:
        """List a complete scope for CLI and management without touching access time."""

        scope = _scope(scope)
        if item_type is not None and item_type not in _ITEM_TYPES:
            raise ValidationError("Memory type must be memory or experience")
        root = self._scope_root(account_key, repository_key, scope)
        with self._locked():
            items = self._list_scope(root)
        if item_type is not None:
            items = tuple(item for item in items if item.type == item_type)
        return items

    def remember(
        self,
        account_key: str,
        repository_key: str,
        text: str,
        *,
        scope: str = "user",
        priority: str = "normal",
    ) -> tuple[MemoryItem, bool]:
        """Create one explicit pinned Memory, exactly deduplicated within its scope."""

        scope = _scope(scope)
        priority = _priority(priority)
        clean_text = self._text(text)
        root = self._scope_root(account_key, repository_key, scope)
        with self._locked():
            items = self._list_scope(root)
            duplicate = _exact_duplicate(items, clean_text)
            if duplicate is not None:
                if not duplicate.pinned or duplicate.type != "memory":
                    pinned = replace(
                        duplicate,
                        type="memory",
                        priority=priority,
                        last_accessed_at=self._timestamp(),
                        pinned=True,
                    )
                    self._commit_scope_states(
                        {
                            root: tuple(
                                pinned
                                if item.relative_path == pinned.relative_path
                                else item
                                for item in items
                            )
                        }
                    )
                    return pinned, False
                return duplicate, False
            relative_path = self._available_path(root, clean_text)
            item = MemoryItem(
                relative_path, "memory", clean_text, priority, self._timestamp(), True
            )
            self._commit_scope_states({root: (*items, item)})
            return item, True

    def forget(
        self,
        account_key: str,
        repository_key: str,
        *,
        scope: str,
        relative_path: str,
    ) -> MemoryItem | None:
        """Explicitly delete an item, including a pinned item."""

        root = self._scope_root(account_key, repository_key, _scope(scope))
        relative_path = _item_path(relative_path)
        with self._locked():
            items = self._list_scope(root)
            item = next(
                (item for item in items if item.relative_path == relative_path), None
            )
            if item is None:
                return None
            self._commit_scope_states(
                {
                    root: tuple(
                        current
                        for current in items
                        if current.relative_path != relative_path
                    )
                }
            )
            return item

    def apply_changes(
        self,
        account_key: str,
        repository_key: str,
        changes: ReflectionChanges,
        *,
        accessed_paths: Iterable[tuple[str, str]] = (),
    ) -> dict[str, tuple[str, ...]]:
        """Apply automatic edits and access touches after a successful business result."""

        if not isinstance(changes, ReflectionChanges):
            raise TypeError("changes must be ReflectionChanges")
        roots = self.roots(account_key, repository_key)
        added: list[str] = []
        replaced: list[str] = []
        deleted: list[str] = []
        skipped: list[str] = []
        now = self._timestamp()
        add_changes = tuple(
            self._validated_change(raw, action="add") for raw in changes.add
        )
        replace_changes = tuple(
            self._validated_change(raw, action="replace") for raw in changes.replace
        )
        delete_changes = tuple(
            self._validated_change(raw, action="delete") for raw in changes.delete
        )
        addressed = [
            (action, change["scope"], change["path"])
            for action, group in (
                ("add", add_changes),
                ("replace", replace_changes),
                ("delete", delete_changes),
            )
            for change in group
        ]
        identities = [(scope, path) for _, scope, path in addressed]
        if len(identities) != len(set(identities)):
            raise ValidationError(
                "Reflection cannot apply multiple changes to the same Memory path"
            )
        with self._locked():
            original_by_scope = {
                scope: {item.relative_path: item for item in self._list_scope(root)}
                for scope, root in (
                    ("user", roots["user_memory"]),
                    ("repository", roots["repository_memory"]),
                )
            }
            by_scope = {
                scope: dict(items) for scope, items in original_by_scope.items()
            }
            for change in add_changes:
                if change["path"] in by_scope[change["scope"]]:
                    raise ValidationError(
                        f"Memory add path already exists: {change['path']}"
                    )
            for change in add_changes:
                scope = change["scope"]
                path = change["path"]
                items = by_scope[scope]
                duplicate = _exact_duplicate(items.values(), change["text"])
                if duplicate is not None:
                    skipped.append(path)
                    continue
                item = MemoryItem(
                    path,
                    change["type"],
                    change["text"],
                    change["priority"],
                    now,
                    False,
                )
                items[path] = item
                added.append(f"{scope}:{path}")

            for change in replace_changes:
                scope = change["scope"]
                path = change["path"]
                current = by_scope[scope].get(path)
                if current is None or current.pinned:
                    skipped.append(path)
                    continue
                item = replace(
                    current,
                    text=change["text"],
                    priority=change["priority"],
                    last_accessed_at=now,
                )
                by_scope[scope][path] = item
                replaced.append(f"{scope}:{path}")

            for change in delete_changes:
                scope = change["scope"]
                path = change["path"]
                current = by_scope[scope].get(path)
                if current is None or current.pinned:
                    skipped.append(path)
                    continue
                del by_scope[scope][path]
                deleted.append(f"{scope}:{path}")

            for root_name, relative_path in set(accessed_paths):
                scope = _ROOT_TO_SCOPE.get(root_name)
                if scope is None:
                    continue
                try:
                    path = _item_path(relative_path)
                except ValidationError:
                    continue
                current = by_scope[scope].get(path)
                if current is None or current.last_accessed_at == now:
                    continue
                touched = replace(current, last_accessed_at=now)
                by_scope[scope][path] = touched

            updates = {
                self._root_for_scope(roots, scope): tuple(by_scope[scope].values())
                for scope in ("user", "repository")
                if by_scope[scope] != original_by_scope[scope]
            }
            self._commit_scope_states(updates)
        return {
            "added": tuple(added),
            "replaced": tuple(replaced),
            "deleted": tuple(deleted),
            "skipped": tuple(skipped),
        }

    def rebuild_index(self, account_key: str, repository_key: str) -> str:
        """Rebuild complete indexes from item files without refreshing access time."""

        roots = self.roots(account_key, repository_key)
        with self._locked():
            for root in roots.values():
                self._write_index(root, self._list_scope(root))
        return self.read_index(account_key, repository_key, full=True)

    def _validated_change(self, raw: dict[str, str], *, action: str) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise ValidationError(f"Memory {action} change must be an object")
        required = {"scope", "path"}
        allowed = set(required)
        if action == "add":
            required |= {"type", "priority", "text"}
            allowed |= {"type", "priority", "text"}
        elif action == "replace":
            required |= {"priority", "text"}
            allowed |= {"priority", "text"}
        if set(raw) - allowed or not required.issubset(raw):
            raise ValidationError(f"Memory {action} change has an invalid shape")
        result = {key: str(value) for key, value in raw.items()}
        result["scope"] = _scope(result["scope"])
        result["path"] = _item_path(result["path"])
        if action == "add" and result["type"] not in _ITEM_TYPES:
            raise ValidationError("Memory type must be memory or experience")
        if action in {"add", "replace"}:
            result["priority"] = _priority(result["priority"])
            result["text"] = self._text(result["text"])
        return result

    def _scope_root(self, account_key: str, repository_key: str, scope: str) -> Path:
        roots = self.roots(account_key, repository_key)
        return self._root_for_scope(roots, scope)

    @staticmethod
    def _root_for_scope(roots: dict[str, Path], scope: str) -> Path:
        return roots["user_memory" if scope == "user" else "repository_memory"]

    def _list_scope(self, root: Path) -> tuple[MemoryItem, ...]:
        items_root = root / "items"
        self._secure_directory(items_root)
        items: list[MemoryItem] = []
        for path in sorted(items_root.glob("*.md")):
            if path.is_symlink():
                raise StateError(f"Memory item must not be a symbolic link: {path}")
            if path.is_file():
                items.append(self._read_item(root, path))
        return tuple(sorted(items, key=_sort_key))

    def _read_scope_index(self, root: Path) -> tuple[MemoryItem, ...]:
        path = root / "MEMORY.md"
        if not path.exists():
            return ()
        if path.is_symlink():
            raise StateError(f"Memory index must not be a symbolic link: {path}")
        try:
            if (
                os.name == "posix"
                and path.stat(follow_symlinks=False).st_uid != os.getuid()
            ):
                raise StateError(f"Memory index is not owned by the current user: {path}")
            raw = path.read_text(encoding="utf-8")
        except StateError:
            raise
        except OSError as exc:
            raise StateError(f"cannot read Memory index: {path}") from exc
        return _parse_index(raw)

    def _read_item(self, root: Path, path: Path) -> MemoryItem:
        resolved = self._resolved_item(
            root, path.relative_to(root).as_posix(), must_exist=True
        )
        try:
            if os.name == "posix":
                if resolved.stat(follow_symlinks=False).st_uid != os.getuid():
                    raise StateError(
                        f"Memory item is not owned by the current user: {resolved}"
                    )
                os.chmod(resolved, 0o600, follow_symlinks=False)
            raw = resolved.read_text(encoding="utf-8")
        except StateError:
            raise
        except OSError as exc:
            raise StateError(f"cannot read Memory item: {resolved}") from exc
        metadata, text = _parse_item(raw)
        text = self._sanitize(text)
        return MemoryItem(
            resolved.relative_to(root).as_posix(),
            metadata["type"],
            text,
            metadata["priority"],
            metadata["last_accessed_at"],
            metadata["pinned"],
        )

    def _write_item(self, root: Path, item: MemoryItem) -> None:
        target = self._resolved_item(root, item.relative_path, must_exist=False)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = (
            "---\n"
            f"type: {item.type}\n"
            f"priority: {item.priority}\n"
            f"last_accessed_at: {item.last_accessed_at}\n"
            f"pinned: {'true' if item.pinned else 'false'}\n"
            "---\n\n"
            f"{item.text.strip()}\n"
        )
        _atomic_write(target, body)

    def _write_index(self, root: Path, items: Iterable[MemoryItem]) -> None:
        ordered = tuple(sorted(items, key=_sort_key))
        lines: list[str] = ["# Memory", ""]
        lines.extend(
            _stored_index_line(item) for item in ordered if item.type == "memory"
        )
        lines.extend(("", "# Experience", ""))
        lines.extend(
            _stored_index_line(item) for item in ordered if item.type == "experience"
        )
        _atomic_write(root / "MEMORY.md", "\n".join(lines).rstrip() + "\n")

    def _commit_scope_states(
        self, updates: dict[Path, tuple[MemoryItem, ...]]
    ) -> None:
        """Stage complete scope states and roll every scope back if commit fails."""

        if not updates:
            return
        transaction_root = Path(
            tempfile.mkdtemp(prefix=".memory-batch-", dir=self.root)
        )
        staged: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        swapped: list[Path] = []
        ordered_updates = tuple(sorted(updates.items(), key=lambda item: str(item[0])))
        try:
            for index, (root, items) in enumerate(ordered_updates):
                stage = transaction_root / f"stage-{index}"
                self._secure_directory(stage / "items")
                for item in items:
                    self._write_item(stage, item)
                self._write_index(stage, items)
                staged[root] = stage

            for index, (root, _) in enumerate(ordered_updates):
                backup = transaction_root / f"backup-{index}"
                os.replace(root, backup)
                backups[root] = backup
                try:
                    os.replace(staged[root], root)
                except BaseException:
                    os.replace(backup, root)
                    backups.pop(root, None)
                    raise
                swapped.append(root)
        except BaseException:
            for rollback_index, root in enumerate(reversed(swapped)):
                backup = backups.get(root)
                if backup is None or not backup.exists():
                    continue
                if root.exists():
                    os.replace(root, transaction_root / f"failed-{rollback_index}")
                os.replace(backup, root)
            raise
        finally:
            shutil.rmtree(transaction_root, ignore_errors=True)

    def _available_path(self, root: Path, text: str) -> str:
        stem = _readable_stem(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        candidates = (f"items/{stem}.md", f"items/{stem}_{digest}.md")
        for candidate in candidates:
            if not self._resolved_item(root, candidate, must_exist=False).exists():
                return candidate
        raise StateError("cannot allocate a unique Memory item path")

    def _resolved_item(
        self, root: Path, relative_path: str, *, must_exist: bool
    ) -> Path:
        relative_path = _item_path(relative_path)
        root_resolved = root.resolve(strict=True)
        candidate = root / Path(relative_path)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise PermissionDenied("Memory path escapes its authorized root") from exc
        if must_exist and not resolved.is_file():
            raise FileNotFoundError(relative_path)
        if candidate.is_symlink() or (
            candidate.exists() and resolved != candidate.absolute()
        ):
            raise PermissionDenied(
                "Memory item cannot be accessed through a symbolic link"
            )
        return resolved

    def _text(self, value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("Memory text must be a string")
        clean = self._sanitize(value).strip()
        if not clean:
            raise ValidationError("Memory text cannot be empty")
        if len(clean) > 8_000:
            raise ValidationError("Memory text must be at most 8000 characters")
        return clean

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise StateError("Memory clock must return a timezone-aware datetime")
        return value.isoformat(timespec="seconds")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.root / ".lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows fallback
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows fallback
                pass
            os.close(descriptor)

    def _secure_directory(self, path: Path) -> None:
        candidate = path
        while True:
            if candidate.is_symlink():
                raise StateError(
                    f"Memory directory must not contain a symbolic link: {candidate}"
                )
            if candidate == self.root or candidate.parent == candidate:
                break
            candidate = candidate.parent
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix":
                if path.stat(follow_symlinks=False).st_uid != os.getuid():
                    raise StateError(
                        f"Memory directory is not owned by the current user: {path}"
                    )
                os.chmod(path, 0o700, follow_symlinks=False)
        except StateError:
            raise
        except OSError as exc:
            raise StateError(f"cannot create Memory directory: {path}") from exc


def _parse_item(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        raise StateError("Memory item is missing YAML frontmatter")
    marker = raw.find("\n---\n", 4)
    if marker < 0:
        raise StateError("Memory item has unterminated YAML frontmatter")
    try:
        metadata = yaml.safe_load(raw[4:marker])
    except yaml.YAMLError as exc:
        raise StateError("Memory item frontmatter is invalid") from exc
    if not isinstance(metadata, dict) or set(metadata) != _FRONTMATTER_FIELDS:
        raise StateError("Memory item frontmatter has an invalid shape")
    if (
        metadata["type"] not in _ITEM_TYPES
        or metadata["priority"] not in _PRIORITY_ORDER
    ):
        raise StateError("Memory item type or priority is invalid")
    if not isinstance(metadata["pinned"], bool):
        raise StateError("Memory item pinned must be a boolean")
    raw_timestamp = metadata["last_accessed_at"]
    timestamp = (
        raw_timestamp.isoformat(timespec="seconds")
        if isinstance(raw_timestamp, datetime)
        else str(raw_timestamp)
    )
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise StateError("Memory item last_accessed_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateError("Memory item last_accessed_at must include a timezone")
    text = raw[marker + 5 :].strip()
    if not text:
        raise StateError("Memory item text cannot be empty")
    return {
        "type": str(metadata["type"]),
        "priority": str(metadata["priority"]),
        "last_accessed_at": timestamp,
        "pinned": metadata["pinned"],
    }, text


def _parse_index(raw: str) -> tuple[MemoryItem, ...]:
    current_type: str | None = None
    items: list[MemoryItem] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "# Memory":
            current_type = "memory"
            continue
        if line == "# Experience":
            current_type = "experience"
            continue
        if current_type is None:
            raise StateError("Memory index has content before a section header")
        match = _INDEX_ENTRY_RE.fullmatch(line)
        if match is None:
            raise StateError("Memory index entry has an invalid shape")
        priority, _, relative_path, summary, timestamp = match.groups()
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise StateError("Memory index last_accessed_at is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise StateError("Memory index last_accessed_at must include a timezone")
        items.append(
            MemoryItem(
                relative_path=_item_path(relative_path),
                type=current_type,
                text=summary,
                priority=priority.casefold(),
                last_accessed_at=timestamp,
                pinned=False,
            )
        )
    return tuple(items)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _key_hash(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Memory scope key cannot be empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(value: str) -> str:
    if value not in _SCOPES:
        raise ValidationError("Memory scope must be user or repository")
    return value


def _priority(value: str) -> str:
    if value not in _PRIORITY_ORDER:
        raise ValidationError("Memory priority must be low, normal, or high")
    return value


def _item_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("Memory path must be a string")
    if "\\" in value or "\x00" in value:
        raise ValidationError("Memory path contains an invalid character")
    path = PurePosixPath(value.strip())
    stem = path.stem
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != "items"
        or path.parts[1] in {"", ".", ".."}
        or path.suffix.casefold() != ".md"
        or not stem
        or any(
            not (character.isalnum() or character in {"_", "-"}) for character in stem
        )
        or any(part == ".." for part in path.parts)
    ):
        raise ValidationError(
            "Memory path must be items/<readable_name>.md without traversal"
        )
    return path.as_posix()


def _canonical(text: str) -> str:
    return " ".join(text.casefold().split())


def _exact_duplicate(items: Iterable[MemoryItem], text: str) -> MemoryItem | None:
    canonical = _canonical(text)
    return next((item for item in items if _canonical(item.text) == canonical), None)


def _sort_key(item: MemoryItem) -> tuple[int, float, str]:
    timestamp = datetime.fromisoformat(item.last_accessed_at).timestamp()
    return (_PRIORITY_ORDER[item.priority], -timestamp, item.relative_path)


def _summary(text: str) -> str:
    paragraph = text.split("\n\n", 1)[0]
    summary = " ".join(paragraph.split())
    return summary if len(summary) <= 240 else summary[:239].rstrip() + "…"


def _title(path: str) -> str:
    return (
        PurePosixPath(path).stem.replace("_", " ").replace("-", " ").strip() or "Memory"
    )


def _display_index_line(item: MemoryItem) -> str:
    return (
        f"- [{item.priority.upper()}] [{_title(item.relative_path)}]"
        f"({item.relative_path}) {_summary(item.text)}"
    )


def _stored_index_line(item: MemoryItem) -> str:
    return (
        f"{_display_index_line(item)} "
        f"<!-- last_accessed_at={item.last_accessed_at} -->"
    )


def _scoped_sort_key(entry: _ScopedIndexItem) -> tuple[int, float, str, str]:
    item = entry.item
    timestamp = datetime.fromisoformat(item.last_accessed_at).timestamp()
    return (
        _PRIORITY_ORDER[item.priority],
        -timestamp,
        item.relative_path,
        entry.scope,
    )


def _render_combined_index(
    entries: Iterable[_ScopedIndexItem], *, full: bool
) -> str:
    ordered = tuple(sorted(entries, key=_scoped_sort_key))
    if not ordered:
        return ""
    if full:
        return _render_selected_index(ordered)

    selected: list[_ScopedIndexItem] = []
    for entry in ordered:
        candidate = (*selected, entry)
        rendered = _render_selected_index(candidate)
        if not _index_within_limits(rendered):
            break
        selected.append(entry)
    return _render_selected_index(tuple(selected)) if selected else ""


def _render_selected_index(entries: tuple[_ScopedIndexItem, ...]) -> str:
    groups: dict[tuple[str, str], list[MemoryItem]] = {
        ("user", "memory"): [],
        ("user", "experience"): [],
        ("repository", "memory"): [],
        ("repository", "experience"): [],
    }
    for entry in entries:
        groups[(entry.scope, entry.item.type)].append(entry.item)

    sections: list[str] = []
    for scope, root_name, title in (
        ("user", "user_memory", "用户长期上下文"),
        ("repository", "repository_memory", "当前仓库长期上下文"),
    ):
        lines = [f"### {title} · root={root_name}", "", "#### Memory"]
        lines.extend(
            [_display_index_line(item) for item in groups[(scope, "memory")]]
            or ["- （无）"]
        )
        lines.extend(("", "#### Experience"))
        lines.extend(
            [_display_index_line(item) for item in groups[(scope, "experience")]]
            or ["- （无）"]
        )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _index_within_limits(value: str) -> bool:
    lines = value.splitlines()
    return len(lines) <= INDEX_LINE_LIMIT and len(value.encode("utf-8")) <= INDEX_BYTE_LIMIT


def _readable_stem(text: str) -> str:
    result: list[str] = []
    separator = False
    for character in unicodedata.normalize("NFKC", text):
        if character.isalnum():
            result.append(character.casefold())
            separator = False
        elif result and not separator:
            result.append("_")
            separator = True
        if len(result) >= 48:
            break
    stem = "".join(result).strip("_")
    return stem or "memory"
