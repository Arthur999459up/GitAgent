"""SQLite control-plane registry for knowledge bases and Markdown documents."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from gitagent.domain.errors import ValidationError

from .models import DocumentRecord, KnowledgeBase, KnowledgeBaseStatus


class KnowledgeBaseRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def list(self) -> tuple[KnowledgeBase, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, description, source_directory, status, created_at, updated_at
                FROM knowledge_bases
                ORDER BY id
                """
            ).fetchall()
            documents = self._documents_by_knowledge_base(connection)
        return tuple(
            self._knowledge_base(row, documents.get(str(row["id"]), ())) for row in rows
        )

    def get(self, knowledge_base_id: str) -> KnowledgeBase | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, description, source_directory, status, created_at, updated_at
                FROM knowledge_bases
                WHERE id = ?
                """,
                (knowledge_base_id,),
            ).fetchone()
            if row is None:
                return None
            documents = self._documents(connection, knowledge_base_id)
        return self._knowledge_base(row, documents)

    def register(self, knowledge_base: KnowledgeBase) -> None:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO knowledge_bases(
                        id, description, source_directory, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        knowledge_base.id,
                        knowledge_base.description,
                        knowledge_base.source_directory,
                        knowledge_base.status.value,
                        knowledge_base.created_at,
                        knowledge_base.updated_at,
                    ),
                )
                self._insert_documents(connection, knowledge_base.documents)
            except sqlite3.IntegrityError as exc:
                raise ValidationError(
                    f"knowledge base already exists: {knowledge_base.id}"
                ) from exc

    def replace_documents(
        self,
        knowledge_base_id: str,
        documents: Iterable[DocumentRecord],
        *,
        status: KnowledgeBaseStatus,
        updated_at: str,
    ) -> None:
        records = tuple(documents)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_bases
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, updated_at, knowledge_base_id),
            )
            if cursor.rowcount != 1:
                raise ValidationError(
                    f"knowledge base does not exist: {knowledge_base_id}"
                )
            connection.execute(
                "DELETE FROM documents WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            )
            self._insert_documents(connection, records)

    def update_document_stats(self, records: Iterable[DocumentRecord]) -> None:
        values = tuple(records)
        if not values:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                UPDATE documents
                SET mtime_ns = ?, size = ?
                WHERE knowledge_base_id = ? AND document_id = ? AND content_hash = ?
                """,
                (
                    (
                        record.mtime_ns,
                        record.size,
                        record.knowledge_base_id,
                        record.document_id,
                        record.content_hash,
                    )
                    for record in values
                ),
            )

    def set_status(self, knowledge_base_id: str, status: KnowledgeBaseStatus) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge_bases SET status = ? WHERE id = ?",
                (status.value, knowledge_base_id),
            )
            if cursor.rowcount != 1:
                raise ValidationError(
                    f"knowledge base does not exist: {knowledge_base_id}"
                )

    def remove(self, knowledge_base_id: str) -> KnowledgeBase | None:
        current = self.get(knowledge_base_id)
        if current is None:
            return None
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM knowledge_bases WHERE id = ?", (knowledge_base_id,)
            )
        return current

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        connection = sqlite3.connect(self.path, timeout=30.0)
        self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    source_directory TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('READY', 'STALE', 'ERROR')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (knowledge_base_id, document_id),
                    UNIQUE (knowledge_base_id, relative_path)
                );

                CREATE INDEX IF NOT EXISTS idx_documents_kb_path
                    ON documents(knowledge_base_id, relative_path);
                """
            )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_documents(
        connection: sqlite3.Connection, records: Iterable[DocumentRecord]
    ) -> None:
        connection.executemany(
            """
            INSERT INTO documents(
                knowledge_base_id, document_id, relative_path, mtime_ns, size, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    record.knowledge_base_id,
                    record.document_id,
                    record.relative_path,
                    record.mtime_ns,
                    record.size,
                    record.content_hash,
                )
                for record in records
            ),
        )

    @classmethod
    def _documents_by_knowledge_base(
        cls, connection: sqlite3.Connection
    ) -> dict[str, tuple[DocumentRecord, ...]]:
        rows = connection.execute(
            """
            SELECT knowledge_base_id, document_id, relative_path, mtime_ns, size, content_hash
            FROM documents
            ORDER BY knowledge_base_id, relative_path
            """
        ).fetchall()
        grouped: dict[str, list[DocumentRecord]] = {}
        for row in rows:
            record = cls._document(row)
            grouped.setdefault(record.knowledge_base_id, []).append(record)
        return {key: tuple(value) for key, value in grouped.items()}

    @classmethod
    def _documents(
        cls, connection: sqlite3.Connection, knowledge_base_id: str
    ) -> tuple[DocumentRecord, ...]:
        rows = connection.execute(
            """
            SELECT knowledge_base_id, document_id, relative_path, mtime_ns, size, content_hash
            FROM documents
            WHERE knowledge_base_id = ?
            ORDER BY relative_path
            """,
            (knowledge_base_id,),
        ).fetchall()
        return tuple(cls._document(row) for row in rows)

    @staticmethod
    def _document(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            knowledge_base_id=str(row["knowledge_base_id"]),
            document_id=str(row["document_id"]),
            relative_path=str(row["relative_path"]),
            mtime_ns=int(row["mtime_ns"]),
            size=int(row["size"]),
            content_hash=str(row["content_hash"]),
        )

    @staticmethod
    def _knowledge_base(
        row: sqlite3.Row, documents: tuple[DocumentRecord, ...]
    ) -> KnowledgeBase:
        return KnowledgeBase(
            id=str(row["id"]),
            description=str(row["description"]),
            source_directory=str(row["source_directory"]),
            status=KnowledgeBaseStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            documents=documents,
        )
