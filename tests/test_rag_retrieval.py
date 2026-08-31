from __future__ import annotations

from gitagent.capability.rag.models import (
    Chunk,
    DocumentRecord,
    KnowledgeBase,
    KnowledgeBaseStatus,
    RAGSettings,
    RetrievalCandidate,
)
from gitagent.capability.rag.retrieval import RetrievalPipeline


def _chunk(index: int, *, section: str = "section-1") -> Chunk:
    return Chunk(
        knowledge_base_id="engineering",
        document_id="doc-1",
        document_name="review.md",
        document_hash="document-hash",
        section_id=section,
        parent_section_id=None,
        chunk_id=f"chunk-{index}",
        chunk_index=index,
        previous_chunk_id=f"chunk-{index - 1}" if index else None,
        next_chunk_id=f"chunk-{index + 1}" if index < 2 else None,
        heading_path=("Review",),
        text=f"Evidence {index}",
        content_hash=f"content-{index}",
    )


class FakeEmbedding:
    @staticmethod
    def embed_query(query):
        del query
        return [0.0] * 1024

    @staticmethod
    def count_tokens(text):
        del text
        return 10


class FakeReranker:
    def __init__(self, scores) -> None:
        self.scores = scores

    def score(self, query, documents):
        del query, documents
        return self.scores


class FakeStore:
    def __init__(self, candidates, section) -> None:
        self.candidates = candidates
        self.section = section

    def search(self, knowledge_base_id, query, dense_vector, documents):
        del knowledge_base_id, query, dense_vector, documents
        return self.candidates

    def section_chunks(self, knowledge_base_id, document, section_id):
        del knowledge_base_id, document, section_id
        return self.section


class SectionStore(FakeStore):
    def __init__(self, candidates, sections) -> None:
        super().__init__(candidates, ())
        self.sections = sections

    def section_chunks(self, knowledge_base_id, document, section_id):
        del knowledge_base_id, document
        return self.sections.get(section_id, ())


def test_retrieval_reranks_filters_and_expands_only_neighbors() -> None:
    chunks = tuple(_chunk(index) for index in range(3))
    candidates = (
        RetrievalCandidate(chunks[1], 0.8, ("dense", "bm25"), {"dense": 1, "bm25": 2}),
        RetrievalCandidate(chunks[0], 0.7, ("dense",), {"dense": 2}),
    )
    store = FakeStore(candidates, chunks)
    settings = RAGSettings(minimum_rerank_score=0.5, context_token_budget=30)
    pipeline = RetrievalPipeline(
        settings,
        store,
        FakeEmbedding(),
        reranker=FakeReranker([0.9, 0.1]),
    )
    document = DocumentRecord(
        "engineering", "doc-1", "review.md", 1, 1, "document-hash"
    )
    knowledge_base = KnowledgeBase(
        "engineering",
        "Engineering standards",
        "/knowledge",
        KnowledgeBaseStatus.READY,
        "created",
        "updated",
        (document,),
    )

    result = pipeline.retrieve(knowledge_base, "regression testing")

    assert len(result.hits) == 1
    assert result.hits[0].chunk_id == "chunk-1"
    assert result.hits[0].expanded_chunk_ids == ("chunk-0", "chunk-2")
    assert result.hits[0].content == "Evidence 0\n\nEvidence 1\n\nEvidence 2"


def test_empty_recall_is_success_without_loading_reranker() -> None:
    store = FakeStore((), ())
    pipeline = RetrievalPipeline(
        RAGSettings(),
        store,
        FakeEmbedding(),
        reranker=FakeReranker(None),
    )
    knowledge_base = KnowledgeBase(
        "engineering",
        "Engineering standards",
        "/knowledge",
        KnowledgeBaseStatus.READY,
        "created",
        "updated",
    )

    result = pipeline.retrieve(knowledge_base, "unmatched query")

    assert result.hits == ()
    assert result.stale is False


def test_context_assembly_adds_one_parent_chunk_within_budget() -> None:
    parent = _chunk(0, section="parent")
    child = Chunk(
        **{
            **_chunk(1, section="child").__dict__,
            "parent_section_id": "parent",
            "previous_chunk_id": None,
            "next_chunk_id": None,
        }
    )
    candidate = RetrievalCandidate(child, 0.8, ("dense",), {"dense": 1})
    store = SectionStore(
        (candidate,),
        {"child": (child,), "parent": (parent,)},
    )
    pipeline = RetrievalPipeline(
        RAGSettings(minimum_rerank_score=0.5, context_token_budget=20),
        store,
        FakeEmbedding(),
        reranker=FakeReranker([0.9]),
    )
    knowledge_base = KnowledgeBase(
        "engineering",
        "Engineering standards",
        "/knowledge",
        KnowledgeBaseStatus.READY,
        "created",
        "updated",
        (DocumentRecord("engineering", "doc-1", "review.md", 1, 1, "document-hash"),),
    )

    result = pipeline.retrieve(knowledge_base, "regression testing")

    assert result.hits[0].expanded_chunk_ids == ("chunk-0",)
    assert result.hits[0].content == "Evidence 0\n\nEvidence 1"
