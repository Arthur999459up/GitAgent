from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gitagent.agent_loop import AgentAction, AgentActionKind, AgentLoop
from gitagent.application.capabilities import build_capability_layer
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
from gitagent.capability.errors import (
    ProviderConflictError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from gitagent.capability.providers import (
    MCPProvider,
    MCPServerDefinition,
    MCPToolDefinition,
    NativeProvider,
)
from gitagent.domain.errors import ValidationError
from gitagent.domain.models import (
    AgentSpec,
    ApprovalIntent,
    PlannedCapabilityCall,
    WorkflowTurnDecision,
)
from gitagent.harness import AgentHarness
from gitagent.infra.github import InMemoryGitHubClient
from gitagent.infra.mcp import MCPTransportError


@dataclass
class ScriptedProvider:
    target: Any
    id: str = "scripted"
    reconnects: int = 0

    def load(self) -> list[CapabilityRegistration]:
        return []

    def invoke(self, binding: CapabilityBinding, arguments: dict[str, Any], context: InvocationContext) -> Any:
        return self.target(arguments, context)

    def reconnect(self, binding: CapabilityBinding) -> None:
        self.reconnects += 1


def registration(
    capability_id: str,
    *,
    access: AccessLevel = AccessLevel.READ,
    provider_id: str = "scripted",
) -> CapabilityRegistration:
    capability = Capability(
        capability_id,
        CapabilityKind.MCP_TOOL,
        "Test capability.",
        capability_id.rsplit(".", 1)[0],
        CapabilityStatus.AVAILABLE,
        access,
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    return CapabilityRegistration(
        capability,
        CapabilityBinding(capability_id, provider_id, object()),
    )


def layer_for(
    provider: ScriptedProvider,
    item: CapabilityRegistration,
    agent_policy: dict[str, Any],
) -> CapabilityLayer:
    policy = PermissionPolicy(agent_policy)
    layer = CapabilityLayer(policy=policy)
    layer.add_provider(provider)
    layer.registry.register(item)
    return layer


def context(agent: str = "reader", *, run_id: str = "run-1", approval_id: str | None = None) -> InvocationContext:
    return InvocationContext(run_id, "session-1", agent, "owner/repo", approval_id=approval_id)


def test_default_deny_hides_and_blocks_without_provider_call() -> None:
    calls = 0

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> str:
        nonlocal calls
        calls += 1
        return arguments["value"]

    provider = ScriptedProvider(target)
    item = registration("sample.read")
    layer = layer_for(provider, item, {"reader": {"discover": [], "invoke": {"allow": []}}})

    assert layer.discover(context()) == ()
    result = layer.invoke("sample.read", {"value": "x"}, context())

    assert result.status == "failed"
    assert result.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert calls == 0


def test_registry_rejects_duplicates_and_invalid_schema_and_replaces_one_source() -> None:
    registry = CapabilityRegistry()
    first = registration("source.first")
    second = registration("source.second")
    registry.register(first)

    with pytest.raises(ValidationError, match="duplicate capability"):
        registry.register(first)

    invalid = CapabilityRegistration(
        Capability(
            "source.invalid",
            CapabilityKind.MCP_TOOL,
            "Invalid schema.",
            "source",
            CapabilityStatus.AVAILABLE,
            AccessLevel.READ,
            {"properties": {}},
        ),
        CapabilityBinding("source.invalid", "scripted", object()),
    )
    with pytest.raises(ValidationError, match="must declare type"):
        registry.register(invalid)

    registry.replace_source("source", [second])

    assert registry.get("source.first") is None
    assert registry.get("source.second") == second.capability


def test_failure_guard_blocks_identical_final_failure_but_allows_changed_arguments() -> None:
    calls = 0

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> None:
        nonlocal calls
        calls += 1
        raise ProviderConflictError(arguments["value"])

    provider = ScriptedProvider(target)
    item = registration("sample.edit", access=AccessLevel.WRITE)
    layer = layer_for(
        provider,
        item,
        {"reader": {"discover": ["sample.edit"], "invoke": {"allow": ["sample.edit"]}}},
    )
    invocation = context()

    first = layer.invoke("sample.edit", {"value": "old"}, invocation)
    repeated = layer.invoke("sample.edit", {"value": "old"}, invocation)
    changed = layer.invoke("sample.edit", {"value": "new"}, invocation)

    assert first.error.type == CapabilityErrorType.CONFLICT
    assert repeated.error.type == CapabilityErrorType.REPEATED_FAILURE
    assert repeated.attempts == 0
    assert changed.error.type == CapabilityErrorType.CONFLICT
    assert calls == 2


def test_read_timeout_retries_once_and_trace_listener_failure_is_isolated() -> None:
    calls = 0

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderTimeoutError("temporary timeout")
        return arguments["value"]

    provider = ScriptedProvider(target)
    item = registration("sample.read")
    layer = layer_for(
        provider,
        item,
        {"reader": {"discover": ["sample.read"], "invoke": {"allow": ["sample.read"]}}},
    )
    layer.trace.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("listener failed")))

    result = layer.invoke("sample.read", {"value": "ok"}, context())

    assert result.status == "success"
    assert result.attempts == 2
    assert calls == 2
    assert [event.event for event in layer.trace.events()] == [
        "call.started",
        "attempt.started",
        "attempt.failed",
        "recovery.started",
        "attempt.started",
        "attempt.succeeded",
        "recovery.succeeded",
        "call.succeeded",
    ]


def test_mutation_timeout_after_send_is_uncertain_and_never_retried() -> None:
    calls = 0

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> None:
        nonlocal calls
        calls += 1
        raise ProviderTimeoutError("connection reset", request_sent=True)

    provider = ScriptedProvider(target)
    item = registration("sample.write", access=AccessLevel.WRITE)
    layer = layer_for(
        provider,
        item,
        {"writer": {"discover": ["sample.write"], "invoke": {"allow": ["sample.write"]}}},
    )

    result = layer.invoke("sample.write", {"value": "x"}, context("writer"))

    assert result.error.type == CapabilityErrorType.EXECUTION_UNCERTAIN
    assert result.attempts == 1
    assert calls == 1


def test_read_only_context_blocks_mutation_before_provider_call() -> None:
    calls = 0

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> str:
        nonlocal calls
        calls += 1
        return arguments["value"]

    provider = ScriptedProvider(target)
    item = registration("sample.write", access=AccessLevel.WRITE)
    layer = layer_for(
        provider,
        item,
        {"writer": {"discover": ["sample.write"], "invoke": {"allow": ["sample.write"]}}},
    )
    invocation = InvocationContext("run-ro", "session-1", "writer", read_only=True)

    result = layer.invoke("sample.write", {"value": "x"}, invocation)

    assert result.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert calls == 0


def test_approval_is_exact_ordered_and_one_time() -> None:
    calls: list[str] = []

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> str:
        calls.append(arguments["value"])
        return arguments["value"]

    provider = ScriptedProvider(target)
    item = registration("sample.write", access=AccessLevel.WRITE)
    layer = layer_for(
        provider,
        item,
        {
            "writer": {
                "discover": ["sample.write"],
                "invoke": {"approval_required": ["sample.write"]},
            },
            "mutator": {"discover": [], "invoke": {"approved_only": ["sample.write"]}},
        },
    )
    proposed = layer.invoke("sample.write", {"value": "exact"}, context("writer"))
    approval = layer.policy.approvals.create(
        session_id="session-1",
        repository="owner/repo",
        summary="write",
        calls=[PlannedCapabilityCall("sample.write", {"value": "exact"})],
    )
    layer.policy.approvals.decide(approval.approval_id, "Approve")

    wrong = layer.invoke(
        "sample.write",
        {"value": "changed"},
        context("mutator", run_id="run-2", approval_id=approval.approval_id),
    )
    executed = layer.invoke(
        "sample.write",
        {"value": "exact"},
        context("mutator", run_id="run-3", approval_id=approval.approval_id),
    )
    replayed = layer.invoke(
        "sample.write",
        {"value": "exact"},
        context("mutator", run_id="run-4", approval_id=approval.approval_id),
    )

    assert proposed.status == "approval_required"
    assert wrong.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert executed.status == "success"
    assert replayed.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert calls == ["exact"]


def test_native_provider_blocks_workspace_escape_and_bash_chaining(tmp_path: Path) -> None:
    provider = NativeProvider(tmp_path)
    policy = PermissionPolicy(
        {
            "coding": {
                "discover": ["native.*"],
                "invoke": {"allow": ["native.*"], "bash_profile": "coding"},
            }
        }
    )
    layer = CapabilityLayer(policy=policy)
    layer.add_provider(provider)
    layer.load()
    invocation = InvocationContext("run-native", "session-1", "coding")

    escaped = layer.invoke("native.read", {"path": "../outside"}, invocation)
    escaped_glob = layer.invoke("native.glob", {"pattern": "../*"}, invocation)
    chained = layer.invoke("native.bash", {"command": "python -V && python -V"}, invocation)

    assert escaped.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert escaped_glob.error.type == CapabilityErrorType.PERMISSION_DENIED
    assert chained.status == "approval_required"


def test_native_edit_conflict_then_identical_call_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("only current text", encoding="utf-8")
    layer = CapabilityLayer(
        policy=PermissionPolicy(
            {"coding": {"discover": ["native.*"], "invoke": {"allow": ["native.*"]}}}
        )
    )
    layer.add_provider(NativeProvider(tmp_path))
    layer.load()
    invocation = InvocationContext("run-edit", "session-1", "coding")
    arguments = {"path": "sample.txt", "old_text": "stale text", "new_text": "new"}

    conflict = layer.invoke("native.edit", arguments, invocation)
    repeated = layer.invoke("native.edit", arguments, invocation)

    assert conflict.error.type == CapabilityErrorType.CONFLICT
    assert repeated.error.type == CapabilityErrorType.REPEATED_FAILURE
    assert repeated.attempts == 0


def test_native_read_missing_and_bash_failure_continue_agent_loop(tmp_path: Path) -> None:
    layer = CapabilityLayer(
        policy=PermissionPolicy(
            {
                "coding": {
                    "discover": ["native.*"],
                    "invoke": {"allow": ["native.*"], "bash_profile": "coding"},
                }
            }
        )
    )
    layer.add_provider(NativeProvider(tmp_path))
    layer.load()
    harness = AgentHarness(layer)
    harness.register(AgentSpec("coding", "test", "test", (), frozenset()))

    class Agent:
        def __init__(self, capability_id: str, arguments: dict[str, Any]) -> None:
            self.capability_id = capability_id
            self.arguments = arguments

        def decide(self, current: Any) -> AgentAction:
            if not current.observations:
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id=self.capability_id,
                    arguments=self.arguments,
                )
            return AgentAction(AgentActionKind.FINISH, message="continued")

        def build_result(self, current: Any) -> str:
            return "continued"

    missing_context = harness.context("coding", "session-missing")
    AgentLoop(harness).start(missing_context, Agent("native.read", {"path": "missing.py"}))
    bash_context = harness.context("coding", "session-bash")
    AgentLoop(harness).start(
        bash_context,
        Agent("native.bash", {"command": "python -m py_compile missing.py"}),
    )

    assert missing_context.finished is True
    assert missing_context.error is None
    assert missing_context.observations[0]["payload"]["error"] == "resource_not_found"
    assert bash_context.finished is True
    assert bash_context.error is None
    bash_error = bash_context.last_capability_call.result.error
    assert bash_error.type == CapabilityErrorType.EXECUTION_FAILED
    assert bash_error.details["exit_code"] != 0
    assert bash_error.details["stderr_tail"]


class RecoveringMCPTransport:
    available = True

    def __init__(self, *, failure: str, disappear: bool = False) -> None:
        self.failure = failure
        self.disappear = disappear
        self.calls = 0
        self.reconnects = 0

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            if self.failure == "timeout":
                raise MCPTransportError("timeout", timed_out=True)
            raise MCPTransportError("disconnected", transport_unavailable=True)
        return {"name": name, "value": arguments["value"]}

    def reconnect(self) -> None:
        self.reconnects += 1

    def list_tools(self) -> list[dict[str, Any]]:
        if self.disappear:
            return []
        return [
            {
                "name": "read",
                "description": "Remote read.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ]


def mcp_layer(client: RecoveringMCPTransport) -> CapabilityLayer:
    layer = CapabilityLayer(
        policy=PermissionPolicy(
            {"reader": {"discover": ["remote.read"], "invoke": {"allow": ["remote.read"]}}}
        )
    )
    layer.add_provider(
        MCPProvider(
            [MCPServerDefinition("remote", "test", {})],
            [
                MCPToolDefinition(
                    "remote.read",
                    "remote",
                    "read",
                    "Remote read.",
                    {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                    None,
                    AccessLevel.READ,
                )
            ],
            clients={"remote": client},
        )
    )
    layer.load()
    return layer


def test_read_mcp_timeout_retries_once() -> None:
    client = RecoveringMCPTransport(failure="timeout")
    result = mcp_layer(client).invoke("remote.read", {"value": "ok"}, context())

    assert result.status == "success"
    assert result.attempts == 2
    assert client.calls == 2
    assert client.reconnects == 0


def test_mcp_disconnect_reconnects_once_and_retries() -> None:
    client = RecoveringMCPTransport(failure="disconnect")
    result = mcp_layer(client).invoke("remote.read", {"value": "ok"}, context())

    assert result.status == "success"
    assert result.attempts == 2
    assert client.calls == 2
    assert client.reconnects == 1


def test_mcp_refresh_removes_capability_that_disappeared() -> None:
    client = RecoveringMCPTransport(failure="disconnect", disappear=True)
    layer = mcp_layer(client)

    result = layer.invoke("remote.read", {"value": "ok"}, context())

    assert result.error.type == CapabilityErrorType.CAPABILITY_NOT_FOUND
    assert result.attempts == 1
    assert layer.discover(context()) == ()


def test_composed_layer_discovers_fixed_sources_by_agent_role(tmp_path: Path) -> None:
    github = InMemoryGitHubClient({"owner/repo": {"files": {}, "issues": {}, "prs": {}}})
    layer = build_capability_layer(github, workspace_root=tmp_path)

    coding = {item.id for item in layer.discover(InvocationContext("r", "s", "coding", "owner/repo"))}
    child = {
        item.id
        for item in layer.discover(InvocationContext("r", "s", "coding_subagent", "owner/repo"))
    }
    mutator = layer.discover(InvocationContext("r", "s", "github_mutator", "owner/repo"))

    assert {"native.read", "context7.query-docs", "skill.code-review", "skill.debug"} <= coding
    assert "native.agent" in coding
    assert "native.agent" not in child
    assert mutator == ()

    skill = layer.invoke(
        "skill.debug",
        {},
        InvocationContext("skill-run", "session-1", "coding", "owner/repo"),
    )
    assert skill.status == "success"
    assert skill.type == "context"
    assert "causal" in skill.content


def test_read_cache_and_file_coverage_are_preserved_through_capabilities(tmp_path: Path) -> None:
    github = InMemoryGitHubClient(
        {
            "owner/repo": {
                "files": {"sample.py": "one\ntwo\nthree\n"},
                "issues": {1: {"number": 1, "title": "Issue"}},
                "prs": {},
            }
        }
    )
    layer = build_capability_layer(github, workspace_root=tmp_path)
    harness = AgentHarness(layer)
    harness.register(AgentSpec("issues", "test", "test", (), frozenset()))
    agent_context = harness.context("issues", "session-cache", repository="owner/repo")

    first_issue = agent_context.invoke("github.get_issue", issue_number=1)
    second_issue = agent_context.invoke("github.get_issue", issue_number=1)
    first_read = agent_context.invoke(
        "repository.read_file",
        path="sample.py",
        start_line=1,
        limit=2,
    )
    second_read = agent_context.invoke(
        "repository.read_file",
        path="sample.py",
        start_line=1,
        limit=2,
    )

    assert first_issue == second_issue
    assert agent_context.read_cache
    assert first_read["content"].splitlines() == ["one", "two"]
    assert second_read["already_read"] is True
    assert agent_context.last_capability_call.covered is True
    assert agent_context.last_capability_call.result.attempts == 0


def test_capability_failure_observation_does_not_finish_agent_loop() -> None:
    provider = ScriptedProvider(lambda arguments, invocation: arguments)
    layer = layer_for(
        provider,
        registration("sample.read"),
        {"test_agent": {"discover": ["sample.*"], "invoke": {"allow": ["sample.*"]}}},
    )
    harness = AgentHarness(layer)
    harness.register(
        AgentSpec(
            name="test_agent",
            role="test",
            system_prompt="test",
            output_schema=(),
            routes=frozenset(),
        )
    )
    agent_context = harness.context("test_agent", "session-1")

    class Agent:
        def decide(self, current: Any) -> AgentAction:
            if not current.observations:
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id="missing.read",
                    arguments={"value": "x"},
                )
            return AgentAction(AgentActionKind.FINISH, message="continued")

        def build_result(self, current: Any) -> str:
            return "continued"

    AgentLoop(harness).start(agent_context, Agent())

    assert agent_context.finished is True
    assert agent_context.error is None
    assert agent_context.result == "continued"
    assert agent_context.observations[0]["kind"] == "capability_error"
    assert agent_context.observations[0]["payload"] == {
        "capability_id": "missing.read",
        "arguments": {"value": "x"},
        "error": "capability_not_found",
        "message": "Capability 不存在：missing.read",
        "details": None,
        "attempts": 0,
    }


def test_native_subagent_enforces_parent_child_permission_intersection_on_invoke(tmp_path: Path) -> None:
    provider = NativeProvider(tmp_path)
    layer = CapabilityLayer(
        policy=PermissionPolicy(
            {
                "coding": {
                    "discover": ["native.read", "native.agent"],
                    "invoke": {"allow": ["native.read", "native.agent"]},
                },
                "coding_subagent": {
                    "discover": ["native.read", "native.write"],
                    "invoke": {"allow": ["native.read", "native.write"]},
                },
            }
        )
    )
    layer.add_provider(provider)
    layer.load()
    provider.permission_resolver = lambda invocation: (
        frozenset({"native.read", "native.agent"})
        if invocation.agent_id == "coding"
        else frozenset({"native.read", "native.write"})
    )

    def run_subagent(
        task: str,
        child_context: InvocationContext,
        effective: frozenset[str],
    ) -> dict[str, Any]:
        del task
        visible = {item.id for item in layer.discover(child_context)}
        fabricated = layer.invoke(
            "native.write",
            {"path": "blocked.txt", "content": "must not be written"},
            child_context,
        )
        return {
            "effective": sorted(effective),
            "visible": sorted(visible),
            "fabricated_status": fabricated.status,
            "fabricated_error": fabricated.error.type.value,
        }

    provider.subagent_runner = run_subagent
    result = layer.invoke(
        "native.agent",
        {"task": "attempt an inherited-permission bypass"},
        InvocationContext("run-subagent", "session-1", "coding"),
    )

    assert result.status == "success"
    assert result.content["effective"] == ["native.read"]
    assert result.content["visible"] == ["native.read"]
    assert result.content["fabricated_status"] == "failed"
    assert result.content["fabricated_error"] == "permission_denied"
    assert not (tmp_path / "blocked.txt").exists()


def test_approved_mutation_failure_returns_observation_and_resumes_agent_loop() -> None:
    calls = 0

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> None:
        nonlocal calls
        del arguments, invocation
        calls += 1
        raise ProviderTimeoutError("request outcome is unknown", request_sent=True)

    provider = ScriptedProvider(target)
    layer = layer_for(
        provider,
        registration("github.post_comment", access=AccessLevel.WRITE),
        {
            "issues": {
                "discover": ["github.post_comment"],
                "invoke": {"approval_required": ["github.post_comment"]},
            },
            "github_mutator": {
                "discover": [],
                "invoke": {"approved_only": ["github.post_comment"]},
            },
        },
    )
    harness = AgentHarness(layer)
    harness.register(AgentSpec("issues", "test", "test", (), frozenset()))
    harness.register(AgentSpec("github_mutator", "test", "test", (), frozenset()))
    agent_context = harness.context("issues", "session-approved-failure", repository="owner/repo")

    class Agent:
        def decide(self, current: Any) -> AgentAction:
            if not current.observations:
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id="github.post_comment",
                    arguments={"value": "exact"},
                    summary="post exact comment",
                )
            return AgentAction(AgentActionKind.FINISH, message="continued after mutation failure")

        def build_result(self, current: Any) -> str:
            return "continued after mutation failure"

    loop = AgentLoop(harness)
    loop.start(agent_context, Agent())
    assert agent_context.pending is not None
    approval_id = agent_context.pending.approval_id

    loop.resume(
        agent_context,
        Agent(),
        WorkflowTurnDecision(ApprovalIntent.APPROVE),
    )

    assert calls == 1
    assert agent_context.finished is True
    assert agent_context.error is None
    assert agent_context.result == "continued after mutation failure"
    assert agent_context.observations[0]["kind"] == "capability_error"
    assert agent_context.observations[0]["payload"]["arguments"] == {"value": "exact"}
    assert agent_context.observations[0]["payload"]["error"] == "execution_uncertain"
    assert agent_context.observations[0]["payload"]["message"] == "request outcome is unknown"
    assert agent_context.observations[0]["payload"]["details"] is None
    assert agent_context.observations[0]["payload"]["attempts"] == 1
    assert harness.approvals.get(approval_id).decision == "Invalidated"


def test_read_rate_limit_with_short_retry_after_retries_once() -> None:
    calls = 0

    def target(arguments: dict[str, Any], invocation: InvocationContext) -> str:
        nonlocal calls
        del invocation
        calls += 1
        if calls == 1:
            raise ProviderRateLimitError("short limit", retry_after=0.0)
        return arguments["value"]

    provider = ScriptedProvider(target)
    layer = layer_for(
        provider,
        registration("sample.read"),
        {"reader": {"discover": ["sample.read"], "invoke": {"allow": ["sample.read"]}}},
    )

    result = layer.invoke("sample.read", {"value": "ok"}, context())

    assert result.status == "success"
    assert result.attempts == 2
    assert calls == 2


def test_mcp_reconnect_failure_is_structured_and_does_not_blind_retry() -> None:
    class FailingReconnectTransport(RecoveringMCPTransport):
        def reconnect(self) -> None:
            self.reconnects += 1
            raise MCPTransportError(
                "reconnect failed",
                transport_unavailable=True,
                request_sent=False,
            )

    client = FailingReconnectTransport(failure="disconnect")
    result = mcp_layer(client).invoke("remote.read", {"value": "ok"}, context())

    assert result.status == "failed"
    assert result.error.type == CapabilityErrorType.UNAVAILABLE
    assert result.attempts == 1
    assert client.calls == 1
    assert client.reconnects == 1
