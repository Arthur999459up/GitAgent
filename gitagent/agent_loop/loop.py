"""Bounded Agent Loop for native Text, Capability calls, and Agent calls."""

from __future__ import annotations

import json
from typing import Any

from gitagent.agent_loop.models import (
    AgentCall,
    AgentLoopAgent,
    AgentResult,
    CapabilityCall,
    WaitForUser,
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
        child_agents: dict[str, AgentLoopAgent] | None = None,
    ) -> None:
        self.harness = harness
        self.max_steps = max_steps
        self.max_structured_retries = max_structured_retries
        self.max_provider_retries = max_provider_retries
        self.dispatcher = StructuredCallDispatcher(harness)
        self.child_agents = dict(child_agents or {})

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
        child = context.active_child
        if child is not None:
            child_agent = self._child_agent(child.agent)
            self.resume(child, child_agent, decision)
            if child.waiting:
                self.dispatcher.emit(context, "waiting", child.waiting_question)
                return context
            call = self._active_child_call(context, child)
            self._complete_child(context, agent, call, child)
            if not context.finished and not context.error and not context.waiting:
                self._advance(context, agent)
            return context
        if context.pending is None:
            raise WorkflowError("agent context is not waiting for approval")
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

    def resume_user_input(
        self,
        context: Any,
        agent: AgentLoopAgent,
        user_input: str,
    ) -> Any:
        """Resume the deepest Runtime-managed child that requested user input."""

        if context.finished or context.error:
            raise WorkflowError("agent context is not waiting for user input")
        child = context.active_child
        if child is not None:
            child_agent = self._child_agent(child.agent)
            self.resume_user_input(child, child_agent, user_input)
            if child.waiting:
                self.dispatcher.emit(context, "waiting", child.waiting_question)
                return context
            call = self._active_child_call(context, child)
            self._complete_child(context, agent, call, child)
            if not context.finished and not context.error and not context.waiting:
                self._advance(context, agent)
            return context

        request = context.user_input_request
        if request is None:
            raise WorkflowError("agent context is not waiting for user input")
        if request.call_id:
            context.append_tool_result(
                {"status": "answered", "answer": user_input},
                call_id=request.call_id,
            )
        else:
            context.append_message({"role": "user", "content": user_input})
        self.dispatcher.observe(context, "user_input", {"answer": user_input})
        context.user_input_request = None
        self._advance(context, agent)
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
                if isinstance(response, WaitForUser):
                    if response.call_id:
                        open_call = context.open_tool_call()
                        function = (open_call or {}).get("function") or {}
                        if (
                            open_call is None
                            or str(open_call.get("id") or "") != response.call_id
                            or str(function.get("name") or "")
                            != "runtime__wait_for_user"
                        ):
                            raise ValidationError(
                                "waiting result does not match the open Runtime call"
                            )
                    else:
                        last = context.messages[-1] if context.messages else {}
                        if not (
                            last.get("role") == "assistant"
                            and last.get("content") == response.question
                        ):
                            context.append_message(
                                {"role": "assistant", "content": response.question}
                            )
                    context.user_input_request = response
                    self.dispatcher.emit(context, "waiting", response.question)
                    return
                self.dispatcher.emit(
                    context,
                    "progress",
                    response.text
                    or (response.call.name if response.call is not None else "model response"),
                )
                if response.call is None:
                    context.final_message = response.text
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
                    validate = getattr(agent, "validate_capability", None)
                    if callable(validate):
                        validate(context, resolved.capability_id)
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
                open_call = context.open_tool_call()
                if open_call is not None:
                    context.append_tool_result(
                        {
                            "status": "failed",
                            "error": "structured_output_error",
                            "message": str(exc),
                        },
                        call_id=str(open_call.get("id") or ""),
                    )
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
                message = context.waiting_question or str(
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
        self._validate_agent_call(context, call)
        prepare = getattr(agent, "prepare_child", None)
        if not callable(prepare):
            raise ValidationError(f"{context.agent} cannot call agent__{call.agent_id}")
        child_agent = self._child_agent(call.agent_id)
        child = self.harness.context(
            call.agent_id,
            context.session_id,
            repository=context.repository,
            goal=str(call.arguments.get("task") or ""),
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            guidance=context.guidance,
        )
        child.origin_turn_seq = context.origin_turn_seq
        prepare(context, call, child)
        child.parent_call_id = call.call_id
        child.parent_call_name = f"agent__{call.agent_id}"
        child.parent_call_arguments = dict(call.arguments)
        context.active_child = child
        self.start(child, child_agent)
        if child.waiting:
            return False
        self._complete_child(context, agent, call, child)
        return not context.waiting

    def _complete_child(
        self,
        context: Any,
        agent: AgentLoopAgent,
        call: AgentCall,
        child: Any,
    ) -> None:
        status = "failed" if child.error else "completed"
        content = str(child.error or child.final_message or "").strip()
        result = AgentResult(
            call.call_id,
            call.agent_id,
            status,
            content,
            (
                {"type": "AgentRuntimeError", "message": str(child.error)}
                if child.error
                else None
            ),
        )
        if not isinstance(result, AgentResult):
            raise ValidationError("child Agent runtime returned an invalid AgentResult")
        if result.call_id != call.call_id or result.agent_id != call.agent_id:
            raise ValidationError("child Agent result correlation does not match its call")
        if result.status not in {"completed", "failed"} or not result.content.strip():
            raise ValidationError("child Agent returned an invalid terminal result")
        context.active_child = None
        context.last_completed_child = child
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
            transition(context, call, result, child, self.dispatcher)

    def validate_context_tree(self, context: Any) -> None:
        """Validate every persisted active-child correlation through one path."""

        child = context.active_child
        if child is None:
            return
        self._active_child_call(context, child)
        if not child.waiting or child.finished or child.error is not None:
            raise ValidationError("stored active child is not paused")
        self.validate_context_tree(child)

    @staticmethod
    def waiting_context(context: Any) -> Any:
        """Return the deepest paused context in one Runtime-managed tree."""

        current = context
        while current.active_child is not None and current.active_child.waiting:
            current = current.active_child
        return current

    @staticmethod
    def _active_child_call(context: Any, child: Any) -> AgentCall:
        call = AgentCall(
            child.parent_call_id,
            child.agent,
            dict(child.parent_call_arguments),
        )
        if child.parent_call_name != f"agent__{child.agent}":
            raise ValidationError("active child correlation is invalid")
        AgentLoop._validate_agent_call(context, call)
        return call

    @staticmethod
    def _validate_agent_call(context: Any, call: AgentCall) -> None:
        open_call = context.open_tool_call()
        function = (open_call or {}).get("function") or {}
        raw_arguments = function.get("arguments") if isinstance(function, dict) else None
        try:
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else dict(raw_arguments or {})
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("active child parent call arguments are invalid") from exc
        expected_name = f"agent__{call.agent_id}"
        if (
            open_call is None
            or str(open_call.get("id") or "") != call.call_id
            or str(function.get("name") or "") != expected_name
            or arguments != call.arguments
        ):
            raise ValidationError(
                "structured Agent call does not match the open provider call"
            )

    def _child_agent(self, agent_id: str) -> AgentLoopAgent:
        child = self.child_agents.get(agent_id)
        if child is None:
            raise ValidationError(f"no Runtime child Agent is registered for {agent_id}")
        return child

    def _fail(self, context: Any, error: str | Exception) -> None:
        open_call = context.open_tool_call()
        if open_call is not None:
            context.append_tool_result(
                {"status": "failed", "reason": str(error)},
                call_id=str(open_call.get("id") or ""),
            )
        context.pending = None
        context.user_input_request = None
        context.active_child = None
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
