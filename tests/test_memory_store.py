from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gitagent.domain.learning import ReflectionChanges
from gitagent.memory import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MemoryStore(Path(self.temporary.name).resolve())
        self.account = "account"
        self.repository = "owner/repository"

    def test_read_index_does_not_prepare_item_directories(self) -> None:
        accounts_root = self.store.root / "accounts"

        self.assertEqual(self.store.read_index(self.account, self.repository), "")

        self.assertFalse(accounts_root.exists())

    def test_read_index_uses_only_persisted_indexes_and_competes_across_scopes(self) -> None:
        user_changes = tuple(
            {
                "scope": "user",
                "path": f"items/user_{index:03d}.md",
                "type": "memory",
                "priority": "low",
                "text": f"user memory {index} " + ("x" * 220),
            }
            for index in range(230)
        )
        repository_change = {
            "scope": "repository",
            "path": "items/repository_critical.md",
            "type": "memory",
            "priority": "high",
            "text": "repository critical memory",
        }
        self.store.apply_changes(
            self.account,
            self.repository,
            ReflectionChanges(add=(*user_changes, repository_change)),
        )

        roots = self.store.roots(self.account, self.repository)
        before = {
            name: (
                (root / "MEMORY.md").read_bytes(),
                (root / "MEMORY.md").stat().st_mtime_ns,
            )
            for name, root in roots.items()
        }

        original_list_scope = self.store._list_scope
        self.store._list_scope = lambda root: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError(f"read_index scanned item bodies under {root}")
        )
        try:
            rendered = self.store.read_index(self.account, self.repository)
        finally:
            self.store._list_scope = original_list_scope  # type: ignore[method-assign]

        self.assertIn("repository critical memory", rendered)
        self.assertLess(rendered.count("user memory"), len(user_changes))
        self.assertIn("user memory 229", roots["user_memory"].joinpath("MEMORY.md").read_text())
        for name, root in roots.items():
            self.assertEqual(before[name][0], (root / "MEMORY.md").read_bytes())
            self.assertEqual(before[name][1], (root / "MEMORY.md").stat().st_mtime_ns)

    def test_same_priority_uses_last_accessed_at_across_scopes(self) -> None:
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        current_time = [old_time]
        store = MemoryStore(
            Path(self.temporary.name).resolve(), now=lambda: current_time[0]
        )
        store.apply_changes(
            self.account,
            self.repository,
            ReflectionChanges(
                add=tuple(
                    {
                        "scope": "user",
                        "path": f"items/old_{index:03d}.md",
                        "type": "memory",
                        "priority": "normal",
                        "text": f"old user memory {index} " + ("x" * 220),
                    }
                    for index in range(230)
                )
            ),
        )
        current_time[0] = datetime(2026, 1, 2, tzinfo=timezone.utc)
        store.apply_changes(
            self.account,
            self.repository,
            ReflectionChanges(
                add=(
                    {
                        "scope": "repository",
                        "path": "items/newest_repository.md",
                        "type": "memory",
                        "priority": "normal",
                        "text": "newest repository memory",
                    },
                )
            ),
        )

        rendered = store.read_index(self.account, self.repository)

        self.assertIn("newest repository memory", rendered)
        self.assertLess(rendered.count("old user memory"), 230)

    def test_reflection_batch_rolls_back_every_scope_when_commit_fails(self) -> None:
        self.store.remember(self.account, self.repository, "existing user", scope="user")
        self.store.remember(
            self.account,
            self.repository,
            "existing repository",
            scope="repository",
        )
        roots = self.store.roots(self.account, self.repository)
        before = {name: _snapshot(root) for name, root in roots.items()}
        ordered_roots = sorted(roots.values(), key=str)
        fail_destination = ordered_roots[1]
        real_replace = os.replace

        def failing_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if source_path.name == "stage-1" and destination_path == fail_destination:
                raise OSError("injected batch commit failure")
            real_replace(source, destination)

        changes = ReflectionChanges(
            add=(
                {
                    "scope": "user",
                    "path": "items/new_user.md",
                    "type": "memory",
                    "priority": "normal",
                    "text": "new user",
                },
                {
                    "scope": "repository",
                    "path": "items/new_repository.md",
                    "type": "experience",
                    "priority": "high",
                    "text": "new repository",
                },
            )
        )
        with patch("gitagent.memory.store.os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                self.store.apply_changes(self.account, self.repository, changes)

        after_roots = self.store.roots(self.account, self.repository)
        self.assertEqual(
            before,
            {name: _snapshot(root) for name, root in after_roots.items()},
        )
        rendered = self.store.read_index(self.account, self.repository, full=True)
        self.assertNotIn("new user", rendered)
        self.assertNotIn("new repository", rendered)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
