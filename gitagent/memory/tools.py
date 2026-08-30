"""Restricted operations used only by Memory Extractor and Dream paths."""

from __future__ import annotations

from .models import MemoryCandidate, MemoryPage
from .pages import MemoryPageStore


class MemoryTools:
    def __init__(self, store: MemoryPageStore, account_key: str, repository_key: str) -> None:
        self.store = store
        self.account_key = account_key
        self.repository_key = repository_key

    def read_memory_file(self, *, scope: str, identifier: str) -> MemoryPage | None:
        return self.store.read_page(
            self.account_key,
            self.repository_key,
            scope=scope,
            identifier=identifier,
            include_inactive=True,
        )

    def write_memory_file(self, candidate: MemoryCandidate) -> tuple[MemoryPage, bool]:
        return self.store.write_candidate(self.account_key, self.repository_key, candidate)

    def disable_memory_file(self, *, scope: str, identifier: str) -> MemoryPage | None:
        return self.store.disable(
            self.account_key,
            self.repository_key,
            scope=scope,
            identifier=identifier,
            allow_manual=False,
        )


__all__ = ["MemoryTools"]
