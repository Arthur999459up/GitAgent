from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from gitagent.capability.rag.ingestion import IngestionPipeline
from gitagent.capability.rag.manager import KnowledgeBaseManager
from gitagent.capability.rag.models import (
    Chunk,
    IndexedChunk,
    KnowledgeBaseStatus,
    RAGSettings,
    RAGUnavailableError,
    RetrievalResult,
)
from gitagent.domain.errors import ValidationError


class FakeEmbedding:
    def reset(self) -> None:
        pass


class FakeIngestion:
    def __init__(self, settings: RAGSettings) -> None:
        self.embedding = FakeEmbedding()
        self.base = IngestionPipeline(settings, embedding=self.embedding)

    def discover(self, source_directory):
        return self.base.discover(source_directory)

    def load_document(self, knowledge_base_id, source):
        return self.base.load_document(knowledge_base_id, source)

    @staticmethod
    def index(document):
        chunk_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{document.record.document_id}:{document.record.content_hash}",
            )
        )
        chunk = Chunk(
            knowledge_base_id=document.record.knowledge_base_id,
            document_id=document.record.document_id,
            document_name=document.record.relative_path,
            document_hash=document.record.content_hash,
            section_id=f"section-{document.record.document_id}",
            parent_section_id=None,
            chunk_id=chunk_id,
            chunk_index=0,
            previous_chunk_id=None,
            next_chunk_id=None,
            heading_path=("Title",),
            text=document.text,
            content_hash=hashlib.sha256(document.text.encode()).hexdigest(),
        )
        return (IndexedChunk(chunk, [0.0] * 1024),)

    def reset(self) -> None:
        pass


class FakeStore:
    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.points: dict[str, dict[str, IndexedChunk]] = {}
        self.fail_upsert = False

    def collection_exists(self, knowledge_base_id: str) -> bool:
        return knowledge_base_id in self.collections

    def create_collection(self, knowledge_base_id: str) -> None:
        self.collections.add(knowledge_base_id)
        self.points[knowledge_base_id] = {}

    def delete_collection(self, knowledge_base_id: str) -> None:
        self.collections.discard(knowledge_base_id)
        self.points.pop(knowledge_base_id, None)

    def upsert(self, knowledge_base_id: str, chunks) -> None:
        if self.fail_upsert:
            raise RAGUnavailableError("index unavailable")
        for indexed in chunks:
            self.points[knowledge_base_id][indexed.chunk.chunk_id] = indexed

    def delete_document_version(
        self, knowledge_base_id: str, document_id: str, content_hash=None
    ) -> None:
        points = self.points.get(knowledge_base_id, {})
        self.points[knowledge_base_id] = {
            key: value
            for key, value in points.items()
            if not (
                value.chunk.document_id == document_id
                and (content_hash is None or value.chunk.document_hash == content_hash)
            )
        }

    def reset(self) -> None:
        pass


class FakeRetrieval:
    @staticmethod
    def retrieve(knowledge_base, query):
        del query
        return RetrievalResult(
            knowledge_base.id,
            knowledge_base.status == KnowledgeBaseStatus.STALE,
            (),
            1.0,
        )

    def reset(self) -> None:
        pass


def _manager(tmp_path: Path) -> tuple[KnowledgeBaseManager, FakeStore]:
    settings = RAGSettings(
        knowledge_base_root=tmp_path / "knowledge_base",
        registry_path=tmp_path / "database/registry.db",
        qdrant_path=tmp_path / "database/qdrant",
        embedding_model_path=tmp_path / "models/embedding",
        reranker_model_path=tmp_path / "models/reranker",
    )
    ingestion = FakeIngestion(settings)
    store = FakeStore()
    manager = KnowledgeBaseManager(
        settings,
        ingestion=ingestion,
        store=store,
        retrieval=FakeRetrieval(),
    )
    return manager, store


def test_register_freshness_and_incremental_sync(tmp_path) -> None:
    manager, store = _manager(tmp_path)
    source = manager.knowledge_base_directory("engineering")
    source.mkdir(parents=True)
    handbook = source / "handbook.md"
    handbook.write_text("# Review\n\nAlways add tests.\n", encoding="utf-8")
    nested = source / "security"
    nested.mkdir()
    secure = nested / "secure.md"
    secure.write_text("# Security\n\nValidate input.\n", encoding="utf-8")

    registered = manager.register_directory(
        "engineering", "Engineering standards", source
    )
    assert source.stat().st_mode & 0o777 == 0o700
    assert registered.status == KnowledgeBaseStatus.READY
    assert [item.relative_path for item in registered.documents] == [
        "handbook.md",
        "security/secure.md",
    ]
    assert "engineering" in store.collections
    old_hashes = {
        item.relative_path: item.content_hash for item in registered.documents
    }

    original_updated_at = registered.updated_at
    handbook.write_text(
        "# Review\n\nAlways add tests and regression coverage.\n", encoding="utf-8"
    )
    stale = manager.freshness_check("engineering")
    assert stale.status == KnowledgeBaseStatus.STALE
    assert stale.updated_at == original_updated_at

    secure.unlink()
    added = source / "release.md"
    added.write_text("# Release\n\nUse a checklist.\n", encoding="utf-8")
    result = manager.sync("engineering")
    assert result.changed == ("handbook.md",)
    assert result.added == ("release.md",)
    assert result.deleted == ("security/secure.md",)
    synced = manager.get("engineering")
    assert synced.status == KnowledgeBaseStatus.READY
    assert [item.relative_path for item in synced.documents] == [
        "handbook.md",
        "release.md",
    ]
    active_points = tuple(store.points["engineering"].values())
    assert not any(
        point.chunk.document_name == "security/secure.md" for point in active_points
    )
    assert not any(
        point.chunk.document_name == "handbook.md"
        and point.chunk.document_hash == old_hashes["handbook.md"]
        for point in active_points
    )


def test_sync_failure_preserves_registry_fingerprint_and_old_index(tmp_path) -> None:
    manager, store = _manager(tmp_path)
    source = manager.knowledge_base_directory("engineering")
    source.mkdir(parents=True)
    document = source / "review.md"
    document.write_text("# Review\n\nOld guidance.\n", encoding="utf-8")
    registered = manager.register_knowledge_base(
        "engineering", "Engineering standards", source
    )
    old_hash = registered.documents[0].content_hash
    old_point_ids = set(store.points["engineering"])

    document.write_text(
        "# Review\n\nNew guidance that fails to index.\n", encoding="utf-8"
    )
    store.fail_upsert = True
    with pytest.raises(RAGUnavailableError, match="index unavailable"):
        manager.sync("engineering")

    failed = manager.get("engineering")
    assert failed.status == KnowledgeBaseStatus.STALE
    assert failed.documents[0].content_hash == old_hash
    assert set(store.points["engineering"]) == old_point_ids


def test_failed_registration_leaves_no_registry_or_collection(tmp_path) -> None:
    manager, store = _manager(tmp_path)
    source = manager.knowledge_base_directory("engineering")
    source.mkdir(parents=True)
    (source / "review.md").write_text("# Review\n\nRules.\n", encoding="utf-8")
    store.fail_upsert = True

    with pytest.raises(RAGUnavailableError):
        manager.register_knowledge_base("engineering", "Engineering standards")

    assert manager.list() == ()
    assert "engineering" not in store.collections


def test_registration_rejects_source_outside_fixed_knowledge_base_directory(
    tmp_path,
) -> None:
    manager, _ = _manager(tmp_path)
    outside = tmp_path / "other"
    outside.mkdir()

    with pytest.raises(ValidationError, match="must use source directory"):
        manager.register_knowledge_base("engineering", "Engineering standards", outside)
