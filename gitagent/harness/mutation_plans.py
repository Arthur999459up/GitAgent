"""Deterministic capability-call plan builders for reviewed code changes."""

from __future__ import annotations

from gitagent.domain.errors import WorkflowError
from gitagent.domain.models import (
    CandidatePatch,
    ChangeRequest,
    HumanReviewPackage,
    PlannedCapabilityCall,
    VerificationReport,
)


def issue_fix_mutation_plan(
    session_id: str,
    request: ChangeRequest,
    candidate: CandidatePatch,
    review: HumanReviewPackage,
) -> list[PlannedCapabilityCall]:
    """Build the fixed mutation plan for applying a verified code change."""
    suffix = session_id.removeprefix("session-")[:32]
    branch = f"gitagent/{suffix}"
    return [
        PlannedCapabilityCall(
            "github.create_branch",
            {"base": request.base_branch, "branch": branch},
        ),
        PlannedCapabilityCall(
            "github.commit",
            {
                "branch": branch,
                "files": candidate.files,
                "deleted_files": candidate.deleted_files,
                "message": candidate.summary,
            },
        ),
        PlannedCapabilityCall("github.push", {"branch": branch}),
        PlannedCapabilityCall(
            "github.create_draft_pr",
            {
                "title": review.suggested_pr_title,
                "body": review.suggested_pr_description,
                "base": request.base_branch,
                "head": branch,
                "draft": True,
            },
        ),
    ]


def repository_change_mutation_plan(
    request: ChangeRequest,
    candidate: CandidatePatch,
) -> list[PlannedCapabilityCall]:
    """Build one atomic default-branch commit for a RepositoryAgent change."""
    if not request.source_ref:
        raise WorkflowError("repository change requires the reviewed default-branch head SHA")
    return [
        PlannedCapabilityCall(
            "github.commit_to_default_branch",
            {
                "expected_head_sha": request.source_ref,
                "files": candidate.files,
                "deleted_files": candidate.deleted_files,
                "message": candidate.summary,
            },
        )
    ]


def repository_change_approval_summary(
    request: ChangeRequest,
    candidate: CandidatePatch,
    report: VerificationReport,
) -> str:
    checks = ", ".join(f"{check.name}={check.status}" for check in report.checks) or "None"
    return (
        f"What will happen: create one commit directly on the default branch `{request.base_branch}`.\n"
        f"Affected repository: {request.repository}\n"
        f"Added files: {', '.join(candidate.added_files) or 'None'}\n"
        f"Modified files: {', '.join(candidate.modified_files) or 'None'}\n"
        f"Deleted files: {', '.join(candidate.deleted_files) or 'None'}\n"
        f"Commit message: {candidate.summary}\n"
        f"Verification result: {checks}\n"
        f"Risk: {'; '.join(candidate.risks) or 'No specific static risk identified.'}"
    )


def code_change_review_package(
    request: ChangeRequest,
    candidate: CandidatePatch,
    report: VerificationReport,
) -> HumanReviewPackage:
    """Build the human review package shown with the single apply approval."""
    title = (request.suggested_title or candidate.summary or request.description).strip().splitlines()[0]
    if request.issue_number is not None and f"#{request.issue_number}" not in title:
        title = f"Fix #{request.issue_number}: {title}"
    title = title.strip().splitlines()[0][:120]
    checks = "\n".join(f"- {check.name}: {check.status} — {check.details}" for check in report.checks) or "- None"
    files = "\n".join(f"- `{path}`" for path in candidate.changed_files) or "- None"
    risks = "\n".join(f"- {risk}" for risk in candidate.risks) or "- No specific static risk identified."
    follow_up = (
        "\n".join(f"- {item}" for item in candidate.verification_required)
        or "- Run the repository test suite and complete human review."
    )
    related_issue = f"\n\n## Related issue\n#{request.issue_number}" if request.issue_number is not None else ""
    body = (
        f"## Summary\n{candidate.summary}\n\n"
        f"## Root cause\n{candidate.root_cause}\n\n"
        f"## Changed files\n{files}\n\n"
        f"## Static verification\n{checks}\n\n"
        f"## Risks\n{risks}\n\n"
        f"## Verification still required\n{follow_up}"
        f"{related_issue}\n\n"
        "> This pull request is intentionally a draft. GitAgent performed only the static checks listed above."
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


def issue_fix_approval_summary(request: ChangeRequest, review: HumanReviewPackage) -> str:
    checks = ", ".join(f"{check.name}={check.status}" for check in review.static_verification.checks)
    return (
        "What will happen: create a branch, commit the reviewed candidate, push it, and create a Draft PR.\n"
        f"Affected repository: {request.repository}\n"
        f"Affected files/resources: {', '.join(review.files_changed)}\n"
        f"Draft PR title: {review.suggested_pr_title}\n"
        f"Proposed content/change: {review.change_summary}\n"
        f"Verification result: {checks}\n"
        f"Risk: {'; '.join(review.potential_risks) or 'No specific static risk identified.'}"
    )
