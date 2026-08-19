"""Exact-approval GitHub mutation executor specification and code-change plan builders."""

from __future__ import annotations

from ..core.models import (
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    HumanReviewPackage,
    PlannedToolCall,
    VerificationReport,
)
from ..prompts import get_prompt_library
from .harness import AgentHarness

_PROMPTS = get_prompt_library()

GITHUB_MUTATOR_SPEC = AgentSpec(
    name="github_mutator",
    role="Execute only an exact, already-approved GitHub mutation plan.",
    system_prompt=_PROMPTS.text("system.github_mutator"),
    allowed_tools=frozenset(
        {
            "github.post_comment",
            "github.update_issue",
            "github.update_pr",
            "github.create_branch",
            "github.commit",
            "github.push",
            "github.create_draft_pr",
            "github.post_review",
            "github.merge",
        }
    ),
    output_schema=(),
    capabilities=frozenset({"github_mutation"}),
)


def register_github_mutator(harness: AgentHarness) -> None:
    harness.register(GITHUB_MUTATOR_SPEC)


def code_change_mutation_plan(
    session_id: str,
    request: ChangeRequest,
    candidate: CandidatePatch,
    review: HumanReviewPackage,
) -> list[PlannedToolCall]:
    """Build the fixed mutation plan for applying a verified code change."""
    del review
    suffix = session_id.removeprefix("session-")[:32]
    branch = f"gitagent/{suffix}"
    return [
        PlannedToolCall(
            "github.create_branch",
            {"repository": request.repository, "base": request.base_branch, "branch": branch},
        ),
        PlannedToolCall(
            "github.commit",
            {
                "repository": request.repository,
                "branch": branch,
                "files": candidate.files,
                "message": candidate.summary,
            },
        ),
        PlannedToolCall("github.push", {"repository": request.repository, "branch": branch}),
        PlannedToolCall(
            "github.create_draft_pr",
            {
                "repository": request.repository,
                "title": candidate.summary,
                "body": _draft_body(request, candidate),
                "base": request.base_branch,
                "head": branch,
                "draft": True,
            },
        ),
    ]


def _draft_body(request: ChangeRequest, candidate: CandidatePatch) -> str:
    return (
        f"## Summary\n{candidate.summary}\n\n"
        f"## Root cause\n{candidate.root_cause}\n\n"
        "Tests were not run by GitAgent. This pull request is intentionally a draft."
    )


def code_change_review_package(
    request: ChangeRequest,
    candidate: CandidatePatch,
    report: VerificationReport,
) -> HumanReviewPackage:
    """Build the human review package shown with the single apply approval."""
    title = request.suggested_title or (
        f"Fix #{request.issue_number}: {request.description}" if request.issue_number else request.description
    )
    title = title.strip().splitlines()[0][:120]
    checks = "\n".join(f"- {check.name}: {check.status} — {check.details}" for check in report.checks)
    body = (
        f"## Summary\n{candidate.summary}\n\n"
        f"## Root cause\n{candidate.root_cause}\n\n"
        f"## Static verification\n{checks}\n\n"
        "Tests were not run by GitAgent. This pull request is intentionally a draft."
    )
    return HumanReviewPackage(
        change_summary=candidate.summary,
        root_cause=candidate.root_cause,
        files_changed=candidate.changed_files,
        important_diff=candidate.patch[:30_000],
        static_verification=report,
        potential_risks=candidate.risks,
        suggested_pr_title=title,
        suggested_pr_description=body,
    )


def code_change_approval_summary(request: ChangeRequest, review: HumanReviewPackage) -> str:
    checks = ", ".join(f"{check.name}={check.status}" for check in review.static_verification.checks)
    return (
        "What will happen: create a branch, commit the reviewed candidate, push it, and create a Draft PR.\n"
        f"Affected repository: {request.repository}\n"
        f"Affected files/resources: {', '.join(review.files_changed)}\n"
        f"Proposed content/change: {review.change_summary}\n"
        f"Verification result: {checks}\n"
        f"Risk: {'; '.join(review.potential_risks) or 'No specific static risk identified.'}"
    )
