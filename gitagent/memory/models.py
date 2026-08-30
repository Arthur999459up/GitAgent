"""Contracts for Claude Code-like persistent Memory Pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

MemoryType = Literal["user", "feedback", "project", "reference"]
MemoryScope = Literal["private", "project"]


@dataclass(frozen=True)
class MemoryCandidate:
    """Sanitized-by-the-store content proposed by a human or extractor."""

    name: str
    description: str
    type: MemoryType
    scope: MemoryScope
    body: str
    category: str = "general"
    importance: int = 3
    source: str = "extractor"
    ttl_days: int | None = None
    tags: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryPage:
    """One Markdown page; its containing directory determines physical isolation."""

    schema_version: int
    id: str
    name: str
    description: str
    type: MemoryType
    scope: MemoryScope
    category: str
    importance: int
    source: str
    signature: str
    created_at: str
    updated_at: str
    ttl_days: int | None
    disabled: bool
    supersedes: tuple[str, ...]
    tags: tuple[str, ...]
    body: str
    relative_path: str

    def expired(self, now: datetime) -> bool:
        if self.ttl_days is None:
            return False
        return _datetime(self.updated_at) + timedelta(days=self.ttl_days) <= now

    def stale(self, now: datetime, *, days: int = 30) -> bool:
        return _datetime(self.updated_at) + timedelta(days=days) < now

    def active(self, now: datetime) -> bool:
        return not self.disabled and not self.expired(now)

    def status(self, now: datetime) -> str:
        if self.disabled:
            return "disabled"
        if self.expired(now):
            return "expired"
        if self.stale(now):
            return "stale"
        return "active"


@dataclass(frozen=True)
class MemorySearchHit:
    id: str
    name: str
    type: MemoryType
    scope: MemoryScope
    description: str
    importance: int
    updated_at: str
    relative_path: str
    stale: bool
    body: str
    score: float


@dataclass(frozen=True)
class PersistentMemoryContext:
    """Ephemeral Main/Domain input; never persisted as a conversation message."""

    index: str = ""
    selected_pages: tuple[MemorySearchHit, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.index or self.selected_pages)


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


__all__ = [
    "MemoryCandidate",
    "MemoryPage",
    "MemoryScope",
    "MemorySearchHit",
    "MemoryType",
    "PersistentMemoryContext",
]
