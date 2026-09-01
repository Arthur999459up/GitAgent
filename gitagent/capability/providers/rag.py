"""Expose registered local knowledge bases through the existing Capability Layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gitagent.capability.rag import (
    KnowledgeBaseManager,
    KnowledgeBaseStatus,
    RAGUnavailableError,
)
from gitagent.domain.errors import ResourceNotFoundError, ValidationError

from ..errors import (
    ProviderExecutionError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..models import (
    AccessLevel,
    Capability,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRegistration,
    CapabilityStatus,
    InvocationContext,
)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_000,
            "description": "A focused knowledge query, not the full Agent context.",
        }
    },
    "required": ["query"],
    "additionalProperties": False,
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "knowledge_base": {"type": "string"},
        "stale": {"type": "boolean"},
        "hits": {"type": "array", "items": {"type": "object"}},
        "hit_count": {"type": "integer"},
        "notice": {"type": "string"},
        "elapsed_ms": {"type": "number"},
    },
    "required": ["knowledge_base", "stale", "hits", "hit_count"],
    "additionalProperties": True,
}


@dataclass(frozen=True)
class RAGDefinition:
    knowledge_base_id: str
    description: str
    status: KnowledgeBaseStatus
    unavailable_reason: str = ""

    @property
    def id(self) -> str:
        return f"rag.{self.knowledge_base_id}"


class RAGProvider:
    id = "rag"

    def __init__(self, manager: KnowledgeBaseManager | None = None) -> None:
        self.manager = manager or KnowledgeBaseManager()
        self.last_load_error = ""

    def load(self) -> list[CapabilityRegistration]:
        try:
            knowledge_bases = self.manager.list()
        except Exception as exc:  # noqa: BLE001 - RAG failure must not block other providers
            self.last_load_error = str(exc)
            return []

        registrations: list[CapabilityRegistration] = []
        self.last_load_error = ""
        for knowledge_base in knowledge_bases:
            try:
                status, reason = self.manager.capability_status(knowledge_base)
            except Exception as exc:  # noqa: BLE001 - isolate one knowledge base
                status, reason = KnowledgeBaseStatus.ERROR, str(exc)
            definition = RAGDefinition(
                knowledge_base.id,
                knowledge_base.description,
                status,
                reason,
            )
            registrations.append(
                CapabilityRegistration(
                    Capability(
                        id=definition.id,
                        kind=CapabilityKind.RAG,
                        description=(
                            f"{definition.description.strip()} "
                            "Use this read-only knowledge base for internal guidance and domain knowledge; "
                            "use Repository capabilities for current repository facts."
                        ),
                        source_id=self.id,
                        status=(
                            CapabilityStatus.AVAILABLE
                            if status
                            in {KnowledgeBaseStatus.READY, KnowledgeBaseStatus.STALE}
                            else CapabilityStatus.UNAVAILABLE
                        ),
                        access=AccessLevel.READ,
                        input_schema=_INPUT_SCHEMA,
                        output_schema=_OUTPUT_SCHEMA,
                    ),
                    CapabilityBinding(definition.id, self.id, definition),
                )
            )
        return registrations

    def invoke(
        self,
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        del context
        definition = binding.target
        if not isinstance(definition, RAGDefinition):
            raise TypeError("RAG binding target is invalid")
        if definition.status == KnowledgeBaseStatus.ERROR:
            reason = definition.unavailable_reason or "knowledge base is unavailable"
            raise ProviderUnavailableError(reason)
        try:
            return self.manager.retrieve(
                definition.knowledge_base_id, str(arguments.get("query") or "")
            ).to_dict()
        except TimeoutError as exc:
            raise ProviderTimeoutError(str(exc), request_sent=False) from exc
        except RAGUnavailableError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        except (ConnectionError, OSError) as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        except ResourceNotFoundError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        except ValidationError:
            raise
        except (ValueError, TypeError):
            raise
        except Exception as exc:
            raise ProviderExecutionError(str(exc)) from exc

    @staticmethod
    def describe_execution(
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        del arguments
        from gitagent.harness.execution import ExecutionProfile

        if not isinstance(binding.target, RAGDefinition):
            return ExecutionProfile.unknown(repository=context.repository)
        return ExecutionProfile.concurrent()

    def reconnect(self, binding: CapabilityBinding) -> None:
        if not isinstance(binding.target, RAGDefinition):
            raise TypeError("RAG binding target is invalid")
        self.manager.reset_runtime()
