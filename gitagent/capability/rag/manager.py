"""Knowledge-base lifecycle and retrieval entry point used by RAGProvider and CLI."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from threading import RLock
from typing import Any

from gitagent.domain.errors import (
    PermissionDenied,
    ResourceNotFoundError,
    ValidationError,
)

from .ingestion import IngestionPipeline, LocalEmbeddingModel, model_directory_error
from .models import (
    DocumentRecord,
    KnowledgeBase,
    KnowledgeBaseStatus,
    MarkdownDocument,
    RAGSettings,
    RAGUnavailableError,
    RetrievalResult,
    SyncResult,
)
from .qdrant import QdrantStore
from .registry import KnowledgeBaseRegistry
from .retrieval import RetrievalPipeline

_KNOWLEDGE_BASE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class KnowledgeBaseManager:
    def __init__(
        self,
        settings: RAGSettings | None = None,
        *,
        registry: KnowledgeBaseRegistry | Any | None = None,
        ingestion: IngestionPipeline | Any | None = None,
        store: QdrantStore | Any | None = None,
        retrieval: RetrievalPipeline | Any | None = None,
    ) -> None:
        self.settings = settings or RAGSettings()
        self.registry = registry or KnowledgeBaseRegistry(self.settings.registry_path)
        embedding = (
            ingestion.embedding
            if ingestion is not None and hasattr(ingestion, "embedding")
            else LocalEmbeddingModel(self.settings)
        )
        self.ingestion = ingestion or IngestionPipeline(
            self.settings, embedding=embedding
        )
        self.store = store or QdrantStore(self.settings)
        self.retrieval = retrieval or RetrievalPipeline(
            self.settings, self.store, embedding
        )
        self._lock = RLock()

    def list(self) -> tuple[KnowledgeBase, ...]:
        return self.registry.list()

    def get(self, knowledge_base_id: str) -> KnowledgeBase:
        knowledge_base = self.registry.get(knowledge_base_id)
        if knowledge_base is None:
            raise ResourceNotFoundError(
                f"knowledge base does not exist: {knowledge_base_id}"
            )
        return knowledge_base

    def capability_status(
        self, knowledge_base: KnowledgeBase
    ) -> tuple[KnowledgeBaseStatus, str]:
        if knowledge_base.status == KnowledgeBaseStatus.ERROR:
            return (
                KnowledgeBaseStatus.ERROR,
                "knowledge base is in ERROR state; run sync",
            )
        embedding_error = model_directory_error(
            self.settings.embedding_model_path, sentence_transformer=True
        )
        if embedding_error:
            return KnowledgeBaseStatus.ERROR, f"embedding model: {embedding_error}"
        reranker_error = model_directory_error(self.settings.reranker_model_path)
        if reranker_error:
            return KnowledgeBaseStatus.ERROR, f"reranker model: {reranker_error}"
        for module in ("qdrant_client", "fastembed", "sentence_transformers"):
            if find_spec(module) is None:
                return (
                    KnowledgeBaseStatus.ERROR,
                    f"Python dependency is missing: {module}",
                )
        try:
            if not self.store.collection_exists(knowledge_base.id):
                return KnowledgeBaseStatus.ERROR, "Qdrant collection is missing"
        except Exception as exc:  # noqa: BLE001 - provider load isolates a single KB
            return KnowledgeBaseStatus.ERROR, str(exc)
        return knowledge_base.status, ""

    def register_knowledge_base(
        self,
        knowledge_base_id: str,
        description: str,
        source_directory: str | Path | None = None,
    ) -> KnowledgeBase:
        self._validate_id(knowledge_base_id)
        clean_description = description.strip()
        if not clean_description:
            raise ValidationError("knowledge base description cannot be empty")
        root = self.knowledge_base_directory(knowledge_base_id)
        if source_directory is not None:
            supplied = Path(source_directory).expanduser().resolve(strict=False)
            if supplied != root:
                raise ValidationError(
                    f"knowledge base {knowledge_base_id} must use source directory {root}"
                )
        if root.is_dir():
            root.chmod(0o700)
        with self._lock:
            if self.registry.get(knowledge_base_id) is not None:
                raise ValidationError(
                    f"knowledge base already exists: {knowledge_base_id}"
                )
            sources = self.ingestion.discover(root)
            if not sources:
                raise ValidationError(
                    f"knowledge base directory contains no Markdown documents: {root}"
                )
            documents = tuple(
                self.ingestion.load_document(knowledge_base_id, source)
                for source in sources
            )
            indexed = tuple(self.ingestion.index(document) for document in documents)
            created_collection = False
            try:
                if self.store.collection_exists(knowledge_base_id):
                    self.store.delete_collection(knowledge_base_id)
                self.store.create_collection(knowledge_base_id)
                created_collection = True
                for chunks in indexed:
                    self.store.upsert(knowledge_base_id, chunks)
                now = _now()
                knowledge_base = KnowledgeBase(
                    id=knowledge_base_id,
                    description=clean_description,
                    source_directory=str(root),
                    status=KnowledgeBaseStatus.READY,
                    created_at=now,
                    updated_at=now,
                    documents=tuple(document.record for document in documents),
                )
                self.registry.register(knowledge_base)
            except Exception:
                if created_collection:
                    try:
                        self.store.delete_collection(knowledge_base_id)
                    except Exception:  # noqa: BLE001, S110 - preserve the original failure
                        pass
                raise
            return knowledge_base

    def knowledge_base_directory(self, knowledge_base_id: str) -> Path:
        self._validate_id(knowledge_base_id)
        root = self.settings.knowledge_base_root.expanduser().resolve(strict=False)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        return root / knowledge_base_id

    def register_directory(
        self,
        knowledge_base_id: str,
        description: str,
        directory: str | Path,
    ) -> KnowledgeBase:
        return self.register_knowledge_base(knowledge_base_id, description, directory)

    def register_document(
        self, knowledge_base_id: str, document: str | Path
    ) -> SyncResult:
        knowledge_base = self.get(knowledge_base_id)
        root = Path(knowledge_base.source_directory).resolve(strict=False)
        path = Path(document).expanduser().resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValidationError(
                "registered Markdown documents must be inside the knowledge base source directory"
            ) from exc
        if path.suffix != ".md" or not path.is_file():
            raise ValidationError(f"Markdown document does not exist: {path}")
        return self.sync(knowledge_base_id)

    def sync(self, knowledge_base_id: str) -> SyncResult:
        with self._lock:
            current = self.get(knowledge_base_id)
            created_collection = False
            try:
                sources = self.ingestion.discover(current.source_directory)
                source_by_path = {source.relative_path: source for source in sources}
                old_by_path = {
                    document.relative_path: document for document in current.documents
                }
                added_paths = sorted(source_by_path.keys() - old_by_path.keys())
                deleted_paths = sorted(old_by_path.keys() - source_by_path.keys())
                changed: list[str] = []
                unchanged: list[str] = []
                loaded: dict[str, MarkdownDocument] = {}
                records: dict[str, DocumentRecord] = {}

                for relative_path, source in source_by_path.items():
                    previous = old_by_path.get(relative_path)
                    if previous is None:
                        document = self.ingestion.load_document(
                            knowledge_base_id, source
                        )
                        loaded[relative_path] = document
                        records[relative_path] = document.record
                        continue
                    if (
                        previous.mtime_ns == source.mtime_ns
                        and previous.size == source.size
                    ):
                        records[relative_path] = previous
                        unchanged.append(relative_path)
                        continue
                    document = self.ingestion.load_document(knowledge_base_id, source)
                    records[relative_path] = document.record
                    if document.record.content_hash == previous.content_hash:
                        unchanged.append(relative_path)
                    else:
                        loaded[relative_path] = document
                        changed.append(relative_path)

                collection_exists = self.store.collection_exists(knowledge_base_id)
                if not collection_exists:
                    loaded = {
                        source.relative_path: self.ingestion.load_document(
                            knowledge_base_id, source
                        )
                        for source in sources
                    }
                    records = {
                        path: document.record for path, document in loaded.items()
                    }
                    self.store.create_collection(knowledge_base_id)
                    created_collection = True

                indexed = {
                    path: self.ingestion.index(document)
                    for path, document in loaded.items()
                }
                for chunks in indexed.values():
                    self.store.upsert(knowledge_base_id, chunks)

                final_records = tuple(records[path] for path in sorted(records))
                self.registry.replace_documents(
                    knowledge_base_id,
                    final_records,
                    status=KnowledgeBaseStatus.READY,
                    updated_at=_now(),
                )

                for relative_path in changed:
                    previous = old_by_path[relative_path]
                    self._clean_document_version(
                        knowledge_base_id,
                        previous.document_id,
                        previous.content_hash,
                    )
                for relative_path in deleted_paths:
                    previous = old_by_path[relative_path]
                    self._clean_document_version(
                        knowledge_base_id, previous.document_id
                    )
                return SyncResult(
                    knowledge_base=knowledge_base_id,
                    added=tuple(added_paths),
                    changed=tuple(sorted(changed)),
                    deleted=tuple(deleted_paths),
                    unchanged=tuple(sorted(unchanged)),
                    status=KnowledgeBaseStatus.READY,
                )
            except Exception:
                if created_collection:
                    try:
                        self.store.delete_collection(knowledge_base_id)
                    except Exception:  # noqa: BLE001, S110 - preserve the sync failure
                        pass
                self._record_sync_failure(current)
                raise

    def freshness_check(self, knowledge_base_id: str) -> KnowledgeBase:
        with self._lock:
            current = self.get(knowledge_base_id)
            if current.status == KnowledgeBaseStatus.ERROR:
                return current
            try:
                sources = self.ingestion.discover(current.source_directory)
            except (OSError, PermissionDenied, ValidationError):
                self.registry.set_status(knowledge_base_id, KnowledgeBaseStatus.STALE)
                return self.get(knowledge_base_id)
            source_by_path = {source.relative_path: source for source in sources}
            old_by_path = {
                document.relative_path: document for document in current.documents
            }
            stale = source_by_path.keys() != old_by_path.keys()
            refreshed_stats: list[DocumentRecord] = []
            if not stale:
                for relative_path, previous in old_by_path.items():
                    source = source_by_path[relative_path]
                    if (
                        previous.mtime_ns == source.mtime_ns
                        and previous.size == source.size
                    ):
                        continue
                    try:
                        document = self.ingestion.load_document(
                            knowledge_base_id, source
                        )
                    except (OSError, PermissionDenied, ValidationError):
                        stale = True
                        break
                    if document.record.content_hash != previous.content_hash:
                        stale = True
                        break
                    refreshed_stats.append(document.record)
            if refreshed_stats:
                self.registry.update_document_stats(refreshed_stats)
            desired = KnowledgeBaseStatus.STALE if stale else KnowledgeBaseStatus.READY
            if current.status != desired:
                self.registry.set_status(knowledge_base_id, desired)
            return self.get(knowledge_base_id)

    def retrieve(self, knowledge_base_id: str, query: str) -> RetrievalResult:
        focused_query = query.strip()
        if not focused_query:
            raise ValidationError("RAG query cannot be empty")
        with self._lock:
            knowledge_base = self.get(knowledge_base_id)
            if knowledge_base.status == KnowledgeBaseStatus.ERROR:
                raise RAGUnavailableError(
                    f"knowledge base {knowledge_base_id} is in ERROR state; run sync"
                )
            if not self.store.collection_exists(knowledge_base_id):
                raise RAGUnavailableError(
                    f"Qdrant collection is missing for knowledge base {knowledge_base_id}"
                )
            knowledge_base = self.freshness_check(knowledge_base_id)
            return self.retrieval.retrieve(knowledge_base, focused_query)

    def remove(self, knowledge_base_id: str) -> KnowledgeBase:
        with self._lock:
            knowledge_base = self.get(knowledge_base_id)
            self.store.delete_collection(knowledge_base_id)
            removed = self.registry.remove(knowledge_base_id)
            if removed is None:
                raise ResourceNotFoundError(
                    f"knowledge base does not exist: {knowledge_base_id}"
                )
            return knowledge_base

    def reset_runtime(self) -> None:
        if hasattr(self.ingestion, "reset"):
            self.ingestion.reset()
        if hasattr(self.retrieval, "reset"):
            self.retrieval.reset()
        elif hasattr(self.store, "reset"):
            self.store.reset()

    def _record_sync_failure(self, knowledge_base: KnowledgeBase) -> None:
        try:
            usable_index = self.store.collection_exists(knowledge_base.id)
            status = (
                KnowledgeBaseStatus.STALE if usable_index else KnowledgeBaseStatus.ERROR
            )
            self.registry.set_status(knowledge_base.id, status)
        except Exception:  # noqa: BLE001, S110 - preserve the sync failure
            pass

    def _clean_document_version(
        self,
        knowledge_base_id: str,
        document_id: str,
        content_hash: str | None = None,
    ) -> None:
        try:
            self.store.delete_document_version(
                knowledge_base_id, document_id, content_hash
            )
        except Exception:  # noqa: BLE001, S110 - active hash filtering preserves correctness
            pass

    @staticmethod
    def _validate_id(knowledge_base_id: str) -> None:
        if not _KNOWLEDGE_BASE_ID.fullmatch(knowledge_base_id):
            raise ValidationError(
                "knowledge base ID must be 1-63 lowercase letters, digits, or hyphens"
            )


def _now() -> str:
    return datetime.now(UTC).isoformat()
