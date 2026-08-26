"""Action contracts exchanged between domain agents and the Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from gitagent.domain.models import PlannedToolCall


class AgentActionKind(str, Enum):
    TOOL = "tool"
    APPLY_ISSUE_FIX = "apply_issue_fix"
    APPLY_REPOSITORY_CHANGE = "apply_repository_change"
    ASK = "ask"
    FINISH = "finish"


@dataclass
class AgentAction:
    """A domain agent's requested next state transition."""

    kind: AgentActionKind
    summary: str = ""
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""
    message: str = ""


@dataclass
class PendingAction:
    """An exact set of tool calls waiting for user approval."""

    approval_id: str
    summary: str
    calls: list[PlannedToolCall]


class AgentLoopAgent(Protocol):
    def decide(self, context: Any) -> AgentAction: ...

    def build_result(self, context: Any) -> Any: ...
