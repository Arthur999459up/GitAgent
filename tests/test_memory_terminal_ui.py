from __future__ import annotations

from io import StringIO

from rich.console import Console

from gitagent.application.terminal_ui import TerminalUI
from gitagent.domain.models import SessionScope
from gitagent.infra.observability import TraceCategory, TraceEvent, TraceStatus
from gitagent.infra.persistence import SessionEventRecorder


def _event(
    name: str,
    status: TraceStatus,
    display_message: str,
) -> TraceEvent:
    return TraceEvent(
        timestamp="2026-08-30T00:00:00+00:00",
        session_id="session-" + "0" * 32,
        category=TraceCategory.WORKFLOW,
        name=name,
        status=status,
        display_message=display_message,
    )


def test_memory_status_uses_readable_labels_and_short_hints() -> None:
    output = StringIO()
    ui = TerminalUI(
        Console(file=output, color_system=None, force_terminal=False, width=240)
    )

    ui.trace(
        _event(
            "memory_extract",
            TraceStatus.STARTED,
            "正在检查本轮是否有值得长期保存的内容…",
        )
    )
    ui.trace(
        _event(
            "memory_extract",
            TraceStatus.COMPLETED,
            "已保存 1 条：private:中文回复偏好。",
        )
    )
    ui.trace(
        _event(
            "memory_dream",
            TraceStatus.COMPLETED,
            "整理完成，当前没有需要合并或停用的 Page。",
        )
    )

    rendered = output.getvalue()
    assert "◆ Memory Extract  正在检查本轮" in rendered
    assert "✓ Memory Extract  已保存 1 条：private:中文回复偏好。" in rendered
    assert "✓ Memory Dream  整理完成" in rendered
    assert "memory_extract" not in rendered
    assert "memory_dream" not in rendered


class _EventLog:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def append(self, _scope: SessionScope, _event_type: str, **kwargs: object) -> None:
        self.data = dict(kwargs.get("data", {}))


def test_display_hint_is_not_persisted_to_session_events() -> None:
    scope = SessionScope("account", "repository", "session-" + "0" * 32)
    event_log = _EventLog()
    recorder = SessionEventRecorder(event_log, lambda _: scope)  # type: ignore[arg-type]

    recorder(
        _event(
            "memory_extract",
            TraceStatus.COMPLETED,
            "仅供终端显示的记忆名称",
        )
    )

    assert "仅供终端显示的记忆名称" not in str(event_log.data)
