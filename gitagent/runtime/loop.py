"""Generic bounded agent loop with exact human approval gates."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..core.errors import PermissionDenied, ValidationError, WorkflowError
from ..core.models import (
    AccessLevel,
    ApprovalIntent,
    PlannedToolCall,
    WorkflowTurnDecision,
)
from ..core.trace import TraceCategory, TraceStatus
from .harness import AgentContext, AgentHarness, debug_context_snapshot, debug_error_details, debug_value
from .mutation import (
    code_change_approval_summary,
    code_change_mutation_plan,
    code_change_review_package,
)


class AgentActionKind(str, Enum):
    TOOL = "tool"
    APPLY_CODE_CHANGE = "apply_code_change"
    SPECIALIST = "specialist"
    ASK = "ask"
    FINISH = "finish"


@dataclass
class AgentAction:
    """What a domain agent wants to do next; the loop executes or gates it."""

    kind: AgentActionKind
    summary: str = ""
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    specialist: str | None = None
    question: str = ""
    message: str = ""


@dataclass
class PendingAction:
    """The exact-scope approval an agent context is waiting on."""

    approval_id: str
    summary: str
    calls: list[PlannedToolCall]
    specialist: str | None = None


class AgentLoopAgent(Protocol):
    def decide(self, context: AgentContext) -> AgentAction: ...

    def build_result(self, context: AgentContext) -> Any: ...

    def run_specialist(self, context: AgentContext, specialist: str) -> dict[str, Any]: ...


class AgentLoop:
    def __init__(self, harness: AgentHarness, *, max_steps: int = 20) -> None:
        self.harness = harness
        self.max_steps = max_steps

    def start(self, context: AgentContext, agent: AgentLoopAgent) -> AgentContext:
        if context.finished or context.error:
            return context
        if context.waiting:
            raise WorkflowError("agent context is waiting for user input")
        self._emit(context, TraceStatus.STARTED, message=context.goal)
        self._step(context, agent)
        return context

    def resume(
        self,
        context: AgentContext,
        agent: AgentLoopAgent,
        decision: WorkflowTurnDecision,
    ) -> AgentContext:
        if context.finished or context.error:
            raise WorkflowError("agent context is not waiting for user input")
        self._apply_decision(context, agent, decision)
        if not context.finished and not context.error and not context.waiting:
            self._step(context, agent)
        return context

    def restore_pending(
        self,
        context: AgentContext,
        *,
        summary: str,
        calls: list[PlannedToolCall],
        specialist: str | None = None,
    ) -> None:
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
            proposal_revision=1,
            proposal_content=summary,
        )
        context.pending = PendingAction(approval.approval_id, summary, calls, specialist=specialist)

    def _apply_decision(
        self,
        context: AgentContext,
        agent: AgentLoopAgent,
        decision: WorkflowTurnDecision,
    ) -> None:
        if context.question:
            self._observe(context, "assistant", context.question)
            self._observe(context, "user", decision.instruction or decision.message or "")
            context.question = ""
            return
        pending = context.pending
        if pending is None:
            raise WorkflowError("agent context is not waiting for user input")
        if decision.action == ApprovalIntent.APPROVE:
            self.harness.approvals.decide(pending.approval_id, "Approve")
            try:
                self._execute_pending(context, agent, pending)
            except Exception as exc:  # noqa: BLE001 - mutation stops fail-closed
                self._fail(context, f"approved action stopped fail-closed: {exc}", exc=exc)
                return
            context.pending = None
            return
        self.harness.approvals.decide(pending.approval_id, "Reject")
        feedback = (decision.instruction or "").strip()
        self._observe(context, "rejection", {"instruction": feedback})
        context.pending = None

    def _step(self, context: AgentContext, agent: AgentLoopAgent) -> None:
        while not context.finished and not context.error and not context.waiting:
            if context.steps >= context.max_steps:
                context.error = f"达到步数上限（{context.max_steps}）"
                context.finished = True
                self._emit(context, TraceStatus.FAILED, message=context.error)
                return
            context.steps += 1
            try:
                action = agent.decide(context)
            except Exception as exc:  # noqa: BLE001 - loop boundary records a fail-closed state
                self._fail(context, f"agent decision failed: {exc}", exc=exc)
                return
            self.harness.trace.emit(
                session_id=context.session_id,
                category=TraceCategory.AGENT,
                name=context.agent,
                status=TraceStatus.PROGRESS,
                details={
                    "debug_event": "decision",
                    "step": context.steps,
                    "decision": _debug_action(action),
                    "context": debug_context_snapshot(context),
                },
            )
            if not self._handle(context, agent, action):
                return

    def _handle(self, context: AgentContext, agent: AgentLoopAgent, action: AgentAction) -> bool:
        try:
            if action.kind == AgentActionKind.TOOL:
                return self._handle_tool(context, agent, action)
            if action.kind == AgentActionKind.APPLY_CODE_CHANGE:
                return self._handle_apply(context, action)
            if action.kind == AgentActionKind.SPECIALIST:
                return self._handle_specialist(context, action)
            if action.kind == AgentActionKind.ASK:
                context.question = action.question or action.summary
                self._emit(context, TraceStatus.WAITING, message=context.question)
                return False
            if action.kind == AgentActionKind.FINISH:
                context.final_message = action.message or context.final_message
                if context.result_required:
                    context.result = agent.build_result(context)
                context.finished = True
                self._emit(context, TraceStatus.COMPLETED, message=context.final_message or "completed")
                return False
        except Exception as exc:  # noqa: BLE001
            self._fail(context, f"action failed: {exc}", exc=exc)
            return False
        raise ValidationError(f"unknown action kind: {action.kind}")

    def _handle_tool(self, context: AgentContext, agent: AgentLoopAgent, action: AgentAction) -> bool:
        if not action.tool:
            raise ValidationError("tool action requires a tool name")
        spec = self.harness.spec(context.agent)
        if action.tool not in spec.allowed_tools:
            raise PermissionDenied(f"agent {context.agent} is not allowed to use {action.tool}")
        arguments = dict(action.arguments)
        if "repository" not in arguments and (
            action.tool.startswith("github.") or action.tool.startswith("repository.")
        ):
            arguments["repository"] = context.repository
        tool = self.harness.server.get_tool(action.tool)
        cache_key = json.dumps([action.tool, arguments], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if tool.access == AccessLevel.READ and cache_key in context.read_cache:
            self._observe(
                context,
                "tool",
                {"tool": action.tool, "arguments": arguments, "data": context.read_cache[cache_key], "cached": True},
            )
            return True
        if tool.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}:
            if context.read_only:
                self._observe(
                    context,
                    "policy",
                    {"tool": action.tool, "blocked": "read_only context forbids mutation proposals"},
                )
                context.final_message = "只读取证阶段已完成；未生成或执行写操作。"
                context.result = agent.build_result(context)
                context.finished = True
                self._emit(context, TraceStatus.COMPLETED, message=context.final_message)
                return False
            call = PlannedToolCall(action.tool, arguments)
            approval = self.harness.approvals.create(
                session_id=context.session_id,
                repository=context.repository,
                summary=action.summary or f"执行 {action.tool}",
                calls=[call],
                proposal_revision=1,
                proposal_content=action.summary or f"执行 {action.tool}",
            )
            context.pending = PendingAction(approval.approval_id, approval.summary, [call])
            self._emit(context, TraceStatus.WAITING, message=approval.summary)
            return False
        result = context.tool(action.tool, **arguments)
        context.read_cache[cache_key] = result
        self._observe(context, "tool", {"tool": action.tool, "arguments": arguments, "data": result})
        return True

    def _handle_apply(self, context: AgentContext, action: AgentAction) -> bool:
        if context.code_candidate is None or context.change_request is None:
            raise WorkflowError("apply_code_change requires a verified candidate and change request")
        if context.verification is not None and not context.verification.passed:
            raise WorkflowError("static verification failed; GitHub mutation is forbidden")
        review = code_change_review_package(context.change_request, context.code_candidate, context.verification)
        calls = code_change_mutation_plan(context.session_id, context.change_request, context.code_candidate, review)
        summary = code_change_approval_summary(context.change_request, review)
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=summary,
            calls=calls,
            proposal_revision=1,
            proposal_content=action.summary or review.change_summary,
        )
        context.pending = PendingAction(approval.approval_id, approval.summary, calls)
        self._emit(context, TraceStatus.WAITING, message=approval.summary)
        return False

    def _handle_specialist(self, context: AgentContext, action: AgentAction) -> bool:
        if not action.specialist:
            raise ValidationError("specialist action requires a specialist name")
        call = PlannedToolCall("agent.invoke_specialist", {"specialist": action.specialist})
        approval = self.harness.approvals.create(
            session_id=context.session_id,
            repository=context.repository,
            summary=action.summary or f"调用 {action.specialist}",
            calls=[call],
            proposal_revision=1,
            proposal_content=action.summary or f"调用 {action.specialist}",
        )
        context.pending = PendingAction(
            approval.approval_id,
            approval.summary,
            [call],
            specialist=action.specialist,
        )
        self._emit(context, TraceStatus.WAITING, message=approval.summary)
        return False

    def _execute_pending(
        self,
        context: AgentContext,
        agent: AgentLoopAgent,
        pending: PendingAction,
    ) -> None:
        if pending.specialist is not None:
            self.harness.approvals.authorize(
                approval_id=pending.approval_id,
                session_id=context.session_id,
                tool="agent.invoke_specialist",
                arguments={"specialist": pending.specialist},
            )
            data = agent.run_specialist(context, pending.specialist)
            self._observe(context, "tool", {"tool": f"specialist:{pending.specialist}", "data": data})
            if not self.harness.approvals.complete(pending.approval_id):
                raise WorkflowError("approved specialist call was not fully consumed")
            return
        mutator = self.harness.context(
            "github_mutator",
            context.session_id,
            repository=context.repository,
            goal=context.goal,
        )
        for call in pending.calls:
            result = mutator.tool(call.tool, approval_id=pending.approval_id, **call.arguments)
            self._observe(
                context,
                "tool",
                {"tool": call.tool, "arguments": dict(call.arguments), "data": result},
            )
        if not self.harness.approvals.complete(pending.approval_id):
            raise WorkflowError("approved mutation plan was not fully consumed")

    def _observe(self, context: AgentContext, kind: str, payload: Any) -> None:
        context.observations.append({"kind": kind, "payload": payload})

    def _fail(self, context: AgentContext, error: str, *, exc: BaseException | None = None) -> None:
        context.error = str(error)
        context.finished = True
        self._emit(context, TraceStatus.FAILED, message=str(error), exc=exc)

    def _emit(
        self,
        context: AgentContext,
        status: TraceStatus,
        message: str = "",
        *,
        exc: BaseException | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "debug_event": status.value,
            "steps": context.steps,
            "goal": context.goal,
            "repository": context.repository,
            "context": debug_context_snapshot(context),
        }
        if exc is not None:
            details["error"] = debug_error_details(exc)
        self.harness.trace.emit(
            session_id=context.session_id,
            category=TraceCategory.AGENT,
            name=context.agent,
            status=status,
            message=message,
            details=details,
        )


def _debug_action(action: AgentAction) -> dict[str, Any]:
    return {
        "kind": action.kind.value,
        "summary": debug_value(action.summary, key="summary"),
        "tool": action.tool,
        "arguments": debug_value(action.arguments),
        "specialist": action.specialist,
        "question": debug_value(action.question, key="question"),
        "message": debug_value(action.message, key="message"),
    }


def rejection_feedback(context: AgentContext) -> str | None:
    """Return the instruction from the most recent rejected proposal."""
    for observation in reversed(context.observations):
        if observation["kind"] == "rejection":
            payload = observation.get("payload") or {}
            return str(payload.get("instruction") or "") if isinstance(payload, dict) else ""
        if observation["kind"] == "user":
            return None
    return None


def render_observations(context: AgentContext) -> str:
    """Return bounded observations as valid JSON; never truncate serialized JSON text."""
    observations = context.observations[-20:]
    for string_limit, item_limit in ((2_000, 20), (750, 10), (240, 5), (240, 5), (240, 5)):
        entries: list[dict[str, Any]] = []
        for observation in observations:
            if observation["kind"] == "tool":
                payload = observation["payload"]
                entries.append(
                    {
                        "tool": payload.get("tool", ""),
                        "arguments": _compact(
                            payload.get("arguments", {}),
                            string_limit=string_limit,
                            item_limit=item_limit,
                            content_limit=8_000,
                        ),
                        "data": _compact(
                            payload.get("data"),
                            string_limit=string_limit,
                            item_limit=item_limit,
                            content_limit=8_000,
                        ),
                    }
                )
            else:
                entries.append(
                    {
                        observation["kind"]: _compact(
                            observation["payload"], string_limit=string_limit, item_limit=item_limit
                        )
                    }
                )
        text = json.dumps(entries, ensure_ascii=False)
        if len(text) <= 16_000:
            return text
        observations = observations[-max(1, len(observations) // 2) :]
    return json.dumps(
        [{"notice": "latest observation exceeded the model context budget and was omitted"}],
        ensure_ascii=False,
    )


def _compact(
    value: Any,
    *,
    depth: int = 0,
    string_limit: int = 2_000,
    item_limit: int = 20,
    content_limit: int | None = None,
    key: str = "",
) -> Any:
    if isinstance(value, str):
        limit = content_limit if key == "content" and content_limit is not None else string_limit
        return value[:limit]
    if isinstance(value, dict):
        if depth > 4:
            return f"<{len(value)} keys>"
        priority_keys = (
            "path",
            "start_line",
            "end_line",
            "truncated",
            "content",
            "number",
            "title",
            "body",
            "state",
            "labels",
            "comments",
            "query",
            "results",
            "entries",
            "name",
            "login",
            "created_at",
            "updated_at",
        )
        ordered_keys = [key for key in priority_keys if key in value]
        ordered_keys.extend(key for key in value if key not in ordered_keys)
        items = [(key, value[key]) for key in ordered_keys[:item_limit]]
        compacted = {
            item_key: _compact(
                item,
                depth=depth + 1,
                string_limit=string_limit,
                item_limit=item_limit,
                content_limit=content_limit,
                key=str(item_key),
            )
            for item_key, item in items
        }
        content = value.get("content")
        if isinstance(content, str) and content_limit is not None and len(content) > content_limit:
            compacted["__content_projection__"] = {
                "truncated": True,
                "original_chars": len(content),
                "retained_chars": content_limit,
            }
        if len(value) > item_limit:
            compacted["__omitted__"] = f"{len(value) - item_limit} more keys"
        return compacted
    if isinstance(value, (list, tuple)):
        if depth > 4:
            return f"<{len(value)} items>"
        items = [
            _compact(
                item,
                depth=depth + 1,
                string_limit=string_limit,
                item_limit=item_limit,
                content_limit=content_limit,
            )
            for item in value[:item_limit]
        ]
        if len(value) > item_limit:
            items.append(f"<{len(value) - item_limit} more>")
        return items
    return value
