from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gitagent.agent_loop import AgentAction, AgentActionKind, AgentLoop
from gitagent.agents.coding import CODING_SPEC, CodingAgent
from gitagent.agents.issues import ISSUE_AGENT_SPEC, IssueAgent
from gitagent.agents.pull_requests import PULL_REQUEST_AGENT_SPEC, PullRequestAgent
from gitagent.agents.repository import REPOSITORY_SPEC, RepositoryAgent
from gitagent.capability import (
    AccessLevel,
    CapabilityErrorType,
    CapabilityResult,
)
from gitagent.capability.errors import capability_error
from gitagent.domain.errors import LLMProviderError, WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    PullRequestOperation,
    Recommendation,
    Replacement,
    RepositoryOperation,
    VerificationReport,
    WorkflowTurnDecision,
)
from gitagent.harness.action_dispatcher import HarnessActionDispatcher
from gitagent.harness.constraints import ApprovalStore
from gitagent.harness.context import assistant_tool_call
from gitagent.harness.context.state import AgentContext
from gitagent.model.reasoner import StructuredValue


class _Trace:
    def emit(self, **details: Any) -> None:
        del details


class _Harness:
    def __init__(self) -> None:
        self.approvals = ApprovalStore()
        self.trace = _Trace()
        self.message_sink = None
        self.compaction_sink = None
        self.specs: dict[str, AgentSpec] = {}
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.approved_invocations: list[str] = []
        self.failed_capabilities: set[str] = set()
        self.last_read_only: bool | None = None
        self.capabilities = (
            SimpleNamespace(
                id="repository.get_repo_tree",
                description="tree",
                access=AccessLevel.READ,
                input_schema={"type": "object"},
            ),
            SimpleNamespace(
                id="rag.search",
                description="knowledge",
                access=AccessLevel.READ,
                input_schema={"type": "object"},
            ),
            SimpleNamespace(
                id="context7.query-docs",
                description="docs",
                access=AccessLevel.READ,
                input_schema={"type": "object"},
            ),
            SimpleNamespace(
                id="native.write",
                description="write",
                access=AccessLevel.WRITE,
                input_schema={"type": "object"},
            ),
        )

    def register(self, spec: AgentSpec) -> None:
        self.specs[spec.name] = spec

    def context_window_for(self, agent_name: str) -> int:
        del agent_name
        return 32_768

    def discover(self, context: AgentContext) -> tuple[Any, ...]:
        del context
        return self.capabilities

    def llm_tools(
        self,
        context: AgentContext,
        *,
        read_only: bool = False,
    ) -> list[dict[str, Any]]:
        del context
        self.last_read_only = read_only
        return [
            {
                "type": "function",
                "function": {
                    "name": self.function_name(capability.id),
                    "description": capability.description,
                    "parameters": capability.input_schema,
                },
            }
            for capability in self.capabilities
            if not read_only or capability.access == AccessLevel.READ
        ]

    def resolve_llm_name(self, name: str, context: AgentContext) -> str:
        del context
        for capability in self.capabilities:
            if self.function_name(capability.id) == name:
                return str(capability.id)
        return name

    @staticmethod
    def function_name(capability_id: str) -> str:
        return "capability__" + capability_id.replace(".", "__").replace("-", "_")

    def invoke(
        self,
        context: AgentContext,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        approval_id: str | None = None,
    ) -> CapabilityResult:
        self.invocations.append((capability_id, dict(arguments)))
        if capability_id == "native.write" and approval_id is None:
            return CapabilityResult(capability_id, "approval_required", "approval")
        if approval_id is not None:
            self.approvals.authorize(
                approval_id=approval_id,
                session_id=context.session_id,
                capability_id=capability_id,
                arguments=arguments,
            )
            self.approved_invocations.append(capability_id)
        if capability_id in self.failed_capabilities:
            return CapabilityResult(
                capability_id,
                "failed",
                "error",
                error=capability_error(
                    CapabilityErrorType.INVALID_INPUT,
                    "invalid capability arguments",
                ),
            )
        content = (
            {"entries": ["src/example.py"], "truncated": False}
            if capability_id == "repository.get_repo_tree"
            else {"results": ["evidence"]}
        )
        return CapabilityResult(capability_id, "success", "data", content)


class _Reasoner:
    def __init__(self, *values: dict[str, Any]) -> None:
        self.values = list(values)
        self.tools: list[list[dict[str, Any]] | None] = []

    def complete_structured_messages(self, **kwargs: Any) -> dict[str, Any]:
        self.tools.append(kwargs.get("final_tools"))
        value = self.values.pop(0)
        capability_id = str(value.get("capability_id") or "")
        name = (
            _Harness.function_name(capability_id)
            if value.get("kind") == "capability"
            else str(kwargs["tool_name"])
        )
        return StructuredValue(
            value,
            assistant_tool_call(
                f"call-{len(self.tools)}",
                name,
                value.get("arguments", {})
                if value.get("kind") == "capability"
                else value,
            ),
        )

    def complete_text_messages(self, **kwargs: Any) -> str:
        del kwargs
        return "text"


class _ForbiddenReasoner:
    def __init__(self) -> None:
        self.calls = 0

    def complete_structured_messages(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.calls += 1
        raise AssertionError("result projection must not invoke structured reasoning")

    def complete_text_messages(self, **kwargs: Any) -> str:
        del kwargs
        self.calls += 1
        raise AssertionError("result projection must not invoke text reasoning")


class _AnalysisCoding:
    def __init__(self, *, forbidden: bool = False) -> None:
        self.forbidden = forbidden
        self.calls: list[str] = []

    def _called(self, name: str) -> None:
        if self.forbidden:
            raise AssertionError(
                f"result projection must not call CodingAgent.{name}()"
            )
        self.calls.append(name)

    def explain(self, *args: Any, **kwargs: Any) -> CodeExplanationResult:
        del args, kwargs
        self._called("explain")
        return CodeExplanationResult(["behavior"], ["symbol"], [], ["scope.py"])

    def review(self, *args: Any, **kwargs: Any) -> CodeReviewResult:
        del args, kwargs
        self._called("review")
        return CodeReviewResult(
            summary="reviewed",
            blocking_issues=[],
            impacts=["scope.py"],
            suggestions=[],
            test_assessment="tests not run",
            risk_level="LOW",
            recommendation=Recommendation.APPROVE,
            goal_alignment="ALIGNED",
        )

    def plan(self, *args: Any, **kwargs: Any) -> CodePlanResult:
        del args, kwargs
        self._called("plan")
        return CodePlanResult("implement safely", ["scope.py"], [], ["pytest"])

    def summarize_review_dialogue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self._called("summarize_review_dialogue")
        return {
            "resolved": ["done"],
            "explained": [],
            "needs_changes": [],
            "discussion": [],
            "conflicts": [],
            "reply_draft": "thanks",
        }

    def analyze_ci(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self._called("analyze_ci")
        return {
            "facts": ["CI passed"],
            "suspected_causes": [],
            "related_changes": ["scope.py"],
            "actions": [],
        }


def _context(
    harness: _Harness, spec: AgentSpec, *, entity_id: str | None = None
) -> AgentContext:
    return AgentContext(
        harness,
        spec,
        "session",
        repository="owner/repo",
        goal="use the best evidence source",
        entity_type="pull_request" if spec.name == "pull_requests" else "issue",
        entity_id=entity_id,
    )


@pytest.mark.parametrize(
    ("agent_type", "spec", "operation", "capability_id"),
    [
        (
            RepositoryAgent,
            REPOSITORY_SPEC,
            RepositoryOperation.EXPLAIN.value,
            "rag.search",
        ),
        (IssueAgent, ISSUE_AGENT_SPEC, "", "repository.get_repo_tree"),
        (
            PullRequestAgent,
            PULL_REQUEST_AGENT_SPEC,
            PullRequestOperation.REVIEW.value,
            "context7.query-docs",
        ),
    ],
)
def test_domain_agents_use_one_shared_autonomous_capability_decision(
    agent_type: type[Any],
    spec: AgentSpec,
    operation: str,
    capability_id: str,
) -> None:
    harness = _Harness()
    reasoner = _Reasoner(
        {
            "kind": "capability",
            "summary": "collect evidence",
            "capability_id": capability_id,
            "arguments": {"query": "focused"},
        }
    )
    agent = agent_type(harness, SimpleNamespace(), SimpleNamespace(), reasoner)
    context = _context(harness, spec, entity_id="7")
    context.operation = operation

    action = agent.decide(context)

    assert action.kind == AgentActionKind.CAPABILITY
    assert action.capability_id == capability_id
    assert any(
        tool["function"]["name"] == _Harness.function_name(capability_id)
        for tool in reasoner.tools[0] or []
    )


def test_coding_autonomously_reads_evidence_but_never_exposes_write_tools() -> None:
    harness = _Harness()
    reasoner = _Reasoner(
        {
            "kind": "capability",
            "summary": "inspect tree",
            "capability_id": "repository.get_repo_tree",
            "arguments": {"depth": 2},
        },
        {
            "behavior_changes": ["changed"],
            "key_symbols": ["example"],
            "call_relationships": [],
            "impact_scope": ["src/example.py"],
        },
    )
    coding = CodingAgent(harness, reasoner)
    context = _context(harness, CODING_SPEC)

    result = coding._explain(context, "explain", {}, None)

    assert result.key_symbols == ["example"]
    assert harness.invocations == [("repository.get_repo_tree", {"depth": 2})]
    assert harness.last_read_only is True
    tool_names = {
        tool["function"]["name"] for tools in reasoner.tools for tool in tools or []
    }
    assert _Harness.function_name("native.write") not in tool_names


def test_repeated_identical_domain_call_terminates_after_one_correction() -> None:
    harness = _Harness()
    spec = AgentSpec("test", "test", "system", (), frozenset())
    context = AgentContext(
        harness,
        spec,
        "session",
        repository="owner/repo",
        goal="loop",
        max_steps=6,
    )
    action = AgentAction(
        AgentActionKind.CAPABILITY,
        capability_id="repository.get_repo_tree",
        arguments={"depth": 2},
        summary="repeat",
    )

    class RepeatingAgent:
        def decide(self, current: AgentContext) -> AgentAction:
            del current
            return action

        def build_result(self, current: AgentContext) -> None:
            del current

    AgentLoop(harness).start(context, RepeatingAgent())

    assert context.finished
    assert "repeated an identical capability call" in str(context.error)
    assert harness.invocations == [("repository.get_repo_tree", {"depth": 2})]


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


def test_unverified_candidate_cannot_be_committed() -> None:
    context = SimpleNamespace(
        agent="pull_requests",
        code_candidate=_candidate(),
        verification=VerificationReport(False, []),
        observations=[],
    )

    with pytest.raises(WorkflowError, match="verified CandidatePatch"):
        HarnessActionDispatcher.validate_protected_capability(
            context,
            "github.commit",
            {
                "files": context.code_candidate.files,
                "deleted_files": [],
                "message": context.code_candidate.summary,
            },
        )


def test_merge_proposal_must_match_ready_reviewed_head_sha() -> None:
    context = SimpleNamespace(
        agent="pull_requests",
        observations=[
            {
                "kind": "capability",
                "payload": {
                    "capability_id": "github.get_pr",
                    "data": {"head": {"sha": "reviewed-sha"}},
                },
            },
            {
                "kind": "agent",
                "payload": {
                    "agent": "pull_requests",
                    "capability": "merge_readiness",
                    "data": {"status": "准备合并"},
                },
            },
        ],
    )

    HarnessActionDispatcher.validate_protected_capability(
        context,
        "github.merge",
        {"pr_number": 7, "expected_head_sha": "reviewed-sha"},
    )
    with pytest.raises(WorkflowError, match="reviewed PR head SHA"):
        HarnessActionDispatcher.validate_protected_capability(
            context,
            "github.merge",
            {"pr_number": 7, "expected_head_sha": "stale-sha"},
        )


def test_fork_pr_returns_candidate_without_commit_proposal() -> None:
    harness = _Harness()
    agent = PullRequestAgent(harness, SimpleNamespace(), SimpleNamespace(), None)
    context = _context(harness, PULL_REQUEST_AGENT_SPEC, entity_id="7")
    context.operation = PullRequestOperation.MODIFY.value
    pull_request = {
        "number": 7,
        "head": {"ref": "feature", "repo": {"full_name": "contributor/repo"}},
        "base": {"ref": "main"},
    }
    context.observations.append(
        {
            "kind": "capability",
            "payload": {"capability_id": "github.get_pr", "data": pull_request},
        }
    )

    def prepared(current: AgentContext, observed: dict[str, Any]) -> str:
        assert observed is pull_request
        current.code_candidate = _candidate()
        current.verification = VerificationReport(True, [])
        return ""

    agent._ensure_candidate = prepared  # type: ignore[method-assign]

    action = agent._prepare_change_action(context)

    assert action.kind == AgentActionKind.FINISH
    assert "Fork Pull Request" in action.message
    assert "diff" in action.message


def test_write_capability_cannot_forge_approval_and_reject_never_executes() -> None:
    harness = _Harness()
    context = _context(harness, CODING_SPEC)
    context.start_message_thread()
    dispatcher = HarnessActionDispatcher(harness)
    action = AgentAction(
        AgentActionKind.CAPABILITY,
        capability_id="native.write",
        arguments={"path": "result.txt", "content": "value", "approval_id": "forged"},
        summary="write result",
    )

    assert dispatcher.handle(context, SimpleNamespace(), action) is False
    assert context.pending is not None
    assert harness.approved_invocations == []

    dispatcher.apply_user_decision(
        context,
        WorkflowTurnDecision(ApprovalIntent.REJECT),
    )

    assert context.pending is None
    assert harness.approved_invocations == []
    assert any(item["kind"] == "rejection" for item in context.observations)


def test_explicit_approval_executes_the_exact_pending_call() -> None:
    harness = _Harness()
    context = _context(harness, CODING_SPEC)
    context.start_message_thread()
    dispatcher = HarnessActionDispatcher(harness)
    arguments = {"path": "result.txt", "content": "value"}

    dispatcher.handle(
        context,
        SimpleNamespace(),
        AgentAction(
            AgentActionKind.CAPABILITY,
            capability_id="native.write",
            arguments=arguments,
            summary="write result",
        ),
    )
    dispatcher.apply_user_decision(
        context,
        WorkflowTurnDecision(ApprovalIntent.APPROVE),
    )

    assert context.pending is None
    assert harness.approved_invocations == ["native.write"]
    assert harness.invocations[-1] == ("native.write", arguments)


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
                "kind": "agent",
                "payload": {
                    "agent": "pull_requests",
                    "capability": "merge_readiness",
                    "data": {"status": "存在阻塞"},
                },
            },
        ],
    )

    with pytest.raises(WorkflowError, match="readiness has not passed"):
        HarnessActionDispatcher.validate_protected_capability(
            context,
            "github.merge",
            {"pr_number": 7, "expected_head_sha": "reviewed-sha"},
        )


def test_coding_refuses_to_overwrite_a_truncated_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness()
    coding = CodingAgent(harness)
    context = _context(harness, CODING_SPEC)
    request = ChangeRequest(
        repository="owner/repo",
        description="replace one value",
        source_ref="head-sha",
        replacements=[Replacement("src/example.py", "old", "new")],
    )

    def invoke(current: AgentContext, capability_id: str, **arguments: Any) -> Any:
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


def test_provider_failures_are_bounded_and_terminal() -> None:
    harness = _Harness()
    spec = AgentSpec("test", "test", "system", (), frozenset())
    context = AgentContext(harness, spec, "session", max_steps=10)

    class FailingAgent:
        def decide(self, current: AgentContext) -> AgentAction:
            del current
            raise LLMProviderError("provider unavailable")

        def build_result(self, current: AgentContext) -> None:
            del current

    AgentLoop(harness).start(context, FailingAgent())

    assert context.finished
    assert context.steps == 2
    assert "一次有限重试后终止" in str(context.error)
    assert [item["kind"] for item in context.observations] == [
        "provider_error",
        "provider_error",
    ]


def test_capability_parameter_failure_is_observed_before_model_replans() -> None:
    harness = _Harness()
    harness.failed_capabilities.add("rag.search")
    spec = AgentSpec("test", "test", "system", (), frozenset())
    context = AgentContext(harness, spec, "session", max_steps=4)
    actions = iter(
        (
            AgentAction(
                AgentActionKind.CAPABILITY,
                capability_id="rag.search",
                arguments={"bad": True},
                summary="try evidence",
            ),
            AgentAction(
                AgentActionKind.FINISH,
                summary="explain failure",
                message="The evidence capability rejected its arguments.",
            ),
        )
    )

    class ReplanningAgent:
        def decide(self, current: AgentContext) -> AgentAction:
            del current
            return next(actions)

        def build_result(self, current: AgentContext) -> None:
            del current

    AgentLoop(harness).start(context, ReplanningAgent())

    assert context.finished and context.error is None
    failure = next(
        item for item in context.observations if item["kind"] == "capability_error"
    )
    assert failure["payload"]["error"] == "invalid_input"
    assert context.final_message == "The evidence capability rejected its arguments."


@pytest.mark.parametrize(
    "operation",
    [
        PullRequestOperation.EXPLAIN,
        PullRequestOperation.REVIEW,
        PullRequestOperation.PLAN,
        PullRequestOperation.CI_ANALYZE,
        PullRequestOperation.REVIEW_DIALOGUE,
    ],
)
def test_pull_request_build_result_is_a_pure_projection(
    operation: PullRequestOperation,
) -> None:
    harness = _Harness()
    coding = _AnalysisCoding(forbidden=True)
    reasoner = _ForbiddenReasoner()
    agent = PullRequestAgent(harness, coding, SimpleNamespace(), reasoner)
    context = _context(harness, PULL_REQUEST_AGENT_SPEC, entity_id="7")
    context.operation = operation.value
    context.final_message = "finished without hidden work"
    observations_before = list(context.observations)
    messages_before = list(context.messages)

    result = agent.build_result(context)

    assert result.answer == "finished without hidden work"
    assert coding.calls == []
    assert reasoner.calls == 0
    assert context.observations == observations_before
    assert context.messages == messages_before


@pytest.mark.parametrize(
    "operation",
    [
        PullRequestOperation.EXPLAIN,
        PullRequestOperation.REVIEW,
        PullRequestOperation.PLAN,
        PullRequestOperation.CI_ANALYZE,
        PullRequestOperation.REVIEW_DIALOGUE,
    ],
)
def test_finish_does_not_trigger_hidden_pull_request_analysis(
    operation: PullRequestOperation,
) -> None:
    harness = _Harness()
    coding = _AnalysisCoding(forbidden=True)
    reasoner = _Reasoner(
        {
            "kind": "finish",
            "summary": "done",
            "message": "the model chose to finish",
        }
    )
    agent = PullRequestAgent(harness, coding, SimpleNamespace(), reasoner)
    context = _context(harness, PULL_REQUEST_AGENT_SPEC, entity_id="7")
    context.operation = operation.value

    AgentLoop(harness).start(context, agent)

    assert context.finished and context.error is None
    assert context.final_message == "the model chose to finish"
    assert coding.calls == []
    assert len(reasoner.tools) == 1


@pytest.mark.parametrize(
    ("analysis", "operation", "field", "member", "expected", "coding_call"),
    [
        (
            "explain",
            PullRequestOperation.EXPLAIN,
            "interpretation",
            "behavior_changes",
            ["behavior"],
            "explain",
        ),
        (
            "review",
            PullRequestOperation.REVIEW,
            "review",
            "summary",
            "reviewed",
            "review",
        ),
        (
            "plan",
            PullRequestOperation.PLAN,
            "plan",
            "direction",
            "implement safely",
            "plan",
        ),
        (
            "ci",
            PullRequestOperation.CI_ANALYZE,
            "ci_analysis",
            "facts",
            ["CI passed"],
            "analyze_ci",
        ),
        (
            "review_dialogue",
            PullRequestOperation.REVIEW_DIALOGUE,
            "review_dialogue",
            "resolved",
            ["done"],
            "summarize_review_dialogue",
        ),
    ],
)
def test_explicit_analysis_action_records_typed_pr_result_before_finish(
    analysis: str,
    operation: PullRequestOperation,
    field: str,
    member: str,
    expected: Any,
    coding_call: str,
) -> None:
    harness = _Harness()
    coding = _AnalysisCoding()
    reasoner = _Reasoner(
        {
            "kind": "complete_analysis",
            "summary": f"complete {analysis}",
            "arguments": {"analysis": analysis},
        },
        {
            "kind": "finish",
            "summary": "done",
            "message": "typed analysis completed",
        },
    )
    agent = PullRequestAgent(harness, coding, SimpleNamespace(), reasoner)
    context = _context(harness, PULL_REQUEST_AGENT_SPEC, entity_id="7")
    context.operation = operation.value

    AgentLoop(harness).start(context, agent)

    assert context.finished and context.error is None
    artifact = getattr(context.result, field)
    value = (
        artifact[member] if isinstance(artifact, dict) else getattr(artifact, member)
    )
    assert value == expected
    assert coding.calls == [coding_call]
    assert len(reasoner.tools) == 2


def test_explicit_analysis_action_rejects_a_kind_outside_the_selected_goal() -> None:
    harness = _Harness()
    coding = _AnalysisCoding()
    reasoner = _Reasoner(
        {
            "kind": "complete_analysis",
            "summary": "try an unrelated review",
            "arguments": {"analysis": "review"},
        }
    )
    agent = PullRequestAgent(harness, coding, SimpleNamespace(), reasoner)
    context = _context(harness, PULL_REQUEST_AGENT_SPEC, entity_id="7")
    context.operation = PullRequestOperation.PLAN.value

    AgentLoop(harness).start(context, agent)

    assert context.finished
    assert "analysis review is not valid for PLAN" in str(context.error)
    assert coding.calls == []
