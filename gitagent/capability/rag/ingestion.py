"""Markdown discovery, structure-aware chunking, and local dense embedding."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from gitagent.domain.errors import PermissionDenied, ValidationError

from .models import (
    Chunk,
    DocumentRecord,
    IndexedChunk,
    MarkdownDocument,
    RAGSettings,
    RAGUnavailableError,
    SourceFile,
)

_HEADERS = [("#" * level, f"h{level}") for level in range(1, 7)]


class LocalEmbeddingModel:
    """Lazy, offline-only Qwen3 embedding model."""

    def __init__(self, settings: RAGSettings) -> None:
        self.settings = settings
        self._model: Any | None = None

    @property
    def tokenizer(self) -> Any:
        model = self._load()
        tokenizer = getattr(model, "tokenizer", None)
        if tokenizer is None:
            raise RAGUnavailableError("the local embedding model has no tokenizer")
        return tokenizer

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        try:
            encoder = getattr(model, "encode_document", model.encode)
            values = encoder(
                texts,
                batch_size=self.settings.embedding_batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise RAGUnavailableError(f"local embedding failed: {exc}") from exc
        return self._vectors(values)

    def embed_query(self, query: str) -> list[float]:
        model = self._load()
        try:
            encoder = getattr(model, "encode_query", model.encode)
            values = encoder(
                [query],
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise RAGUnavailableError(f"local query embedding failed: {exc}") from exc
        vectors = self._vectors(values)
        if len(vectors) != 1:
            raise RAGUnavailableError(
                "local embedding returned an invalid query vector"
            )
        return vectors[0]

    def count_tokens(self, text: str) -> int:
        try:
            return len(self.tokenizer.encode(text, add_special_tokens=False))
        except Exception as exc:
            raise RAGUnavailableError(f"local tokenizer failed: {exc}") from exc

    def reset(self) -> None:
        self._model = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        path = self.settings.embedding_model_path
        error = model_directory_error(path, sentence_transformer=True)
        if error:
            raise RAGUnavailableError(f"embedding model is unavailable: {error}")
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                str(path),
                local_files_only=True,
                trust_remote_code=True,
            )
            dimension = model.get_sentence_embedding_dimension()
        except Exception as exc:
            raise RAGUnavailableError(
                f"cannot load local embedding model at {path}: {exc}"
            ) from exc
        if dimension != self.settings.embedding_dimension:
            raise RAGUnavailableError(
                "local embedding dimension is "
                f"{dimension}, expected {self.settings.embedding_dimension}"
            )
        self._model = model
        return model

    def _vectors(self, values: Any) -> list[list[float]]:
        vectors = [list(map(float, value)) for value in values]
        if any(len(value) != self.settings.embedding_dimension for value in vectors):
            raise RAGUnavailableError(
                f"local embedding output must contain {self.settings.embedding_dimension} values"
            )
        return vectors


class IngestionPipeline:
    def __init__(
        self,
        settings: RAGSettings,
        *,
        embedding: LocalEmbeddingModel | Any | None = None,
    ) -> None:
        self.settings = settings
        self.embedding = embedding or LocalEmbeddingModel(settings)

    def discover(self, source_directory: str | Path) -> tuple[SourceFile, ...]:
        root = Path(source_directory).expanduser().resolve(strict=False)
        if not root.is_dir():
            raise ValidationError(
                f"knowledge base source directory does not exist: {root}"
            )
        files: list[SourceFile] = []
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError as exc:
                raise PermissionDenied(
                    f"Markdown document escapes the knowledge base directory: {path}"
                ) from exc
            stat = resolved.stat()
            files.append(SourceFile(relative, resolved, stat.st_mtime_ns, stat.st_size))
        return tuple(files)

    def load_document(
        self, knowledge_base_id: str, source: SourceFile
    ) -> MarkdownDocument:
        try:
            content = source.path.read_bytes()
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                f"Markdown document must be UTF-8: {source.relative_path}"
            ) from exc
        document_id = _stable_id("document", knowledge_base_id, source.relative_path)
        record = DocumentRecord(
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            relative_path=source.relative_path,
            mtime_ns=source.mtime_ns,
            size=source.size,
            content_hash=hashlib.sha256(content).hexdigest(),
        )
        return MarkdownDocument(record, source.path, text)

    def index(self, document: MarkdownDocument) -> tuple[IndexedChunk, ...]:
        chunks = self.chunk(document)
        vectors = self.embedding.embed_documents(
            [chunk.embedding_text for chunk in chunks]
        )
        if len(chunks) != len(vectors):
            raise RAGUnavailableError(
                "embedding result count does not match Markdown chunks"
            )
        return tuple(
            IndexedChunk(chunk=chunk, dense_vector=vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
        )

    def chunk(self, document: MarkdownDocument) -> tuple[Chunk, ...]:
        try:
            from langchain_text_splitters import (
                MarkdownHeaderTextSplitter,
                MarkdownTextSplitter,
            )

            sections = MarkdownHeaderTextSplitter(
                headers_to_split_on=_HEADERS,
                strip_headers=True,
            ).split_text(document.text)
            splitter = MarkdownTextSplitter.from_huggingface_tokenizer(
                self.embedding.tokenizer,
                chunk_size=self.settings.chunk_tokens,
                chunk_overlap=self.settings.chunk_overlap_tokens,
                keep_separator="start",
            )
        except ImportError as exc:
            raise RAGUnavailableError(
                "langchain-text-splitters is required for Markdown ingestion"
            ) from exc
        except Exception as exc:
            raise RAGUnavailableError(
                f"cannot initialize Markdown chunking: {exc}"
            ) from exc

        pending: list[dict[str, Any]] = []
        heading_ids: dict[tuple[str, ...], str] = {}
        section_occurrences: dict[tuple[str, ...], int] = {}
        for section in sections:
            heading_path = tuple(
                str(section.metadata[key]).strip()
                for _, key in _HEADERS
                if str(section.metadata.get(key) or "").strip()
            )
            occurrence = section_occurrences.get(heading_path, 0)
            section_occurrences[heading_path] = occurrence + 1
            section_id = _stable_id(
                "section",
                document.record.document_id,
                "/".join(heading_path),
                str(occurrence),
            )
            parent_id = (
                heading_ids.get(heading_path[:-1])
                or _stable_id(
                    "section-parent",
                    document.record.document_id,
                    "/".join(heading_path[:-1]),
                )
                if len(heading_path) > 1
                else None
            )
            heading_ids[heading_path] = section_id
            try:
                pieces = splitter.split_text(section.page_content)
            except Exception as exc:
                raise RAGUnavailableError(
                    f"cannot split Markdown document {document.record.relative_path}: {exc}"
                ) from exc
            for text in pieces:
                cleaned = text.strip()
                if cleaned:
                    pending.append(
                        {
                            "section_id": section_id,
                            "parent_section_id": parent_id,
                            "heading_path": heading_path,
                            "text": cleaned,
                        }
                    )

        if not pending and document.text.strip():
            pending.append(
                {
                    "section_id": _stable_id(
                        "section", document.record.document_id, "root", "0"
                    ),
                    "parent_section_id": None,
                    "heading_path": (),
                    "text": document.text.strip(),
                }
            )

        chunk_ids = [
            _stable_id(
                "chunk",
                document.record.document_id,
                document.record.content_hash,
                str(index),
            )
            for index in range(len(pending))
        ]
        return tuple(
            Chunk(
                knowledge_base_id=document.record.knowledge_base_id,
                document_id=document.record.document_id,
                document_name=document.record.relative_path,
                document_hash=document.record.content_hash,
                section_id=str(item["section_id"]),
                parent_section_id=(
                    str(item["parent_section_id"])
                    if item["parent_section_id"] is not None
                    else None
                ),
                chunk_id=chunk_ids[index],
                chunk_index=index,
                previous_chunk_id=chunk_ids[index - 1] if index else None,
                next_chunk_id=(
                    chunk_ids[index + 1] if index + 1 < len(chunk_ids) else None
                ),
                heading_path=tuple(item["heading_path"]),
                text=str(item["text"]),
                content_hash=hashlib.sha256(
                    str(item["text"]).encode("utf-8")
                ).hexdigest(),
            )
            for index, item in enumerate(pending)
        )

    def reset(self) -> None:
        if hasattr(self.embedding, "reset"):
            self.embedding.reset()


def model_directory_error(
    path: str | Path, *, sentence_transformer: bool = False
) -> str:
    root = Path(path)
    if not root.is_dir():
        return f"directory does not exist: {root}"
    required = [root / "config.json", root / "tokenizer_config.json"]
    if sentence_transformer:
        required.append(root / "modules.json")
    missing = [item.name for item in required if not item.is_file()]
    if missing:
        return f"missing {', '.join(missing)} in {root}"
    if (
        not any(root.glob("*.safetensors"))
        and not (root / "pytorch_model.bin").is_file()
    ):
        return f"missing model weights in {root}"
    if not (root / "tokenizer.json").is_file() and not (root / "vocab.json").is_file():
        return f"missing tokenizer data in {root}"
    return ""


def _stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "\0".join(parts)))
