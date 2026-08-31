"""Action contracts exchanged between domain agents and the Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from gitagent.domain.models import PlannedCapabilityCall


class AgentActionKind(str, Enum):
    CAPABILITY = "capability"
    COMPLETE_ANALYSIS = "complete_analysis"
    PREPARE_CODE_CHANGE = "prepare_code_change"
    APPLY_ISSUE_FIX = "apply_issue_fix"
    APPLY_REPOSITORY_CHANGE = "apply_repository_change"
    ASK = "ask"
    FINISH = "finish"


@dataclass
class AgentAction:
    """A domain agent's requested next state transition."""

    kind: AgentActionKind
    summary: str = ""
    capability_id: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    question: str = ""
    message: str = ""


@dataclass
class PendingAction:
    """An exact set of capability calls waiting for user approval."""

    approval_id: str
    summary: str
    calls: list[PlannedCapabilityCall]


class AgentLoopAgent(Protocol):
    def decide(self, context: Any) -> AgentAction: ...

    def build_result(self, context: Any) -> Any: ...
