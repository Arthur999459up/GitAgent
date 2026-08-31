"""Qdrant local-mode adapter for dense + BM25 retrieval and RRF fusion."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from .models import (
    Chunk,
    DocumentRecord,
    IndexedChunk,
    RAGSettings,
    RAGUnavailableError,
    RetrievalCandidate,
)

_DENSE_VECTOR = "dense"
_SPARSE_VECTOR = "bm25"
_BM25_MODEL = "Qdrant/bm25"


class QdrantStore:
    def __init__(self, settings: RAGSettings) -> None:
        self.settings = settings
        self._sparse_model: Any | None = None

    def collection_exists(self, knowledge_base_id: str) -> bool:
        try:
            with self._client() as client:
                return bool(
                    client.collection_exists(self.collection_name(knowledge_base_id))
                )
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(f"cannot inspect Qdrant index: {exc}") from exc

    def create_collection(self, knowledge_base_id: str) -> None:
        models = self._models()
        try:
            with self._client() as client:
                client.create_collection(
                    collection_name=self.collection_name(knowledge_base_id),
                    vectors_config={
                        _DENSE_VECTOR: models.VectorParams(
                            size=self.settings.embedding_dimension,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={
                        _SPARSE_VECTOR: models.SparseVectorParams(
                            index=models.SparseIndexParams(on_disk=True),
                            modifier=models.Modifier.IDF,
                        )
                    },
                )
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(f"cannot create Qdrant index: {exc}") from exc

    def delete_collection(self, knowledge_base_id: str) -> None:
        try:
            with self._client() as client:
                name = self.collection_name(knowledge_base_id)
                if client.collection_exists(name):
                    client.delete_collection(name)
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(f"cannot delete Qdrant index: {exc}") from exc

    def upsert(
        self, knowledge_base_id: str, indexed_chunks: Iterable[IndexedChunk]
    ) -> None:
        records = tuple(indexed_chunks)
        if not records:
            return
        models = self._models()
        sparse = self._embed_sparse([item.chunk.embedding_text for item in records])
        points = [
            models.PointStruct(
                id=item.chunk.chunk_id,
                vector={
                    _DENSE_VECTOR: item.dense_vector,
                    _SPARSE_VECTOR: models.SparseVector(
                        indices=sparse_vector[0], values=sparse_vector[1]
                    ),
                },
                payload=item.chunk.payload(),
            )
            for item, sparse_vector in zip(records, sparse, strict=True)
        ]
        try:
            with self._client() as client:
                client.upsert(
                    collection_name=self.collection_name(knowledge_base_id),
                    points=points,
                    wait=True,
                )
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(f"cannot write Qdrant index: {exc}") from exc

    def search(
        self,
        knowledge_base_id: str,
        query: str,
        dense_vector: list[float],
        documents: tuple[DocumentRecord, ...],
    ) -> tuple[RetrievalCandidate, ...]:
        if not documents:
            return ()
        models = self._models()
        sparse_indices, sparse_values = self._embed_sparse([query])[0]
        sparse_vector = models.SparseVector(
            indices=sparse_indices, values=sparse_values
        )
        active_filter = self._active_document_filter(documents)
        try:
            with self._client() as client:
                collection = self.collection_name(knowledge_base_id)
                fused = client.query_points(
                    collection_name=collection,
                    prefetch=[
                        models.Prefetch(
                            query=dense_vector,
                            using=_DENSE_VECTOR,
                            filter=active_filter,
                            limit=self.settings.dense_recall_limit,
                        ),
                        models.Prefetch(
                            query=sparse_vector,
                            using=_SPARSE_VECTOR,
                            filter=active_filter,
                            limit=self.settings.sparse_recall_limit,
                        ),
                    ],
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    with_payload=True,
                    limit=self.settings.coarse_limit,
                ).points
                dense = client.query_points(
                    collection_name=collection,
                    query=dense_vector,
                    using=_DENSE_VECTOR,
                    query_filter=active_filter,
                    with_payload=False,
                    limit=self.settings.dense_recall_limit,
                ).points
                sparse_points = client.query_points(
                    collection_name=collection,
                    query=sparse_vector,
                    using=_SPARSE_VECTOR,
                    query_filter=active_filter,
                    with_payload=False,
                    limit=self.settings.sparse_recall_limit,
                ).points
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(f"Qdrant hybrid retrieval failed: {exc}") from exc

        dense_ranks = {str(point.id): index for index, point in enumerate(dense, 1)}
        sparse_ranks = {
            str(point.id): index for index, point in enumerate(sparse_points, 1)
        }
        candidates: list[RetrievalCandidate] = []
        for point in fused:
            point_id = str(point.id)
            ranks = {
                name: rank
                for name, rank in (
                    ("dense", dense_ranks.get(point_id)),
                    ("bm25", sparse_ranks.get(point_id)),
                )
                if rank is not None
            }
            candidates.append(
                RetrievalCandidate(
                    chunk=self._chunk(point.payload),
                    fused_score=float(point.score),
                    retrieval_sources=tuple(ranks),
                    retrieval_ranks=ranks,
                )
            )
        return tuple(candidates)

    def section_chunks(
        self,
        knowledge_base_id: str,
        document: DocumentRecord,
        section_id: str,
    ) -> tuple[Chunk, ...]:
        models = self._models()
        conditions = [
            self._match("document_id", document.document_id),
            self._match("document_hash", document.content_hash),
            self._match("section_id", section_id),
        ]
        points: list[Any] = []
        offset: Any | None = None
        try:
            with self._client() as client:
                while True:
                    page, offset = client.scroll(
                        collection_name=self.collection_name(knowledge_base_id),
                        scroll_filter=models.Filter(must=conditions),
                        limit=256,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    points.extend(page)
                    if offset is None:
                        break
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(f"cannot expand Qdrant context: {exc}") from exc
        return tuple(
            sorted(
                (self._chunk(point.payload) for point in points),
                key=lambda item: item.chunk_index,
            )
        )

    def delete_document_version(
        self,
        knowledge_base_id: str,
        document_id: str,
        content_hash: str | None = None,
    ) -> None:
        models = self._models()
        conditions = [self._match("document_id", document_id)]
        if content_hash is not None:
            conditions.append(self._match("document_hash", content_hash))
        try:
            with self._client() as client:
                client.delete(
                    collection_name=self.collection_name(knowledge_base_id),
                    points_selector=models.FilterSelector(
                        filter=models.Filter(must=conditions)
                    ),
                    wait=True,
                )
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(f"cannot clean Qdrant index: {exc}") from exc

    def reset(self) -> None:
        self._sparse_model = None

    @staticmethod
    def collection_name(knowledge_base_id: str) -> str:
        return f"gitagent_kb_{knowledge_base_id.replace('-', '_')}"

    def _active_document_filter(self, documents: tuple[DocumentRecord, ...]) -> Any:
        models = self._models()
        return models.Filter(
            should=[
                models.Filter(
                    must=[
                        self._match("document_id", document.document_id),
                        self._match("document_hash", document.content_hash),
                    ]
                )
                for document in documents
            ]
        )

    def _match(self, key: str, value: str) -> Any:
        models = self._models()
        return models.FieldCondition(key=key, match=models.MatchValue(value=value))

    @staticmethod
    def _chunk(payload: Any) -> Chunk:
        if not isinstance(payload, dict):
            raise RAGUnavailableError("Qdrant returned a point without RAG metadata")
        try:
            headings = payload.get("heading_path") or []
            return Chunk(
                knowledge_base_id=str(payload["knowledge_base"]),
                document_id=str(payload["document_id"]),
                document_name=str(payload["document_name"]),
                document_hash=str(payload["document_hash"]),
                section_id=str(payload["section_id"]),
                parent_section_id=(
                    str(payload["parent_section_id"])
                    if payload.get("parent_section_id") is not None
                    else None
                ),
                chunk_id=str(payload["chunk_id"]),
                chunk_index=int(payload["chunk_index"]),
                previous_chunk_id=(
                    str(payload["previous_chunk_id"])
                    if payload.get("previous_chunk_id") is not None
                    else None
                ),
                next_chunk_id=(
                    str(payload["next_chunk_id"])
                    if payload.get("next_chunk_id") is not None
                    else None
                ),
                heading_path=tuple(str(item) for item in headings),
                text=str(payload["text"]),
                content_hash=str(payload["content_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RAGUnavailableError("Qdrant RAG metadata is incomplete") from exc

    def _embed_sparse(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        try:
            values = list(self._sparse().embed(texts))
        except RAGUnavailableError:
            raise
        except Exception as exc:
            raise RAGUnavailableError(
                f"local Qdrant BM25 embedding failed: {exc}"
            ) from exc
        return [
            (
                [int(value) for value in item.indices.tolist()],
                [float(value) for value in item.values.tolist()],
            )
            for item in values
        ]

    def _sparse(self) -> Any:
        if self._sparse_model is not None:
            return self._sparse_model
        try:
            from fastembed import SparseTextEmbedding

            local_path = self.settings.qdrant_path / "bm25"
            local_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            local_path.chmod(0o700)
            self._sparse_model = SparseTextEmbedding(
                model_name=_BM25_MODEL,
                specific_model_path=str(local_path),
                local_files_only=True,
                disable_stemmer=True,
            )
        except Exception as exc:
            raise RAGUnavailableError(
                f"cannot initialize offline Qdrant BM25: {exc}"
            ) from exc
        return self._sparse_model

    @staticmethod
    def _models() -> Any:
        try:
            from qdrant_client import models
        except ImportError as exc:
            raise RAGUnavailableError(
                "qdrant-client[fastembed] is required for built-in RAG"
            ) from exc
        return models

    @contextmanager
    def _client(self) -> Iterator[Any]:
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise RAGUnavailableError(
                "qdrant-client[fastembed] is required for built-in RAG"
            ) from exc
        self.settings.qdrant_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.settings.qdrant_path.chmod(0o700)
        client = QdrantClient(path=str(self.settings.qdrant_path))
        try:
            yield client
        finally:
            client.close()
