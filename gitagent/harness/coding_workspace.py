"""Isolated Git worktree lifecycle for one CodingAgent patch task."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gitagent.domain.errors import ValidationError, WorkflowError
from gitagent.domain.models import RepositoryRef
from gitagent.harness.file_access import safe_repository_path

_REPOSITORIES_ROOT = Path("/home/starry/intern/AGENT/Git-worktrees/repositories")
_WORKTREES_ROOT = Path("/home/starry/intern/AGENT/Git-worktrees/worktrees")
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40,64}")
_SAFE_TASK = re.compile(r"[^A-Za-z0-9._-]+")
_GIT_EXECUTABLE = shutil.which("git") or "git"


class CodingWorkspace:
    """Own one detached worktree and its revision/verification bookkeeping."""

    def __init__(
        self,
        github: Any,
        *,
        repository: str,
        source_ref: str,
        task_id: str,
        coordinator: Any,
    ) -> None:
        if not _COMMIT_SHA.fullmatch(source_ref):
            raise ValidationError("CodingWorkspace source_ref must be an exact commit SHA")
        try:
            repository_ref = RepositoryRef.parse(repository)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        safe_task = _SAFE_TASK.sub("-", task_id).strip("-.") or "coding"
        self.github = github
        self.coordinator = coordinator
        self.repository = str(repository_ref)
        self.source_ref = source_ref.lower()
        self.repository_root = _REPOSITORIES_ROOT / repository_ref.owner / repository_ref.name
        self.root = _WORKTREES_ROOT / safe_task
        self.revision = 0
        self.last_validated_revision: int | None = None
        self._prepared = False
        self._cleaned = False

    @classmethod
    def create(
        cls,
        github: Any,
        *,
        repository: str,
        source_ref: str,
        task_id: str,
        coordinator: Any,
    ) -> CodingWorkspace:
        workspace = cls(
            github,
            repository=repository,
            source_ref=source_ref,
            task_id=task_id,
            coordinator=coordinator,
        )
        try:
            with workspace._repository_cache_claim():
                workspace._prepare()
        except BaseException:
            workspace.cleanup(suppress_errors=True)
            raise
        return workspace

    def record_mutation(self) -> None:
        self.revision += 1

    def record_validation(self) -> None:
        self.last_validated_revision = self.revision

    def worktree_state(self) -> tuple[bytes, tuple[tuple[str, bytes], ...]]:
        """Capture Git-visible content directly, without inventing a revision hash."""

        self._require_prepared()
        tracked = self._git(
            [
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "HEAD",
                "--",
            ],
            cwd=self.root,
            text=False,
        ).stdout
        raw_untracked = self._git(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=self.root,
            text=False,
        ).stdout
        paths = raw_untracked.decode(
            "utf-8", errors="surrogateescape"
        ).split("\0")
        untracked: list[tuple[str, bytes]] = []
        for raw_path in paths:
            if not raw_path:
                continue
            path = safe_repository_path(raw_path)
            target = self.root / path
            metadata = target.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                content = os.fsencode(os.readlink(target))
            elif stat.S_ISREG(metadata.st_mode):
                content = target.read_bytes()
            else:
                content = f"mode:{metadata.st_mode}".encode("ascii")
            untracked.append((path, content))
        return tracked, tuple(untracked)

    def snapshot(self) -> dict[str, Any]:
        """Read the final changed paths, Git patch, and changed-file contents."""

        self._require_prepared()
        tracked = self._name_status()
        untracked = self._git(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=self.root,
            text=False,
        ).stdout.decode("utf-8", errors="surrogateescape").split("\0")
        untracked = [safe_repository_path(path) for path in untracked if path]

        added: set[str] = set(untracked)
        modified: set[str] = set()
        deleted: set[str] = set()
        for status, raw_path in tracked:
            path = safe_repository_path(raw_path)
            if status.startswith("A"):
                added.add(path)
            elif status.startswith("D"):
                deleted.add(path)
            else:
                modified.add(path)

        modified -= added
        deleted -= added
        changed = sorted(added | modified | deleted)
        files: dict[str, str] = {}
        for path in sorted(added | modified):
            target = (self.root / path).resolve(strict=True)
            try:
                target.relative_to(self.root.resolve())
            except ValueError as exc:
                raise WorkflowError(f"changed file escapes CodingWorkspace: {path}") from exc
            if not target.is_file():
                raise WorkflowError(f"changed path is not a regular file: {path}")
            files[path] = target.read_text(encoding="utf-8", errors="replace")

        tracked_patch = self._git(
            ["diff", "--no-ext-diff", "--no-renames", "HEAD", "--"],
            cwd=self.root,
        ).stdout
        patch_parts = [tracked_patch]
        for path in sorted(added & set(untracked)):
            result = self._git(
                ["diff", "--no-index", "--", "/dev/null", path],
                cwd=self.root,
                allowed_exit_codes=(0, 1),
            )
            patch_parts.append(result.stdout)
        return {
            "added_files": sorted(added),
            "modified_files": sorted(modified),
            "deleted_files": sorted(deleted),
            "changed_files": changed,
            "patch": "".join(patch_parts),
            "files": files,
        }

    def cleanup(self, *, suppress_errors: bool = False) -> None:
        if self._cleaned:
            return
        try:
            with self._repository_cache_claim():
                self._cleanup_locked(suppress_errors=suppress_errors)
        except BaseException:
            if suppress_errors:
                return
            raise

    def _cleanup_locked(self, *, suppress_errors: bool) -> None:
        errors: list[str] = []
        if self.repository_root.exists() and self.root.exists():
            result = self._git(
                ["worktree", "remove", "--force", str(self.root)],
                cwd=self.repository_root,
                allowed_exit_codes=(0, 1, 128),
            )
            if result.returncode != 0 and self.root.exists():
                errors.append(result.stderr.strip() or "git worktree remove failed")
        if self.root.exists():
            try:
                shutil.rmtree(self.root)
            except OSError as exc:
                errors.append(str(exc))
        if self.repository_root.exists():
            self._git(
                ["worktree", "prune"],
                cwd=self.repository_root,
                allowed_exit_codes=(0, 1, 128),
            )
        self._cleaned = not self.root.exists()
        self._prepared = False
        if errors and not suppress_errors:
            raise WorkflowError("CodingWorkspace cleanup failed: " + "; ".join(errors))

    @contextmanager
    def _repository_cache_claim(self) -> Iterator[None]:
        from gitagent.harness.execution import ResourceClaims

        key = f"repository-cache:{self.repository.casefold()}"
        with self.coordinator.claim_resources(ResourceClaims(write=(key,))):
            yield

    def _prepare(self) -> None:
        _REPOSITORIES_ROOT.mkdir(parents=True, exist_ok=True)
        _WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
        self.repository_root.parent.mkdir(parents=True, exist_ok=True)
        if not (self.repository_root / ".git").exists():
            if self.repository_root.exists():
                shutil.rmtree(self.repository_root)
            clone_url = str(self.github.get_clone_url(self.repository))
            self._run(
                [
                    _GIT_EXECUTABLE,
                    "clone",
                    "--no-checkout",
                    "--",
                    clone_url,
                    str(self.repository_root),
                ],
                cwd=self.repository_root.parent,
            )
        if not self._has_commit():
            self._git(["fetch", "--prune", "origin"], allowed_exit_codes=(0, 1, 128))
        if not self._has_commit():
            self._git(
                ["fetch", "origin", self.source_ref],
                allowed_exit_codes=(0, 1, 128),
            )
        if not self._has_commit():
            raise WorkflowError(
                f"source_ref {self.source_ref} is unavailable after fetch; refusing to change baseline"
            )
        if self.root.exists():
            shutil.rmtree(self.root)
        self._git(["worktree", "add", "--detach", str(self.root), self.source_ref])
        resolved = self._git(["rev-parse", "HEAD"], cwd=self.root).stdout.strip().lower()
        if resolved != self.source_ref:
            raise WorkflowError(
                f"CodingWorkspace baseline mismatch: expected {self.source_ref}, got {resolved}"
            )
        self._prepared = True

    def _has_commit(self) -> bool:
        if not self.repository_root.exists():
            return False
        result = self._git(
            ["cat-file", "-e", f"{self.source_ref}^{{commit}}"],
            allowed_exit_codes=(0, 1, 128),
        )
        return result.returncode == 0

    def _name_status(self) -> list[tuple[str, str]]:
        result = self._git(
            ["diff", "--name-status", "-z", "--no-renames", "HEAD", "--"],
            cwd=self.root,
            text=False,
        )
        fields = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        fields = [field for field in fields if field]
        if len(fields) % 2:
            raise WorkflowError("Git returned an invalid name-status payload")
        return [(fields[index], fields[index + 1]) for index in range(0, len(fields), 2)]

    def _git(
        self,
        arguments: list[str],
        *,
        cwd: Path | None = None,
        allowed_exit_codes: tuple[int, ...] = (0,),
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        return self._run(
            [_GIT_EXECUTABLE, *arguments],
            cwd=cwd or self.repository_root,
            allowed_exit_codes=allowed_exit_codes,
            text=text,
        )

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        allowed_exit_codes: tuple[int, ...] = (0,),
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        environment = os.environ.copy()
        token = str(getattr(self.github, "token", "") or "")
        if token:
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
                }
            )
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            timeout=120,
            check=False,
        )
        if completed.returncode not in allowed_exit_codes:
            stderr = (
                completed.stderr
                if isinstance(completed.stderr, str)
                else completed.stderr.decode("utf-8", errors="replace")
            )
            raise WorkflowError(
                f"Git workspace command failed ({completed.returncode}): {stderr[-2000:]}"
            )
        return completed

    def _require_prepared(self) -> None:
        if not self._prepared or self._cleaned or not self.root.is_dir():
            raise WorkflowError("CodingWorkspace is not active")
