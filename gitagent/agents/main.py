"""Single conversational Main Agent for Session-scoped routing."""

from __future__ import annotations

import json
from typing import Any

from gitagent.domain.errors import RoutingError, ValidationError
from gitagent.domain.models import AgentSpec, MainDecision, Route, RoutingContext, to_plain
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.model import Reasoner, structured_request_payload

_DOMAIN_AGENTS = {"issues", "pull_requests", "repository"}
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

_MAIN_SYSTEM = """You are GitAgent's Main Agent. One Session is one continuous Main Agent context.
Understand the user's current request from the Session summary, recent history, working state, and explicit memories.
Do not invent or manage tasks, runs, workflow lifecycles, approvals, or tool calls.
If repository work is needed, choose exactly one target_agent: issues, pull_requests, or repository.
Route every Pull Request request—including Review, CI, PR-scoped code work, approval, readiness, and merge—to pull_requests.
Route direct repository exploration, search, explanation, impact analysis, planning, history, and arbitrary repository modification to repository.
Route Issue-scoped code fixes to issues rather than repository.
If no child agent is needed, return an empty target_agent and answer directly in message.
Set clarify=true only when a safe interpretation is impossible; otherwise clarify=false.
When the user refers to a concrete Issue, Pull Request, or workflow run, return its entity_type and entity_id from the request/context.
Set requested_reply=true only when the user wants to compose, revise, or publish an Issue reply/comment.
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
        prompt = self._prompt(text, repository, context)
        raw = self.reasoner.complete_structured(
            system=agent_context.system_prompt,
            prompt=prompt,
            schema=_MAIN_SCHEMA,
            tool_name="route_session_turn",
        )
        return self._validate(raw, text)

    def render_input_context(self, user_input: str, repository: str, context: RoutingContext) -> str:
        """Serialize the exact Main Agent request counted by the shared context budget."""

        request = structured_request_payload(
            self.harness.spec("main").system_prompt,
            self._prompt(user_input, repository, context),
            schema=_MAIN_SCHEMA,
            tool_name="route_session_turn",
        )
        return json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str)

    def _prompt(self, text: str, repository: str, context: RoutingContext) -> str:
        payload: dict[str, Any] = {
            "user_input": text,
            "repository": repository,
            "capabilities": self._capabilities(),
            "session": {
                "summary": context.summary,
                "recent_history": list(context.history_units),
                "working_state": context.working_state,
                "user_memory": [to_plain(item) for item in context.user_memories],
                "repository_memory": [to_plain(item) for item in context.repository_memories],
            },
        }
        return (
            "Decide whether to answer directly or choose one domain agent. "
            "Do not select tools or create lifecycle objects.\n"
            + json.dumps(payload, ensure_ascii=False)
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
            message = "请再具体说明你希望处理的仓库问题。" if clarify else "我需要更多上下文才能回答。"
        return MainDecision(
            target_agent=target or None,
            entity_type=entity_type,
            entity_id=entity_id,
            request=request,
            message=message,
            clarify=clarify,
            requested_reply=bool(raw.get("requested_reply", False)),
        )

    def _capabilities(self) -> list[dict[str, Any]]:
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
                        "examples": [example for spec in specs for example in spec.routing_examples],
                    }
                )
        return catalog
