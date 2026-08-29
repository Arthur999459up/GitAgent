from gitagent.domain.errors import ValidationError
from gitagent.domain.models import AgentSpec, SessionScope
from gitagent.harness.context import (
    ContextBuilder,
    assistant_tool_call,
    derive_domain_messages,
    derive_main_messages,
    tool_result_message,
)
from gitagent.harness.context.projector import CHECKPOINT_PREFIX
from gitagent.harness.context.state import AgentContext
from gitagent.infra.persistence import SessionEventLog, SessionManager, StateStore
from gitagent.memory import MemoryStore
from gitagent.model import ChatResponse, LLMReasoner, ToolCall


def test_live_main_messages_equal_restart_replay(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    manager = SessionManager(store, log)
    scope = SessionScope(
        "https://api.github.com#user:1",
        "https://api.github.com#repo:2",
        "session-" + "b" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    turn = manager.start_turn(scope, "inspect issue 7")
    route = manager.record_model_message(
        scope,
        assistant_tool_call("delegate", "route_session_turn", {"target_agent": "issues"}),
        turn_seq=turn.seq,
        agent="main",
    )
    summary = manager.record_model_message(
        scope,
        tool_result_message("delegate", "Issue #7 is open; no mutation executed."),
        turn_seq=turn.seq,
        agent="main",
    )
    manager.complete_turn(
        scope,
        turn.seq,
        assistant_text="Issue #7 remains open.",
        workflow_summary="Issue #7 is open; no mutation executed.",
        route={"route": "issues", "status": "completed"},
        entity_manifests=[],
        working_state={
            "version": 4,
            "goal": "inspect issue 7",
            "focus": None,
            "manifests": [],
            "open_question": "",
        },
    )
    live = [
        {"role": "user", "content": "inspect issue 7"},
        route,
        summary,
        {"role": "assistant", "content": "Issue #7 remains open."},
    ]

    restarted = SessionManager(store, log)
    replay = derive_main_messages(restarted.event_log.iter_events(scope))

    assert replay == live


def test_reasoning_content_round_trips_through_jsonl(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    manager = SessionManager(store, log)
    scope = SessionScope(
        "https://api.github.com#user:105",
        "https://api.github.com#repo:106",
        "session-" + "7" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    turn = manager.start_turn(scope, "inspect issue")
    message = assistant_tool_call(
        "provider-call",
        "capability__github__get_issue",
        {"issue_number": 7},
    )
    message["reasoning_content"] = "provider-private-thinking-metadata"

    persisted = manager.record_model_message(
        scope,
        message,
        turn_seq=turn.seq,
        agent="issues",
        run_id="run-reasoning",
    )
    replay = derive_domain_messages(
        SessionManager(store, log).event_log.iter_events(scope),
        agent="issues",
        run_id="run-reasoning",
    )

    assert persisted["reasoning_content"] == "provider-private-thinking-metadata"
    assert replay == [persisted]



def test_long_main_messages_round_trip_without_fixed_truncation(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    manager = SessionManager(store, log)
    scope = SessionScope(
        "https://api.github.com#user:101",
        "https://api.github.com#repo:102",
        "session-" + "c" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    user_text = "u" * 20_000
    assistant_text = "a" * 20_000
    turn = manager.start_turn(scope, user_text)
    builder = ContextBuilder(manager, MemoryStore(tmp_path / "long-memory"))

    live, _ = builder.build(
        scope,
        "owner/repo",
        user_text,
        system="current system",
        turn_seq=turn.seq,
    )
    assert live[-1] == {"role": "user", "content": user_text}

    manager.complete_turn(
        scope,
        turn.seq,
        assistant_text=assistant_text,
        workflow_summary="",
        route=None,
        entity_manifests=[],
        working_state={
            "version": 4,
            "goal": "long message round trip",
            "focus": None,
            "manifests": [],
            "open_question": "",
        },
    )
    replay = derive_main_messages(SessionManager(store, log).event_log.iter_events(scope))

    assert replay == [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_text},
    ]


def test_large_compaction_checkpoint_round_trips_without_truncation(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    manager = SessionManager(store, log)
    scope = SessionScope(
        "https://api.github.com#user:103",
        "https://api.github.com#repo:104",
        "session-" + "9" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    turn = manager.start_turn(scope, "current")
    checkpoint = "checkpoint:" + "x" * 30_000

    manager.record_message_compaction(
        scope,
        turn_seq=turn.seq,
        agent="main",
        checkpoint=checkpoint,
        retain_message_indexes=[],
    )

    replay = derive_main_messages(SessionManager(store, log).event_log.iter_events(scope))
    assert replay == [
        {"role": "system", "content": CHECKPOINT_PREFIX + checkpoint},
    ]



def test_main_pressure_compaction_is_durable_and_replays_exact_request(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    manager = SessionManager(store, log)
    scope = SessionScope(
        "https://api.github.com#user:11",
        "https://api.github.com#repo:22",
        "session-" + "d" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    for index in range(6):
        turn = manager.start_turn(scope, f"U{index} " + "u" * 3000)
        manager.complete_turn(
            scope,
            turn.seq,
            assistant_text=f"A{index} " + "a" * 3000,
            workflow_summary="",
            route=None,
            entity_manifests=[],
            working_state={
                "version": 4,
                "goal": f"U{index}",
                "focus": None,
                "manifests": [],
                "open_question": "",
            },
        )
    current = manager.start_turn(scope, "U-current")
    builder = ContextBuilder(
        manager,
        MemoryStore(tmp_path / "memory"),
        context_window_tokens=12_608,
        max_output_tokens=4096,
        safety_tokens=0,
        retry_reserve_tokens=512,
    )

    live, _ = builder.build(
        scope,
        "owner/repo",
        "U-current",
        system="current system",
        turn_seq=current.seq,
    )

    assert builder.last_compression_level in {"summary", "emergency"}
    restarted = SessionManager(store, log)
    replay = [
        {"role": "system", "content": "current system"},
        *derive_main_messages(restarted.event_log.iter_events(scope)),
    ]
    assert replay == live


class _ToolProtocolHarness:
    context_budget = 8000
    message_sink = None
    compaction_sink = None

    @staticmethod
    def function_name(capability_id: str) -> str:
        return "capability__" + capability_id.replace(".", "__").replace("-", "_")



def test_capability_result_never_reuses_an_unrelated_control_call() -> None:
    context = AgentContext(
        _ToolProtocolHarness(),
        AgentSpec("issues", "role", "domain system", (), frozenset()),
        "session-" + "8" * 32,
        repository="owner/repo",
        goal="inspect issue",
    )
    context.start_message_thread()
    context.append_message(
        assistant_tool_call(
            "control-1",
            "decide_action",
            {
                "kind": "capability",
                "capability_id": "github.get_issue",
                "arguments": {"issue_number": 7},
            },
        )
    )

    try:
        context.ensure_capability_tool_call("github.get_issue", {"issue_number": 7})
    except ValidationError as exc:
        assert "does not match capability" in str(exc)
    else:
        raise AssertionError("unrelated control call must not be reused as a capability call")

    context.complete_control_call(
        {"status": "accepted", "action": "capability", "capability_id": "github.get_issue"}
    )
    capability_call_id = context.ensure_capability_tool_call(
        "github.get_issue", {"issue_number": 7}
    )

    assert capability_call_id != "control-1"
    assert context.messages[-1]["tool_calls"][0]["function"]["name"] == (
        "capability__github__get_issue"
    )



class _DurableDomainHarness:
    context_budget = 8000

    def __init__(self, manager: SessionManager, scope: SessionScope, turn_seq: int) -> None:
        self.manager = manager
        self.scope = scope
        self.turn_seq = turn_seq
        self.message_sink = self._message_sink
        self.compaction_sink = self._compaction_sink

    def _message_sink(self, context: AgentContext, message: dict) -> dict:
        return self.manager.record_model_message(
            self.scope,
            message,
            turn_seq=self.turn_seq,
            agent=context.agent,
            run_id=context.run_id,
        )

    def _compaction_sink(self, context: AgentContext, plan) -> None:
        self.manager.record_message_compaction(
            self.scope,
            turn_seq=self.turn_seq,
            agent=context.agent,
            checkpoint=plan.checkpoint,
            retain_message_indexes=plan.retain_message_indexes,
            tool_replacements=plan.tool_replacements,
            run_id=context.run_id,
        )


def test_domain_pressure_compaction_mutates_live_thread_and_replays_exactly(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    manager = SessionManager(store, log)
    scope = SessionScope(
        "https://api.github.com#user:31",
        "https://api.github.com#repo:32",
        "session-" + "e" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    turn = manager.start_turn(scope, "delegate issue work")
    harness = _DurableDomainHarness(manager, scope, turn.seq)
    context = AgentContext(
        harness,
        AgentSpec("issues", "role", "domain system", (), frozenset()),
        scope.session_id,
        repository="owner/repo",
        goal="delegate issue work",
    )
    context.origin_turn_seq = turn.seq
    context.start_message_thread()
    context.append_message(assistant_tool_call("tool-a", "github_get_issue", {"number": 7}))
    context.append_message(tool_result_message("tool-a", "x" * 14_000))
    context.append_message({"role": "user", "content": "continue"})

    live = list(context.model_messages())

    assert any(
        message.get("role") == "tool" and "compacted after use" in message.get("content", "")
        for message in live
    )
    restarted = SessionManager(store, log)
    replay = derive_domain_messages(
        restarted.event_log.iter_events(scope),
        agent="issues",
        run_id=context.run_id,
    )
    assert replay == live


class _RetryClient:
    model = "fake"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    def __init__(self) -> None:
        self.requests = []
        self.responses = [
            ChatResponse(content="not structured"),
            ChatResponse(tool_calls=[ToolCall("fixed", "respond", {"answer": "ok"})]),
        ]

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append([dict(message) for message in messages])
        return self.responses.pop(0)


def test_domain_structured_retry_messages_are_persisted_in_same_thread(tmp_path) -> None:
    store = StateStore(tmp_path / "state.db")
    log = SessionEventLog(tmp_path / "events", redactor=store.redact, fsync=False)
    manager = SessionManager(store, log)
    scope = SessionScope(
        "https://api.github.com#user:41",
        "https://api.github.com#repo:42",
        "session-" + "f" * 32,
    )
    manager.create_session(
        scope.account_key,
        scope.repository_key,
        "owner/repo",
        session_id=scope.session_id,
    )
    turn = manager.start_turn(scope, "delegate issue work")
    harness = _DurableDomainHarness(manager, scope, turn.seq)
    context = AgentContext(
        harness,
        AgentSpec("issues", "role", "domain system", (), frozenset()),
        scope.session_id,
        repository="owner/repo",
        goal="delegate issue work",
    )
    context.origin_turn_seq = turn.seq
    client = _RetryClient()
    value = context.reason_structured(
        LLMReasoner(client),
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    context.record_model_response(value, tool_name="respond")
    context.complete_control_call(value)

    assert [message["role"] for message in context.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]
    assert client.requests[1][:-2] == client.requests[0]
    replay = derive_domain_messages(
        SessionManager(store, log).event_log.iter_events(scope),
        agent="issues",
        run_id=context.run_id,
    )
    assert replay == context.messages
