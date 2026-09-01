"""Static-only verification agent."""

from __future__ import annotations

import ast
import json
import re
import tomllib

import yaml

from gitagent.domain.models import (
    AgentSpec,
    CandidatePatch,
    VerificationCheck,
    VerificationReport,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness, ExecutionProfile
from gitagent.harness.file_access import safe_repository_path

VERIFICATION_SPEC = AgentSpec(
    name="static_verifier",
    role="Run syntax, lint, and static analysis only on candidate files.",
    system_prompt="Deterministic verifier; no model interaction.",
    output_schema=("passed", "checks", "skipped", "attempts"),
    agent_depth=0,
    execution_profile=ExecutionProfile.unknown(),
)


class StaticVerifier:
    def __init__(self, harness: AgentHarness) -> None:
        self.harness = harness
        harness.register(VERIFICATION_SPEC)

    def verify(self, candidate: CandidatePatch, *, session_id: str, attempts: int = 1) -> VerificationReport:
        return self.harness.run(
            "static_verifier",
            session_id=session_id,
            operation=lambda context: self._verify(context, candidate, attempts),
            goal=f"Static verification: {candidate.summary}",
        )

    @staticmethod
    def _verify(context: AgentContext, candidate: CandidatePatch, attempts: int) -> VerificationReport:
        del context
        static_errors: list[str] = []
        lint_errors: list[str] = []
        skipped: list[str] = []
        files = sorted(candidate.files)
        for raw_path, content in candidate.files.items():
            path = safe_repository_path(raw_path)
            try:
                if path.endswith(".py"):
                    ast.parse(content, filename=path)
                elif path.endswith(".json"):
                    json.loads(content)
                elif path.endswith(".toml"):
                    tomllib.loads(content)
                elif path.endswith((".yaml", ".yml")):
                    yaml.safe_load(content)
            except (SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
                static_errors.append(f"{path}: {exc}")
            if re.search(r"^(?:<{7}|={7}|>{7})", content, re.MULTILINE):
                static_errors.append(f"{path}: unresolved merge-conflict marker")
            for number, line in enumerate(content.splitlines(), 1):
                if line.rstrip() != line:
                    lint_errors.append(f"{path}:{number}: trailing whitespace")
                if "\t" in line and path.endswith(".py"):
                    lint_errors.append(f"{path}:{number}: tab indentation in Python")
                if len(line) > 160:
                    lint_errors.append(f"{path}:{number}: line exceeds 160 characters")
        skipped.append("type check: no repository-specific type-checker configuration was provided")
        checks = [
            VerificationCheck(
                name="syntax_and_static_analysis",
                status="PASS" if not static_errors else "FAIL",
                details="; ".join(static_errors)
                or "Changed files parsed successfully; no conflict markers found.",
                files=files,
            ),
            VerificationCheck(
                name="bounded_lint",
                status="PASS" if not lint_errors else "WARN",
                details="; ".join(lint_errors) or "No bounded lint findings.",
                files=files,
            ),
        ]
        return VerificationReport(
            passed=not static_errors,
            checks=checks,
            skipped=skipped,
            attempts=attempts,
        )
