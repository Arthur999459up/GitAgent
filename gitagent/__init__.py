"""GitAgent public API."""

from gitagent.application import CLIConfig, GitAgentService
from gitagent.domain.models import (
    AccessLevel,
    ApprovalIntent,
    CandidatePatch,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    DomainAction,
    IssueAgentResult,
    IssueOperation,
    IssueSummary,
    MainDecision,
    MutationRejectedResult,
    PullRequestAgentResult,
    PullRequestOperation,
    PullRequestSummary,
    Replacement,
    Route,
    RoutingContext,
    WorkflowTurnDecision,
)
from gitagent.harness import AgentHarness
from gitagent.harness.constraints import ApprovalRequest, ApprovalStore
from gitagent.harness.tools import MCPClient, ToolRegistry, ToolSpec
from gitagent.infra.observability import TraceBus, TraceCategory, TraceEvent, TraceStatus
from gitagent.infra.tool_hosts import GitHubMCPServer, MCPServer
from gitagent.model import ChatResponse, LiteLLMChatClient, LLMReasoner, OpenAIChatClient

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
    "CodeExplanationResult",
    "CodePlanResult",
    "CodeReviewResult",
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
    "MutationRejectedResult",
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
