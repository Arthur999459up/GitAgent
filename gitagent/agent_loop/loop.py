"""Bounded Agent Loop state-transition engine."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop.actions import AgentLoopAgent
from gitagent.domain.errors import (
    ContextWindowExceeded,
    LLMProviderError,
    StructuredOutputError,
    WorkflowError,
)
from gitagent.domain.models import WorkflowTurnDecision
from gitagent.harness.action_dispatcher import HarnessActionDispatcher


class AgentLoop:
    def __init__(
        self,
        harness: Any,
        *,
        max_steps: int = 20,
        max_structured_retries: int = 1,
        max_provider_retries: int = 1,
    ) -> None:
        self.harness = harness
        self.max_steps = max_steps
        self.max_structured_retries = max_structured_retries
        self.max_provider_retries = max_provider_retries
        self.dispatcher = HarnessActionDispatcher(harness)

    def start(self, context: Any, agent: AgentLoopAgent) -> Any:
        if context.finished or context.error:
            return context
        if context.waiting:
            raise WorkflowError("agent context is waiting for user input")
        context.start_message_thread()
        self.dispatcher.emit(context, "started", context.goal)
        self._advance(context, agent)
        return context

    def resume(
        self, context: Any, agent: AgentLoopAgent, decision: WorkflowTurnDecision
    ) -> Any:
        if context.finished or context.error:
            raise WorkflowError("agent context is not waiting for user input")
        context.start_message_thread()
        try:
            self.dispatcher.apply_user_decision(context, decision)
        except Exception as exc:  # noqa: BLE001 - loop boundary records programming failures
            self._fail(context, f"user decision failed: {exc}")
            return context
        if not context.finished and not context.error and not context.waiting:
            self._advance(context, agent)
        elif context.finished:
            self.dispatcher.emit(
                context, "completed", context.final_message or "completed"
            )
        return context

    def restore_pending(self, context: Any, *, summary: str, calls: list[Any]) -> None:
        self.dispatcher.restore_pending(context, summary=summary, calls=calls)

    def _advance(self, context: Any, agent: AgentLoopAgent) -> None:
        structured_failures = 0
        provider_failures = 0
        while not context.finished and not context.error and not context.waiting:
            if context.steps >= context.max_steps:
                self._fail(context, f"达到步数上限（{context.max_steps}）")
                return
            context.steps += 1
            try:
                action = agent.decide(context)
                structured_failures = 0
                provider_failures = 0
                self.dispatcher.emit(
                    context, "progress", action.summary or action.kind.value
                )
                should_continue = self.dispatcher.handle(context, agent, action)
            except StructuredOutputError as exc:
                structured_failures += 1
                self.dispatcher.observe(
                    context,
                    "structured_output_error",
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        **exc.details,
                    },
                )
                self.dispatcher.emit(
                    context, "progress", "structured model output rejected"
                )
                if structured_failures > self.max_structured_retries:
                    self._fail(
                        context,
                        "模型连续返回无效的结构化动作；已在一次纠正重试后终止。"
                        f" 最后错误：{exc}",
                    )
                    return
                context.structured_retry_instruction = (
                    "Your previous response violated the structured-response contract. "
                    "Correct it now: choose exactly one provided capability or one allowed "
                    "structured action, and satisfy its schema exactly."
                )
                continue
            except LLMProviderError as exc:
                provider_failures += 1
                self.dispatcher.observe(
                    context,
                    "provider_error",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
                if provider_failures > self.max_provider_retries:
                    self._fail(
                        context,
                        f"模型提供方连续失败；已在一次有限重试后终止。 最后错误：{exc}",
                    )
                    return
                continue
            except ContextWindowExceeded as exc:
                self.dispatcher.observe(
                    context,
                    "context_window_error",
                    {
                        "error_type": type(exc).__name__,
                        "context_window_tokens": exc.context_window_tokens,
                        "input_tokens": exc.input_tokens,
                        "requested_output_tokens": exc.requested_output_tokens,
                        "remaining_tokens": exc.remaining_tokens,
                        "breakdown": exc.breakdown,
                    },
                )
                self._fail(context, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - loop boundary records programming failures
                self._fail(context, f"agent step failed: {exc}")
                return
            if context.finished:
                self.dispatcher.emit(
                    context, "completed", context.final_message or "completed"
                )
                return
            if context.waiting or not should_continue:
                message = context.question or str(
                    getattr(context.pending, "summary", "") or "waiting for user input"
                )
                self.dispatcher.emit(context, "waiting", message)
                return

    def _fail(self, context: Any, error: str) -> None:
        context.complete_control_call({"status": "failed", "reason": str(error)})
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
            return (
                str(payload.get("instruction") or "")
                if isinstance(payload, dict)
                else ""
            )
        if observation["kind"] == "user":
            return None
    return None
