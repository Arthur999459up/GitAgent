"""Small frontmatter-backed retrieval for Main and each isolated Domain agent."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from gitagent.domain.errors import ValidationError

from .models import MemoryPage, MemorySearchHit, PersistentMemoryContext
from .pages import MemoryPageStore

MAX_SEARCH_HITS = 5
DEFAULT_CONTEXT_BYTES = 20 * 1024
STALE_WARNING = (
    "WARNING: This memory has not been updated for more than 30 days. "
    "Verify it against current repository/GitHub evidence before relying on it."
)


class MemorySearch:
    def __init__(
        self,
        store: MemoryPageStore,
        *,
        now: Callable[[], datetime] | None = None,
        context_bytes: int = DEFAULT_CONTEXT_BYTES,
    ) -> None:
        if not isinstance(context_bytes, int) or isinstance(context_bytes, bool) or context_bytes < 1024:
            raise ValueError("Memory context budget must be at least 1024 bytes")
        self.store = store
        self._now = now or (lambda: datetime.now().astimezone())
        self.context_bytes = context_bytes

    def search(
        self,
        account_key: str,
        repository_key: str,
        query: str,
        *,
        limit: int = MAX_SEARCH_HITS,
    ) -> tuple[MemorySearchHit, ...]:
        if not isinstance(query, str):
            raise ValidationError("Memory search query must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_SEARCH_HITS:
            raise ValidationError(f"Memory search limit must be from 0 to {MAX_SEARCH_HITS}")
        normalized = " ".join(query.casefold().split())
        if not normalized or limit == 0:
            return ()
        query_terms = _terms(normalized)
        now = self._aware_now()
        ranked: list[tuple[float, MemoryPage]] = []
        for page in self.store.list_pages(
            account_key, repository_key, include_inactive=False
        ):
            score = _score(page, normalized, query_terms)
            if score > 0:
                ranked.append((score, page))
        ranked.sort(
            key=lambda item: (
                -item[0],
                -item[1].importance,
                -datetime.fromisoformat(item[1].updated_at).timestamp(),
                item[1].name,
                item[1].id,
            )
        )
        hits = tuple(
            MemorySearchHit(
                id=page.id,
                name=page.name,
                type=page.type,
                scope=page.scope,
                description=page.description,
                importance=page.importance,
                updated_at=page.updated_at,
                relative_path=page.relative_path,
                stale=page.stale(now),
                body=page.body,
                score=score,
            )
            for score, page in ranked[:limit]
        )
        return self._clip(hits)

    def context(
        self,
        account_key: str,
        repository_key: str,
        query: str,
    ) -> PersistentMemoryContext:
        return PersistentMemoryContext(
            index=self.store.read_index(account_key, repository_key),
            selected_pages=self.search(account_key, repository_key, query),
        )

    @staticmethod
    def render(hits: tuple[MemorySearchHit, ...]) -> str:
        sections: list[str] = []
        for hit in hits:
            lines = [
                f"### Memory: {hit.name}",
                f"Type: {hit.type}",
                f"Scope: {hit.scope}",
            ]
            if hit.stale:
                lines.extend(["", STALE_WARNING])
            lines.extend(["", hit.body])
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def _clip(self, hits: tuple[MemorySearchHit, ...]) -> tuple[MemorySearchHit, ...]:
        selected: list[MemorySearchHit] = []
        used = 0
        for hit in hits:
            empty = replace(hit, body="")
            overhead = len(self.render((empty,)).encode("utf-8"))
            separator = 2 if selected else 0
            available = self.context_bytes - used - separator - overhead
            if available <= 0:
                break
            body = _clip_utf8(hit.body, available)
            clipped = replace(hit, body=body)
            selected.append(clipped)
            used += separator + len(self.render((clipped,)).encode("utf-8"))
        return tuple(selected)

    def _aware_now(self) -> datetime:
        value = self._now()
        return value.astimezone() if value.tzinfo is None else value


def _terms(value: str) -> set[str]:
    words = {
        item
        for item in re.findall(r"[^\W_]+", value, flags=re.UNICODE)
        if len(item) > 1
    }
    expanded = set(words)
    for word in words:
        if len(word) >= 2 and any(ord(character) > 127 for character in word):
            expanded.update(word[index : index + 2] for index in range(len(word) - 1))
    return expanded


def _score(page: MemoryPage, normalized_query: str, query_terms: set[str]) -> float:
    fields = {
        "name": page.name.replace("-", " ").casefold(),
        "description": page.description.casefold(),
        "tags": " ".join(page.tags).replace("-", " ").casefold(),
        "category": page.category.replace("-", " ").casefold(),
    }
    score = 0.0
    weights = {"name": 8.0, "description": 5.0, "tags": 4.0, "category": 2.0}
    for field, text in fields.items():
        if normalized_query in text:
            score += weights[field] * 2
        overlap = query_terms & _terms(text)
        score += len(overlap) * weights[field]
    if score <= 0:
        return 0.0
    return score + page.importance * 0.25


def _clip_utf8(value: str, budget: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= budget:
        return value
    marker = "\n[Memory body truncated to context budget]"
    marker_bytes = marker.encode()
    if budget <= len(marker_bytes):
        return ""
    head = encoded[: budget - len(marker_bytes)].decode("utf-8", errors="ignore")
    return head + marker


__all__ = [
    "DEFAULT_CONTEXT_BYTES",
    "MAX_SEARCH_HITS",
    "STALE_WARNING",
    "MemorySearch",
]
