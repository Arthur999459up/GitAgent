"""Bounded Agent Loop for native Text, Capability calls, and Agent calls."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop.models import (
    AgentCall,
    AgentLoopAgent,
    AgentResult,
    CapabilityCall,
)
from gitagent.domain.errors import (
    ContextWindowExceeded,
    LLMProviderError,
    StructuredOutputError,
    ValidationError,
    WorkflowError,
)
from gitagent.domain.models import WorkflowTurnDecision
from gitagent.harness.structured_call_dispatcher import StructuredCallDispatcher


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
        self.dispatcher = StructuredCallDispatcher(harness)

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
        self,
        context: Any,
        agent: AgentLoopAgent,
        decision: WorkflowTurnDecision,
    ) -> Any:
        if context.finished or context.error:
            raise WorkflowError("agent context is not waiting for user input")
        context.start_message_thread()
        try:
            self.dispatcher.apply_user_decision(context, decision)
        except Exception as exc:  # noqa: BLE001 - loop boundary records failures
            self._fail(context, f"user decision failed: {exc}")
            return context
        if not context.finished and not context.error and not context.waiting:
            self._advance(context, agent)
        elif context.finished:
            self.dispatcher.emit(
                context, "completed", context.final_message or "completed"
            )
        return context

    def restore_pending(
        self,
        context: Any,
        *,
        approval_id: str,
        summary: str,
        calls: list[Any],
        provider_call_id: str | None = None,
    ) -> None:
        self.dispatcher.restore_pending(
            context,
            approval_id=approval_id,
            summary=summary,
            calls=calls,
            provider_call_id=provider_call_id,
        )

    def _advance(self, context: Any, agent: AgentLoopAgent) -> None:
        structured_failures = 0
        provider_failures = 0
        while not context.finished and not context.error and not context.waiting:
            if context.steps >= context.max_steps:
                self._fail(context, f"达到步数上限（{context.max_steps}）")
                return
            context.steps += 1
            try:
                response = agent.step(context)
                structured_failures = 0
                provider_failures = 0
                self.dispatcher.emit(
                    context,
                    "progress",
                    response.text
                    or (response.call.name if response.call is not None else "model response"),
                )
                if response.call is None:
                    context.final_message = response.text
                    if context.question:
                        self.dispatcher.emit(context, "waiting", context.question)
                        return
                    context.result = agent.build_result(context)
                    context.finished = True
                    self.dispatcher.emit(context, "completed", response.text)
                    return

                schemas = getattr(agent, "agent_schemas", dict)()
                resolved = self.harness.resolve_model_call(
                    response.call,
                    context,
                    agent_schemas=schemas,
                )
                if isinstance(resolved, CapabilityCall):
                    should_continue = self.dispatcher.handle_capability(
                        context,
                        resolved,
                        summary=response.text,
                    )
                elif isinstance(resolved, AgentCall):
                    should_continue = self._handle_agent_call(
                        context,
                        agent,
                        resolved,
                    )
                else:  # pragma: no cover - resolver has a closed return type
                    raise ValidationError("unsupported structured call")
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
                if structured_failures > self.max_structured_retries:
                    self._fail(
                        context,
                        "模型连续返回无效响应；已在一次有限重试后终止。"
                        f" 最后错误：{exc}",
                    )
                    return
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
            except Exception as exc:  # noqa: BLE001 - loop boundary records failures
                self._fail(context, f"agent step failed: {exc}")
                return

            if context.waiting or not should_continue:
                message = context.question or str(
                    getattr(context.pending, "summary", "")
                    or "waiting for user input"
                )
                self.dispatcher.emit(context, "waiting", message)
                return

    def _handle_agent_call(
        self,
        context: Any,
        agent: AgentLoopAgent,
        call: AgentCall,
    ) -> bool:
        open_call = context.open_tool_call()
        if (
            open_call is None
            or str(open_call.get("id") or "") != call.call_id
            or str((open_call.get("function") or {}).get("name") or "")
            != f"agent__{call.agent_id}"
        ):
            raise ValidationError(
                "structured Agent call does not match the open provider call"
            )
        invoke = getattr(agent, "invoke_child", None)
        if not callable(invoke):
            raise ValidationError(f"{context.agent} cannot call agent__{call.agent_id}")
        result = invoke(context, call)
        if not isinstance(result, AgentResult):
            raise ValidationError("child Agent runtime returned an invalid AgentResult")
        if result.call_id != call.call_id or result.agent_id != call.agent_id:
            raise ValidationError("child Agent result correlation does not match its call")
        if result.status not in {"completed", "failed"} or not result.content.strip():
            raise ValidationError("child Agent returned an invalid terminal result")
        context.append_tool_result(
            {
                "status": result.status,
                "agent": result.agent_id,
                "content": result.content,
                **({"error": result.error} if result.error is not None else {}),
            },
            call_id=call.call_id,
        )
        self.dispatcher.observe(
            context,
            "agent_result",
            {
                "agent": result.agent_id,
                "call_id": result.call_id,
                "status": result.status,
            },
        )
        transition = getattr(agent, "after_agent_result", None)
        if callable(transition):
            transition(context, call, result, self.dispatcher)
        return not context.waiting

    def _fail(self, context: Any, error: str | Exception) -> None:
        open_call = context.open_tool_call()
        if open_call is not None:
            context.append_tool_result(
                {"status": "failed", "reason": str(error)},
                call_id=str(open_call.get("id") or ""),
            )
        context.pending = None
        context.question = ""
        context.error = str(error)
        context.finished = True
        self.dispatcher.emit(context, "failed", str(error))


def rejection_feedback(context: Any) -> str | None:
    """Return the explicit instruction from the most recent rejected proposal."""

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
