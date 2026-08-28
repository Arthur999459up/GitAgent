"""Domain contracts for reflection, durable knowledge, and learning evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .models import SessionScope


class LearningAction(str, Enum):
    """A semantic consolidation decision made by MainAgent reflection."""

    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"
    DISCARD = "discard"


@dataclass(frozen=True)
class KnowledgeRecord:
    """One user-visible item in the durable, non-authoritative knowledge store."""

    knowledge_id: str
    account_key: str
    repository_key: str | None
    scope: str
    kind: str
    topic: str
    content: str
    conditions: str
    source: str
    confidence: str
    provenance: tuple[dict[str, Any], ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DomainInteractionRecord:
    """High-fidelity Domain workflow evidence kept outside conversation history."""

    interaction_id: str
    account_key: str
    repository_key: str
    repository_full_name: str
    session_id: str
    origin_turn_seq: int
    completed_turn_seq: int
    agent: str
    entity_type: str | None
    entity_id: str | None
    goal: str
    evidence: dict[str, Any]
    reflection_status: str
    reflection_error: str
    created_at: str
    reflected_at: str | None

    @property
    def scope(self) -> SessionScope:
        return SessionScope(self.account_key, self.repository_key, self.session_id)


@dataclass(frozen=True)
class ReflectionContext:
    """A temporary working set that never enters MainAgent conversation context."""

    scope: SessionScope
    repository_full_name: str
    trigger: str
    conversation_units: tuple[dict[str, Any], ...] = ()
    interaction: dict[str, Any] | None = None
    existing_knowledge: tuple[KnowledgeRecord, ...] = ()
    selection_metadata: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class LearningCandidate:
    """One model-proposed knowledge consolidation operation."""

    action: LearningAction
    scope: str
    kind: str
    topic: str
    content: str
    conditions: str
    target_id: str
    reason: str
    evidence_strength: str
    correction: bool


@dataclass(frozen=True)
class LearningProposal:
    """Structured output of one MainAgent reflection invocation."""

    candidates: tuple[LearningCandidate, ...]
    summary: str


@dataclass(frozen=True)
class KnowledgeChange:
    """One durable Knowledge mutation produced by consolidation."""

    action: LearningAction
    record: KnowledgeRecord


@dataclass(frozen=True)
class ConsolidationResult:
    """Durable effects after validating and consolidating a proposal."""

    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    changes: tuple[KnowledgeChange, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


__all__ = [
    "ConsolidationResult",
    "DomainInteractionRecord",
    "KnowledgeChange",
    "KnowledgeRecord",
    "LearningAction",
    "LearningCandidate",
    "LearningProposal",
    "ReflectionContext",
]
