"""MCP 基础公共工具。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from gitagent.domain.errors import ToolExecutionError, ValidationError


def safe_repository_path(path: str) -> str:
    """校验远程仓库内路径，拒绝空路径和目录穿越。"""
    normalized = str(PurePosixPath(path.strip().lstrip("/")))
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in normalized:
        raise ValidationError(f"invalid repository path: {path!r}")
    return normalized


def parse_file_read_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the shared ranged-request contract used by both repository backends."""

    if not requests:
        raise ValidationError("read_files requires one or more requests")
    if len(requests) > 20:
        raise ValidationError("read_files is limited to 20 targeted requests")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in requests:
        if not isinstance(raw, dict):
            raise ValidationError("each read_files request must be an object")
        unknown = set(raw) - {"path", "start_line", "limit"}
        if unknown:
            raise ValidationError(f"file read request contains unknown field: {min(unknown)}")
        path = safe_repository_path(str(raw.get("path") or ""))
        if path in seen:
            raise ValidationError(f"read_files contains duplicate path: {path}")
        seen.add(path)
        start_line = raw.get("start_line", 1)
        limit = raw.get("limit", 200)
        if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
            raise ValidationError("start_line must be a positive integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        parsed.append({"path": path, "start_line": start_line, "limit": min(limit, 400)})
    return parsed


def select_file_lines(
    content: str,
    *,
    start_line: int,
    limit: int,
    max_characters: int = 120_000,
) -> dict[str, Any]:
    """Return complete lines only, so continuation never skips partially returned content."""

    start_line = max(1, start_line)
    limit = max(1, min(limit, 400))
    lines = content.splitlines(keepends=True)
    selected_lines: list[str] = []
    characters = 0
    for line in lines[start_line - 1 : start_line - 1 + limit]:
        if len(line) > max_characters:
            raise ToolExecutionError(f"line {start_line + len(selected_lines)} exceeds the maximum readable line size")
        if selected_lines and characters + len(line) > max_characters:
            break
        selected_lines.append(line)
        characters += len(line)
    end_line = start_line + len(selected_lines) - 1
    if not selected_lines:
        end_line = len(lines)
    return {
        "start_line": start_line,
        "end_line": end_line,
        "content": "".join(selected_lines),
        "truncated": end_line < len(lines),
    }


__all__ = ["parse_file_read_requests", "safe_repository_path", "select_file_lines"]
