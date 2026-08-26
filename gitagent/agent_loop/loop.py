"""Bounded Agent Loop state-transition engine."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop.actions import AgentAction, AgentLoopAgent
from gitagent.domain.errors import WorkflowError
from gitagent.domain.models import WorkflowTurnDecision
from gitagent.harness.action_dispatcher import HarnessActionDispatcher


class AgentLoop:
    def __init__(self, harness: Any, *, max_steps: int = 20) -> None:
        self.harness = harness
        self.max_steps = max_steps
        self.dispatcher = HarnessActionDispatcher(harness)

    def start(self, context: Any, agent: AgentLoopAgent) -> Any:
        if context.finished or context.error:
            return context
        if context.waiting:
            raise WorkflowError("agent context is waiting for user input")
        self.dispatcher.emit(context, "started", context.goal)
        self._advance(context, agent)
        return context

    def resume(self, context: Any, agent: AgentLoopAgent, decision: WorkflowTurnDecision) -> Any:
        if context.finished or context.error:
            raise WorkflowError("agent context is not waiting for user input")
        try:
            self.dispatcher.apply_user_decision(context, decision)
        except Exception as exc:
            self._fail(context, f"user decision failed: {exc}")
            return context
        if not context.finished and not context.error and not context.waiting:
            self._advance(context, agent)
        elif context.finished:
            self.dispatcher.emit(context, "completed", context.final_message or "completed")
        return context

    def restore_pending(self, context: Any, *, summary: str, calls: list[Any]) -> None:
        self.dispatcher.restore_pending(context, summary=summary, calls=calls)

    def _advance(self, context: Any, agent: AgentLoopAgent) -> None:
        while not context.finished and not context.error and not context.waiting:
            if context.steps >= context.max_steps:
                self._fail(context, f"达到步数上限（{context.max_steps}）")
                return
            context.steps += 1
            try:
                action = agent.decide(context)
                self.dispatcher.emit(context, "progress", action.summary or action.kind.value)
                should_continue = self.dispatcher.handle(context, agent, action)
            except Exception as exc:
                self._fail(context, f"agent step failed: {exc}")
                return
            if context.finished:
                self.dispatcher.emit(context, "completed", context.final_message or "completed")
                return
            if context.waiting or not should_continue:
                message = context.question or str(getattr(context.pending, "summary", "") or "waiting for user input")
                self.dispatcher.emit(context, "waiting", message)
                return

    def _fail(self, context: Any, error: str) -> None:
        context.pending = None
        context.question = ""
        context.error = str(error)
        context.finished = True
        self.dispatcher.emit(context, "failed", str(error))


def rejection_feedback(context: Any) -> str | None:
    """Return the instruction from the most recent rejected proposal."""

    for observation in reversed(context.observations):
        if observation["kind"] == "rejection":
            payload = observation.get("payload") or {}
            return str(payload.get("instruction") or "") if isinstance(payload, dict) else ""
        if observation["kind"] == "user":
            return None
    return None
