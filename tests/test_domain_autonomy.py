from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from gitagent.agent_loop import (
    AgentCall,
    AgentLoop,
    AgentResult,
    CapabilityCall,
    ModelResponse,
    StructuredCall,
)
from gitagent.agents.coding import CodingAgent
from gitagent.agents.pull_requests import PullRequestAgent
from gitagent.application.service import GitAgentService
from gitagent.capability import (
    AccessLevel,
    Capability,
    CapabilityBinding,
    CapabilityErrorType,
    CapabilityKind,
    CapabilityLayer,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilityStatus,
    InvocationContext,
    PermissionPolicy,
)
from gitagent.domain.errors import LLMProviderError, ValidationError, WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    PlannedCapabilityCall,
    Replacement,
    SessionScope,
    VerificationReport,
    WorkflowTurnDecision,
)
from gitagent.harness.context import assistant_tool_call
from gitagent.harness.execution import AgentHarness
from gitagent.harness.structured_call_dispatcher import StructuredCallDispatcher


@dataclass
class Target:
    name: str


class Provider:
    id = "provider"

    def __init__(self, capabilities: list[Capability], results: dict[str, Any]) -> None:
        self.capabilities = capabilities
        self.results = results
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def load(self) -> list[CapabilityRegistration]:
        return [
            CapabilityRegistration(
                capability,
                CapabilityBinding(capability.id, self.id, Target(capability.id)),
            )
            for capability in self.capabilities
        ]

    def invoke(self, binding: CapabilityBinding, arguments: dict[str, Any], context: Any) -> Any:
        del context
        self.invocations.append((binding.capability_id, arguments))
        return self.results[binding.capability_id]


def capability(
    capability_id: str,
    access: AccessLevel,
    *,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> Capability:
    return Capability(
        capability_id,
        CapabilityKind.NATIVE_TOOL,
        capability_id,
        "test",
        CapabilityStatus.AVAILABLE,
        access,
        input_schema,
        output_schema,
    )


def policy(*, allow: list[str] | None = None, ask: list[str] | None = None) -> PermissionPolicy:
    return PermissionPolicy(
        {
            "worker": {
                "discover": ["test.*"],
                "invoke": {
                    "allow": allow or [],
                    "ask": ask or [],
                    "deny": [],
                },
            }
        }
    )


def test_registry_uses_full_jsonschema_for_schema_definitions() -> None:
    registry = CapabilityRegistry()
    invalid = capability(
        "test.invalid",
        AccessLevel.READ,
        input_schema={"type": "object", "properties": {"x": {"type": "not-a-type"}}},
        output_schema={"type": "object"},
    )

    with pytest.raises(ValidationError):
        registry.register(
            CapabilityRegistration(
                invalid,
                CapabilityBinding(invalid.id, "provider", Target(invalid.id)),
            )
        )


def test_input_validation_supports_jsonschema_composition() -> None:
    item = capability(
        "test.read",
        AccessLevel.READ,
        input_schema={
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [
                        {"type": "string", "pattern": "^[A-Z]+$"},
                        {"type": "integer", "minimum": 10},
                    ]
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )
    provider = Provider([item], {item.id: {"ok": True}})
    layer = CapabilityLayer(policy=policy(allow=[item.id]))
    layer.add_provider(provider)
    layer.load()

    result = layer.invoke(
        item.id,
        {"value": "lowercase"},
        InvocationContext("run", "session", "worker"),
    )

    assert result.status == "failed"
    assert result.error.type == CapabilityErrorType.INVALID_INPUT
    assert provider.invocations == []


def test_invalid_mutation_output_is_failure_and_is_never_replayed() -> None:
    item = capability(
        "test.write",
        AccessLevel.WRITE,
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"commit": {"type": "string"}},
            "required": ["commit"],
            "additionalProperties": False,
        },
    )
    provider = Provider([item], {item.id: {"unexpected": True}})
    layer = CapabilityLayer(policy=policy(ask=[item.id]))
    layer.add_provider(provider)
    layer.load()
    planned = []
    from gitagent.domain.models import PlannedCapabilityCall

    planned.append(PlannedCapabilityCall(item.id, {}))
    approval = layer.policy.approvals.create(
        session_id="session", repository="o/r", summary="write", calls=planned
    )
    layer.policy.approvals.decide(approval.approval_id, "Approve")

    result = layer.invoke(
        item.id,
        {},
        InvocationContext(
            "run",
            "session",
            "worker",
            repository="o/r",
            approval_id=approval.approval_id,
        ),
    )

    assert result.status == "failed"
    assert result.error.type == CapabilityErrorType.INVALID_OUTPUT
    assert result.error.details == {
        "provider_executed": True,
        "side_effect_possible": True,
    }
    assert provider.invocations == [(item.id, {})]
    assert any(
        event.event == "output_validation.failed"
        and event.details["side_effect_possible"] is True
        for event in layer.trace.events("run")
    )


def test_agent_harness_resolves_namespaces_and_validates_agent_input() -> None:
    item = capability(
        "test.read",
        AccessLevel.READ,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    provider = Provider([item], {item.id: {}})
    layer = CapabilityLayer(policy=policy(allow=[item.id]))
    layer.add_provider(provider)
    layer.load()
    harness = AgentHarness(layer)
    harness.register(AgentSpec("worker", "worker", "system", ()))
    context = harness.context("worker", "session")
    schema = {
        "type": "object",
        "properties": {"task": {"type": "string", "minLength": 1}},
        "required": ["task"],
        "additionalProperties": False,
    }

    resolved = harness.resolve_model_call(
        StructuredCall("call-a", "agent__coding", {"task": "inspect"}),
        context,
        agent_schemas={"coding": schema},
    )
    assert isinstance(resolved, AgentCall)
    assert resolved.call_id == "call-a"

    with pytest.raises(ValidationError):
        harness.resolve_model_call(
            StructuredCall("call-b", "agent__coding", {"task": ""}),
            context,
            agent_schemas={"coding": schema},
        )


def test_agent_loop_returns_only_child_final_text_to_parent_model() -> None:
    layer = CapabilityLayer(policy=policy())
    layer.load()
    harness = AgentHarness(layer)
    harness.register(AgentSpec("worker", "worker", "system", ()))
    context = harness.context("worker", "session", goal="parent task")

    class Agent:
        calls = 0

        @staticmethod
        def agent_schemas() -> dict[str, dict[str, Any]]:
            return {
                "coding": {
                    "type": "object",
                    "properties": {"task": {"type": "string"}},
                    "required": ["task"],
                    "additionalProperties": False,
                }
            }

        def step(self, current: Any) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                call = StructuredCall("provider-agent-call", "agent__coding", {"task": "child"})
                message = current.append_message(
                    assistant_tool_call(call.call_id, call.name, call.arguments)
                )
                return ModelResponse("delegating", call, message)
            message = current.append_message(
                {"role": "assistant", "content": "parent final"}
            )
            return ModelResponse("parent final", None, message)

        @staticmethod
        def invoke_child(current: Any, call: AgentCall) -> AgentResult:
            current.code_plan = {"private_typed_artifact": "must not cross"}
            return AgentResult(call.call_id, "coding", "completed", "CHILD FINAL TEXT")

        @staticmethod
        def build_result(current: Any) -> str:
            return current.final_message

    AgentLoop(harness).start(context, Agent())

    assert context.finished
    assert context.final_message == "parent final"
    tool_messages = [message for message in context.messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "provider-agent-call"
    payload = json.loads(tool_messages[0]["content"])
    assert payload["content"] == "CHILD FINAL TEXT"
    assert "private_typed_artifact" not in tool_messages[0]["content"]


def test_coding_child_emits_its_own_final_assistant_text_after_typed_result() -> None:
    agents = {
        name: {
            "discover": [],
            "invoke": {"allow": [], "ask": [], "deny": []},
        }
        for name in ("worker", "coding")
    }
    layer = CapabilityLayer(policy=PermissionPolicy(agents))
    layer.load()
    harness = AgentHarness(layer)

    class CodingReasoner:
        def __init__(self) -> None:
            self.requests: list[list[dict[str, Any]]] = []
            self.responses = [
                ModelResponse(
                    "",
                    StructuredCall(
                        "typed-explanation",
                        "explain_code_change",
                        {
                            "behavior_changes": ["behavior changed"],
                            "key_symbols": ["symbol"],
                            "call_relationships": [],
                            "impact_scope": ["src/example.py"],
                        },
                    ),
                    assistant_tool_call(
                        "typed-explanation",
                        "explain_code_change",
                        {
                            "behavior_changes": ["behavior changed"],
                            "key_symbols": ["symbol"],
                            "call_relationships": [],
                            "impact_scope": ["src/example.py"],
                        },
                    ),
                ),
                ModelResponse(
                    "The behavior changed in src/example.py.",
                    None,
                    {
                        "role": "assistant",
                        "content": "The behavior changed in src/example.py.",
                    },
                ),
            ]

        def complete_messages(self, **kwargs: Any) -> ModelResponse:
            self.requests.append([dict(item) for item in kwargs["messages"]])
            return self.responses.pop(0)

    reasoner = CodingReasoner()
    coding = CodingAgent(harness, reasoner)  # type: ignore[arg-type]
    harness.register(AgentSpec("worker", "worker", "worker system", ()))
    parent = harness.context(
        "worker", "session", repository="owner/repo", goal="explain"
    )

    result, artifact = coding.run_call(
        parent,
        call_id="coding-call",
        mode="explain",
        task="explain the change",
        evidence={"changed_files": ["src/example.py"]},
    )

    assert result.content == "The behavior changed in src/example.py."
    assert artifact.key_symbols == ["symbol"]
    assert parent.messages == []
    assert [message["role"] for message in reasoner.requests[-1][-3:]] == [
        "user",
        "assistant",
        "tool",
    ]


def test_service_main_agent_call_uses_fresh_child_and_returns_child_final_text() -> None:
    agents = {
        name: {
            "discover": [],
            "invoke": {"allow": [], "ask": [], "deny": []},
        }
        for name in ("main", "issues", "pull_requests", "repository", "coding", "static_verifier")
    }
    layer = CapabilityLayer(policy=PermissionPolicy(agents))
    layer.load()

    class QueueReasoner:
        def __init__(self) -> None:
            self.responses = [
                ModelResponse(
                    "delegating",
                    StructuredCall(
                        "main-agent-call",
                        "agent__issues",
                        {"task": "Inspect Issue 7", "issue_number": 7, "mode": "task"},
                    ),
                    assistant_tool_call(
                        "main-agent-call",
                        "agent__issues",
                        {"task": "Inspect Issue 7", "issue_number": 7, "mode": "task"},
                    ),
                ),
                ModelResponse(
                    "CHILD FINAL TEXT",
                    None,
                    {"role": "assistant", "content": "CHILD FINAL TEXT"},
                ),
                ModelResponse(
                    "MAIN FINAL TEXT",
                    None,
                    {"role": "assistant", "content": "MAIN FINAL TEXT"},
                ),
            ]

        def complete_messages(self, **kwargs: Any) -> ModelResponse:
            del kwargs
            return self.responses.pop(0)

        def complete_text_messages(self, **kwargs: Any) -> str:
            del kwargs
            return "text"

    class Sessions:
        def __init__(self) -> None:
            self.agent_context: dict[str, Any] | None = None
            self.event_log = SimpleNamespace(iter_events=lambda scope: ())

        def get_session(self, *args: Any) -> Any:
            del args
            return SimpleNamespace(
                agent_context=self.agent_context,
                repository_full_name="owner/repo",
            )

        def record_model_message(self, scope: Any, message: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            del scope, kwargs
            return message

        def record_message_compaction(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def save_agent_context(self, scope: Any, value: dict[str, Any] | None) -> None:
            del scope
            self.agent_context = value

    reasoner = QueueReasoner()
    sessions = Sessions()
    memory = SimpleNamespace(
        context=lambda account, repository, query: SimpleNamespace(index="", selected_pages=())
    )
    scope = SessionScope("account", "repo", "session")
    service = GitAgentService(
        layer,
        main_reasoner=reasoner,
        agent_reasoner=reasoner,
        session_manager=sessions,
        memory_search=memory,
        session_scope=scope,
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect issue 7"},
    ]

    result = service.handle(
        "inspect issue 7",
        repository="owner/repo",
        main_messages=messages,
        session_scope=scope,
        turn_seq=1,
    )

    assert result.output == "MAIN FINAL TEXT"
    assert result.domain_output.answer == "CHILD FINAL TEXT"
    tool = next(message for message in messages if message["role"] == "tool")
    assert tool["tool_call_id"] == "main-agent-call"
    assert json.loads(tool["content"])["content"] == "CHILD FINAL TEXT"


def _candidate() -> CandidatePatch:
    return CandidatePatch(
        summary="safe change",
        root_cause="test",
        added_files=[],
        modified_files=["src/example.py"],
        deleted_files=[],
        patch="diff",
        files={"src/example.py": "value = 2\n"},
    )


def test_unverified_candidate_and_stale_merge_head_are_rejected() -> None:
    unverified = SimpleNamespace(
        agent="pull_requests",
        code_candidate=_candidate(),
        verification=VerificationReport(False, []),
        observations=[],
    )
    with pytest.raises(WorkflowError, match="verified CandidatePatch"):
        StructuredCallDispatcher.validate_protected_capability(
            unverified,
            "github.commit",
            {
                "branch": "feature",
                "files": unverified.code_candidate.files,
                "deleted_files": [],
                "message": unverified.code_candidate.summary,
            },
        )

    ready = SimpleNamespace(
        agent="pull_requests",
        observations=[
            {
                "kind": "capability",
                "payload": {
                    "capability_id": "github.get_pr",
                    "data": {"number": 7, "head": {"sha": "reviewed-sha"}},
                },
            },
            {
                "kind": "agent_artifact",
                "payload": {
                    "name": "merge_readiness",
                    "data": {"status": "准备合并"},
                },
            },
        ],
    )
    StructuredCallDispatcher.validate_protected_capability(
        ready,
        "github.merge",
        {"pr_number": 7, "expected_head_sha": "reviewed-sha"},
    )
    with pytest.raises(WorkflowError, match="reviewed PR head SHA"):
        StructuredCallDispatcher.validate_protected_capability(
            ready,
            "github.merge",
            {"pr_number": 7, "expected_head_sha": "stale-sha"},
        )


def test_merge_readiness_failure_cannot_be_bypassed() -> None:
    context = SimpleNamespace(
        agent="pull_requests",
        observations=[
            {
                "kind": "capability",
                "payload": {
                    "capability_id": "github.get_pr",
                    "data": {"number": 7, "head": {"sha": "reviewed-sha"}},
                },
            },
            {
                "kind": "agent_artifact",
                "payload": {
                    "name": "merge_readiness",
                    "data": {"status": "存在阻塞"},
                },
            },
        ],
    )

    with pytest.raises(WorkflowError, match="readiness has not passed"):
        StructuredCallDispatcher.validate_protected_capability(
            context,
            "github.merge",
            {"pr_number": 7, "expected_head_sha": "reviewed-sha"},
        )


def _write_runtime() -> tuple[Provider, AgentHarness, Any]:
    item = capability(
        "test.write",
        AccessLevel.WRITE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"commit": {"type": "string"}},
            "required": ["commit"],
            "additionalProperties": False,
        },
    )
    provider = Provider([item], {item.id: {"commit": "abc123"}})
    layer = CapabilityLayer(policy=policy(ask=[item.id]))
    layer.add_provider(provider)
    layer.load()
    harness = AgentHarness(layer)
    harness.register(AgentSpec("worker", "worker", "system", ()))
    return provider, harness, harness.context("worker", "session", repository="o/r")


def test_reject_never_executes_and_approval_executes_the_exact_provider_call() -> None:
    arguments = {"path": "result.txt", "content": "value"}
    provider, harness, context = _write_runtime()
    dispatcher = StructuredCallDispatcher(harness)
    context.start_message_thread()
    context.append_message(
        assistant_tool_call(
            "provider-write-call", harness.function_name("test.write"), arguments
        )
    )
    call = CapabilityCall("provider-write-call", "test.write", arguments)

    assert dispatcher.handle_capability(context, call, summary="write result") is False
    assert context.pending is not None
    assert context.pending.provider_call_id == "provider-write-call"
    assert provider.invocations == []

    dispatcher.apply_user_decision(
        context, WorkflowTurnDecision(ApprovalIntent.REJECT)
    )
    assert context.pending is None
    assert provider.invocations == []
    rejected = json.loads(context.messages[-1]["content"])
    assert context.messages[-1]["tool_call_id"] == "provider-write-call"
    assert rejected["status"] == "rejected"

    provider, harness, context = _write_runtime()
    dispatcher = StructuredCallDispatcher(harness)
    context.start_message_thread()
    context.append_message(
        assistant_tool_call(
            "provider-write-call", harness.function_name("test.write"), arguments
        )
    )
    dispatcher.handle_capability(
        context,
        CapabilityCall("provider-write-call", "test.write", arguments),
        summary="write result",
    )
    dispatcher.apply_user_decision(
        context, WorkflowTurnDecision(ApprovalIntent.APPROVE)
    )

    assert context.pending is None
    assert provider.invocations == [("test.write", arguments)]
    assert context.messages[-1]["tool_call_id"] == "provider-write-call"


def test_pending_approval_restore_preserves_identity_and_frozen_call() -> None:
    _, harness, context = _write_runtime()
    dispatcher = StructuredCallDispatcher(harness)
    calls = [
        PlannedCapabilityCall(
            "test.write", {"path": "result.txt", "content": "value"}
        )
    ]
    dispatcher.queue(context, "write result", calls, provider_call_id="provider-call")
    assert context.pending is not None
    approval_id = context.pending.approval_id

    restored = harness.context("worker", "session", repository="o/r")
    dispatcher.restore_pending(
        restored,
        approval_id=approval_id,
        summary="write result",
        calls=calls,
        provider_call_id="provider-call",
    )

    assert restored.pending is not None
    assert restored.pending.approval_id == approval_id
    assert restored.pending.calls == calls
    assert restored.pending.provider_call_id == "provider-call"


def test_revised_review_supersedes_the_original_call_and_freezes_a_new_call() -> None:
    _, harness, context = _write_runtime()
    dispatcher = StructuredCallDispatcher(harness)
    original = {
        "pr_number": 7,
        "event": "COMMENT",
        "body": "original review",
    }
    context.start_message_thread()
    context.append_message(
        assistant_tool_call(
            "original-review-call",
            harness.function_name("github.post_review"),
            original,
        )
    )
    dispatcher.queue(
        context,
        "post review",
        [PlannedCapabilityCall("github.post_review", original)],
        provider_call_id="original-review-call",
    )
    service = object.__new__(GitAgentService)
    service.harness = harness
    service.loop = AgentLoop(harness)
    service._revise_text = lambda current, artifact, body, instruction: (  # type: ignore[method-assign]
        f"{body} — {instruction}"
    )

    service._revise_pr_review(context, "add evidence")

    assert context.pending is not None
    assert context.pending.provider_call_id != "original-review-call"
    assert context.pending.calls[0].arguments["body"] == (
        "original review — add evidence"
    )
    assert context.messages[-2]["role"] == "tool"
    assert context.messages[-2]["tool_call_id"] == "original-review-call"
    assert context.messages[-1]["role"] == "assistant"
    assert context.messages[-1]["tool_calls"][0]["id"] == (
        context.pending.provider_call_id
    )
def test_repeated_identical_capability_call_is_bounded() -> None:
    item = capability(
        "test.read",
        AccessLevel.READ,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )
    provider = Provider([item], {item.id: {"ok": True}})
    layer = CapabilityLayer(policy=policy(allow=[item.id]))
    layer.add_provider(provider)
    layer.load()
    harness = AgentHarness(layer)
    harness.register(AgentSpec("worker", "worker", "system", ()))
    context = harness.context("worker", "session", max_steps=6)

    class RepeatingAgent:
        calls = 0

        @staticmethod
        def agent_schemas() -> dict[str, dict[str, Any]]:
            return {}

        def step(self, current: Any) -> ModelResponse:
            self.calls += 1
            call_id = f"repeat-{self.calls}"
            name = harness.function_name(item.id)
            message = current.append_message(
                assistant_tool_call(call_id, name, {"value": 1})
            )
            return ModelResponse(
                "read",
                StructuredCall(call_id, name, {"value": 1}),
                message,
            )

        @staticmethod
        def build_result(current: Any) -> None:
            del current

    AgentLoop(harness).start(context, RepeatingAgent())

    assert context.finished
    assert "repeated an identical capability call" in str(context.error)
    assert provider.invocations == [(item.id, {"value": 1})]


def test_capability_validation_failure_is_observed_before_model_replans() -> None:
    item = capability(
        "test.read",
        AccessLevel.READ,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )
    provider = Provider([item], {item.id: {"ok": True}})
    layer = CapabilityLayer(policy=policy(allow=[item.id]))
    layer.add_provider(provider)
    layer.load()
    harness = AgentHarness(layer)
    harness.register(AgentSpec("worker", "worker", "system", ()))
    context = harness.context("worker", "session", max_steps=4)

    class ReplanningAgent:
        calls = 0

        @staticmethod
        def agent_schemas() -> dict[str, dict[str, Any]]:
            return {}

        def step(self, current: Any) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                call_id = "invalid-read"
                name = harness.function_name(item.id)
                message = current.append_message(
                    assistant_tool_call(call_id, name, {})
                )
                return ModelResponse(
                    "read",
                    StructuredCall(call_id, name, {}),
                    message,
                )
            message = current.append_message(
                {"role": "assistant", "content": "The read input was invalid."}
            )
            return ModelResponse("The read input was invalid.", None, message)

        @staticmethod
        def build_result(current: Any) -> str:
            return current.final_message

    AgentLoop(harness).start(context, ReplanningAgent())

    assert context.finished and context.error is None
    failure = next(
        item for item in context.observations if item["kind"] == "capability_error"
    )
    assert failure["payload"]["error"] == "invalid_input"
    assert provider.invocations == []
    assert context.final_message == "The read input was invalid."


def test_provider_failures_are_bounded_and_terminal() -> None:
    layer = CapabilityLayer(policy=policy())
    layer.load()
    harness = AgentHarness(layer)
    harness.register(AgentSpec("worker", "worker", "system", ()))
    context = harness.context("worker", "session", max_steps=10)

    class FailingAgent:
        @staticmethod
        def step(current: Any) -> ModelResponse:
            del current
            raise LLMProviderError("provider unavailable")

        @staticmethod
        def build_result(current: Any) -> None:
            del current

    AgentLoop(harness).start(context, FailingAgent())

    assert context.finished
    assert context.steps == 2
    assert "一次有限重试后终止" in str(context.error)


def test_coding_refuses_to_overwrite_a_truncated_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = {
        "coding": {
            "discover": [],
            "invoke": {"allow": [], "ask": [], "deny": []},
        }
    }
    layer = CapabilityLayer(policy=PermissionPolicy(agents))
    layer.load()
    harness = AgentHarness(layer)
    coding = CodingAgent(harness)
    context = harness.context("coding", "session", repository="owner/repo")
    request = ChangeRequest(
        repository="owner/repo",
        description="replace one value",
        source_ref="head-sha",
        replacements=[Replacement("src/example.py", "old", "new")],
    )

    def invoke(current: Any, capability_id: str, **arguments: Any) -> Any:
        del current, arguments
        if capability_id == "repository.get_file_status":
            return {"existing_files": ["src/example.py"]}
        if capability_id == "repository.read_files":
            return {
                "files": [
                    {
                        "path": "src/example.py",
                        "content": "old",
                        "truncated": True,
                    }
                ]
            }
        raise AssertionError(capability_id)

    monkeypatch.setattr(CodingAgent, "_invoke_capability", staticmethod(invoke))

    with pytest.raises(WorkflowError, match="truncated overwrite"):
        coding._create(context, request, None)


def test_fork_pr_candidate_never_queues_a_commit() -> None:
    context = SimpleNamespace(
        repository="owner/repo",
        code_candidate=_candidate(),
        verification=VerificationReport(True, []),
        observations=[
            {
                "kind": "capability",
                "payload": {
                    "capability_id": "github.get_pr",
                    "data": {
                        "number": 7,
                        "head": {
                            "ref": "feature",
                            "repo": {"full_name": "contributor/repo"},
                        },
                    },
                },
            }
        ],
    )
    queued: list[Any] = []
    dispatcher = SimpleNamespace(queue=lambda *args, **kwargs: queued.append((args, kwargs)))
    agent = object.__new__(PullRequestAgent)

    agent.after_agent_result(
        context,
        AgentCall("coding-call", "coding", {"task": "fix", "mode": "patch"}),
        AgentResult("coding-call", "coding", "completed", "candidate ready"),
        dispatcher,
    )

    assert queued == []
