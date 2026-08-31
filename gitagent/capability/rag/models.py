"""Small data contracts shared by the built-in RAG implementation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

_AGENT_ROOT = Path("/home/starry/intern/AGENT")


class KnowledgeBaseStatus(str, Enum):
    READY = "READY"
    STALE = "STALE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class RAGSettings:
    """Global first-version settings; individual knowledge bases do not override them."""

    knowledge_base_root: Path = _AGENT_ROOT / "database/knowledge_base"
    registry_path: Path = _AGENT_ROOT / "database/rag/registry.db"
    qdrant_path: Path = _AGENT_ROOT / "database/rag/qdrant"
    embedding_model_path: Path = _AGENT_ROOT / "models/rag/qwen3-embedding-0.6b"
    reranker_model_path: Path = _AGENT_ROOT / "models/rag/qwen3-reranker-0.6b"
    embedding_dimension: int = 1024
    chunk_tokens: int = 500
    chunk_overlap_tokens: int = 75
    dense_recall_limit: int = 30
    sparse_recall_limit: int = 30
    coarse_limit: int = 20
    result_limit: int = 6
    context_token_budget: int = 4_000
    minimum_rerank_score: float = 0.1
    embedding_batch_size: int = 16


@dataclass(frozen=True)
class DocumentRecord:
    knowledge_base_id: str
    document_id: str
    relative_path: str
    mtime_ns: int
    size: int
    content_hash: str


@dataclass(frozen=True)
class KnowledgeBase:
    id: str
    description: str
    source_directory: str
    status: KnowledgeBaseStatus
    created_at: str
    updated_at: str
    documents: tuple[DocumentRecord, ...] = ()

    @property
    def capability_id(self) -> str:
        return f"rag.{self.id}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["capability_id"] = self.capability_id
        value["document_count"] = len(self.documents)
        return value


@dataclass(frozen=True)
class MarkdownDocument:
    record: DocumentRecord
    path: Path
    text: str


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    path: Path
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class Chunk:
    knowledge_base_id: str
    document_id: str
    document_name: str
    document_hash: str
    section_id: str
    parent_section_id: str | None
    chunk_id: str
    chunk_index: int
    previous_chunk_id: str | None
    next_chunk_id: str | None
    heading_path: tuple[str, ...]
    text: str
    content_hash: str

    @property
    def embedding_text(self) -> str:
        heading = " > ".join(self.heading_path)
        return f"{heading}\n\n{self.text}" if heading else self.text

    def payload(self) -> dict[str, Any]:
        return {
            "knowledge_base": self.knowledge_base_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "document_hash": self.document_hash,
            "section_id": self.section_id,
            "parent_section_id": self.parent_section_id,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "previous_chunk_id": self.previous_chunk_id,
            "next_chunk_id": self.next_chunk_id,
            "heading_path": list(self.heading_path),
            "text": self.text,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk: Chunk
    fused_score: float
    retrieval_sources: tuple[str, ...]
    retrieval_ranks: dict[str, int]
    rerank_score: float = 0.0


@dataclass(frozen=True)
class RetrievalHit:
    document_id: str
    document_name: str
    section_id: str
    chunk_id: str
    heading_path: tuple[str, ...]
    content: str
    retrieval_sources: tuple[str, ...]
    retrieval_ranks: dict[str, int]
    rerank_score: float
    expanded_chunk_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_name": self.document_name,
            "section_id": self.section_id,
            "chunk_id": self.chunk_id,
            "heading_path": list(self.heading_path),
            "section": " > ".join(self.heading_path),
            "content": self.content,
            "retrieval_source": list(self.retrieval_sources),
            "retrieval_rank": dict(self.retrieval_ranks),
            "rerank_score": self.rerank_score,
            "expanded_chunk_ids": list(self.expanded_chunk_ids),
        }


@dataclass(frozen=True)
class RetrievalResult:
    knowledge_base: str
    stale: bool
    hits: tuple[RetrievalHit, ...]
    elapsed_ms: float
    notice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_base": self.knowledge_base,
            "stale": self.stale,
            "hits": [hit.to_dict() for hit in self.hits],
            "hit_count": len(self.hits),
            "notice": self.notice,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


@dataclass(frozen=True)
class SyncResult:
    knowledge_base: str
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    status: KnowledgeBaseStatus = KnowledgeBaseStatus.READY

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True)
class IndexedChunk:
    chunk: Chunk
    dense_vector: list[float]


class RAGUnavailableError(RuntimeError):
    """The configured local RAG runtime or index cannot currently be used."""
