"""GitAgent stable public API."""

from .app import CLIConfig, GitAgentService
from .core import ApprovalRequest, ApprovalStore
from .core.models import (
    AccessLevel,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    DomainAction,
    IssueAgentResult,
    IssueOperation,
    IssueSummary,
    MainDecision,
    PullRequestAgentResult,
    PullRequestOperation,
    PullRequestSummary,
    Replacement,
    Route,
    RoutingContext,
    WorkflowTurnDecision,
)
from .core.trace import TraceBus, TraceCategory, TraceEvent, TraceStatus
from .mcp import GitHubMCPServer, MCPClient, MCPServer, ToolRegistry, ToolSpec
from .reasoning import ChatResponse, LiteLLMChatClient, LLMReasoner, OpenAIChatClient
from .runtime import AgentHarness

__version__ = "0.1.0"

__all__ = [
    "AccessLevel",
    "AgentHarness",
    "ApprovalIntent",
    "ApprovalRequest",
    "ApprovalStore",
    "CLIConfig",
    "CandidatePatch",
    "ChangeRequest",
    "ChatResponse",
    "DomainAction",
    "GitAgentService",
    "GitHubMCPServer",
    "IssueAgentResult",
    "IssueOperation",
    "IssueSummary",
    "LLMReasoner",
    "LiteLLMChatClient",
    "MCPClient",
    "MCPServer",
    "MainDecision",
    "OpenAIChatClient",
    "PullRequestAgentResult",
    "PullRequestOperation",
    "PullRequestSummary",
    "Replacement",
    "Route",
    "RoutingContext",
    "ToolRegistry",
    "ToolSpec",
    "TraceBus",
    "TraceCategory",
    "TraceEvent",
    "TraceStatus",
    "WorkflowTurnDecision",
    "__version__",
]
