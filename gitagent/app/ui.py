"""GitAgent 的 Rich 终端组件与实时 Trace 渲染。"""

from __future__ import annotations

import json
from typing import Any

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from ..core.trace import TraceCategory, TraceEvent, TraceStatus

_PANEL_STYLES = {
    "user": "blue",
    "router": "bright_cyan",
    "agent": "cyan",
    "review": "magenta",
    "ci": "yellow",
    "workflow": "bright_blue",
    "verification_pass": "green",
    "verification_fail": "red",
    "approval": "yellow",
    "success": "green",
    "error": "red",
    "info": "dim",
}

_TRACE_LABELS = {
    TraceCategory.AGENT: ("Agent", "cyan"),
    TraceCategory.TOOL_USE: ("Tool", "magenta"),
    TraceCategory.WORKFLOW: ("Flow", "yellow"),
}

_STATUS_STYLES = {
    TraceStatus.STARTED: ("›", "bright_blue"),
    TraceStatus.PROGRESS: ("·", "blue"),
    TraceStatus.COMPLETED: ("✓", "green"),
    TraceStatus.WAITING: ("…", "yellow"),
    TraceStatus.FAILED: ("✗", "red"),
    TraceStatus.DENIED: ("!", "red"),
    TraceStatus.CANCELLED: ("×", "yellow"),
}

_AGENT_NAMES = {
    "main": "Main Agent",
    "issues": "Issues",
    "pull_requests": "Pull Requests",
    "repository": "Repository",
    "coding": "Coding",
    "github_mutator": "GitHub Mutator",
    "static_verifier": "Static Verifier",
}


class TerminalUI:
    """Small presentation layer: concise by default, complete trace on demand."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def user(self, message: str) -> None:
        """Render an explicit user message when callers need it outside the interactive prompt."""
        self.console.print(
            Panel.fit(
                Text(message),
                title=Text("You", style="bold"),
                title_align="left",
                border_style=_PANEL_STYLES["user"],
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def markdown(self, content: str, *, title: str, kind: str = "agent", subtitle: str | None = None) -> None:
        self.console.print(
            Panel(
                Markdown(content or "_无内容_"),
                title=Text(title, style="bold"),
                title_align="left",
                subtitle=Text(subtitle, style="dim") if subtitle else None,
                subtitle_align="right",
                border_style=_PANEL_STYLES[kind],
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )

    def text(self, content: str, *, title: str, kind: str = "info", subtitle: str | None = None) -> None:
        self.console.print(
            Panel(
                Text(content),
                title=Text(title, style="bold"),
                title_align="left",
                subtitle=Text(subtitle, style="dim") if subtitle else None,
                subtitle_align="right",
                border_style=_PANEL_STYLES[kind],
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )

    def json(self, value: Any, *, title: str) -> None:
        content = json.dumps(value, ensure_ascii=False, indent=2)
        self.console.print(
            Panel(
                Syntax(content, "json", word_wrap=True),
                title=Text(title, style="bold"),
                title_align="left",
                border_style=_PANEL_STYLES["info"],
                box=box.ROUNDED,
            )
        )

    def trace(self, event: TraceEvent) -> None:
        """Render a low-noise live trace.

        The TraceBus still records every event. The interactive stream intentionally
        hides successful completion echoes, task ids, long agent messages and raw
        argument JSON; `/trace` renders the complete history when needed.
        """
        if event.status == TraceStatus.COMPLETED:
            return
        if event.status == TraceStatus.PROGRESS and not event.message:
            return

        symbol, status_style = _STATUS_STYLES[event.status]
        if event.status == TraceStatus.STARTED:
            symbol = {
                TraceCategory.AGENT: "◇",
                TraceCategory.TOOL_USE: "↳",
                TraceCategory.WORKFLOW: "◆",
            }[event.category]
        line = Text("  ")
        line.append(f"{symbol} ", style=f"bold {status_style}")
        line.append(_display_name(event), style="bold" if event.category == TraceCategory.AGENT else "default")

        if event.status == TraceStatus.STARTED:
            arguments = event.details.get("arguments")
            if event.category == TraceCategory.TOOL_USE and isinstance(arguments, dict):
                summary = _compact_arguments(arguments)
                if summary:
                    line.append(f"  {summary}", style="dim")
        elif event.message and not (
            event.category == TraceCategory.AGENT and event.status == TraceStatus.WAITING
        ):
            line.append(f"  {_single_line(event.message, 140)}", style=status_style)

        self.console.print(line)

    def trace_history(self, events: list[TraceEvent]) -> None:
        """Render the complete audit-friendly trace, including ids/arguments/durations."""
        if not events:
            self.text("当前没有 Trace 事件。", title="Trace", kind="info")
            return
        self.console.print(
            Panel.fit(
                f"{len(events)} events · 完整 Session id / 参数 / 耗时",
                title="[bold]Trace[/bold]",
                title_align="left",
                border_style="dim",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        for event in events:
            self._trace_verbose(event)

    def debug_history(self, events: list[TraceEvent], *, session_id: str, agent: str | None = None) -> None:
        """Render bounded developer history without feeding it back into any agent context."""

        if not events:
            target = f"Agent {agent}" if agent else "当前 Session"
            self.text(f"{target} 没有可用的进程内 Debug History。", title="Debug", kind="info")
            return
        suffix = f" · agent={agent}" if agent else ""
        self.console.print(
            Panel.fit(
                f"{len(events)} events · {session_id}{suffix}\n"
                "[dim]只读诊断；源码正文/凭据会被省略或脱敏。[/dim]",
                title="[bold]Agent Debug History[/bold]",
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        for index, event in enumerate(events, 1):
            payload = _debug_payload(event)
            timestamp = event.timestamp[11:19] if len(event.timestamp) >= 19 else event.timestamp
            label = _TRACE_LABELS[event.category][0]
            title = f"{index:03d} · {timestamp} · {label} · {event.name} · {event.status.value}"
            self.console.print(
                Panel(
                    Syntax(json.dumps(payload, ensure_ascii=False, indent=2), "json", word_wrap=True),
                    title=title,
                    title_align="left",
                    border_style="dim",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )

    def _trace_verbose(self, event: TraceEvent) -> None:
        label, category_style = _TRACE_LABELS[event.category]
        symbol, status_style = _STATUS_STYLES[event.status]
        line = Text()
        line.append(f"{label:<6}", style=f"bold {category_style}")
        line.append(f" {symbol} ", style=f"bold {status_style}")
        line.append(event.name, style="bold")
        line.append(f"  [{event.session_id}]", style="dim")
        if event.message:
            line.append(f" · {_single_line(event.message, 240)}", style="default")
        arguments = event.details.get("arguments")
        if arguments and event.status == TraceStatus.STARTED:
            summary = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            line.append(f"  {summary[:240]}", style="dim")
        if event.details.get("debug_event") == "decision":
            decision = event.details.get("decision") or {}
            kind = decision.get("kind") if isinstance(decision, dict) else None
            if kind:
                line.append(f" · decision={kind}", style="dim")
        if event.duration_ms is not None:
            line.append(f"  {_duration(event.duration_ms)}", style="dim")
        self.console.print(line)


def _debug_payload(event: TraceEvent) -> dict[str, Any]:
    details = event.details
    payload: dict[str, Any] = {}
    if event.message:
        payload["message"] = event.message
    if event.duration_ms is not None:
        payload["duration_ms"] = round(event.duration_ms, 2)
    if event.category == TraceCategory.AGENT:
        for key in ("debug_event", "step", "decision", "context", "result", "error", "output_type"):
            if key in details:
                payload[key] = details[key]
        return payload
    if event.category == TraceCategory.TOOL_USE:
        payload["agent"] = details.get("agent")
        payload["arguments"] = details.get("debug_arguments", details.get("arguments", {}))
        for key in ("classification", "result", "error"):
            if key in details:
                payload[key] = details[key]
        return payload
    payload.update(details)
    return payload


def _display_name(event: TraceEvent) -> str:
    if event.category == TraceCategory.AGENT:
        return _AGENT_NAMES.get(event.name, event.name.replace("_", " ").title())
    return event.name


def _compact_arguments(arguments: dict[str, Any]) -> str:
    parts: list[str] = []
    if "issue_number" in arguments:
        parts.append(f"#{arguments['issue_number']}")
    if "pr_number" in arguments:
        parts.append(f"PR #{arguments['pr_number']}")
    if "path" in arguments and arguments.get("path"):
        parts.append(str(arguments["path"]))
    paths = arguments.get("paths")
    if isinstance(paths, (list, tuple)) and paths:
        parts.append(f"{len(paths)} files")
    query = arguments.get("query")
    if isinstance(query, str) and query:
        parts.append(f"query={_single_line(query, 36)!r}")
    state = arguments.get("state")
    if state and state != "open":
        parts.append(f"state={state}")
    depth = arguments.get("depth")
    if depth is not None and "path" not in arguments:
        parts.append(f"depth={depth}")
    return " · ".join(parts[:3])


def _single_line(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _duration(duration_ms: float) -> str:
    if duration_ms < 1:
        return "<1ms"
    if duration_ms < 1000:
        return f"{duration_ms:.0f}ms"
    return f"{duration_ms / 1000:.2f}s"
