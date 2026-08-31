from __future__ import annotations

import json
from pathlib import Path

from gitagent.capability import (
    CapabilityErrorType,
    CapabilityLayer,
    CapabilityStatus,
    InvocationContext,
    PermissionPolicy,
)
from gitagent.capability.providers import RAGProvider
from gitagent.capability.rag.models import (
    KnowledgeBase,
    KnowledgeBaseStatus,
    RAGUnavailableError,
    RetrievalHit,
    RetrievalResult,
)
from gitagent.domain.errors import ResourceNotFoundError


class FakeManager:
    def __init__(self, *, status=KnowledgeBaseStatus.READY, fail=False) -> None:
        self.knowledge_base = KnowledgeBase(
            id="engineering",
            description="Engineering review and testing standards.",
            source_directory="/knowledge",
            status=status,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        self.fail = fail
        self.calls = 0
        self.resets = 0

    def list(self):
        return (self.knowledge_base,)

    @staticmethod
    def capability_status(knowledge_base):
        return (
            knowledge_base.status,
            "broken" if knowledge_base.status.value == "ERROR" else "",
        )

    def retrieve(self, knowledge_base_id, query):
        self.calls += 1
        if self.fail:
            raise RAGUnavailableError("temporary retrieval outage")
        assert knowledge_base_id == "engineering"
        assert query == "regression testing"
        hit = RetrievalHit(
            document_id="doc-1",
            document_name="review.md",
            section_id="section-1",
            chunk_id="chunk-1",
            heading_path=("Review",),
            content="SECRET KNOWLEDGE BODY",
            retrieval_sources=("dense", "bm25"),
            retrieval_ranks={"dense": 1, "bm25": 2},
            rerank_score=0.9,
        )
        return RetrievalResult("engineering", False, (hit,), 12.5)

    def reset_runtime(self):
        self.resets += 1


def _layer(manager: FakeManager) -> CapabilityLayer:
    policy = PermissionPolicy(
        {
            "reviewer": {
                "discover": ["rag.engineering"],
                "invoke": {
                    "allow": ["rag.engineering"],
                    "ask": [],
                    "deny": [],
                },
            }
        }
    )
    layer = CapabilityLayer(policy=policy)
    layer.add_provider(RAGProvider(manager))
    layer.load()
    return layer


def _context() -> InvocationContext:
    return InvocationContext("run-1", "session-1", "reviewer")


def test_provider_registers_one_read_capability_and_sanitizes_trace() -> None:
    manager = FakeManager()
    layer = _layer(manager)
    discovered = layer.discover(_context())
    assert [item.id for item in discovered] == ["rag.engineering"]
    assert discovered[0].status == CapabilityStatus.AVAILABLE

    result = layer.invoke(
        "rag.engineering", {"query": "regression testing"}, _context()
    )
    assert result.status == "success"
    assert result.type == "retrieval"
    assert result.content["hits"][0]["content"] == "SECRET KNOWLEDGE BODY"

    trace = json.dumps(
        [event.details for event in layer.trace.events()], ensure_ascii=False
    )
    assert "regression testing" not in trace
    assert "SECRET KNOWLEDGE BODY" not in trace
    assert "doc-1" in trace
    assert "chunk-1" in trace
    assert "query_sha256" in trace


def test_provider_uses_read_retry_and_failure_guard() -> None:
    manager = FakeManager(fail=True)
    layer = _layer(manager)

    first = layer.invoke("rag.engineering", {"query": "regression testing"}, _context())
    assert first.status == "failed"
    assert first.error.type == CapabilityErrorType.UNAVAILABLE
    assert first.attempts == 2
    assert manager.calls == 2
    assert manager.resets == 1

    repeated = layer.invoke(
        "rag.engineering", {"query": "regression testing"}, _context()
    )
    assert repeated.error.type == CapabilityErrorType.REPEATED_FAILURE
    assert repeated.attempts == 0


def test_removed_knowledge_base_refreshes_stale_capability_registration() -> None:
    manager = FakeManager()
    layer = _layer(manager)

    def missing(*args):
        del args
        manager.list = lambda: ()
        raise ResourceNotFoundError("knowledge base was removed")

    manager.retrieve = missing
    result = layer.invoke(
        "rag.engineering", {"query": "regression testing"}, _context()
    )

    assert result.error.type == CapabilityErrorType.CAPABILITY_NOT_FOUND
    assert layer.registry.get("rag.engineering") is None


def test_error_knowledge_base_is_registered_but_not_discovered() -> None:
    manager = FakeManager(status=KnowledgeBaseStatus.ERROR)
    layer = _layer(manager)
    assert layer.registry.get("rag.engineering").status == CapabilityStatus.UNAVAILABLE
    assert layer.discover(_context()) == ()
    result = layer.invoke(
        "rag.engineering", {"query": "regression testing"}, _context()
    )
    assert result.error.type == CapabilityErrorType.UNAVAILABLE
    assert "broken" in result.error.message


def test_refresh_tracks_stale_error_and_removed_knowledge_base() -> None:
    manager = FakeManager()
    layer = _layer(manager)

    manager.knowledge_base = KnowledgeBase(
        **{
            **manager.knowledge_base.__dict__,
            "status": KnowledgeBaseStatus.STALE,
        }
    )
    layer.refresh("rag")
    assert layer.registry.get("rag.engineering").status == CapabilityStatus.AVAILABLE

    manager.knowledge_base = KnowledgeBase(
        **{
            **manager.knowledge_base.__dict__,
            "status": KnowledgeBaseStatus.ERROR,
        }
    )
    layer.refresh("rag")
    assert layer.registry.get("rag.engineering").status == CapabilityStatus.UNAVAILABLE

    manager.list = lambda: ()
    layer.refresh("rag")
    assert layer.registry.get("rag.engineering") is None


def test_project_policy_grants_only_autonomous_agents() -> None:
    policy = PermissionPolicy.from_file(Path(__file__).parents[1] / "capabilities.yaml")
    layer = CapabilityLayer(policy=policy)
    layer.add_provider(RAGProvider(FakeManager()))
    layer.load()

    for agent_id in (
        "repository",
        "issues",
        "pull_requests",
        "coding",
        "coding_subagent",
    ):
        context = InvocationContext("run", "session", agent_id)
        assert [item.id for item in layer.discover(context)] == ["rag.engineering"]
    for agent_id in ("main", "static_verifier"):
        context = InvocationContext("run", "session", agent_id)
        assert layer.discover(context) == ()
