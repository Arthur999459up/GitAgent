"""Bounded projections of Session-scoped agent results into conversation history."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from gitagent.domain.errors import ValidationError
from gitagent.domain.models import (
    DomainAction,
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
    history_text: str
    route_summary: list[dict[str, Any]]
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
        result.output,
        turn_seq=turn_seq,
        text_sanitizer=text_sanitizer,
    )
    if result.decision.clarify:
        return TurnProjection(assistant, "", [], manifests, focus, open_question=assistant)
    if result.agent is None:
        return TurnProjection(assistant, "", [], manifests, focus)

    resolved = []
    if result.entity_id is not None and result.entity_type in {"issue", "pull_request", "workflow_run"}:
        resolved = [{"type": result.entity_type, "id": result.entity_id}]
    workflow_status = _workflow_status(result.output)
    route_summary = [
        {
            "route": _bounded(result.agent, 80, text_sanitizer),
            "session_goal": _bounded(result.goal, 1000, text_sanitizer),
            "resolved_references": resolved,
            "workflow_type": _bounded(result.agent, 80, text_sanitizer),
            "workflow_status": workflow_status,
        }
    ]
    if focus is None and result.entity_id is not None and result.entity_type in {"issue", "pull_request"}:
        focus = {
            "type": result.entity_type,
            "id": result.entity_id,
            "short_label": _reference_label(result.entity_type, result.entity_id),
        }
    history = f"{result.agent} | goal={_bounded(result.goal, 1000, text_sanitizer)} | {workflow_status}"
    return TurnProjection(
        assistant_text=assistant,
        history_text=_bounded(history, 2 * 1024, text_sanitizer),
        route_summary=route_summary,
        entity_manifests=manifests,
        focus=focus,
        open_question=result.output.question if isinstance(result.output, AgentContext) and result.output.question else None,
        goals=(result.goal,) if result.goal else (),
    )


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
        text = output.question if output.action == DomainAction.CLARIFY and output.question else output.answer
        files = ", ".join(output.files[:20])
        text += f"\n相关文件：{files}" if files else ""
        return _bounded(text, 8 * 1024, text_sanitizer), [], None
    if isinstance(output, IssueAgentResult):
        if output.action == DomainAction.CLARIFY:
            return _bounded(output.question, 8 * 1024, text_sanitizer), [], None
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
        if output.action == DomainAction.CLARIFY:
            return _bounded(output.question, 8 * 1024, text_sanitizer), [], None
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
    elif context.question:
        parts.append(f"问题：{context.question}")
    elif context.pending is not None:
        parts.append(f"提案：{context.pending.summary}")
        for call in context.pending.calls:
            arguments = json.dumps(call.arguments, ensure_ascii=False, indent=2, sort_keys=True)
            parts.append(f"待执行 `{call.capability_id}`：\n{arguments}")
        parts.append("待人工批准后执行。")
    mutation = _last_write_like_observation(context)
    if mutation is not None:
        parts.append("执行结果：" + json.dumps(mutation, ensure_ascii=False, sort_keys=True))
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
            return payload.get("data")
    return None


def _workflow_status(output: Any) -> str:
    if isinstance(output, MutationRejectedResult):
        return "rejected"
    if isinstance(output, AgentContext):
        if output.error:
            return "failed"
        if output.pending is not None:
            return "awaiting_approval"
        if output.question:
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
