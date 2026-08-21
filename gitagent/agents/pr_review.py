"""Read-only pull-request review based on diff and targeted context."""

from __future__ import annotations

import json
import re

from ..core.models import (
    AgentGuidance,
    AgentSpec,
    PRReviewResult,
    Recommendation,
)
from ..prompts import get_prompt_library
from ..reasoning import Reasoner
from ..runtime import AgentContext, AgentHarness
from .guidance import guidance_section

_PROMPTS = get_prompt_library()

PR_REVIEW_SPEC = AgentSpec(
    name="pr_review",
    role="Review pull requests from metadata, diffs, changed files, and bounded repository context.",
    system_prompt=_PROMPTS.text("system.pr_review"),
    allowed_tools=frozenset(
        {
            "github.get_pr",
            "github.get_pr_comments",
            "repository.get_repo_tree",
            "repository.get_pr_diff",
            "repository.get_changed_files",
            "repository.read_files",
        }
    ),
    output_schema=(
        "summary",
        "important_changes",
        "risk_level",
        "potential_issues",
        "test_assessment",
        "recommendation",
    ),
    capabilities=frozenset({"pr_review"}),
)


class PRReviewAgent:
    def __init__(self, harness: AgentHarness, reasoner: Reasoner | None = None) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(PR_REVIEW_SPEC)

    def review(
        self,
        repository: str,
        pr_number: int,
        *,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> PRReviewResult:
        return self.harness.run(
            "pr_review",
            session_id=session_id,
            operation=lambda context: self._review(context, repository, pr_number, guidance),
            repository=repository,
            goal=f"Review Pull Request #{pr_number}",
            entity_type="pull_request",
            entity_id=str(pr_number),
            guidance=guidance,
        )

    def _review(
        self,
        context: AgentContext,
        repository: str,
        pr_number: int,
        guidance: AgentGuidance | None,
    ) -> PRReviewResult:
        metadata = context.tool("github.get_pr", repository=repository, pr_number=pr_number)
        head = metadata.get("head") or {}
        head_ref = str(head.get("sha") or head.get("ref") or "") if isinstance(head, dict) else str(head)
        tree = context.tool(
            "repository.get_repo_tree", repository=repository, depth=2, ref=head_ref or None
        )
        diff = context.tool("repository.get_pr_diff", repository=repository, pr_number=pr_number)["diff"]
        changed_files = context.tool(
            "repository.get_changed_files", repository=repository, pr_number=pr_number
        )["files"]
        readable = [path for path in changed_files if path in set(tree["entries"])]
        files = (
            context.tool(
                "repository.read_files",
                repository=repository,
                requests=[{"path": path, "limit": 180} for path in readable[:12]],
                ref=head_ref or None,
            )["files"]
            if readable
            else []
        )
        comments = context.tool("github.get_pr_comments", repository=repository, pr_number=pr_number)["comments"]
        evidence = {
            "metadata": metadata,
            "diff": diff,
            "changed_files": changed_files,
            "files": files,
            "comments": comments[:30],
        }

        if self.reasoner:
            value = self.reasoner.complete_structured(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.pr_review",
                    pr_number=pr_number,
                    repository=repository,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(guidance),
                ),
            )
            try:
                recommendation = Recommendation(str(value.get("recommendation")))
            except ValueError:
                recommendation = Recommendation.NEEDS_HUMAN_REVIEW
            return PRReviewResult(
                summary=str(value.get("summary", "")),
                important_changes=[str(item) for item in value.get("important_changes", changed_files)],
                risk_level=str(value.get("risk_level", "MEDIUM")).upper(),
                potential_issues=[str(item) for item in value.get("potential_issues", [])],
                test_assessment=str(value.get("test_assessment", "Not assessed")),
                recommendation=recommendation,
            )

        issues = self._detect_issues(diff)
        source_files = [path for path in changed_files if not self._is_test(path)]
        test_files = [path for path in changed_files if self._is_test(path)]
        sensitive = any(
            re.search(r"(?:auth|security|permission|migration|schema|payment)", path, re.IGNORECASE)
            for path in changed_files
        )
        risk = (
            "HIGH" if sensitive or len(changed_files) > 20 else "MEDIUM" if issues or len(changed_files) > 5 else "LOW"
        )
        if source_files and not test_files:
            test_assessment = (
                "Production files changed with no test-file changes visible in the diff; tests were not executed and human "
                "verification is required."
            )
        elif test_files:
            test_assessment = f"Static diff includes {len(test_files)} test file(s); tests were not executed."
        else:
            test_assessment = (
                "No production code or tests were identified from the changed paths; tests were not executed."
            )
        recommendation = (
            Recommendation.REQUEST_CHANGES
            if issues
            else Recommendation.NEEDS_HUMAN_REVIEW
            if risk == "HIGH"
            else Recommendation.APPROVE
        )
        return PRReviewResult(
            summary=str(metadata.get("title") or f"PR #{pr_number}") + f" changes {len(changed_files)} file(s).",
            important_changes=changed_files,
            risk_level=risk,
            potential_issues=issues,
            test_assessment=test_assessment,
            recommendation=recommendation,
        )

    @staticmethod
    def _detect_issues(diff: str) -> list[str]:
        added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        checks = [
            (r"except\s+Exception\s*:\s*(?:pass)?", "Broad exception handling may hide failures."),
            (r"\beval\s*\(", "New eval() usage can execute untrusted input."),
            (r"(?i)(?:api[_-]?key|secret|token)\s*=\s*['\"][^'\"]+", "Possible hard-coded credential in added lines."),
            (r"verify\s*=\s*False", "TLS verification appears to be disabled."),
        ]
        return [message for pattern, message in checks if re.search(pattern, added)]

    @staticmethod
    def _is_test(path: str) -> bool:
        return bool(re.search(r"(^|/)(tests?|specs?)(/|$)|(?:_test|\.test|\.spec)\.", path, re.IGNORECASE))
