"""Single conversational Main Agent for Session-scoped routing."""

from __future__ import annotations

import json
from typing import Any

from gitagent.domain.errors import RoutingError, ValidationError
from gitagent.domain.learning import (
    ReflectionChanges,
    ReflectionInput,
)
from gitagent.domain.models import (
    AgentSpec,
    MainDecision,
    Route,
    RoutingContext,
    to_plain,
)
from gitagent.harness.context import estimate_tokens
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.model import Reasoner, structured_request_payload
from gitagent.prompts import get_prompt_library

_DOMAIN_AGENTS = {"issues", "pull_requests", "repository"}
_PROMPTS = get_prompt_library()
_MAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "target_agent": {"type": "string"},
        "entity_type": {"type": "string"},
        "entity_id": {"type": "string"},
        "request": {"type": "string"},
        "message": {"type": "string"},
        "clarify": {"type": "boolean"},
        "requested_reply": {"type": "boolean"},
    },
    "required": ["target_agent", "request", "message", "clarify", "requested_reply"],
}

_REFLECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["user", "repository"]},
                    "path": {"type": "string", "pattern": "^items/[^/]+\\.md$"},
                    "type": {"type": "string", "enum": ["memory", "experience"]},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "text": {"type": "string", "maxLength": 8000},
                },
                "required": ["scope", "path", "type", "priority", "text"],
                "additionalProperties": False,
            },
        },
        "replace": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["user", "repository"]},
                    "path": {"type": "string", "pattern": "^items/[^/]+\\.md$"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "text": {"type": "string", "maxLength": 8000},
                },
                "required": ["scope", "path", "priority", "text"],
                "additionalProperties": False,
            },
        },
        "delete": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["user", "repository"]},
                    "path": {"type": "string", "pattern": "^items/[^/]+\\.md$"},
                },
                "required": ["scope", "path"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["add", "replace", "delete"],
    "additionalProperties": False,
}

_MAIN_SYSTEM = """You are GitAgent's Main Agent. One Session is one continuous Main Agent context.
Understand the user's current request from the Session summary, recent history, working state, and long-term Memory index.
Memory and Experience are non-authoritative context. The current request and current verifiable evidence always win.
Read a linked Memory item with native.read only when its index summary is relevant but insufficient; use the stated root and relative path.
Do not invent or manage tasks, runs, workflow lifecycles, approvals, or capability calls.
If repository work is needed, choose exactly one target_agent: issues, pull_requests, or repository.
Route every Pull Request request—including Review, CI, PR-scoped code work, approval, readiness, and merge—to pull_requests.
Route direct repository exploration, search, explanation, impact analysis, planning, history, and arbitrary repository modification to repository.
Route Issue-scoped code fixes to issues rather than repository.
If no child agent is needed, return an empty target_agent and answer directly in message.
Set clarify=true only when a safe interpretation is impossible; otherwise clarify=false.
When the user refers to a concrete Issue, Pull Request, or workflow run, return its entity_type and entity_id from the request/context.
Set requested_reply=true only when the user wants to compose, revise, or publish an Issue reply/comment.
READ actions may execute directly when allowed by runtime policy.
WRITE and DESTRUCTIVE actions require explicit user approval enforced by the runtime.
After approval, the same agent executes the exact approved capability call.
Never claim a mutation succeeded before observing a successful capability result.
Main Agent does not invoke capabilities or manage approvals; those are deterministic runtime responsibilities."""

_MAIN_SPEC = AgentSpec(
    name="main",
    role="Own Session conversation intent and choose the appropriate domain agent when repository work is needed.",
    system_prompt=_MAIN_SYSTEM,
    output_schema=(
        "target_agent",
        "entity_type",
        "entity_id",
        "request",
        "message",
        "clarify",
        "requested_reply",
    ),
    routes=frozenset({"conversation_orchestration"}),
)

_REFLECTION_SPEC = AgentSpec(
    name="main_reflection",
    role="Use the Main Agent identity to maintain durable Memory from temporary evidence.",
    system_prompt=_PROMPTS.text("system.main_reflection"),
    output_schema=("add", "replace", "delete"),
    routes=frozenset({"internal_reflection"}),
)


class MainAgent:
    def __init__(self, harness: AgentHarness, reasoner: Reasoner) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(_MAIN_SPEC)
        harness.register(_REFLECTION_SPEC)

    def decide(
        self,
        user_input: str,
        *,
        repository: str,
        context: RoutingContext,
    ) -> MainDecision:
        text = user_input.strip()
        if not text:
            raise RoutingError("request cannot be empty")
        return self.harness.run(
            "main",
            session_id=context.scope.session_id,
            operation=lambda agent_context: self._semantic_decision(
                agent_context, text, repository, context
            ),
            repository=repository,
            goal=text,
        )

    def _semantic_decision(
        self,
        agent_context: AgentContext,
        text: str,
        repository: str,
        context: RoutingContext,
    ) -> MainDecision:
        observations: list[dict[str, Any]] = []
        for _ in range(4):
            prompt = self._prompt(text, repository, context, observations=observations)
            self._ensure_budget(agent_context, prompt)
            raw = self.reasoner.complete_structured(
                system=agent_context.system_prompt,
                prompt=prompt,
                schema=_MAIN_SCHEMA,
                tool_name="route_session_turn",
                tools=self.harness.llm_tools(agent_context),
            )
            if raw.get("kind") != "capability":
                return self._validate(raw, text)
            observations.append(self._read_memory(agent_context, raw))
        raise RoutingError("Main Agent exceeded the bounded Memory read limit")

    def render_input_context(
        self, user_input: str, repository: str, context: RoutingContext
    ) -> str:
        """Serialize the exact Main Agent request counted by the shared context budget."""

        probe = self.harness.context(
            "main",
            context.scope.session_id,
            repository=repository,
            goal=user_input,
        )
        request = structured_request_payload(
            self.harness.spec("main").system_prompt,
            self._prompt(user_input, repository, context),
            schema=_MAIN_SCHEMA,
            tool_name="route_session_turn",
            tools=self.harness.llm_tools(probe),
        )
        return json.dumps(
            request, ensure_ascii=False, separators=(",", ":"), default=str
        )

    def reflect(self, context: ReflectionInput) -> ReflectionChanges:
        """Run the conversation owner's isolated, Memory-read-only learning invocation."""

        return self.harness.run(
            "main_reflection",
            session_id=context.scope.session_id,
            repository=context.repository_full_name,
            goal=f"Reflect on {context.trigger} evidence",
            operation=lambda agent_context: self._semantic_reflection(
                agent_context, context
            ),
        )

    def _semantic_reflection(
        self,
        agent_context: AgentContext,
        context: ReflectionInput,
    ) -> ReflectionChanges:
        observations: list[dict[str, Any]] = []
        for _ in range(4):
            payload = {
                "trigger": context.trigger,
                "scope": to_plain(context.scope),
                "repository": context.repository_full_name,
                "conversation": list(context.conversation_units),
                "learning_trace": to_plain(context.learning_trace),
                "memory_index": context.memory_index,
                "memory_reads": observations,
            }
            prompt = _PROMPTS.render(
                "agents.main_reflection",
                payload=json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"), default=str
                ),
            )
            self._ensure_budget(agent_context, prompt)
            raw = self.reasoner.complete_structured(
                system=agent_context.system_prompt,
                prompt=prompt,
                schema=_REFLECTION_SCHEMA,
                tool_name="propose_long_term_learning",
                tools=self.harness.llm_tools(agent_context),
            )
            if raw.get("kind") != "capability":
                return self._validate_reflection(raw)
            observations.append(self._read_memory(agent_context, raw))
        raise ValidationError(
            "MainAgent reflection exceeded the bounded Memory read limit"
        )

    @staticmethod
    def _validate_reflection(raw: dict[str, Any]) -> ReflectionChanges:
        if not isinstance(raw, dict) or any(
            not isinstance(raw.get(key), list) for key in ("add", "replace", "delete")
        ):
            raise ValidationError(
                "MainAgent reflection output must contain add, replace, and delete lists"
            )
        return ReflectionChanges(
            tuple(
                {str(key): str(value).strip() for key, value in item.items()}
                for item in raw["add"]
            ),
            tuple(
                {str(key): str(value).strip() for key, value in item.items()}
                for item in raw["replace"]
            ),
            tuple(
                {str(key): str(value).strip() for key, value in item.items()}
                for item in raw["delete"]
            ),
        )

    def _prompt(
        self,
        text: str,
        repository: str,
        context: RoutingContext,
        *,
        observations: list[dict[str, Any]] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "user_input": text,
            "repository": repository,
            "routes": self._routes(),
            "session": {
                "summary": context.summary,
                "recent_history": list(context.history_units),
                "working_state": context.working_state,
                "memory_index": context.memory_index,
                "memory_reads": list(observations or ()),
            },
        }
        return (
            "Decide whether to answer directly or choose one domain agent. "
            "Only native.read of an indexed Memory item is allowed before the final routing decision. "
            "Do not select repository capabilities or create lifecycle objects.\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _read_memory(
        self, context: AgentContext, raw: dict[str, Any]
    ) -> dict[str, Any]:
        capability_id = self.harness.resolve_llm_name(
            str(raw.get("capability_id") or ""), context
        )
        arguments = dict(raw.get("arguments") or {})
        if capability_id != "native.read" or arguments.get("root") not in {
            "user_memory",
            "repository_memory",
        }:
            raise ValidationError(
                "Main Agent may only read indexed files from authorized Memory roots"
            )
        result = context.invoke(capability_id, **arguments)
        call = context.last_capability_call
        if call is None or call.result.status != "success":
            return {
                "capability_id": capability_id,
                "arguments": arguments,
                "error": to_plain(call.result.error)
                if call is not None
                else "read failed",
            }
        return {"capability_id": capability_id, "arguments": arguments, "data": result}

    @staticmethod
    def _ensure_budget(context: AgentContext, prompt: str) -> None:
        if estimate_tokens(prompt) + context.prompt_overhead() > context.context_budget:
            raise ValidationError(
                "MainAgent Memory reads exceed the unified context budget"
            )

    def _validate(self, raw: dict[str, Any], text: str) -> MainDecision:
        if not isinstance(raw, dict):
            raise ValidationError("Main Agent output must be an object")
        target = str(raw.get("target_agent") or "").strip()
        if target and target not in _DOMAIN_AGENTS:
            raise ValidationError("Main Agent selected an unknown domain agent")
        entity_type = str(raw.get("entity_type") or "").strip() or None
        entity_id = str(raw.get("entity_id") or "").strip() or None
        message = str(raw.get("message") or "").strip()
        request = str(raw.get("request") or text).strip() or text
        clarify = bool(raw.get("clarify", False))
        if (
            target == "pull_requests"
            and entity_type == "pull_request"
            and entity_id is not None
            and not entity_id.isdigit()
        ):
            return MainDecision(
                request=request,
                message="当前一次只能处理一个 Pull Request；多 PR 对比尚未实现。请改为指定一个 PR 编号。",
                clarify=True,
                requested_reply=bool(raw.get("requested_reply", False)),
            )
        if not target and not message:
            message = (
                "请再具体说明你希望处理的仓库问题。"
                if clarify
                else "我需要更多上下文才能回答。"
            )
        return MainDecision(
            target_agent=target or None,
            entity_type=entity_type,
            entity_id=entity_id,
            request=request,
            message=message,
            clarify=clarify,
            requested_reply=bool(raw.get("requested_reply", False)),
        )

    def _routes(self) -> list[dict[str, Any]]:
        names = {
            Route.ISSUE: "issues",
            Route.PULL_REQUEST: "pull_requests",
            Route.REPOSITORY: "repository",
        }
        catalog: list[dict[str, Any]] = []
        for route, target in names.items():
            specs = self.harness.specs_for(route)
            if specs:
                catalog.append(
                    {
                        "target_agent": target,
                        "description": " / ".join(spec.role for spec in specs),
                        "examples": [
                            example
                            for spec in specs
                            for example in spec.routing_examples
                        ],
                    }
                )
        return catalog
