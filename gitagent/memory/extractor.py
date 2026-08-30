"""Isolated, transcript-free extraction of durable Memory candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from gitagent.domain.errors import ValidationError
from gitagent.domain.models import SessionScope
from gitagent.harness.context import estimate_tokens
from gitagent.infra.persistence import SessionManager
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .models import MemoryCandidate
from .pages import MemoryPageStore
from .tools import MemoryTools

MAX_EXTRACTOR_TURNS = 5
MAX_CANDIDATES = 8
_PROMPTS = get_prompt_library()

_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": MAX_CANDIDATES,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 120},
                    "description": {"type": "string", "maxLength": 500},
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                    },
                    "scope": {"type": "string", "enum": ["private", "project"]},
                    "category": {"type": "string", "maxLength": 80},
                    "importance": {"type": "integer", "minimum": 0, "maximum": 5},
                    "ttl_days": {"type": ["integer", "null"], "minimum": 1},
                    "tags": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "maxLength": 80},
                    },
                    "body": {"type": "string", "maxLength": 8000},
                },
                "required": [
                    "name",
                    "description",
                    "type",
                    "scope",
                    "category",
                    "importance",
                    "ttl_days",
                    "tags",
                    "body",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class MemoryExtractionContext:
    scope: SessionScope
    repository_full_name: str
    extracted_through_seq: int
    target_through_seq: int
    conversation_units: tuple[dict[str, Any], ...]
    memory_index: str


@dataclass(frozen=True)
class MemoryExtractionResult:
    through_seq: int
    candidates: int
    written: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    written_labels: tuple[str, ...] = ()
    skipped_labels: tuple[str, ...] = ()

    @property
    def noop(self) -> bool:
        return not self.written


class MemoryExtractionContextBuilder:
    """Project only bounded Main-visible completed Turns around an incremental cursor."""

    def __init__(
        self,
        sessions: SessionManager,
        memory: MemoryPageStore,
        *,
        input_budget_tokens: int,
        max_context_turns: int = 20,
    ) -> None:
        if not isinstance(input_budget_tokens, int) or isinstance(input_budget_tokens, bool) or input_budget_tokens < 4096:
            raise ValueError("Memory extraction input budget must be at least 4096 tokens")
        self.sessions = sessions
        self.memory = memory
        self.input_budget_tokens = input_budget_tokens
        self.max_context_turns = max_context_turns

    def build(
        self,
        scope: SessionScope,
        repository_full_name: str,
        *,
        extracted_through_seq: int,
        target_through_seq: int,
    ) -> MemoryExtractionContext:
        if target_through_seq <= extracted_through_seq:
            raise ValidationError("Memory extraction target must be after its cursor")
        completed = {
            turn.seq
            for turn in self.sessions.list_turns(
                scope.account_key, scope.repository_key, scope.session_id
            )
            if turn.status == "completed" and turn.seq <= target_through_seq
        }
        projection: dict[int, dict[str, Any]] = {
            seq: {
                "seq": seq,
                "evidence": seq > extracted_through_seq,
                "user": "",
                "assistant": "",
                "route": None,
                "domain_summary": "",
            }
            for seq in completed
        }
        for event in self.sessions.event_log.iter_events(scope):
            seq = event.turn_seq
            if seq not in projection:
                continue
            unit = projection[seq]
            if event.type == "user_message" and event.agent in {None, "main"}:
                unit["user"] = _bounded(event.data.get("content"), 2_000)
            elif event.type == "assistant_message" and event.agent in {None, "main"}:
                unit["assistant"] = _bounded(event.data.get("content"), 2_000)
            elif event.type == "route_selected":
                unit["route"] = event.data.get("route", event.data.get("routes"))
            elif event.type == "workflow_outcome":
                unit["domain_summary"] = _bounded(event.data.get("summary"), 2_000)
        ordered = tuple(projection[seq] for seq in sorted(projection))
        evidence = tuple(
            unit for unit in ordered if int(unit["seq"]) > extracted_through_seq
        )
        context_only = tuple(
            unit for unit in ordered if int(unit["seq"]) <= extracted_through_seq
        )
        context_slots = max(0, self.max_context_turns - len(evidence))
        units = (*context_only[-context_slots:], *evidence) if context_slots else evidence
        context = MemoryExtractionContext(
            scope=scope,
            repository_full_name=repository_full_name,
            extracted_through_seq=extracted_through_seq,
            target_through_seq=target_through_seq,
            conversation_units=units,
            memory_index=self.memory.read_index(scope.account_key, scope.repository_key),
        )
        while (
            _context_tokens(context) > self.input_budget_tokens
            and units
            and not bool(units[0]["evidence"])
        ):
            units = units[1:]
            context = MemoryExtractionContext(
                scope,
                repository_full_name,
                extracted_through_seq,
                target_through_seq,
                units,
                context.memory_index,
            )
        if _context_tokens(context) > self.input_budget_tokens:
            raise ValidationError("Memory extraction context exceeds its isolated input budget")
        return context


class MemoryExtractor:
    """A short-lived meta-agent with no shell, repository, GitHub, or Session tools."""

    max_turns = MAX_EXTRACTOR_TURNS

    def __init__(self, reasoner: Reasoner, memory: MemoryPageStore) -> None:
        self.reasoner = reasoner
        self.memory = memory

    def extract(self, context: MemoryExtractionContext) -> MemoryExtractionResult:
        payload = {
            "repository": context.repository_full_name,
            "extracted_through_seq": context.extracted_through_seq,
            "target_through_seq": context.target_through_seq,
            "conversation": list(context.conversation_units),
            "memory_index": context.memory_index,
        }
        raw = self.reasoner.complete_structured_messages(
            messages=[
                {"role": "system", "content": _PROMPTS.text("system.memory_extractor")},
                {
                    "role": "user",
                    "content": _PROMPTS.render(
                        "agents.memory_extractor",
                        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                },
            ],
            schema=_CANDIDATE_SCHEMA,
            tool_name="extract_persistent_memories",
            tools=None,
        )
        candidates = _candidates(raw)
        tools = MemoryTools(
            self.memory, context.scope.account_key, context.scope.repository_key
        )
        written: list[str] = []
        skipped: list[str] = []
        written_labels: list[str] = []
        skipped_labels: list[str] = []
        for candidate in candidates:
            previous = tools.read_memory_file(
                scope=candidate.scope, identifier=candidate.name
            )
            page, created = tools.write_memory_file(candidate)
            identity = f"{page.scope}:{page.id}"
            changed = created or (
                previous is not None and previous.signature != page.signature
            )
            label = f"{page.scope}:{page.name}"
            if changed:
                written.append(identity)
                written_labels.append(label)
            else:
                skipped.append(identity)
                skipped_labels.append(label)
        return MemoryExtractionResult(
            through_seq=context.target_through_seq,
            candidates=len(candidates),
            written=tuple(written),
            skipped=tuple(skipped),
            written_labels=tuple(written_labels),
            skipped_labels=tuple(skipped_labels),
        )


def _candidates(raw: dict[str, Any]) -> tuple[MemoryCandidate, ...]:
    values = raw.get("candidates") if isinstance(raw, dict) else None
    if not isinstance(values, list):
        raise ValidationError("Memory Extractor must return a candidates list")
    result: list[MemoryCandidate] = []
    for item in values:
        if not isinstance(item, dict):
            raise ValidationError("Memory candidate must be an object")
        result.append(
            MemoryCandidate(
                name=str(item["name"]),
                description=str(item["description"]),
                type=str(item["type"]),  # type: ignore[arg-type]
                scope=str(item["scope"]),  # type: ignore[arg-type]
                category=str(item["category"]),
                importance=int(item["importance"]),
                ttl_days=int(item["ttl_days"]) if item["ttl_days"] is not None else None,
                tags=tuple(str(value) for value in item["tags"]),
                body=str(item["body"]),
                source="extractor",
            )
        )
    return tuple(result)


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    half = max(1, (limit - 20) // 2)
    return text[:half] + " … content omitted … " + text[-half:]


def _context_tokens(context: MemoryExtractionContext) -> int:
    return estimate_tokens(json.dumps(context, default=lambda value: value.__dict__, ensure_ascii=False))


__all__ = [
    "MAX_EXTRACTOR_TURNS",
    "MemoryExtractionContext",
    "MemoryExtractionContextBuilder",
    "MemoryExtractionResult",
    "MemoryExtractor",
]
