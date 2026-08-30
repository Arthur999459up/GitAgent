from __future__ import annotations

from gitagent.domain.models import AgentGuidance, AgentSpec
from gitagent.harness.context.state import AgentContext


class _Harness:
    context_budget = 8_000
    message_sink = None
    compaction_sink = None


def test_domain_memory_is_in_provider_request_but_not_durable_thread() -> None:
    context = AgentContext(
        _Harness(),  # type: ignore[arg-type]
        AgentSpec(
            name="repository",
            role="test",
            system_prompt="BASE SYSTEM",
            output_schema=(),
            routes=frozenset(),
        ),
        "session-test",
        repository="owner/repository",
        goal="review this repository",
        guidance=AgentGuidance(
            persistent_memory_index="INDEX ENTRY",
            persistent_memory_pages="PRIVATE MEMORY BODY",
        ),
    )

    context.start_message_thread()
    assert "PRIVATE MEMORY BODY" not in str(context.messages)
    request = context.model_messages()
    assert "PRIVATE MEMORY BODY" in str(request)
    assert "PRIVATE MEMORY BODY" not in str(context.messages)

    context._record_ephemeral_memory_read(
        "private_memory", "review-style.md", "FALLBACK MEMORY BODY"
    )
    fallback_request = context.model_messages()
    assert "FALLBACK MEMORY BODY" in str(fallback_request)
    assert "FALLBACK MEMORY BODY" not in str(context.messages)
    assert "FALLBACK MEMORY BODY" not in str(context.read_cache)
