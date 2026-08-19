"""Single conversational Main Agent for Session-scoped routing."""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import RoutingError, ValidationError
from ..core.models import AgentSpec, MainDecision, Route, RoutingContext, to_plain
from ..reasoning import Reasoner
from ..runtime import AgentContext, AgentHarness

_DOMAIN_AGENTS = {"issues", "pull_requests", "ci_diagnosis", "repo_qa", "code_change"}
_MAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "target_agent": {"type": "string"},
        "entity_type": {"type": "string"},
        "entity_id": {"type": "string"},
        "request": {"type": "string"},
        "message": {"type": "string"},
        "clarify": {"type": "boolean"},
        "requested_fix": {"type": "boolean"},
        "requested_reply": {"type": "boolean"},
    },
    "required": ["target_agent", "request", "message", "clarify", "requested_fix", "requested_reply"],
}

_MAIN_SYSTEM = """You are GitAgent's Main Agent. One Session is one continuous Main Agent context.
Understand the user's current request from the Session summary, recent history, working state, and explicit memories.
Do not invent or manage tasks, runs, workflow lifecycles, approvals, or tool calls.
If repository work is needed, choose exactly one target_agent: issues, pull_requests, ci_diagnosis, repo_qa, or code_change.
If no child agent is needed, return an empty target_agent and answer directly in message.
Set clarify=true only when a safe interpretation is impossible; otherwise clarify=false.
When the user refers to a concrete Issue, Pull Request, or workflow run, return its entity_type and entity_id from the request/context.
Set requested_reply=true only when the user wants to compose, revise, or publish an Issue reply/comment.
Set requested_fix=true only when the user explicitly wants a code change rather than analysis alone.
Approval and mutation safety are deterministic runtime responsibilities; never claim write authority."""

_MAIN_SPEC = AgentSpec(
    name="main",
    role="Own Session conversation intent and choose the appropriate domain agent when repository work is needed.",
    system_prompt=_MAIN_SYSTEM,
    allowed_tools=frozenset(),
    output_schema=(
        "target_agent",
        "entity_type",
        "entity_id",
        "request",
        "message",
        "clarify",
        "requested_fix",
        "requested_reply",
    ),
    capabilities=frozenset({"conversation_orchestration"}),
)


class MainAgent:
    def __init__(self, harness: AgentHarness, reasoner: Reasoner) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(_MAIN_SPEC)

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
            operation=lambda agent_context: self._semantic_decision(agent_context, text, repository, context),
        )

    def _semantic_decision(
        self,
        agent_context: AgentContext,
        text: str,
        repository: str,
        context: RoutingContext,
    ) -> MainDecision:
        payload: dict[str, Any] = {
            "user_input": text,
            "repository": repository,
            "capabilities": self._capabilities(),
            "session": {
                "summary": context.summary,
                "recent_history": list(context.history_units[-8:]),
                "working_state": context.working_state,
                "user_memory": [to_plain(item) for item in context.user_memories],
                "repository_memory": [to_plain(item) for item in context.repository_memories],
            },
        }
        raw = self.reasoner.complete_structured(
            system=agent_context.system_prompt,
            prompt=(
                "Decide whether to answer directly or choose one domain agent. "
                "Do not select tools or create lifecycle objects.\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
            schema=_MAIN_SCHEMA,
            tool_name="route_session_turn",
        )
        return self._validate(raw, text)

    def _validate(self, raw: dict[str, Any], text: str) -> MainDecision:
        if not isinstance(raw, dict):
            raise ValidationError("Main Agent output must be an object")
        target = str(raw.get("target_agent") or "").strip()
        if target and target not in _DOMAIN_AGENTS:
            raise ValidationError("Main Agent selected an unknown domain agent")
        message = str(raw.get("message") or "").strip()
        request = str(raw.get("request") or text).strip() or text
        clarify = bool(raw.get("clarify", False))
        if not target and not message:
            message = "请再具体说明你希望处理的仓库问题。" if clarify else "我需要更多上下文才能回答。"
        return MainDecision(
            target_agent=target or None,
            entity_type=str(raw.get("entity_type") or "").strip() or None,
            entity_id=str(raw.get("entity_id") or "").strip() or None,
            request=request,
            message=message,
            clarify=clarify,
            requested_fix=bool(raw.get("requested_fix", False)),
            requested_reply=bool(raw.get("requested_reply", False)),
        )

    def _capabilities(self) -> list[dict[str, Any]]:
        names = {
            Route.ISSUE: "issues",
            Route.PULL_REQUEST: "pull_requests",
            Route.CI_DIAGNOSIS: "ci_diagnosis",
            Route.REPO_QA: "repo_qa",
            Route.CODE_CHANGE: "code_change",
        }
        catalog: list[dict[str, Any]] = []
        for route, target in names.items():
            specs = self.harness.specs_for(route)
            if specs:
                catalog.append(
                    {
                        "target_agent": target,
                        "description": " / ".join(spec.role for spec in specs),
                        "examples": [example for spec in specs for example in spec.routing_examples],
                    }
                )
        return catalog
