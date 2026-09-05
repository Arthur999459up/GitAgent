"""Session-scoped Main Agent using native Agent calls."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from gitagent.agent_loop import AgentCall, ModelResponse
from gitagent.capability import AccessLevel
from gitagent.domain.errors import RoutingError
from gitagent.domain.models import AgentGuidance, AgentSpec, IssueReplyWorkflow
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness, ExecutionProfile
from gitagent.memory import MemorySearch, PersistentMemoryContext
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

_PROMPTS = get_prompt_library()

_ISSUES_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "minLength": 1},
        "issue_number": {"type": "integer", "minimum": 1},
        "mode": {"type": "string", "enum": ["task", "reply"]},
    },
    "required": ["task", "mode"],
    "additionalProperties": False,
}
_PULL_REQUESTS_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "minLength": 1},
        "pr_number": {"type": "integer", "minimum": 1},
        "workflow_run_id": {"type": "integer", "minimum": 1},
    },
    "required": ["task"],
    "additionalProperties": False,
}
_REPOSITORY_SCHEMA = {
    "type": "object",
    "properties": {"task": {"type": "string", "minLength": 1}},
    "required": ["task"],
    "additionalProperties": False,
}

_MAIN_SYSTEM = _PROMPTS.text("system.main")

_MAIN_SPEC = AgentSpec(
    name="main",
    role="Own the Session conversation and directly call the appropriate Domain Agent.",
    system_prompt=_MAIN_SYSTEM,
    output_schema=(),
    agent_depth=0,
    execution_profile=ExecutionProfile.unknown(),
)


class MainAgent:
    def __init__(
        self,
        harness: AgentHarness,
        reasoner: Reasoner,
        *,
        guidance_resolver: Callable[
            [str, str | None, str | None], AgentGuidance | None
        ],
    ) -> None:
        self.harness = harness
        self.reasoner = reasoner
        self.guidance_resolver = guidance_resolver
        harness.register(_MAIN_SPEC)

    @staticmethod
    def agent_schemas() -> dict[str, dict[str, Any]]:
        return {
            "issues": _ISSUES_SCHEMA,
            "pull_requests": _PULL_REQUESTS_SCHEMA,
            "repository": _REPOSITORY_SCHEMA,
        }

    def step(
        self,
        context: AgentContext,
    ) -> ModelResponse:
        if not context.messages:
            raise RoutingError("Main Agent message thread cannot be empty")
        tools = context.model_tools or self.provider_tools(
            session_id=context.session_id,
            repository=context.repository,
            goal=context.goal,
        )
        return context.reason(self.reasoner, tools=tools)

    @staticmethod
    def build_result(context: AgentContext) -> str:
        return context.final_message

    def validate_capability(
        self, context: AgentContext, capability_id: str
    ) -> None:
        capability = next(
            (
                item
                for item in self.harness.discover(context)
                if item.id == capability_id
            ),
            None,
        )
        if capability is None or capability.access != AccessLevel.READ:
            raise RoutingError("Main Agent may only call visible READ capabilities")

    def prepare_child(
        self,
        context: AgentContext,
        call: AgentCall,
        child: AgentContext,
    ) -> None:
        del context
        arguments = call.arguments
        goal = str(arguments["task"])
        entity_type: str | None = None
        entity_id: str | None = None
        if call.agent_id == "issues":
            entity_type = "issue"
            if arguments.get("issue_number") is not None:
                entity_id = str(arguments["issue_number"])
        elif call.agent_id == "pull_requests":
            if arguments.get("pr_number") is not None:
                entity_type = "pull_request"
                entity_id = str(arguments["pr_number"])
            elif arguments.get("workflow_run_id") is not None:
                entity_type = "workflow_run"
                entity_id = str(arguments["workflow_run_id"])
            else:
                entity_type = "pull_request"
        elif call.agent_id == "repository":
            entity_type = "repository"
        else:
            raise RoutingError(f"unsupported Domain Agent: {call.agent_id}")

        child.goal = goal
        child.entity_type = entity_type
        child.entity_id = entity_id
        child.guidance = self.guidance_resolver(goal, entity_type, entity_id)
        if call.agent_id == "issues" and arguments.get("mode") == "reply":
            child.issue_reply = IssueReplyWorkflow()

    def provider_tools(
        self, *, session_id: str, repository: str, goal: str
    ) -> list[dict[str, Any]]:
        probe = self.harness.context(
            "main", session_id, repository=repository, goal=goal
        )
        return [
            *self.harness.llm_tools(probe, read_only=True),
            self.harness.agent_tool(
                "issues",
                "Delegate one complete GitHub Issue task.",
                _ISSUES_SCHEMA,
            ),
            self.harness.agent_tool(
                "pull_requests",
                "Delegate one complete Pull Request or PR-related workflow task.",
                _PULL_REQUESTS_SCHEMA,
            ),
            self.harness.agent_tool(
                "repository",
                "Delegate one complete repository exploration, analysis, plan, history, or modification task.",
                _REPOSITORY_SCHEMA,
            ),
        ]

    def current_system(
        self,
        *,
        repository: str,
        memory_context: PersistentMemoryContext | None = None,
    ) -> str:
        memory_context = memory_context or PersistentMemoryContext()
        selected = MemorySearch.render(memory_context.selected_pages)
        persistent = (
            "\n\n## Persistent Memory index\n"
            + memory_context.index
            + (
                "\n\n## Selected Persistent Memory Pages\n" + selected
                if selected
                else ""
            )
        )
        return _MAIN_SYSTEM + "\n\nCurrent repository:\n" + json.dumps(
            {"repository": repository}, ensure_ascii=False, separators=(",", ":")
        ) + persistent

MAIN_AGENT_SCHEMAS = MainAgent.agent_schemas()
