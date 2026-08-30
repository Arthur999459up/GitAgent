"""Shared contracts for agents, Session context, approval, and audit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class Route(str, Enum):
    ISSUE = "ISSUE"
    PULL_REQUEST = "PULL_REQUEST"
    REPOSITORY = "REPOSITORY"


class ApprovalIntent(str, Enum):
    """The user's natural-language intent toward an open proposal."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REVISE = "REVISE"
    QUESTION = "QUESTION"
    AMBIGUOUS = "AMBIGUOUS"


class Recommendation(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


@dataclass(frozen=True)
class SessionScope:
    """Immutable ownership boundary for one live, session-aware service."""

    account_key: str
    repository_key: str
    session_id: str


@dataclass(frozen=True)
class SessionEvent:
    """One observable, durable event in a Session event stream."""

    version: int
    seq: int
    type: str
    time: str
    session_id: str
    turn_seq: int | None
    agent: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class ResolvedReference:
    """A repository entity resolved from the user's request and Session context."""

    type: str
    id: str


@dataclass(frozen=True)
class AgentGuidance:
    """Validated, non-authoritative auxiliary data for a domain agent."""

    persistent_memory_index: str = ""
    persistent_memory_pages: str = ""
    resolved_references: tuple[ResolvedReference, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.persistent_memory_index
            or self.persistent_memory_pages
            or self.resolved_references
        )


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    name: str

    @classmethod
    def parse(cls, value: str) -> RepositoryRef:
        cleaned = value.strip().removesuffix(".git").strip("/")
        parts = cleaned.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repository must be the unambiguous 'owner/name' form")
        if any(part in {".", ".."} for part in parts):
            raise ValueError("invalid repository")
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class MainDecision:
    target_agent: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    request: str = ""
    message: str = ""
    clarify: bool = False
    requested_reply: bool = False


@dataclass(frozen=True)
class WorkflowTurnDecision:
    """Classification of a user's natural-language turn on an open proposal."""

    action: ApprovalIntent
    instruction: str = ""
    message: str = ""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    role: str
    system_prompt: str
    output_schema: tuple[str, ...]
    routes: frozenset[Route | str]
    required_context: tuple[str, ...] = ()
    routing_examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedCapabilityCall:
    capability_id: str
    arguments: dict[str, Any]


@dataclass
class VerificationCheck:
    name: str
    status: str
    details: str
    files: list[str] = field(default_factory=list)


@dataclass
class VerificationReport:
    passed: bool
    checks: list[VerificationCheck]
    skipped: list[str] = field(default_factory=list)
    attempts: int = 1


@dataclass(frozen=True)
class Replacement:
    path: str
    old: str
    new: str


@dataclass
class ChangeRequest:
    repository: str
    description: str
    base_branch: str = "main"
    target_files: list[str] = field(default_factory=list)
    replacements: list[Replacement] = field(default_factory=list)
    proposed_files: dict[str, str] = field(default_factory=dict)
    deleted_files: list[str] = field(default_factory=list)
    issue_number: int | None = None
    suggested_title: str | None = None
    source_ref: str | None = None


@dataclass
class CandidatePatch:
    summary: str
    root_cause: str
    added_files: list[str]
    modified_files: list[str]
    deleted_files: list[str]
    patch: str
    files: dict[str, str]
    static_checks: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    verification_required: list[str] = field(default_factory=list)

    @property
    def changed_files(self) -> list[str]:
        return sorted({*self.added_files, *self.modified_files, *self.deleted_files})


@dataclass(frozen=True)
class CandidatePreparationResult:
    candidate: CandidatePatch | None
    verification: VerificationReport | None = None
    message: str = ""
    capability_error: dict[str, Any] | None = None


@dataclass
class HumanReviewPackage:
    change_summary: str
    root_cause: str
    files_changed: list[str]
    important_diff: str
    static_verification: VerificationReport
    potential_risks: list[str]
    suggested_pr_title: str
    suggested_pr_description: str


@dataclass
class DraftResult:
    entity_type: str
    entity_id: str | None
    title: str
    body: str
    note: str = ""


@dataclass(frozen=True)
class MutationRejectedResult:
    """An approved remote mutation was rejected without breaking the Session."""

    summary: str
    reason: str


@dataclass
class CodeExplanationResult:
    behavior_changes: list[str]
    key_symbols: list[str]
    call_relationships: list[str]
    impact_scope: list[str]


@dataclass
class CodeReviewResult:
    summary: str
    blocking_issues: list[str]
    impacts: list[str]
    suggestions: list[str]
    test_assessment: str
    risk_level: str
    recommendation: Recommendation
    goal_alignment: str


@dataclass
class CodePlanResult:
    direction: str
    files: list[str]
    tradeoffs: list[str]
    tests: list[str]


class DomainAction(str, Enum):
    ANSWER = "ANSWER"
    CLARIFY = "CLARIFY"


class RepositoryOperation(str, Enum):
    EXPLORE = "EXPLORE"
    SEARCH = "SEARCH"
    EXPLAIN = "EXPLAIN"
    IMPACT_ANALYZE = "IMPACT_ANALYZE"
    PLAN = "PLAN"
    HISTORY = "HISTORY"
    MODIFY = "MODIFY"


@dataclass
class RepositoryResult:
    action: DomainAction
    operation: RepositoryOperation
    answer: str
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    reasoning: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    interpretation: CodeExplanationResult | None = None
    plan: CodePlanResult | None = None
    candidate: CandidatePatch | None = None
    verification: VerificationReport | None = None
    question: str = ""


class IssueOperation(str, Enum):
    CREATE = "CREATE"
    LIST = "LIST"
    GET = "GET"
    UPDATE = "UPDATE"
    SEARCH = "SEARCH"
    SUMMARIZE = "SUMMARIZE"


@dataclass(frozen=True)
class IssueSummary:
    number: int
    title: str
    state: str
    locked: bool
    labels: list[str]
    assignees: list[str]
    milestone: str | None
    author: str
    updated_at: str
    url: str


@dataclass
class IssueAgentResult:
    action: DomainAction
    operation: IssueOperation | None
    answer: str
    issues: list[IssueSummary]
    issue_number: int | None = None
    question: str = ""


class PullRequestOperation(str, Enum):
    LIST = "LIST"
    GET = "GET"
    SEARCH = "SEARCH"
    SUMMARIZE = "SUMMARIZE"
    EXPLAIN = "EXPLAIN"
    REVIEW = "REVIEW"
    REVIEW_DIALOGUE = "REVIEW_DIALOGUE"
    CI_ANALYZE = "CI_ANALYZE"
    PLAN = "PLAN"
    MODIFY = "MODIFY"
    CI_FIX = "CI_FIX"
    POST_REVIEW = "POST_REVIEW"
    MERGE_READINESS = "MERGE_READINESS"
    MERGE = "MERGE"


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    state: str
    author: str
    head: str
    base: str
    draft: bool
    updated_at: str
    url: str


@dataclass
class PullRequestAgentResult:
    action: DomainAction
    operation: PullRequestOperation | None
    answer: str
    pull_requests: list[PullRequestSummary]
    pr_number: int | None = None
    requested_outcome: str | None = None
    changed_files: list[str] = field(default_factory=list)
    interpretation: CodeExplanationResult | None = None
    review: CodeReviewResult | None = None
    review_dialogue: dict[str, list[str] | str] | None = None
    ci_analysis: dict[str, list[str]] | None = None
    plan: CodePlanResult | None = None
    candidate: CandidatePatch | None = None
    verification: VerificationReport | None = None
    merge_readiness: str = ""
    execution_result: dict[str, Any] | None = None
    question: str = ""


def to_plain(value: Any) -> Any:
    """Convert dataclass/enum output to JSON-compatible builtins."""
    if is_dataclass(value):
        return to_plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_plain(item) for item in value]
    return value
