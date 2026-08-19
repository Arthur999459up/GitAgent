"""Static-only verification agent."""

from __future__ import annotations

from ..core.models import AgentSpec, CandidatePatch, VerificationCheck, VerificationReport
from ..prompts import get_prompt_library
from ..runtime import AgentContext, AgentHarness

_PROMPTS = get_prompt_library()

VERIFICATION_SPEC = AgentSpec(
    name="static_verifier",
    role="Run syntax, lint, and static analysis only on candidate files.",
    system_prompt=_PROMPTS.text("system.static_verifier"),
    allowed_tools=frozenset({"verification.run_lint", "verification.run_static_check"}),
    output_schema=("passed", "checks", "skipped", "attempts"),
    capabilities=frozenset({"static_verification"}),
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
        )

    @staticmethod
    def _verify(context: AgentContext, candidate: CandidatePatch, attempts: int) -> VerificationReport:
        static = context.tool("verification.run_static_check", files=candidate.files)
        lint = context.tool("verification.run_lint", files=candidate.files)
        checks = [
            VerificationCheck(
                name="syntax_and_static_analysis",
                status="PASS" if static["passed"] else "FAIL",
                details="; ".join(static.get("errors", []))
                or "Changed files parsed successfully; no conflict markers found.",
                files=static.get("files", []),
            ),
            VerificationCheck(
                name="bounded_lint",
                status="PASS" if lint["passed"] else "WARN",
                details="; ".join(lint.get("errors", [])) or "No bounded lint findings.",
                files=lint.get("files", []),
            ),
        ]
        return VerificationReport(
            passed=bool(static["passed"]),
            checks=checks,
            skipped=list(static.get("skipped", [])),
            attempts=attempts,
        )
