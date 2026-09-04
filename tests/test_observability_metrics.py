from __future__ import annotations

from gitagent.agent_loop.models import ModelResponse
from gitagent.application.metrics import (
    AGENT_NAMES,
    project_context_usage,
    project_turn_latencies,
)
from gitagent.domain.models import AgentSpec, SessionEvent
from gitagent.harness.context.state import AgentContext
from gitagent.infra.persistence import TurnRecord
from gitagent.token_accounting import request_tokens


class _Harness:
    message_sink = None
    compaction_sink = None

    def __init__(self) -> None:
        self.usages: list[tuple[str, int]] = []

    def context_window_for(self, agent_name: str) -> int:
        return 10_000

    def record_context_usage(self, context: AgentContext, *, input_tokens: int) -> None:
        self.usages.append((context.agent, input_tokens))


class _Reasoner:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.tools: list[dict] | None = None

    def complete_messages(self, *, messages, tools=None, context_window_tokens=None):
        self.messages = messages
        self.tools = tools
        return ModelResponse("ok", [], {"role": "assistant", "content": "ok"})

    def complete_text_messages(self, *, messages, tools=None, context_window_tokens=None):
        self.messages = messages
        self.tools = tools
        return "done"


def _context(harness: _Harness) -> AgentContext:
    spec = AgentSpec("main", "test", "system", (), 0, None)
    return AgentContext(harness, spec, "session-1", goal="goal", max_steps=3)


def test_agent_context_records_exact_payload_for_reason_and_complete_text() -> None:
    harness = _Harness()
    reasoner = _Reasoner()
    context = _context(harness)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    context.reason(reasoner, tools=tools)
    assert harness.usages[-1] == ("main", request_tokens(reasoner.messages, tools))

    second = _context(harness)
    second.complete_text(reasoner, prompt="explain")
    assert harness.usages[-1] == ("main", request_tokens(reasoner.messages))


def test_context_usage_projection_keeps_latest_snapshot_and_empty_agents() -> None:
    events = [
        SessionEvent(
            1,
            1,
            "workflow_step",
            "2026-09-05T00:00:00+00:00",
            "session-1",
            1,
            "main",
            {
                "details": {
                    "debug_event": "context_usage",
                    "run_id": "run-1",
                    "input_tokens": 100,
                    "context_window_tokens": 1000,
                }
            },
        ),
        SessionEvent(
            1,
            2,
            "workflow_step",
            "2026-09-05T00:00:01+00:00",
            "session-1",
            2,
            "main",
            {
                "details": {
                    "debug_event": "context_usage",
                    "run_id": "run-2",
                    "input_tokens": 250,
                    "context_window_tokens": 1000,
                }
            },
        ),
    ]

    rows = project_context_usage(
        events,
        agents=AGENT_NAMES,
        context_windows={"default": 2000, "main": 1000},
    )

    assert rows[0].agent == "main"
    assert rows[0].input_tokens == 250
    assert rows[0].ratio == 0.25
    assert rows[0].turn_seq == 2
    assert rows[1].input_tokens is None
    assert rows[1].context_window_tokens == 2000


def test_turn_latency_projection_uses_persisted_turn_boundaries() -> None:
    turns = (
        TurnRecord(
            "session-1",
            1,
            "completed",
            "hello",
            "world",
            [],
            "2026-09-05T00:00:00.000000+00:00",
            "2026-09-05T00:00:01.234000+00:00",
        ),
        TurnRecord(
            "session-1",
            2,
            "started",
            "next",
            "",
            [],
            "2026-09-05T00:00:02.000000+00:00",
            None,
        ),
    )

    rows = project_turn_latencies(turns)
    assert rows[0].duration_ms == 1234.0
    assert rows[1].duration_ms is None
