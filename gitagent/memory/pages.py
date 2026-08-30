"""Secure Markdown Page storage, migration, locking, and atomic updates."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from gitagent.domain.errors import StateError, ValidationError

from .index import render_combined_index, write_scope_index
from .models import MemoryCandidate, MemoryPage, MemoryScope, MemoryType

SCHEMA_VERSION = 1
_TYPES = {"user", "feedback", "project", "reference"}
_SCOPES = {"private", "project"}
_ROOT_TO_SCOPE = {"private_memory": "private", "project_memory": "project"}
_PAGE_ID = re.compile(r"^mem-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$")
_SAFE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SOURCE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_FRONTMATTER_FIELDS = {
    "schema_version",
    "id",
    "name",
    "description",
    "type",
    "scope",
    "category",
    "importance",
    "source",
    "signature",
    "created_at",
    "updated_at",
    "ttl_days",
    "disabled",
    "supersedes",
    "tags",
}


class MemoryPageStore:
    """The Page files are the source of truth; indexes are derived state."""

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
        self._thread_lock = threading.RLock()
        self._secure_directory(self.root)

    def roots(self, account_key: str, repository_key: str) -> dict[str, Path]:
        roots = self._root_paths(account_key, repository_key)
        with self._locked():
            for path in roots.values():
                self._secure_directory(path)
            self._migrate_legacy(account_key, repository_key, roots)
            for path in roots.values():
                if not (path / "MEMORY.md").exists():
                    self._rebuild_scope(path)
        return roots

    def read_index(self, account_key: str, repository_key: str) -> str:
        roots = self.roots(account_key, repository_key)
        with self._locked():
            # TTL is time-dependent, so refresh the derived active index before use.
            self._rebuild_scope(roots["private_memory"])
            self._rebuild_scope(roots["project_memory"])
            private = self._read_index_file(roots["private_memory"])
            project = self._read_index_file(roots["project_memory"])
        return render_combined_index(private, project)

    def list_pages(
        self,
        account_key: str,
        repository_key: str,
        *,
        scope: str | None = None,
        include_inactive: bool = True,
        memory_type: str | None = None,
    ) -> tuple[MemoryPage, ...]:
        roots = self.roots(account_key, repository_key)
        scopes = (_scope(scope),) if scope is not None else ("private", "project")
        if memory_type is not None and memory_type not in _TYPES:
            raise ValidationError("Memory type must be user, feedback, project, or reference")
        now = self._aware_now()
        with self._locked():
            pages = tuple(
                page
                for selected in scopes
                for page in self._list_scope(self._scope_root_from_roots(roots, selected))
                if (include_inactive or page.active(now))
                and (memory_type is None or page.type == memory_type)
            )
        return tuple(sorted(pages, key=lambda page: (page.scope, page.name, page.id)))

    def read_page(
        self,
        account_key: str,
        repository_key: str,
        *,
        scope: str,
        identifier: str,
        include_inactive: bool = False,
    ) -> MemoryPage | None:
        selected = _scope(scope)
        root = self._scope_root(account_key, repository_key, selected)
        with self._locked():
            page = self._find_page(self._list_scope(root), identifier)
        if page is None:
            return None
        if not include_inactive and not page.active(self._aware_now()):
            return None
        return page

    def write_candidate(
        self,
        account_key: str,
        repository_key: str,
        candidate: MemoryCandidate,
    ) -> tuple[MemoryPage, bool]:
        clean = self._candidate(candidate)
        root = self._scope_root(account_key, repository_key, clean.scope)
        with self._locked():
            pages = list(self._list_scope(root))
            signature = self.signature(
                clean.scope, clean.type, clean.description, clean.body
            )
            duplicate = next((page for page in pages if page.signature == signature), None)
            if duplicate is not None:
                if clean.source == "manual" and duplicate.source != "manual":
                    duplicate = replace(
                        duplicate,
                        source="manual",
                        importance=max(4, duplicate.importance),
                        updated_at=self._timestamp(),
                    )
                    self._write_page(root, duplicate)
                    self._rebuild_scope(root)
                return duplicate, False
            same_name = next((page for page in pages if page.name == clean.name), None)
            if same_name is not None and same_name.source == "manual" and clean.source != "manual":
                return same_name, False
            now = self._timestamp()
            if same_name is not None:
                page = replace(
                    same_name,
                    description=clean.description,
                    type=clean.type,
                    category=clean.category,
                    importance=clean.importance,
                    source=clean.source,
                    signature=signature,
                    updated_at=now,
                    ttl_days=clean.ttl_days,
                    disabled=False,
                    supersedes=_unique((*same_name.supersedes, *clean.supersedes)),
                    tags=clean.tags,
                    body=clean.body,
                )
                self._write_page(root, page)
                self._rebuild_scope(root)
                return page, False
            memory_id = self._new_id()
            relative_path = self._available_path(root, clean.name)
            page = MemoryPage(
                schema_version=SCHEMA_VERSION,
                id=memory_id,
                name=Path(relative_path).stem,
                description=clean.description,
                type=clean.type,
                scope=clean.scope,
                category=clean.category,
                importance=clean.importance,
                source=clean.source,
                signature=signature,
                created_at=now,
                updated_at=now,
                ttl_days=clean.ttl_days,
                disabled=False,
                supersedes=clean.supersedes,
                tags=clean.tags,
                body=clean.body,
                relative_path=relative_path,
            )
            self._write_page(root, page)
            self._rebuild_scope(root)
            return page, True

    def manual_write(
        self,
        account_key: str,
        repository_key: str,
        text: str,
        *,
        scope: str = "private",
    ) -> tuple[MemoryPage, bool]:
        selected = _scope(scope)
        body = self._text(text, label="Memory body", maximum=8_000)
        description = _description(body)
        name = _slug(description) or f"memory-{hashlib.sha256(body.encode()).hexdigest()[:8]}"
        memory_type: MemoryType = "user" if selected == "private" else "project"
        return self.write_candidate(
            account_key,
            repository_key,
            MemoryCandidate(
                name=name,
                description=description,
                type=memory_type,
                scope=selected,
                body=body,
                category="general",
                importance=4,
                source="manual",
                tags=(),
            ),
        )

    def disable(
        self,
        account_key: str,
        repository_key: str,
        *,
        scope: str,
        identifier: str,
        allow_manual: bool = False,
    ) -> MemoryPage | None:
        root = self._scope_root(account_key, repository_key, _scope(scope))
        with self._locked():
            page = self._find_page(self._list_scope(root), identifier)
            if page is None or (page.source == "manual" and not allow_manual):
                return None
            updated = replace(page, disabled=True, updated_at=self._timestamp())
            self._write_page(root, updated)
            self._rebuild_scope(root)
            return updated

    def forget(
        self,
        account_key: str,
        repository_key: str,
        *,
        identifier: str,
        scope: str | None = None,
    ) -> MemoryPage | None:
        roots = self.roots(account_key, repository_key)
        scopes = (_scope(scope),) if scope is not None else ("private", "project")
        with self._locked():
            matches = [
                (selected, self._scope_root_from_roots(roots, selected), page)
                for selected in scopes
                for page in [
                    self._find_page(
                        self._list_scope(self._scope_root_from_roots(roots, selected)),
                        identifier,
                    )
                ]
                if page is not None
            ]
            if len(matches) > 1:
                raise ValidationError("Memory identifier is ambiguous; specify its scope")
            if not matches:
                return None
            _, root, page = matches[0]
            path = self._safe_page_path(root, page.relative_path, must_exist=True)
            path.unlink()
            self._fsync_directory(root)
            self._rebuild_scope(root)
            return page

    def rebuild_index(self, account_key: str, repository_key: str) -> None:
        roots = self.roots(account_key, repository_key)
        with self._locked():
            for root in roots.values():
                self._rebuild_scope(root)

    def maintain(self, account_key: str, repository_key: str) -> dict[str, tuple[str, ...]]:
        """Deterministically disable redundant automatic pages and rebuild indexes."""

        roots = self.roots(account_key, repository_key)
        disabled: list[str] = []
        preserved: list[str] = []
        with self._locked():
            scoped = {
                "private": (roots["private_memory"], list(self._list_scope(roots["private_memory"]))),
                "project": (roots["project_memory"], list(self._list_scope(roots["project_memory"]))),
            }
            by_id = {
                page.id: (scope, root, page)
                for scope, (root, pages) in scoped.items()
                for page in pages
            }
            disabled_ids: set[str] = set()

            # A Page may explicitly declare that it supersedes older Pages. Dream
            # honors that relationship without ever disabling a manual Page.
            for _, pages in scoped.values():
                for page in pages:
                    if page.disabled:
                        continue
                    for superseded_id in page.supersedes:
                        target = by_id.get(superseded_id)
                        if target is None:
                            continue
                        target_scope, target_root, old = target
                        if old.source == "manual" or old.disabled or old.id in disabled_ids:
                            continue
                        changed = replace(old, disabled=True, updated_at=self._timestamp())
                        self._write_page(target_root, changed)
                        disabled_ids.add(old.id)
                        disabled.append(f"{target_scope}:{old.id}")

            for scope, (root, pages) in scoped.items():
                groups: dict[tuple[str, str], list[MemoryPage]] = {}
                for page in pages:
                    if page.disabled or page.id in disabled_ids:
                        continue
                    description = " ".join(page.description.casefold().split())
                    groups.setdefault(("signature", page.signature), []).append(page)
                    groups.setdefault(
                        (f"description:{page.type}", description), []
                    ).append(page)
                for group in groups.values():
                    if len(group) < 2:
                        continue
                    keep = min(
                        group,
                        key=lambda page: (page.source != "manual", -page.importance, page.updated_at, page.id),
                    )
                    preserved.append(f"{scope}:{keep.id}")
                    for page in group:
                        if (
                            page.id == keep.id
                            or page.source == "manual"
                            or page.id in disabled_ids
                        ):
                            continue
                        changed = replace(page, disabled=True, updated_at=self._timestamp())
                        self._write_page(root, changed)
                        disabled_ids.add(page.id)
                        disabled.append(f"{scope}:{page.id}")
            for root, _ in scoped.values():
                self._rebuild_scope(root)
        return {
            "disabled": tuple(disabled),
            "preserved": tuple(dict.fromkeys(preserved)),
        }

    @staticmethod
    def signature(scope: str, memory_type: str, description: str, body: str) -> str:
        normalized = "\n".join(
            " ".join(value.casefold().split())
            for value in (scope, memory_type, description, body)
        )
        return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _root_paths(self, account_key: str, repository_key: str) -> dict[str, Path]:
        account = self.root / "accounts" / _key_hash(account_key)
        return {
            "private_memory": account / "private",
            "project_memory": account / "projects" / _key_hash(repository_key),
        }

    def _scope_root(self, account_key: str, repository_key: str, scope: str) -> Path:
        roots = self.roots(account_key, repository_key)
        return self._scope_root_from_roots(roots, scope)

    @staticmethod
    def _scope_root_from_roots(roots: dict[str, Path], scope: str) -> Path:
        return roots["private_memory" if scope == "private" else "project_memory"]

    def _candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        if not isinstance(candidate, MemoryCandidate):
            raise TypeError("candidate must be a MemoryCandidate")
        selected_scope = _scope(candidate.scope)
        selected_type = _memory_type(candidate.type)
        raw_name = self._text(candidate.name, label="Memory name", maximum=120)
        name = _slug(raw_name)
        if not name:
            name = "memory-" + hashlib.sha256(
                f"{raw_name}\n{candidate.description}\n{candidate.body}".encode()
            ).hexdigest()[:8]
        description = self._text(candidate.description, label="Memory description", maximum=500)
        body = self._text(candidate.body, label="Memory body", maximum=8_000)
        category = _slug(self._text(candidate.category or "general", label="Memory category", maximum=80)) or "general"
        if not isinstance(candidate.importance, int) or isinstance(candidate.importance, bool) or not 0 <= candidate.importance <= 5:
            raise ValidationError("Memory importance must be an integer from 0 to 5")
        source = str(candidate.source).strip().casefold()
        if not _SOURCE.fullmatch(source):
            raise ValidationError("Memory source is invalid")
        ttl_days = candidate.ttl_days
        if ttl_days is not None and (
            not isinstance(ttl_days, int) or isinstance(ttl_days, bool) or ttl_days < 1
        ):
            raise ValidationError("Memory ttl_days must be null or a positive integer")
        if isinstance(candidate.tags, (str, bytes)):
            raise ValidationError("Memory tags must be a sequence of strings")
        if isinstance(candidate.supersedes, (str, bytes)):
            raise ValidationError("Memory supersedes must be a sequence of IDs")
        tags = tuple(_slug(self._text(tag, label="Memory tag", maximum=80)) for tag in candidate.tags)
        tags = tuple(tag for tag in _unique(tags) if tag)[:20]
        supersedes = tuple(self._memory_id(value, label="supersedes ID") for value in candidate.supersedes)[:20]
        return MemoryCandidate(
            name=name,
            description=description,
            type=selected_type,
            scope=selected_scope,
            body=body,
            category=category,
            importance=candidate.importance,
            source=source,
            ttl_days=ttl_days,
            tags=tags,
            supersedes=_unique(supersedes),
        )

    def _list_scope(self, root: Path) -> tuple[MemoryPage, ...]:
        self._secure_directory(root)
        pages: list[MemoryPage] = []
        for path in sorted(root.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            pages.append(self._read_page_file(root, path))
        return tuple(pages)

    def _read_page_file(self, root: Path, path: Path) -> MemoryPage:
        resolved = self._safe_page_path(root, path.name, must_exist=True)
        self._verify_file(resolved)
        raw = resolved.read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(raw)
        if set(metadata) != _FRONTMATTER_FIELDS:
            missing = sorted(_FRONTMATTER_FIELDS - set(metadata))
            extra = sorted(set(metadata) - _FRONTMATTER_FIELDS)
            raise ValidationError(
                f"Memory frontmatter fields are invalid (missing={missing}, extra={extra})"
            )
        page = MemoryPage(
            schema_version=_integer(metadata["schema_version"], "schema_version", minimum=1, maximum=SCHEMA_VERSION),
            id=self._memory_id(metadata["id"]),
            name=_safe_name(metadata["name"]),
            description=_string(metadata["description"], "description", maximum=500),
            type=_memory_type(metadata["type"]),
            scope=_scope(metadata["scope"]),
            category=_safe_name(metadata["category"]),
            importance=_integer(metadata["importance"], "importance", minimum=0, maximum=5),
            source=_source(metadata["source"]),
            signature=_signature(metadata["signature"]),
            created_at=_timestamp(metadata["created_at"]),
            updated_at=_timestamp(metadata["updated_at"]),
            ttl_days=_optional_positive_integer(metadata["ttl_days"], "ttl_days"),
            disabled=_boolean(metadata["disabled"], "disabled"),
            supersedes=tuple(self._memory_id(value, label="supersedes ID") for value in _string_list(metadata["supersedes"], "supersedes")),
            tags=tuple(_safe_name(value) for value in _string_list(metadata["tags"], "tags")),
            body=_string(body.strip(), "body", maximum=8_000),
            relative_path=resolved.name,
        )
        if page.relative_path != f"{page.name}.md":
            raise ValidationError("Memory filename must match its frontmatter name")
        expected = self.signature(page.scope, page.type, page.description, page.body)
        if page.signature != expected:
            raise ValidationError(f"Memory signature is invalid: {page.relative_path}")
        return page

    def _write_page(self, root: Path, page: MemoryPage) -> None:
        path = self._safe_page_path(root, page.relative_path)
        metadata = asdict(page)
        metadata.pop("body")
        metadata.pop("relative_path")
        metadata["supersedes"] = list(page.supersedes)
        metadata["tags"] = list(page.tags)
        frontmatter = yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        self._atomic_write(path, f"---\n{frontmatter}\n---\n\n{page.body.strip()}\n")

    def _rebuild_scope(self, root: Path) -> None:
        write_scope_index(root, self._list_scope(root), now=self._aware_now(), writer=self)

    @staticmethod
    def _read_index_file(root: Path) -> str:
        path = root / "MEMORY.md"
        return path.read_text(encoding="utf-8") if path.is_file() else "# Memory\n"

    @staticmethod
    def _find_page(pages: Iterable[MemoryPage], identifier: str) -> MemoryPage | None:
        value = str(identifier).strip()
        filename = PurePosixPath(value).name
        matches = [
            page
            for page in pages
            if value in {page.id, page.name, page.relative_path}
            or filename == page.relative_path
        ]
        if len(matches) > 1:
            raise ValidationError("Memory identifier is ambiguous")
        return matches[0] if matches else None

    def _migrate_legacy(self, account_key: str, repository_key: str, roots: dict[str, Path]) -> None:
        account = self.root / "accounts" / _key_hash(account_key)
        legacy = (
            ("private", account / "user", roots["private_memory"], "user"),
            (
                "project",
                account / "repositories" / _key_hash(repository_key),
                roots["project_memory"],
                f"repository-{_key_hash(repository_key)}",
            ),
        )
        for scope, old_root, new_root, label in legacy:
            items = old_root / "items"
            if not items.is_dir():
                continue
            for old_path in sorted(items.glob("*.md")):
                metadata, body = _parse_frontmatter(old_path.read_text(encoding="utf-8"))
                if str(metadata.get("type") or "memory") == "experience":
                    archive = account / "archive" / "legacy-experience" / label
                    self._secure_directory(archive)
                    shutil.move(str(old_path), str(self._available_archive_path(archive, old_path.name)))
                    continue
                legacy_importance = {"high": 5, "normal": 3, "low": 1}.get(
                    str(metadata.get("priority") or "normal"), 3
                )
                clean_body = self._text(body.strip(), label="legacy Memory body", maximum=8_000)
                name = _slug(old_path.stem) or f"memory-{hashlib.sha256(clean_body.encode()).hexdigest()[:8]}"
                memory_type: MemoryType = "user" if scope == "private" else "project"
                candidate = self._candidate(
                    MemoryCandidate(
                        name=name,
                        description=_description(clean_body),
                        type=memory_type,
                        scope=scope,  # type: ignore[arg-type]
                        body=clean_body,
                        importance=legacy_importance,
                        source="migration",
                    )
                )
                existing = list(self._list_scope(new_root))
                signature = self.signature(scope, memory_type, candidate.description, candidate.body)
                if not any(page.signature == signature for page in existing):
                    now = self._timestamp()
                    relative = self._available_path(new_root, candidate.name)
                    page = MemoryPage(
                        SCHEMA_VERSION,
                        self._new_id(),
                        Path(relative).stem,
                        candidate.description,
                        memory_type,
                        candidate.scope,
                        candidate.category,
                        candidate.importance,
                        candidate.source,
                        signature,
                        now,
                        now,
                        None,
                        False,
                        (),
                        (),
                        candidate.body,
                        relative,
                    )
                    self._write_page(new_root, page)
            archive_root = account / "archive" / "legacy-memory"
            self._secure_directory(archive_root)
            destination = self._available_archive_path(archive_root, label)
            shutil.move(str(old_root), str(destination))
            self._fsync_directory(archive_root)
            self._rebuild_scope(new_root)

    @staticmethod
    def _available_archive_path(root: Path, name: str) -> Path:
        candidate = root / name
        index = 2
        while candidate.exists():
            candidate = root / f"{name}-{index}"
            index += 1
        return candidate

    def _available_path(self, root: Path, name: str) -> str:
        candidate = name
        index = 2
        while (root / f"{candidate}.md").exists():
            candidate = f"{name}-{index}"
            index += 1
        return f"{candidate}.md"

    def _safe_page_path(self, root: Path, relative_path: str, *, must_exist: bool = False) -> Path:
        pure = PurePosixPath(str(relative_path))
        if pure.is_absolute() or len(pure.parts) != 1 or pure.name in {"", ".", "..", "MEMORY.md"} or pure.suffix != ".md":
            raise ValidationError("Memory Page path must be one relative Markdown filename")
        candidate = root / pure.name
        if candidate.is_symlink():
            raise StateError("Memory Page must not be a symbolic link")
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ValidationError("Memory Page path escapes its scope") from exc
        if must_exist and not resolved.is_file():
            raise FileNotFoundError(relative_path)
        return resolved

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            lock_path = self.root / ".memory.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                if os.name == "posix":
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _atomic_write(self, path: Path, content: str) -> None:
        self._secure_directory(path.parent)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600, follow_symlinks=False)
            self._fsync_directory(path.parent)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _secure_directory(self, path: Path) -> None:
        if os.name == "posix":
            for candidate in (path, *path.parents):
                if candidate.is_symlink():
                    raise StateError(f"Memory path must not contain a symbolic link: {candidate}")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise StateError(f"Memory path is not a directory: {path}")
        if os.name == "posix":
            if path.stat(follow_symlinks=False).st_uid != os.getuid():
                raise StateError(f"Memory directory is not owned by the current user: {path}")
            os.chmod(path, 0o700, follow_symlinks=False)

    @staticmethod
    def _verify_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise StateError(f"Memory Page is not a regular owned file: {path}")
        if os.name == "posix":
            if path.stat(follow_symlinks=False).st_uid != os.getuid():
                raise StateError(f"Memory Page is not owned by the current user: {path}")
            os.chmod(path, 0o600, follow_symlinks=False)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _text(self, value: Any, *, label: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{label} must be a string")
        clean = self._sanitize(value).strip()
        if not clean:
            raise ValidationError(f"{label} cannot be empty")
        if len(clean) > maximum:
            raise ValidationError(f"{label} must be at most {maximum} characters")
        return clean

    def _new_id(self) -> str:
        now = self._aware_now()
        entropy = hashlib.sha256(f"{now.isoformat()}:{os.urandom(16).hex()}".encode()).hexdigest()[:8]
        return f"mem-{now:%Y%m%d-%H%M%S}-{entropy}"

    def _aware_now(self) -> datetime:
        value = self._now()
        return value.astimezone() if value.tzinfo is None else value

    def _timestamp(self) -> str:
        return self._aware_now().isoformat(timespec="seconds")

    @staticmethod
    def _memory_id(value: Any, *, label: str = "Memory ID") -> str:
        text = str(value)
        if not _PAGE_ID.fullmatch(text):
            raise ValidationError(f"{label} is invalid")
        return text


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        raise ValidationError("Memory Page must start with YAML frontmatter")
    marker = raw.find("\n---\n", 4)
    if marker < 0:
        raise ValidationError("Memory Page frontmatter is not closed")
    try:
        metadata = yaml.safe_load(raw[4:marker])
    except yaml.YAMLError as exc:
        raise ValidationError("Memory Page frontmatter is invalid YAML") from exc
    if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
        raise ValidationError("Memory Page frontmatter must be an object")
    return metadata, raw[marker + 5 :].lstrip("\n")


def _key_hash(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Memory isolation key cannot be empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(value: Any) -> MemoryScope:
    text = str(value).strip().casefold()
    aliases = {"user": "private", "repository": "project", "repo": "project"}
    text = aliases.get(text, text)
    if text not in _SCOPES:
        raise ValidationError("Memory scope must be private or project")
    return text  # type: ignore[return-value]


def _memory_type(value: Any) -> MemoryType:
    text = str(value).strip().casefold()
    if text not in _TYPES:
        raise ValidationError("Memory type must be user, feedback, project, or reference")
    return text  # type: ignore[return-value]


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", normalized)).strip("-")[:100]


def _safe_name(value: Any) -> str:
    text = str(value)
    if not _SAFE_NAME.fullmatch(text):
        raise ValidationError("Memory name/category/tag is not a safe slug")
    return text


def _source(value: Any) -> str:
    text = str(value)
    if not _SOURCE.fullmatch(text):
        raise ValidationError("Memory source is invalid")
    return text


def _signature(value: Any) -> str:
    text = str(value)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", text):
        raise ValidationError("Memory signature is invalid")
    return text


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValidationError("Memory timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValidationError("Memory timestamp must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValidationError(f"Memory {label} must be an integer from {minimum} to {maximum}")
    return value


def _optional_positive_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"Memory {label} must be null or a positive integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"Memory {label} must be a boolean")
    return value


def _string(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Memory {label} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum:
        raise ValidationError(f"Memory {label} is too long")
    return text


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20 or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"Memory {label} must be a list of at most 20 strings")
    return value


def _description(body: str) -> str:
    paragraph = next((part.strip() for part in body.split("\n\n") if part.strip()), body.strip())
    one_line = " ".join(paragraph.split())
    return one_line if len(one_line) <= 240 else one_line[:237].rstrip() + "..."


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = ["SCHEMA_VERSION", "MemoryPageStore"]
