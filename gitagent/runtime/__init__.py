"""Agent 运行时：执行上下文、权限约束与通用 agent loop。"""

from .file_reads import FileReadLedger
from .harness import AgentContext, AgentHarness
from .loop import (
    AgentAction,
    AgentActionKind,
    AgentLoop,
    AgentLoopAgent,
    PendingAction,
    rejection_feedback,
    render_observations,
)
from .mutation import (
    GITHUB_MUTATOR_SPEC,
    code_change_approval_summary,
    code_change_mutation_plan,
    code_change_review_package,
    register_github_mutator,
)

__all__ = [
    "GITHUB_MUTATOR_SPEC",
    "AgentAction",
    "AgentActionKind",
    "AgentContext",
    "AgentHarness",
    "AgentLoop",
    "AgentLoopAgent",
    "FileReadLedger",
    "PendingAction",
    "code_change_approval_summary",
    "code_change_mutation_plan",
    "code_change_review_package",
    "register_github_mutator",
    "rejection_feedback",
    "render_observations",
]
