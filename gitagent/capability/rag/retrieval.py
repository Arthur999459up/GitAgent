"""Hybrid retrieval, Qwen3 reranking, and bounded context assembly."""

from __future__ import annotations

from dataclasses import replace
from time import perf_counter
from typing import Any

from .ingestion import (
    LocalEmbeddingModel,
    model_directory_error,
    quiet_transformers_model_loading,
)
from .models import (
    KnowledgeBase,
    RAGSettings,
    RAGUnavailableError,
    RetrievalCandidate,
    RetrievalHit,
    RetrievalResult,
)
from .qdrant import QdrantStore


class LocalReranker:
    """Lazy, offline-only Qwen3 cross-encoder reranker."""

    def __init__(self, settings: RAGSettings) -> None:
        self.settings = settings
        self._model: Any | None = None

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = self._load()
        try:
            import torch

            scores = model.predict(
                [(query, document) for document in documents],
                activation_fn=torch.nn.Sigmoid(),
                show_progress_bar=False,
            )
        except Exception as exc:
            raise RAGUnavailableError(f"local reranking failed: {exc}") from exc
        return [float(score) for score in scores]

    def reset(self) -> None:
        self._model = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        path = self.settings.reranker_model_path
        error = model_directory_error(path)
        if error:
            raise RAGUnavailableError(f"reranker model is unavailable: {error}")
        try:
            from sentence_transformers import CrossEncoder

            with quiet_transformers_model_loading():
                model = CrossEncoder(
                    str(path),
                    local_files_only=True,
                    trust_remote_code=True,
                )
        except Exception as exc:
            raise RAGUnavailableError(
                f"cannot load local reranker model at {path}: {exc}"
            ) from exc
        self._model = model
        return model


class RetrievalPipeline:
    def __init__(
        self,
        settings: RAGSettings,
        store: QdrantStore | Any,
        embedding: LocalEmbeddingModel | Any,
        *,
        reranker: LocalReranker | Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.embedding = embedding
        self.reranker = reranker or LocalReranker(settings)

    def retrieve(self, knowledge_base: KnowledgeBase, query: str) -> RetrievalResult:
        started = perf_counter()
        dense_query = self.embedding.embed_query(query)
        candidates = self.store.search(
            knowledge_base.id,
            query,
            dense_query,
            knowledge_base.documents,
        )
        ranked = self._rerank(query, candidates)
        hits = self._assemble(knowledge_base, ranked)
        stale = knowledge_base.status.value == "STALE"
        notice = (
            "Knowledge base sources changed after the last successful sync; "
            f"results use the previous index. Run `gitagent rag sync {knowledge_base.id}`."
            if stale
            else ""
        )
        return RetrievalResult(
            knowledge_base=knowledge_base.id,
            stale=stale,
            hits=hits,
            notice=notice,
            elapsed_ms=(perf_counter() - started) * 1000,
        )

    def reset(self) -> None:
        if hasattr(self.embedding, "reset"):
            self.embedding.reset()
        if hasattr(self.reranker, "reset"):
            self.reranker.reset()
        if hasattr(self.store, "reset"):
            self.store.reset()

    def _rerank(
        self, query: str, candidates: tuple[RetrievalCandidate, ...]
    ) -> tuple[RetrievalCandidate, ...]:
        if not candidates:
            return ()
        scores = self.reranker.score(
            query, [candidate.chunk.embedding_text for candidate in candidates]
        )
        if len(scores) != len(candidates):
            raise RAGUnavailableError("reranker result count does not match candidates")
        ranked = sorted(
            (
                replace(candidate, rerank_score=score)
                for candidate, score in zip(candidates, scores, strict=True)
                if score >= self.settings.minimum_rerank_score
            ),
            key=lambda item: (item.rerank_score, item.fused_score),
            reverse=True,
        )
        return tuple(ranked)

    def _assemble(
        self,
        knowledge_base: KnowledgeBase,
        candidates: tuple[RetrievalCandidate, ...],
    ) -> tuple[RetrievalHit, ...]:
        selected = self._diverse(candidates)
        documents = {
            document.document_id: document for document in knowledge_base.documents
        }
        used_chunks: set[str] = set()
        used_content: set[str] = set()
        remaining = self.settings.context_token_budget
        hits: list[RetrievalHit] = []
        for candidate in selected:
            chunk = candidate.chunk
            if chunk.chunk_id in used_chunks or chunk.content_hash in used_content:
                continue
            document = documents.get(chunk.document_id)
            if document is None:
                continue
            section = self.store.section_chunks(
                knowledge_base.id, document, chunk.section_id
            )
            by_id = {item.chunk_id: item for item in section}
            center = by_id.get(chunk.chunk_id, chunk)
            ordered = [center]
            for neighbor_id in (center.previous_chunk_id, center.next_chunk_id):
                if neighbor_id and neighbor_id in by_id:
                    ordered.append(by_id[neighbor_id])
            if center.parent_section_id:
                parent = self.store.section_chunks(
                    knowledge_base.id,
                    document,
                    center.parent_section_id,
                )
                if parent:
                    ordered.append(parent[0])
            accepted = []
            for item in ordered:
                if item.chunk_id in used_chunks or item.content_hash in used_content:
                    continue
                cost = self.embedding.count_tokens(item.text)
                if cost > remaining:
                    if item.chunk_id == center.chunk_id:
                        accepted = []
                        break
                    continue
                accepted.append(item)
                used_chunks.add(item.chunk_id)
                used_content.add(item.content_hash)
                remaining -= cost
            if not accepted:
                continue
            accepted.sort(key=lambda item: item.chunk_index)
            hits.append(
                RetrievalHit(
                    document_id=chunk.document_id,
                    document_name=chunk.document_name,
                    section_id=chunk.section_id,
                    chunk_id=chunk.chunk_id,
                    heading_path=chunk.heading_path,
                    content=_merge_chunks([item.text for item in accepted]),
                    retrieval_sources=candidate.retrieval_sources,
                    retrieval_ranks=candidate.retrieval_ranks,
                    rerank_score=candidate.rerank_score,
                    expanded_chunk_ids=tuple(
                        item.chunk_id
                        for item in accepted
                        if item.chunk_id != chunk.chunk_id
                    ),
                )
            )
            if len(hits) >= self.settings.result_limit or remaining <= 0:
                break
        return tuple(hits)

    def _diverse(
        self, candidates: tuple[RetrievalCandidate, ...]
    ) -> tuple[RetrievalCandidate, ...]:
        selected: list[RetrievalCandidate] = []
        deferred: list[RetrievalCandidate] = []
        document_counts: dict[str, int] = {}
        section_counts: dict[str, int] = {}
        for candidate in candidates:
            document_id = candidate.chunk.document_id
            section_id = candidate.chunk.section_id
            if (
                document_counts.get(document_id, 0) >= 2
                or section_counts.get(section_id, 0) >= 1
            ):
                deferred.append(candidate)
                continue
            selected.append(candidate)
            document_counts[document_id] = document_counts.get(document_id, 0) + 1
            section_counts[section_id] = section_counts.get(section_id, 0) + 1
            if len(selected) >= self.settings.result_limit:
                return tuple(selected)
        for candidate in deferred:
            if len(selected) >= self.settings.result_limit:
                break
            selected.append(candidate)
        return tuple(selected)


def _merge_chunks(chunks: list[str]) -> str:
    if not chunks:
        return ""
    merged = chunks[0].strip()
    for chunk in chunks[1:]:
        value = chunk.strip()
        overlap = _overlap(merged, value)
        merged = f"{merged}\n\n{value[overlap:].lstrip()}".rstrip()
    return merged


def _overlap(left: str, right: str) -> int:
    maximum = min(len(left), len(right), 2_000)
    for size in range(maximum, 31, -1):
        if left[-size:] == right[:size]:
            return size
    return 0
