"""Bounded Agent Loop with Harness-owned multi-call execution."""

from __future__ import annotations

import json
from typing import Any

from gitagent.agent_loop.models import (
    AgentCall,
    AgentLoopAgent,
    AgentResult,
    CapabilityCall,
    StructuredCall,
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
        child_agents: dict[str, AgentLoopAgent] | None = None,
    ) -> None:
        self.harness = harness
        self.dispatcher = StructuredCallDispatcher(harness)
        self.child_agents = dict(child_agents or {})

    def start(self, context: Any, agent: AgentLoopAgent) -> Any:
        with self.harness.coordinator.cancellation_scope(context):
            return self._start(context, agent)

    def _start(self, context: Any, agent: AgentLoopAgent) -> Any:
        if context.finished or context.error:
            return context
        if context.waiting:
            raise WorkflowError("agent context is waiting for user input")
        if self._stop_if_cancelled(context):
            return context
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
        with self.harness.coordinator.cancellation_scope(context):
            return self._resume(context, agent, decision)

    def _resume(
        self,
        context: Any,
        agent: AgentLoopAgent,
        decision: WorkflowTurnDecision,
    ) -> Any:
        if context.finished or context.error:
            raise WorkflowError("agent context is not waiting for user input")
        if self._stop_if_cancelled(context):
            return context
        child = context.first_waiting_child()
        if child is not None:
            self.resume(child, self._child_agent(child.agent), decision)
            if child.waiting:
                self.dispatcher.emit(context, "waiting", child.waiting_question)
                return context
            if not self._continue_open_batch(context, agent):
                self.dispatcher.emit(context, "waiting", context.waiting_question)
                return context
            if not context.finished and not context.error:
                self._advance(context, agent)
            return context
        if context.pending is None:
            raise WorkflowError("agent context is not waiting for approval")
        context.start_message_thread()
        try:
            self.dispatcher.apply_user_decision(context, decision)
        except Exception as exc:  # noqa: BLE001 - loop boundary records failures
            if self._stop_if_cancelled(context):
                return context
            self._fail(
                context,
                f"user decision failed: {exc}",
                error_type=type(exc).__name__,
            )
            return context
        if not context.waiting and not self._continue_open_batch(context, agent):
            self.dispatcher.emit(context, "waiting", context.waiting_question)
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
        with self.harness.coordinator.cancellation_scope(context):
            return self._resume_user_input(context, agent, user_input)

    def _resume_user_input(
        self,
        context: Any,
        agent: AgentLoopAgent,
        user_input: str,
    ) -> Any:
        """Resume the earliest provider-ordered child that requested user input."""

        if context.finished or context.error:
            raise WorkflowError("agent context is not waiting for user input")
        if self._stop_if_cancelled(context):
            return context
        child = context.first_waiting_child()
        if child is not None:
            self.resume_user_input(child, self._child_agent(child.agent), user_input)
            if child.waiting:
                self.dispatcher.emit(context, "waiting", child.waiting_question)
                return context
            if not self._continue_open_batch(context, agent):
                self.dispatcher.emit(context, "waiting", context.waiting_question)
                return context
            if not context.finished and not context.error:
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
        if not self._continue_open_batch(context, agent):
            self.dispatcher.emit(context, "waiting", context.waiting_question)
            return context
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

    def cancel(self, context: Any) -> bool:
        """Cancel the active batch rooted at this Context, including nested children."""

        return self.harness.coordinator.cancel(context)

    def _advance(self, context: Any, agent: AgentLoopAgent) -> None:
        structured_failures = 0
        provider_failures = 0
        while not context.finished and not context.error and not context.waiting:
            if self._stop_if_cancelled(context):
                return
            if context.steps >= context.max_steps:
                self._fail(
                    context,
                    f"达到步数上限（{context.max_steps}）",
                    error_type="StepLimitExceeded",
                )
                return
            context.steps += 1
            try:
                response = agent.step(context)
                provider_failures = 0
                if self._stop_if_cancelled(context):
                    return
                if isinstance(response, WaitForUser):
                    self._enter_waiting(context, response)
                    return
                if len(response.calls) > self.harness.max_calls_per_turn:
                    raise StructuredOutputError(
                        "model response exceeds execution.max_calls_per_turn",
                        max_calls_per_turn=self.harness.max_calls_per_turn,
                        actual_calls=len(response.calls),
                    )
                call_ids = [call.call_id for call in response.calls]
                if any(not call_id for call_id in call_ids) or len(set(call_ids)) != len(
                    call_ids
                ):
                    raise StructuredOutputError(
                        "structured calls must have unique non-empty call_id values"
                    )
                historical_ids = [
                    str(call.get("id") or "")
                    for call in context.provider_tool_calls()
                ]
                if any(historical_ids.count(call_id) != 1 for call_id in call_ids):
                    raise StructuredOutputError(
                        "structured call_id values must be unique in the Agent thread"
                    )
                self.dispatcher.emit(
                    context,
                    "progress",
                    response.text
                    or (response.calls[0].name if response.calls else "model response"),
                )
                if not response.calls:
                    if self._unfinished_coding_patch(context):
                        continue
                    context.final_message = response.text
                    context.result = agent.build_result(context)
                    context.finished = True
                    self.dispatcher.emit(context, "completed", response.text)
                    return

                schemas = getattr(agent, "agent_schemas", dict)()
                resolved = [
                    self.harness.resolve_model_call(
                        call,
                        context,
                        agent_schemas=schemas,
                    )
                    for call in response.calls
                ]
                should_continue = self._execute_batch(
                    context,
                    agent,
                    resolved,
                    summary=response.text,
                )
                structured_failures = 0
            except StructuredOutputError as exc:
                if self._stop_if_cancelled(context):
                    return
                structured_failures += 1
                open_calls = context.open_tool_calls()
                self._close_open_calls(
                    context,
                    {
                        "status": "failed",
                        "error": "structured_output_error",
                        "message": str(exc),
                    },
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
                if structured_failures > self.harness.max_structured_retries:
                    self._fail(
                        context,
                        "模型连续返回无效响应；已达到结构化响应重试上限。"
                        f" 最后错误：{exc}",
                        error_type=type(exc).__name__,
                    )
                    return
                if not open_calls:
                    context.append_message(
                        {
                            "role": "user",
                            "content": (
                                "Previous response was invalid. Follow the active "
                                "structured-call contract exactly."
                            ),
                        }
                    )
                continue
            except LLMProviderError as exc:
                if self._stop_if_cancelled(context):
                    return
                provider_failures += 1
                self.dispatcher.observe(
                    context,
                    "provider_error",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
                if provider_failures > self.harness.max_provider_retries:
                    self._fail(
                        context,
                        "模型提供方连续失败；已达到提供方重试上限。"
                        f" 最后错误：{exc}",
                        error_type=type(exc).__name__,
                    )
                    return
                continue
            except ContextWindowExceeded as exc:
                if self._stop_if_cancelled(context):
                    return
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
                self._fail(
                    context, str(exc), error_type=type(exc).__name__
                )
                return
            except Exception as exc:  # noqa: BLE001 - loop boundary records failures
                if self._stop_if_cancelled(context):
                    return
                self._fail(
                    context,
                    f"agent step failed: {exc}",
                    error_type=type(exc).__name__,
                )
                return
            except BaseException as exc:
                self._cancel_context(
                    context,
                    reason=(
                        f"execution interrupted by {type(exc).__name__}"
                    ),
                )
                raise

            if context.finished or context.error:
                return
            if context.waiting or not should_continue:
                message = context.waiting_question or str(
                    getattr(context.pending, "summary", "")
                    or "waiting for user input"
                )
                self.dispatcher.emit(context, "waiting", message)
                return

    def _enter_waiting(self, context: Any, response: WaitForUser) -> None:
        if response.call_id:
            open_call = context.unresolved_tool_call(response.call_id)
            function = (open_call or {}).get("function") or {}
            if (
                open_call is None
                or str(function.get("name") or "") != "runtime__wait_for_user"
            ):
                raise ValidationError(
                    "waiting result does not match the unresolved Runtime call"
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

    def _execute_batch(
        self,
        context: Any,
        agent: AgentLoopAgent,
        calls: list[CapabilityCall | AgentCall],
        *,
        summary: str,
    ) -> bool:
        if calls and all(isinstance(call, CapabilityCall) for call in calls):
            validate = getattr(agent, "validate_capability", None)
            if callable(validate):
                for call in calls:
                    validate(context, call.capability_id)
            completed = self.dispatcher.execute_capability_batch(
                context,
                calls,  # type: ignore[arg-type]
                summary=summary,
            )
            if not completed and not context.waiting:
                self._cancel_context(context)
            return completed

        from gitagent.harness.execution import ExecutionProfile

        profiles: list[Any] = []
        prepared_capabilities: dict[str, Any] = {}
        preflight_results: dict[str, Any] = {}
        failure_guard_preflight: dict[str, bool] = {}
        for call in calls:
            if isinstance(call, CapabilityCall):
                validate = getattr(agent, "validate_capability", None)
                if callable(validate):
                    validate(context, call.capability_id)
                decision = self.harness.capability_permission_decision(context, call)
                profile = self.harness.describe_capability_execution(context, call)
                if decision in {"ASK", "DENY"}:
                    profile = ExecutionProfile.exclusive(
                        read=profile.resource_claims.read,
                        write=profile.resource_claims.write,
                    )
                profiles.append(profile)
                continue
            self._validate_agent_call(context, call)
            profiles.append(self.harness.describe_agent_execution(context, call))

        def prepare_call(call: CapabilityCall | AgentCall) -> None:
            if isinstance(call, AgentCall):
                self._prepare_child(context, agent, call)
            else:
                preflight = self.dispatcher.preflight_capability(context, call)
                if preflight is not None:
                    preflight_results[call.call_id] = preflight
                else:
                    prepared_capabilities[call.call_id] = (
                        context.prepare_capability_call(
                            call.call_id,
                            call.capability_id,
                            call.arguments,
                        )
                    )
                prepared = prepared_capabilities.get(call.call_id)
                guard_arguments = (
                    prepared.execution_arguments
                    if prepared is not None
                    and prepared.execution_arguments is not None
                    else call.arguments
                )
                failure_guard_preflight[call.call_id] = not (
                    self.harness.capability_failure_blocked(
                        context, call, arguments=guard_arguments
                    )
                )

        def run_call(call: CapabilityCall | AgentCall) -> Any:
            if isinstance(call, CapabilityCall):
                if call.call_id in preflight_results:
                    return preflight_results[call.call_id]
                return context.execute_capability_call(
                    prepared_capabilities[call.call_id],
                    preflighted=failure_guard_preflight[call.call_id],
                )
            child = context.active_children[call.call_id]
            self.start(child, self._child_agent(call.agent_id))
            if child.waiting and self._unfinished_coding_patch(child):
                raise WorkflowError(
                    "workspace-sensitive Coding work paused before producing its artifact"
                )
            return child

        def suspend_call(call: CapabilityCall | AgentCall, outcome: Any) -> None:
            if not isinstance(call, CapabilityCall):
                return
            context.uncommitted_capability_results[call.call_id] = (
                self.dispatcher.capability_record(call, outcome)
            )

        def commit_call(call: CapabilityCall | AgentCall, outcome: Any) -> str:
            if isinstance(call, CapabilityCall):
                return self.dispatcher.commit_capability(
                    context,
                    call,
                    self.dispatcher.capability_record(call, outcome),
                    summary=summary,
                )
            child = context.active_children[call.call_id]
            if isinstance(outcome, Exception):
                child.error = str(outcome)
                child.finished = True
                child.user_input_request = None
                child.pending = None
            if child.waiting:
                return "waiting"
            failed = child.error is not None
            self._complete_child(context, agent, call, child)
            if context.waiting:
                return "waiting"
            return "failed" if failed else "continue"

        def cancel_call(call: CapabilityCall | AgentCall, reason: str) -> None:
            if isinstance(call, AgentCall):
                child = context.active_children.pop(call.call_id, None)
                if child is not None:
                    self._cancel_context(child, reason=reason)
            context.uncommitted_capability_results.pop(call.call_id, None)
            if (
                context.pending is not None
                and context.pending.provider_call_id == call.call_id
            ):
                context.pending = None
            if context.unresolved_tool_call(call.call_id) is None:
                return
            context.append_tool_result(
                {"status": "cancelled", "reason": reason},
                call_id=call.call_id,
            )
            self.dispatcher.observe_call_cancelled(
                context,
                {
                    "call_id": call.call_id,
                    "call_type": (
                        "capability"
                        if isinstance(call, CapabilityCall)
                        else "agent"
                    ),
                    "target": (
                        call.capability_id
                        if isinstance(call, CapabilityCall)
                        else call.agent_id
                    ),
                    "reason": reason,
                },
            )

        completed = self.harness.coordinator.execute(
            calls,
            profiles,
            prepare_call=prepare_call,
            run_call=run_call,
            commit_call=commit_call,
            suspend_call=suspend_call,
            cancel_call=cancel_call,
            lane_for=lambda call: (
                "capability"
                if isinstance(call, CapabilityCall)
                else "domain"
                if context.spec.agent_depth == 0
                else "inline"
            ),
            provider_for=lambda call: (
                self.harness.provider_id(call.capability_id)
                if isinstance(call, CapabilityCall)
                else None
            ),
            owner=context,
            observe_failure=lambda call, profile, action: (
                self.dispatcher.observe_failure_scope(
                    context, call, profile, action
                )
            ),
        )
        if not completed and not context.waiting:
            self._cancel_context(context)
        return completed

    def _continue_open_batch(
        self, context: Any, agent: AgentLoopAgent
    ) -> bool:
        schemas = getattr(agent, "agent_schemas", dict)()
        while True:
            open_calls = context.open_tool_calls()
            if not open_calls:
                return True
            progressed = False
            for provider_call in open_calls:
                structured = self._provider_structured_call(provider_call)
                resolved = self.harness.resolve_model_call(
                    structured, context, agent_schemas=schemas
                )
                if (
                    isinstance(resolved, AgentCall)
                    and resolved.call_id in context.active_children
                ):
                    child = context.active_children[resolved.call_id]
                    self._active_child_call(context, child)
                    if child.waiting:
                        return False
                    failed = child.error is not None
                    self._complete_child(context, agent, resolved, child)
                    progressed = True
                    if context.waiting:
                        return False
                    if failed:
                        profile = self.harness.describe_agent_execution(
                            context, resolved
                        )
                        if self.harness.coordinator.failure_stops_batch(
                            resolved,
                            profile,
                            observe_failure=lambda call, profile, action: (
                                self.dispatcher.observe_failure_scope(
                                    context, call, profile, action
                                )
                            ),
                        ):
                            self._cancel_remaining_open_calls(
                                context,
                                "stopped after a resumed Agent fence failed",
                            )
                            return True
                    continue
                if (
                    isinstance(resolved, CapabilityCall)
                    and resolved.call_id in context.uncommitted_capability_results
                ):
                    record = context.uncommitted_capability_results.pop(
                        resolved.call_id
                    )
                    decision = self.dispatcher.commit_capability(
                        context, resolved, record
                    )
                    progressed = True
                    if decision == "waiting":
                        return False
                    if decision == "failed":
                        profile = self.harness.describe_capability_execution(
                            context, resolved
                        )
                        if self.harness.coordinator.failure_stops_batch(
                            resolved,
                            profile,
                            observe_failure=lambda call, profile, action: (
                                self.dispatcher.observe_failure_scope(
                                    context, call, profile, action
                                )
                            ),
                        ):
                            self._cancel_remaining_open_calls(
                                context,
                                "stopped after a resumed execution fence failed",
                            )
                            return True
                    continue
                break
            else:
                if progressed:
                    continue
                raise ValidationError("unresolved provider calls cannot be resumed")

            missing_calls = []
            for item in context.open_tool_calls():
                call_id = str(item.get("id") or "")
                if (
                    call_id in context.active_children
                    or call_id in context.uncommitted_capability_results
                ):
                    continue
                missing_calls.append(
                    self.harness.resolve_model_call(
                        self._provider_structured_call(item),
                        context,
                        agent_schemas=schemas,
                    )
                )
            if not missing_calls:
                if progressed:
                    continue
                raise ValidationError("unresolved provider calls cannot be resumed")
            return self._execute_batch(context, agent, missing_calls, summary="")

    def _prepare_child(
        self,
        context: Any,
        agent: AgentLoopAgent,
        call: AgentCall,
    ) -> Any:
        prepare = getattr(agent, "prepare_child", None)
        if not callable(prepare):
            raise ValidationError(f"{context.agent} cannot call agent__{call.agent_id}")
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
        child.parent_run_id = context.run_id
        child.parent_call_id = call.call_id
        child.parent_call_name = f"agent__{call.agent_id}"
        child.parent_call_arguments = dict(call.arguments)
        try:
            prepare(context, call, child)
        except Exception as exc:  # noqa: BLE001 - isolated child result boundary
            child.error = str(exc)
            child.finished = True
        context.active_children[call.call_id] = child
        return child

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
        if result.call_id != call.call_id or result.agent_id != call.agent_id:
            raise ValidationError("child Agent result correlation does not match its call")
        if result.status not in {"completed", "failed"} or not result.content.strip():
            raise ValidationError("child Agent returned an invalid terminal result")
        context.active_children.pop(call.call_id, None)
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

        overlap = set(context.active_children) & set(
            context.uncommitted_capability_results
        )
        if overlap:
            raise ValidationError("one call_id has both Agent and Capability state")
        for call_id, record in context.uncommitted_capability_results.items():
            provider_call = context.unresolved_tool_call(call_id)
            if record.call_id != call_id or provider_call is None:
                raise ValidationError(
                    "stored uncommitted Capability result correlation is invalid"
                )
            structured = self._provider_structured_call(provider_call)
            if (
                structured.name
                != self.harness.function_name(record.result.capability_id)
                or structured.arguments != record.arguments
            ):
                raise ValidationError(
                    "stored uncommitted Capability payload correlation is invalid"
                )
        for call_id, child in context.active_children.items():
            if call_id != child.parent_call_id:
                raise ValidationError("stored active child call_id is invalid")
            if child.parent_run_id != context.run_id:
                raise ValidationError("stored active child parent_run_id is invalid")
            self._active_child_call(context, child)
            if not child.waiting and not child.finished and child.error is None:
                raise ValidationError(
                    "stored active child is neither paused nor complete"
                )
            if (
                child.agent == "coding"
                and child.waiting
                and not child.coding_task_completed
            ):
                raise ValidationError(
                    "stored Coding child paused before its workspace phase completed"
                )
            self.validate_context_tree(child)

    @staticmethod
    def waiting_context(context: Any) -> Any:
        """Return the deepest provider-ordered paused context."""

        current = context
        while True:
            child = current.first_waiting_child()
            if child is None:
                return current
            current = child

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
        open_call = context.unresolved_tool_call(call.call_id)
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
            or str(function.get("name") or "") != expected_name
            or arguments != call.arguments
        ):
            raise ValidationError(
                "structured Agent call does not match the unresolved provider call"
            )

    @staticmethod
    def _provider_structured_call(provider_call: dict[str, Any]) -> StructuredCall:
        function = provider_call.get("function") or {}
        if not isinstance(function, dict):
            raise ValidationError("provider tool call function is invalid")
        raw_arguments = function.get("arguments")
        try:
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else dict(raw_arguments or {})
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("provider tool call arguments are invalid") from exc
        return StructuredCall(
            str(provider_call.get("id") or ""),
            str(function.get("name") or ""),
            arguments,
        )

    def _child_agent(self, agent_id: str) -> AgentLoopAgent:
        child = self.child_agents.get(agent_id)
        if child is None:
            raise ValidationError(f"no Runtime child Agent is registered for {agent_id}")
        return child

    def _cancel_remaining_open_calls(self, context: Any, reason: str) -> None:
        for call in context.open_tool_calls():
            call_id = str(call.get("id") or "")
            function = call.get("function") or {}
            name = (
                str(function.get("name") or "")
                if isinstance(function, dict)
                else ""
            )
            child = context.active_children.pop(call_id, None)
            if child is not None:
                self._cancel_context(child, reason=reason)
            context.uncommitted_capability_results.pop(call_id, None)
            context.append_tool_result(
                {"status": "cancelled", "reason": reason}, call_id=call_id
            )
            self.dispatcher.observe_call_cancelled(
                context,
                {"call_id": call_id, "target": name, "reason": reason},
            )

    @staticmethod
    def _close_open_calls(context: Any, payload: dict[str, Any]) -> None:
        for call in context.open_tool_calls():
            context.append_tool_result(
                payload, call_id=str(call.get("id") or "")
            )

    def _fail(
        self,
        context: Any,
        error: str | Exception,
        *,
        error_type: str | None = None,
    ) -> None:
        self._cleanup_coding_workspace(context)
        self._close_open_calls(
            context, {"status": "failed", "reason": str(error)}
        )
        context.pending = None
        context.user_input_request = None
        context.active_children.clear()
        context.uncommitted_capability_results.clear()
        context.error = str(error)
        context.finished = True
        self.dispatcher.emit(
            context,
            "failed",
            str(error),
            details={
                "failure_domain": "agent",
                "error_type": error_type or type(error).__name__,
            },
        )

    def _cancel_context(
        self, context: Any, *, reason: str = "execution was cancelled"
    ) -> None:
        self._cleanup_coding_workspace(context)
        already_cancelled = context.finished and context.error == "execution cancelled"
        self._cancel_remaining_open_calls(context, reason)
        context.pending = None
        context.user_input_request = None
        context.active_children.clear()
        context.uncommitted_capability_results.clear()
        context.final_message = "execution cancelled"
        context.error = context.final_message
        context.finished = True
        if not already_cancelled:
            self.dispatcher.emit(context, "cancelled", context.final_message)

    def _stop_if_cancelled(self, context: Any) -> bool:
        if not self.harness.coordinator.cancellation_requested():
            return False
        self._cancel_context(context)
        return True

    @staticmethod
    def _unfinished_coding_patch(context: Any) -> bool:
        task = getattr(context, "coding_task", None)
        return bool(
            getattr(context, "agent", "") == "coding"
            and task is not None
            and getattr(task, "mode", None) == "patch"
            and not getattr(context, "coding_task_completed", False)
        )

    @staticmethod
    def _cleanup_coding_workspace(context: Any) -> None:
        workspace = getattr(context, "coding_workspace", None)
        if workspace is None:
            return
        try:
            workspace.cleanup(suppress_errors=True)
        finally:
            context.coding_workspace = None


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
