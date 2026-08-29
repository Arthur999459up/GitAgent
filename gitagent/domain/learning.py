"""Small contracts for file-backed memory and isolated reflection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .models import SessionScope

MemoryType = Literal["memory", "experience"]
MemoryPriority = Literal["low", "normal", "high"]
MemoryScope = Literal["user", "repository"]


@dataclass(frozen=True)
class MemoryItem:
    """One item whose scope is determined by its containing directory."""

    relative_path: str
    type: MemoryType
    text: str
    priority: MemoryPriority
    last_accessed_at: str
    pinned: bool = False


@dataclass(frozen=True)
class TraceStep:
    """One causally useful, bounded step from a completed Domain workflow."""

    action: str
    result: str


@dataclass(frozen=True)
class LearningTrace:
    """Ephemeral evidence used once by reflection and never persisted."""

    goal: str
    outcome: str
    trajectory: tuple[TraceStep, ...]


@dataclass(frozen=True)
class ReflectionInput:
    """An isolated learning context, separate from the normal conversation."""

    scope: SessionScope
    repository_full_name: str
    trigger: str
    memory_index: str
    conversation_units: tuple[dict[str, Any], ...] = ()
    learning_trace: LearningTrace | None = None


@dataclass(frozen=True)
class ReflectionChanges:
    """The only three durable operations that reflection can request."""

    add: tuple[dict[str, str], ...] = ()
    replace: tuple[dict[str, str], ...] = ()
    delete: tuple[dict[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.add or self.replace or self.delete)


__all__ = [
    "LearningTrace",
    "MemoryItem",
    "MemoryPriority",
    "MemoryScope",
    "MemoryType",
    "ReflectionChanges",
    "ReflectionInput",
    "TraceStep",
]
