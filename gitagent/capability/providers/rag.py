"""RAG provider interface retained without a built-in backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ProviderUnavailableError
from ..models import (
    AccessLevel,
    Capability,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRegistration,
    CapabilityStatus,
    InvocationContext,
)


@dataclass(frozen=True)
class RAGDefinition:
    id: str
    description: str
    source_id: str
    input_schema: dict[str, Any]
    backend: Any = None


class RAGProvider:
    id = "rag"

    def __init__(self, definitions: list[RAGDefinition] | None = None) -> None:
        self._definitions = definitions or []

    def load(self) -> list[CapabilityRegistration]:
        return [
            CapabilityRegistration(
                Capability(
                    definition.id,
                    CapabilityKind.RAG,
                    definition.description,
                    definition.source_id,
                    CapabilityStatus.AVAILABLE if definition.backend is not None else CapabilityStatus.UNAVAILABLE,
                    AccessLevel.READ,
                    definition.input_schema,
                ),
                CapabilityBinding(definition.id, self.id, definition),
            )
            for definition in self._definitions
        ]

    def invoke(
        self,
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        definition = binding.target
        if not isinstance(definition, RAGDefinition) or definition.backend is None:
            raise ProviderUnavailableError("RAG backend is not configured")
        return definition.backend.retrieve(arguments, context)

