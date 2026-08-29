"""Workspace-bounded native capabilities inspired by CoreCoder's tool duties."""

from __future__ import annotations

import difflib
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gitagent.domain.errors import PermissionDenied, ValidationError

from ..errors import (
    ProviderConflictError,
    ProviderExecutionError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..models import (
    AccessLevel,
    Capability,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRegistration,
    CapabilityStatus,
    InvocationContext,
)


@dataclass(frozen=True)
class NativeToolDefinition:
    id: str
    description: str
    access: AccessLevel
    handler: Callable[[dict[str, Any], InvocationContext], Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class NativeProvider:
    id = "native"
    _SKIP_DIRS = frozenset(
        {
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            ".tox",
            "dist",
            "build",
        }
    )

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        subagent_runner: Callable[[str, InvocationContext, frozenset[str]], Any]
        | None = None,
        permission_resolver: Callable[[InvocationContext], frozenset[str]]
        | None = None,
        memory_roots: Mapping[str, str | Path] | None = None,
        memory_read_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.is_dir():
            raise ValidationError(
                f"workspace root is not a directory: {self.workspace_root}"
            )
        self.read_roots = {"workspace": self.workspace_root}
        for name, value in dict(memory_roots or {}).items():
            if name not in {"user_memory", "repository_memory"}:
                raise ValidationError(f"unknown native read root: {name}")
            path = Path(value).resolve()
            if not path.is_dir():
                raise ValidationError(f"native read root is not a directory: {path}")
            self.read_roots[name] = path
        self.memory_read_callback = memory_read_callback
        self.subagent_runner = subagent_runner
        self.permission_resolver = permission_resolver
        self._definitions = self._build_definitions()

    def load(self) -> list[CapabilityRegistration]:
        return [
            CapabilityRegistration(
                Capability(
                    definition.id,
                    CapabilityKind.NATIVE_TOOL,
                    definition.description,
                    "native",
                    CapabilityStatus.AVAILABLE,
                    definition.access,
                    definition.input_schema,
                    definition.output_schema,
                ),
                CapabilityBinding(definition.id, self.id, definition),
            )
            for definition in self._definitions
        ]

    def invoke(
        self,
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        definition = binding.target
        if not isinstance(definition, NativeToolDefinition):
            raise TypeError("native binding target is invalid")
        return definition.handler(arguments, context)

    def _build_definitions(self) -> tuple[NativeToolDefinition, ...]:
        object_output = {"type": "object"}
        return (
            NativeToolDefinition(
                "native.read",
                "Read a UTF-8 file from the workspace or an authorized read-only Memory root.",
                AccessLevel.READ,
                self._read,
                _object_schema(
                    {
                        "path": {"type": "string", "minLength": 1},
                        "root": {
                            "type": "string",
                            "enum": ["workspace", "user_memory", "repository_memory"],
                        },
                        "offset": {"type": "integer", "minimum": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 4000},
                    },
                    ["path"],
                ),
                object_output,
            ),
            NativeToolDefinition(
                "native.glob",
                "Find workspace files matching a glob pattern, bounded to 100 results.",
                AccessLevel.READ,
                self._glob,
                _object_schema(
                    {
                        "pattern": {"type": "string", "minLength": 1},
                        "path": {"type": "string"},
                    },
                    ["pattern"],
                ),
                object_output,
            ),
            NativeToolDefinition(
                "native.grep",
                "Search workspace text with a regular expression and return bounded line matches.",
                AccessLevel.READ,
                self._grep,
                _object_schema(
                    {
                        "pattern": {"type": "string", "minLength": 1},
                        "path": {"type": "string"},
                        "include": {"type": ["string", "null"]},
                    },
                    ["pattern"],
                ),
                object_output,
            ),
            NativeToolDefinition(
                "native.now",
                "Return the current local date and time.",
                AccessLevel.READ,
                self._now,
                _object_schema({}, []),
                object_output,
            ),
            NativeToolDefinition(
                "native.write",
                "Create or completely overwrite one workspace text file.",
                AccessLevel.WRITE,
                self._write,
                _object_schema(
                    {
                        "path": {"type": "string", "minLength": 1},
                        "content": {"type": "string"},
                    },
                    ["path", "content"],
                ),
                object_output,
            ),
            NativeToolDefinition(
                "native.edit",
                "Replace one exact, unique text occurrence in an existing workspace file.",
                AccessLevel.WRITE,
                self._edit,
                _object_schema(
                    {
                        "path": {"type": "string", "minLength": 1},
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                    },
                    ["path", "old_text", "new_text"],
                ),
                object_output,
            ),
            NativeToolDefinition(
                "native.bash",
                "Execute one policy-approved command in the fixed workspace directory.",
                AccessLevel.WRITE,
                self._bash,
                _object_schema(
                    {
                        "command": {"type": "string", "minLength": 1},
                        "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
                    },
                    ["command"],
                ),
                object_output,
            ),
            NativeToolDefinition(
                "native.agent",
                "Run one restricted coding sub-agent with inherited effective permissions.",
                AccessLevel.READ,
                self._agent,
                _object_schema({"task": {"type": "string", "minLength": 1}}, ["task"]),
                object_output,
            ),
        )

    def _safe_path(
        self, value: str, *, must_exist: bool = False, root: str = "workspace"
    ) -> Path:
        try:
            authorized_root = self.read_roots[root]
        except KeyError as exc:
            raise PermissionDenied(
                f"read root is not authorized for this Session: {root}"
            ) from exc
        candidate = Path(value)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise PermissionDenied(
                "file paths must be relative and cannot contain '..'"
            )
        resolved = (authorized_root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(authorized_root)
        except ValueError as exc:
            raise PermissionDenied(
                "file path escapes the selected authorized root"
            ) from exc
        if root == "workspace" and self._memory_root_for(resolved) is not None:
            raise PermissionDenied(
                "Memory files must be read through their authorized named root"
            )
        if must_exist and not resolved.exists():
            raise FileNotFoundError(value)
        return resolved

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace_root).as_posix()

    def _memory_root_for(self, path: Path) -> str | None:
        for name, root in self.read_roots.items():
            if name == "workspace":
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return name
        return None

    def _safe_mutation_path(self, value: str, *, must_exist: bool = False) -> Path:
        path = self._safe_path(value, must_exist=must_exist)
        if self._memory_root_for(path) is not None:
            raise PermissionDenied("authorized Memory roots are read-only")
        return path

    def _read(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        del context
        root = str(arguments.get("root") or "workspace")
        path = self._safe_path(str(arguments["path"]), must_exist=True, root=root)
        if not path.is_file():
            raise ValidationError(f"path is not a file: {arguments['path']}")
        offset = int(arguments.get("offset", 1))
        limit = int(arguments.get("limit", 2000))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        relative = path.relative_to(self.read_roots[root]).as_posix()
        if root != "workspace" and self.memory_read_callback is not None:
            self.memory_read_callback(root, relative)
        return {
            "root": root,
            "path": relative,
            "offset": offset,
            "lines": [
                f"{offset + index}\t{line}" for index, line in enumerate(selected)
            ],
            "content": "\n".join(selected),
            "total_lines": len(lines),
            "truncated": offset - 1 + limit < len(lines),
        }

    def _glob(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        del context
        base = self._safe_path(str(arguments.get("path") or "."), must_exist=True)
        if not base.is_dir():
            raise ValidationError(
                f"glob path is not a directory: {arguments.get('path')}"
            )
        pattern = Path(str(arguments["pattern"]))
        if pattern.is_absolute() or any(part == ".." for part in pattern.parts):
            raise PermissionDenied(
                "glob patterns must be relative and cannot contain '..'"
            )
        hits: list[Path] = []
        for path in base.glob(pattern.as_posix()):
            resolved = path.resolve(strict=False)
            try:
                relative = resolved.relative_to(self.workspace_root)
            except ValueError:
                continue
            if (
                any(part in self._SKIP_DIRS for part in relative.parts)
                or self._memory_root_for(resolved) is not None
                or not resolved.is_file()
            ):
                continue
            hits.append(resolved)
        hits.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return {
            "matches": [self._relative(item) for item in hits[:100]],
            "truncated": len(hits) > 100,
        }

    def _grep(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        del context
        try:
            expression = re.compile(str(arguments["pattern"]))
        except re.error as exc:
            raise ValidationError(f"invalid regular expression: {exc}") from exc
        base = self._safe_path(str(arguments.get("path") or "."), must_exist=True)
        include = arguments.get("include")
        include_path = Path(str(include or "*"))
        if include_path.is_absolute() or any(
            part == ".." for part in include_path.parts
        ):
            raise PermissionDenied(
                "grep include patterns must be relative and cannot contain '..'"
            )
        candidates = [base] if base.is_file() else base.rglob(include_path.as_posix())
        matches: list[dict[str, Any]] = []
        scanned = 0
        for path in candidates:
            relative_parts = path.relative_to(base).parts if path != base else ()
            if (
                any(part in self._SKIP_DIRS for part in relative_parts)
                or not path.is_file()
            ):
                continue
            resolved = path.resolve(strict=False)
            if self._memory_root_for(resolved) is not None:
                continue
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError:
                continue
            scanned += 1
            if scanned > 5000:
                break
            try:
                text = resolved.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if expression.search(line):
                    matches.append(
                        {
                            "path": self._relative(resolved),
                            "line": line_number,
                            "text": line[:1000],
                        }
                    )
                    if len(matches) == 200:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": scanned > 5000}

    @staticmethod
    def _now(arguments: dict[str, Any], context: InvocationContext) -> dict[str, Any]:
        del arguments, context
        now = datetime.now().astimezone()
        return {"datetime": now.isoformat(), "timezone": str(now.tzinfo)}

    def _write(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        del context
        path = self._safe_mutation_path(str(arguments["path"]))
        if path.exists() and not path.is_file():
            raise ValidationError(f"path is not a file: {arguments['path']}")
        content = str(arguments["content"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "path": self._relative(path),
            "lines": len(content.splitlines()),
            "written": True,
        }

    def _edit(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        del context
        path = self._safe_mutation_path(str(arguments["path"]), must_exist=True)
        if not path.is_file():
            raise ValidationError(f"path is not a file: {arguments['path']}")
        content = path.read_text(encoding="utf-8")
        old_text = str(arguments["old_text"])
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ProviderConflictError(
                f"old_text must occur exactly once; found {occurrences}"
            )
        new_content = content.replace(old_text, str(arguments["new_text"]), 1)
        path.write_text(new_content, encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{self._relative(path)}",
                tofile=f"b/{self._relative(path)}",
                n=3,
            )
        )
        return {
            "path": self._relative(path),
            "edited": True,
            "diff": diff[:5000],
            "truncated": len(diff) > 5000,
        }

    def _bash(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        del context
        command = str(arguments["command"])
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise ValidationError(f"invalid shell syntax: {exc}") from exc
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TERM",
                "TMPDIR",
                "PYTHONPATH",
                "VIRTUAL_ENV",
            }
        }
        try:
            completed = subprocess.run(
                argv,
                cwd=self.workspace_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(arguments.get("timeout", 120)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderTimeoutError(
                f"command timed out after {exc.timeout}s"
            ) from exc
        except OSError as exc:
            raise ProviderExecutionError(f"command could not start: {exc}") from exc
        details = {
            "exit_code": completed.returncode,
            "stdout_tail": self._redact(completed.stdout[-4000:]),
            "stderr_tail": self._redact(completed.stderr[-4000:]),
        }
        if completed.returncode != 0:
            raise ProviderExecutionError(
                f"command exited with code {completed.returncode}", details=details
            )
        return details

    @staticmethod
    def _redact(value: str) -> str:
        redacted = value
        for key, secret in os.environ.items():
            if secret and any(
                marker in key.upper()
                for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
            ):
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def _agent(self, arguments: dict[str, Any], context: InvocationContext) -> Any:
        if self.subagent_runner is None or self.permission_resolver is None:
            raise ProviderUnavailableError("coding sub-agent runner is not configured")
        child_context = InvocationContext(
            run_id=context.run_id,
            session_id=context.session_id,
            agent_id="coding_subagent",
            repository=context.repository,
            delegation_depth=context.delegation_depth + 1,
        )
        parent = self.permission_resolver(context)
        child = self.permission_resolver(child_context)
        effective = frozenset(parent & child)
        restricted_context = InvocationContext(
            run_id=child_context.run_id,
            session_id=child_context.session_id,
            agent_id=child_context.agent_id,
            repository=child_context.repository,
            approval_id=child_context.approval_id,
            delegation_depth=child_context.delegation_depth,
            effective_capabilities=effective,
        )
        return self.subagent_runner(
            str(arguments["task"]), restricted_context, effective
        )


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
