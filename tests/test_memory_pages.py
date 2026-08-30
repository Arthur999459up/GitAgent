from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from gitagent.memory import MemoryCandidate, MemoryPageStore, MemorySearch
from gitagent.memory.index import (
    INDEX_BYTE_LIMIT,
    INDEX_LINE_LIMIT,
    TRUNCATION_WARNING,
    render_scope_index,
)
from gitagent.memory.models import MemoryPage


def _candidate(**overrides: object) -> MemoryCandidate:
    values = {
        "name": "real-database-tests",
        "description": "Use real database fixtures for integration tests",
        "type": "feedback",
        "scope": "private",
        "body": "Use real database fixtures for database integration tests.",
        "category": "testing",
        "importance": 4,
        "source": "extractor",
        "ttl_days": None,
        "tags": ("testing", "database"),
    }
    values.update(overrides)
    return MemoryCandidate(**values)  # type: ignore[arg-type]


def test_page_write_index_search_and_exact_dedup(tmp_path: Path) -> None:
    now = datetime.now().astimezone()
    store = MemoryPageStore(tmp_path.resolve(), now=lambda: now)

    first, created = store.write_candidate("account", "repository", _candidate())
    duplicate, duplicated = store.write_candidate(
        "account", "repository", _candidate(name="another-name")
    )

    assert created is True
    assert duplicated is False
    assert duplicate.id == first.id
    assert first.relative_path == "real-database-tests.md"
    raw = store.roots("account", "repository")["private_memory"].joinpath(
        first.relative_path
    ).read_text(encoding="utf-8")
    metadata = yaml.safe_load(raw.split("---", 2)[1])
    assert metadata["schema_version"] == 1
    assert metadata["signature"].startswith("sha256:")
    assert "real-database-tests.md" in store.read_index("account", "repository")

    hits = MemorySearch(store, now=lambda: now).search(
        "account", "repository", "database integration tests"
    )
    assert [hit.id for hit in hits] == [first.id]
    assert MemorySearch(store).search("account", "repository", "unrelated bananas") == ()


def test_ttl_stale_disabled_and_manual_protection(tmp_path: Path) -> None:
    clock = [datetime.now().astimezone()]
    store = MemoryPageStore(tmp_path.resolve(), now=lambda: clock[0])
    expiring, _ = store.write_candidate(
        "account", "repository", _candidate(ttl_days=1)
    )
    manual, _ = store.manual_write(
        "account", "repository", "Prefer concise review summaries."
    )

    clock[0] += timedelta(days=2)
    active = store.list_pages(
        "account", "repository", include_inactive=False
    )
    assert expiring.id not in {page.id for page in active}
    assert expiring.name not in store.read_index("account", "repository")
    assert store.disable(
        "account", "repository", scope="private", identifier=manual.id
    ) is None

    clock[0] += timedelta(days=29)
    hits = MemorySearch(store, now=lambda: clock[0]).search(
        "account", "repository", "concise review summaries"
    )
    assert len(hits) == 1
    assert hits[0].stale is True


def test_concurrent_candidates_are_optimistically_deduplicated(tmp_path: Path) -> None:
    store = MemoryPageStore(tmp_path.resolve())

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: store.write_candidate("account", "repository", _candidate()),
                range(16),
            )
        )

    assert sum(created for _, created in results) == 1
    assert len(store.list_pages("account", "repository")) == 1


def test_dream_honors_supersedes_but_protects_manual_pages(tmp_path: Path) -> None:
    store = MemoryPageStore(tmp_path.resolve())
    old, _ = store.write_candidate("account", "repository", _candidate())
    replacement, _ = store.write_candidate(
        "account",
        "repository",
        _candidate(
            name="real-database-tests-v2",
            description="Prefer real database fixtures for persistence integration tests",
            body="Use real database fixtures for persistence integration tests.",
            supersedes=(old.id,),
        ),
    )
    manual, _ = store.manual_write(
        "account", "repository", "Always include a concise risk summary."
    )
    protected, _ = store.write_candidate(
        "account",
        "repository",
        _candidate(
            name="risk-summary-reference",
            description="A later automatic candidate",
            body="A later automatic candidate.",
            supersedes=(manual.id,),
        ),
    )

    result = store.maintain("account", "repository")
    pages = {page.id: page for page in store.list_pages("account", "repository")}

    assert f"private:{old.id}" in result["disabled"]
    assert pages[old.id].disabled is True
    assert pages[replacement.id].disabled is False
    assert pages[manual.id].disabled is False
    assert pages[protected.id].disabled is False


def test_legacy_memory_migrates_and_experience_is_archived(tmp_path: Path) -> None:
    import hashlib

    account = hashlib.sha256(b"account").hexdigest()
    legacy = tmp_path / "accounts" / account / "user" / "items"
    legacy.mkdir(parents=True)
    (legacy / "preference.md").write_text(
        "---\ntype: memory\npriority: high\nlast_accessed_at: '2026-01-01T00:00:00+00:00'\npinned: true\n---\n\nPrefer concise answers.\n",
        encoding="utf-8",
    )
    (legacy / "trajectory.md").write_text(
        "---\ntype: experience\npriority: normal\nlast_accessed_at: '2026-01-01T00:00:00+00:00'\npinned: false\n---\n\nA one-off successful tool trajectory.\n",
        encoding="utf-8",
    )

    store = MemoryPageStore(tmp_path.resolve())
    pages = store.list_pages("account", "repository")

    assert [(page.type, page.scope, page.source) for page in pages] == [
        ("user", "private", "migration")
    ]
    assert not (tmp_path / "accounts" / account / "user").exists()
    archived = tmp_path / "accounts" / account / "archive" / "legacy-experience"
    assert any(path.name.startswith("trajectory.md") for path in archived.rglob("*"))


def test_index_has_hard_line_and_byte_limits() -> None:
    now = datetime.now().astimezone()
    pages = [
        MemoryPage(
            schema_version=1,
            id=f"mem-20260830-120000-{index:08x}",
            name=f"memory-{index}",
            description="description " + ("x" * 400),
            type="reference",
            scope="project",
            category="general",
            importance=3,
            source="extractor",
            signature="sha256:" + (f"{index:064x}"[-64:]),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            ttl_days=None,
            disabled=False,
            supersedes=(),
            tags=(),
            body="body",
            relative_path=f"memory-{index}.md",
        )
        for index in range(250)
    ]

    rendered = render_scope_index(pages, now=now)

    assert len(rendered.splitlines()) <= INDEX_LINE_LIMIT
    assert len(rendered.encode()) <= INDEX_BYTE_LIMIT
    assert TRUNCATION_WARNING in rendered
