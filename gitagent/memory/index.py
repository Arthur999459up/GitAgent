"""Rebuildable, bounded ``MEMORY.md`` indexes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .models import MemoryPage

INDEX_LINE_LIMIT = 200
INDEX_BYTE_LIMIT = 25 * 1024
TRUNCATION_WARNING = (
    "> WARNING: Memory index truncated. Additional memories exist but are not listed here."
)


def render_scope_index(pages: Iterable[MemoryPage], *, now: datetime) -> str:
    active = sorted(
        (page for page in pages if page.active(now)),
        key=lambda page: (-page.importance, page.name, page.id),
    )
    lines = ["# Memory", ""]
    entries = [
        f"- [{page.name}]({page.relative_path}) - {_one_line(page.description)}"
        for page in active
    ]
    truncated = False
    for entry in entries:
        candidate = "\n".join([*lines, entry]) + "\n"
        if len(lines) + 1 > INDEX_LINE_LIMIT or len(candidate.encode("utf-8")) > INDEX_BYTE_LIMIT:
            truncated = True
            break
        lines.append(entry)
    if truncated:
        while lines and len(
            ("\n".join([*lines, "", TRUNCATION_WARNING]) + "\n").encode("utf-8")
        ) > INDEX_BYTE_LIMIT:
            lines.pop()
        if len(lines) + 2 <= INDEX_LINE_LIMIT:
            lines.extend(["", TRUNCATION_WARNING])
    return "\n".join(lines).rstrip() + "\n"


def render_combined_index(private: str, project: str) -> str:
    return (
        "## Persistent memory index\n\n"
        "### private\n"
        f"{private.strip()}\n\n"
        "### project\n"
        f"{project.strip()}"
    ).strip()


def write_scope_index(root: Path, pages: Iterable[MemoryPage], *, now: datetime, writer: object) -> None:
    content = render_scope_index(pages, now=now)
    path = root / "MEMORY.md"
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    writer._atomic_write(path, content)


def _one_line(value: str) -> str:
    return " ".join(value.split()).replace("]", "\\]")


__all__ = [
    "INDEX_BYTE_LIMIT",
    "INDEX_LINE_LIMIT",
    "TRUNCATION_WARNING",
    "render_combined_index",
    "render_scope_index",
    "write_scope_index",
]
