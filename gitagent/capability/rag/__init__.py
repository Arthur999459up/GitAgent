"""Built-in, read-only knowledge-base retrieval for the Capability Layer."""

from .manager import KnowledgeBaseManager
from .models import (
    DocumentRecord,
    KnowledgeBase,
    KnowledgeBaseStatus,
    RAGSettings,
    RAGUnavailableError,
    RetrievalResult,
    SyncResult,
)

__all__ = [
    "DocumentRecord",
    "KnowledgeBase",
    "KnowledgeBaseManager",
    "KnowledgeBaseStatus",
    "RAGSettings",
    "RAGUnavailableError",
    "RetrievalResult",
    "SyncResult",
]
