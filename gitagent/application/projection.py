"""Bounded projections of Session-scoped agent results into conversation history."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from gitagent.domain.errors import ValidationError
from gitagent.domain.models import (
    DraftResult,
    IssueAgentResult,
    MutationRejectedResult,
    PullRequestAgentResult,
    RepositoryResult,
)
from gitagent.harness.context.state import AgentContext

from .service import ServiceResult

T = TypeVar("T")
TextSanitizer = Callable[[str], str]
MAX_VISIBLE_ITEMS = 20


def visible_items(items: list[T]) -> tuple[tuple[T, ...], bool]:
    return tuple(items[:MAX_VISIBLE_ITEMS]), len(items) > MAX_VISIBLE_ITEMS


@dataclass(frozen=True)
class TurnProjection:
    assistant_text: str
    workflow_summary: str
    route: dict[str, Any] | None
    entity_manifests: list[dict[str, Any]]
    focus: dict[str, str] | None
    open_question: str | None = None
    goals: tuple[str, ...] = ()


def project_service_result(
    result: ServiceResult,
    *,
    turn_seq: int,
    text_sanitizer: TextSanitizer | None = None,
) -> TurnProjection:
    assistant, manifests, focus = project_output(
        result.domain_output if result.domain_output is not None else result.output,
        turn_seq=turn_seq,
        text_sanitizer=text_sanitizer,
    )
    final_assistant = (
        _sanitized(result.output, text_sanitizer)
        if isinstance(result.output, str)
        else assistant
    )
    if result.agent is None:
        return TurnProjection(final_assistant, assistant, None, manifests, focus)

    resolved = []
    if result.entity_id is not None and result.entity_type in {"issue", "pull_request", "workflow_run"}:
        resolved = [{"type": result.entity_type, "id": result.entity_id}]
    workflow_status = _workflow_status(result.domain_output)
    route = {
        "route": _bounded(result.agent, 80, text_sanitizer),
        "goal": _bounded(result.goal, 1000, text_sanitizer),
        "resolved_references": resolved,
        "status": workflow_status,
    }
    if focus is None and result.entity_id is not None and result.entity_type in {"issue", "pull_request"}:
        focus = {
            "type": result.entity_type,
            "id": result.entity_id,
            "short_label": _reference_label(result.entity_type, result.entity_id),
        }
    summary = result.workflow_summary or domain_summary(
        agent=result.agent,
        goal=result.goal,
        output=result.domain_output,
        entity_type=result.entity_type,
        entity_id=result.entity_id,
    )
    return TurnProjection(
        assistant_text=final_assistant,
        workflow_summary=_bounded(summary, 8 * 1024, text_sanitizer),
        route=route,
        entity_manifests=manifests,
        focus=focus,
        open_question=(
            result.domain_output.waiting_question
            if isinstance(result.domain_output, AgentContext)
            and result.domain_output.waiting_question
            else None
        ),
        goals=(result.goal,) if result.goal else (),
    )


def domain_summary(
    *,
    agent: str,
    goal: str,
    output: Any,
    entity_type: str | None = None,
    entity_id: str | None = None,
    context: AgentContext | None = None,
) -> str:
    """Build the bounded semantic bridge from a Domain run to Main."""

    status = _workflow_status(output)
    try:
        result_text, _, _ = project_output(output)
    except ValidationError:
        result_text = str(output or "")
    references: list[str] = []
    if entity_type and entity_id:
        references.append(f"{entity_type}:{entity_id}")
    if isinstance(output, RepositoryResult):
        references.extend(f"file:{path}" for path in output.files[:20])
    elif isinstance(output, IssueAgentResult):
        references.extend(f"issue:{item.number}" for item in output.issues[:20])
    elif isinstance(output, PullRequestAgentResult):
        references.extend(f"pull_request:{item.number}" for item in output.pull_requests[:20])
        references.extend(f"file:{path}" for path in output.changed_files[:20])

    runtime = context or (output if isinstance(output, AgentContext) else None)
    mutation_observation = (
        _last_write_like_observation(runtime) if runtime is not None else None
    )
    mutation_executed = mutation_observation is not None
    mutation = (
        mutation_observation.get("data")
        if isinstance(mutation_observation, dict)
        else None
    )
    if isinstance(output, PullRequestAgentResult) and output.execution_result is not None:
        mutation_executed = True
        mutation = output.execution_result
    pending = runtime.pending.summary if runtime is not None and runtime.pending is not None else ""
    question = runtime.waiting_question if runtime is not None else ""
    failure = runtime.error if runtime is not None else ""
    if isinstance(output, MutationRejectedResult):
        failure = output.reason
    incomplete = question or pending
    payload = {
        "task": _bounded(goal, 1000),
        "agent": agent,
        "status": status,
        "result": _bounded(result_text, 6000),
        "mutation_executed": mutation_executed,
        "mutation_result": mutation,
        "key_references": list(dict.fromkeys(references)),
        "unfinished_or_next": _bounded(incomplete, 2000),
        "pending_confirmation": _bounded(pending or question, 2000),
        "failure_reason": _bounded(failure, 2000),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def project_output(
    output: Any,
    *,
    turn_seq: int = 0,
    text_sanitizer: TextSanitizer | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, str] | None]:
    if output is None:
        return "", [], None
    if isinstance(output, str):
        return _bounded(output, 8 * 1024, text_sanitizer), [], None
    if isinstance(output, DraftResult):
        text = f"{output.title}\n\n{output.body}"
        if output.note:
            text += f"\n\n{output.note}"
        focus = None
        if output.entity_id and output.entity_type in {"issue", "pull_request"}:
            focus = {
                "type": output.entity_type,
                "id": output.entity_id,
                "short_label": _reference_label(output.entity_type, output.entity_id),
            }
        return _bounded(text, 8 * 1024, text_sanitizer), [], focus
    if isinstance(output, MutationRejectedResult):
        text = f"操作：{output.summary}\n结果：未执行\n失败原因：{output.reason}"
        return _bounded(text, 8 * 1024, text_sanitizer), [], None
    if isinstance(output, AgentContext):
        return _project_context(output, turn_seq=turn_seq, text_sanitizer=text_sanitizer)
    if isinstance(output, RepositoryResult):
        text = output.answer
        files = ", ".join(output.files[:20])
        text += f"\n相关文件：{files}" if files else ""
        return _bounded(text, 8 * 1024, text_sanitizer), [], None
    if isinstance(output, IssueAgentResult):
        selected, truncated = visible_items(output.issues)
        lines = [output.answer]
        lines.extend(
            f"#{issue.number} {issue.title} [{issue.state}{', locked' if issue.locked else ''}]"
            for issue in selected
        )
        if truncated:
            lines.append(f"[仅显示前 {MAX_VISIBLE_ITEMS} 项]")
        manifests = []
        if output.issues and output.operation and output.operation.value in {"LIST", "SEARCH", "SUMMARIZE"}:
            manifests = [_manifest(turn_seq, "issue", [(issue.number, issue.title) for issue in selected])]
        focus = None
        if output.issue_number is not None and len(selected) == 1:
            focus = {
                "type": "issue",
                "id": str(output.issue_number),
                "short_label": _bounded(selected[0].title, 120, text_sanitizer),
            }
        return _bounded("\n".join(filter(None, lines)), 8 * 1024, text_sanitizer), manifests, focus
    if isinstance(output, PullRequestAgentResult):
        selected, truncated = visible_items(output.pull_requests)
        lines = [output.answer]
        lines.extend(f"PR #{item.number} {item.title} [{item.state}]" for item in selected)
        if output.changed_files:
            lines.append("变更文件：" + ", ".join(output.changed_files[:100]))
        if truncated:
            lines.append(f"[仅显示前 {MAX_VISIBLE_ITEMS} 项]")
        manifests = []
        if output.pull_requests and output.operation and output.operation.value in {"LIST", "SEARCH", "SUMMARIZE"}:
            manifests = [_manifest(turn_seq, "pull_request", [(item.number, item.title) for item in selected])]
        focus = None
        if output.pr_number is not None and len(selected) == 1:
            focus = {
                "type": "pull_request",
                "id": str(output.pr_number),
                "short_label": _bounded(selected[0].title, 120, text_sanitizer),
            }
        return _bounded("\n".join(filter(None, lines)), 8 * 1024, text_sanitizer), manifests, focus
    raise ValidationError(f"unsupported output type: {type(output).__name__}")


def _project_context(
    context: AgentContext,
    *,
    turn_seq: int,
    text_sanitizer: TextSanitizer | None,
) -> tuple[str, list[dict[str, Any]], dict[str, str] | None]:
    parts: list[str] = []
    manifests: list[dict[str, Any]] = []
    focus: dict[str, str] | None = None
    if context.error:
        parts.append(f"错误：{context.error}")
    elif context.waiting_question:
        parts.append(f"问题：{context.waiting_question}")
    elif context.pending is not None:
        parts.append(f"提案：{context.pending.summary}")
        for call in context.pending.calls:
            arguments = json.dumps(call.arguments, ensure_ascii=False, indent=2, sort_keys=True)
            parts.append(f"待执行 `{call.capability_id}`：\n{arguments}")
        parts.append("待人工批准后执行。")
    mutation = _last_write_like_observation(context)
    if mutation is not None:
        parts.append(
            "执行结果："
            + json.dumps(mutation.get("data"), ensure_ascii=False, sort_keys=True)
        )
    if context.result is not None:
        nested = project_output(context.result, turn_seq=turn_seq, text_sanitizer=text_sanitizer)
        parts.append(nested[0])
        manifests = nested[1]
        focus = nested[2]
    elif context.final_message:
        parts.append(context.final_message)
    if focus is None and context.entity_type in {"issue", "pull_request"} and context.entity_id:
        focus = {
            "type": context.entity_type,
            "id": context.entity_id,
            "short_label": _reference_label(context.entity_type, context.entity_id),
        }
    return _bounded("\n\n".join(filter(None, parts)), 8 * 1024, text_sanitizer), manifests, focus


def _last_write_like_observation(context: AgentContext) -> Any | None:
    for observation in reversed(context.observations):
        if observation.get("kind") != "capability":
            continue
        payload = observation.get("payload") or {}
        capability_id = str(payload.get("capability_id") or "")
        if capability_id.startswith("github.") and capability_id not in {
            "github.list_issues",
            "github.get_issue",
            "github.get_issue_comments",
            "github.list_pull_requests",
            "github.get_pr",
            "github.get_pr_comments",
            "github.get_pr_reviews",
            "github.get_workflow_runs",
            "github.get_job_logs",
        }:
            return payload
    return None


def _workflow_status(output: Any) -> str:
    if isinstance(output, MutationRejectedResult):
        return "rejected"
    if isinstance(output, DraftResult):
        return "awaiting_input"
    if isinstance(output, AgentContext):
        if output.error:
            return "failed"
        if output.pending is not None:
            return "awaiting_approval"
        if output.waiting_question:
            return "awaiting_input"
    return "completed"


def _manifest(turn_seq: int, entity_type: str, items: list[tuple[Any, str]]) -> dict[str, Any]:
    return {
        "turn_seq": turn_seq,
        "entity_type": entity_type,
        "items": [
            {"position": index, "entity_id": str(identifier), "short_label": title[:120]}
            for index, (identifier, title) in enumerate(items, 1)
        ],
    }


def _reference_label(reference_type: str, identifier: str) -> str:
    prefix = {"issue": "Issue", "pull_request": "PR", "workflow_run": "Workflow run"}.get(
        reference_type, reference_type
    )
    return f"{prefix} #{identifier}"


def _bounded(value: Any, limit: int, text_sanitizer: TextSanitizer | None = None) -> str:
    text = _sanitized(value, text_sanitizer)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 15] + "…[TRUNCATED]"


def _sanitized(value: Any, text_sanitizer: TextSanitizer | None) -> str:
    raw = str(value)
    return text_sanitizer(raw) if text_sanitizer is not None else raw
