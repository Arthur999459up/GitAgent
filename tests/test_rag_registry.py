from __future__ import annotations

import stat

from gitagent.capability.rag.models import (
    DocumentRecord,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from gitagent.capability.rag.registry import KnowledgeBaseRegistry


def test_registry_persists_knowledge_base_and_documents(tmp_path) -> None:
    registry = KnowledgeBaseRegistry(tmp_path / "rag" / "registry.db")
    document = DocumentRecord(
        knowledge_base_id="engineering",
        document_id="document-1",
        relative_path="review/testing.md",
        mtime_ns=10,
        size=20,
        content_hash="old-hash",
    )
    knowledge_base = KnowledgeBase(
        id="engineering",
        description="Engineering standards",
        source_directory=str(tmp_path / "knowledge"),
        status=KnowledgeBaseStatus.READY,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        documents=(document,),
    )

    registry.register(knowledge_base)

    assert stat.S_IMODE(registry.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600

    loaded = registry.get("engineering")
    assert loaded == knowledge_base
    assert registry.list() == (knowledge_base,)

    current = DocumentRecord(
        knowledge_base_id="engineering",
        document_id="document-1",
        relative_path="review/testing.md",
        mtime_ns=30,
        size=40,
        content_hash="new-hash",
    )
    registry.replace_documents(
        "engineering",
        (current,),
        status=KnowledgeBaseStatus.STALE,
        updated_at="2026-01-02T00:00:00+00:00",
    )
    changed = registry.get("engineering")
    assert changed is not None
    assert changed.status == KnowledgeBaseStatus.STALE
    assert changed.documents == (current,)

    assert registry.remove("engineering") == changed
    assert registry.get("engineering") is None
