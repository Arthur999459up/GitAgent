from __future__ import annotations

from typing import Any

import pytest

from gitagent.capability.schema import validate_schema
from gitagent.domain.errors import StructuredOutputError, ValidationError
from gitagent.domain.models import SessionScope
from gitagent.memory.extractor import MemoryExtractionContext, MemoryExtractor
from gitagent.memory.pages import MemoryPageStore


class _Reasoner:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.messages: list[list[dict[str, Any]]] = []

    def complete_structured_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.messages.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if schema is not None:
            try:
                validate_schema(response, schema, label="structured output")
            except ValidationError as exc:
                raise StructuredOutputError(str(exc)) from exc
        return response


def _context() -> MemoryExtractionContext:
    return MemoryExtractionContext(
        scope=SessionScope("account", "repository", "session"),
        repository_full_name="owner/repo",
        extracted_through_seq=0,
        target_through_seq=1,
        conversation_units=(
            {
                "seq": 1,
                "evidence": True,
                "user": "请用中文回复",
                "assistant": "好的",
                "route": None,
                "domain_summary": "",
            },
        ),
        memory_index="",
    )


def _valid_result() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "name": "reply-language",
                "description": "User response language preference",
                "type": "user",
                "scope": "private",
                "category": "preference",
                "importance": 4,
                "ttl_days": None,
                "tags": ["language", "chinese"],
                "body": "The user prefers Chinese replies.",
            }
        ]
    }


def test_memory_extraction_retries_invalid_structured_output(tmp_path: Any) -> None:
    reasoner = _Reasoner([{"candidates": "invalid"}, _valid_result()])
    extractor = MemoryExtractor(
        reasoner,  # type: ignore[arg-type]
        MemoryPageStore(tmp_path.resolve()),
        context_window_tokens=32_768,
        max_structured_retries=1,
    )

    result = extractor.extract(_context())

    assert result.candidates == 1
    assert len(result.written) == 1
    assert len(reasoner.messages) == 2
    assert "Previous structured output was invalid" in str(reasoner.messages[1][-1]["content"])


def test_memory_extraction_stops_at_structured_retry_limit(tmp_path: Any) -> None:
    reasoner = _Reasoner(
        [StructuredOutputError("invalid JSON"), StructuredOutputError("still invalid")]
    )
    extractor = MemoryExtractor(
        reasoner,  # type: ignore[arg-type]
        MemoryPageStore(tmp_path.resolve()),
        context_window_tokens=32_768,
        max_structured_retries=1,
    )

    with pytest.raises(StructuredOutputError, match="still invalid"):
        extractor.extract(_context())

    assert len(reasoner.messages) == 2
