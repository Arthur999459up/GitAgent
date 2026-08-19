"""Read-only CI failure diagnosis."""

from __future__ import annotations

import json
import re

from ..core.errors import ToolExecutionError
from ..core.models import (
    AgentGuidance,
    AgentSpec,
    CIDiagnosisResult,
    Route,
)
from ..prompts import get_prompt_library
from ..reasoning import Reasoner
from ..runtime import AgentContext, AgentHarness
from .guidance import guidance_section

_PROMPTS = get_prompt_library()

CI_DIAGNOSIS_SPEC = AgentSpec(
    name="ci_diagnosis",
    role="Map failed CI jobs and logs to likely repository causes.",
    system_prompt=_PROMPTS.text("system.ci_diagnosis"),
    allowed_tools=frozenset(
        {
            "github.get_workflow_runs",
            "github.get_job_logs",
            "repository.get_repo_tree",
            "repository.search_code",
            "repository.read_files",
        }
    ),
    output_schema=(
        "failed_job",
        "failure_summary",
        "relevant_log",
        "suspected_files",
        "probable_root_cause",
        "suggested_fix",
        "confidence",
    ),
    capabilities=frozenset({Route.CI_DIAGNOSIS}),
    required_context=("repository",),
    routing_examples=(
        "最近一次 CI 为什么失败？",
        "诊断 workflow run #123，并给出修复建议",
    ),
)


class CIDiagnosisAgent:
    def __init__(self, harness: AgentHarness, reasoner: Reasoner | None = None) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(CI_DIAGNOSIS_SPEC)

    def diagnose(
        self,
        repository: str,
        *,
        pr_number: int | None = None,
        workflow_run_id: int | None = None,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> CIDiagnosisResult:
        return self.harness.run(
            "ci_diagnosis",
            session_id=session_id,
            operation=lambda context: self._diagnose(context, repository, pr_number, workflow_run_id, guidance),
        )

    def _diagnose(
        self,
        context: AgentContext,
        repository: str,
        pr_number: int | None,
        workflow_run_id: int | None,
        guidance: AgentGuidance | None,
    ) -> CIDiagnosisResult:
        runs = context.tool(
            "github.get_workflow_runs",
            repository=repository,
            pr_number=pr_number,
            workflow_run_id=workflow_run_id,
        )["runs"]
        failed_runs = [
            run for run in runs if str(run.get("conclusion", run.get("status", ""))).casefold() in {"failure", "failed"}
        ]
        if not failed_runs:
            raise ToolExecutionError("no failed workflow run matched the supplied context")
        run = failed_runs[0]
        jobs_result = context.tool("github.get_job_logs", repository=repository, run_id=int(run["id"]))
        failed_jobs = [
            job
            for job in jobs_result["jobs"]
            if str(job.get("conclusion", job.get("status", ""))).casefold() in {"failure", "failed"}
        ]
        if not failed_jobs:
            raise ToolExecutionError("failed workflow contains no failed job logs")
        job = failed_jobs[0]
        log = str(job.get("log", ""))
        relevant_log = self._relevant_log(log)
        tree = context.tool("repository.get_repo_tree", repository=repository, depth=4)
        tree_paths = set(tree["entries"])
        suspected = [path for path in self._paths_from_log(log) if path in tree_paths][:8]
        if not suspected:
            token = self._error_token(log)
            search = context.tool("repository.search_code", repository=repository, query=token, max_results=10)
            suspected = list(dict.fromkeys(hit["path"] for hit in search["results"]))[:8]
        files = (
            context.tool(
                "repository.read_files", repository=repository, paths=suspected, limit_per_file=180
            )["files"]
            if suspected
            else []
        )
        evidence = {
            "run": run,
            "job": {k: v for k, v in job.items() if k != "log"},
            "log": relevant_log,
            "files": files,
        }

        if self.reasoner:
            value = self.reasoner.complete_structured(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.ci_diagnosis",
                    repository=repository,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(guidance),
                ),
            )
            return CIDiagnosisResult(
                failed_job=str(value.get("failed_job", job.get("name", "unknown"))),
                failure_summary=str(value.get("failure_summary", "")),
                relevant_log=str(value.get("relevant_log", relevant_log)),
                suspected_files=[str(item) for item in value.get("suspected_files", suspected)],
                probable_root_cause=str(value.get("probable_root_cause", "")),
                suggested_fix=str(value.get("suggested_fix", "")),
                confidence=max(0.0, min(float(value.get("confidence", 0.5)), 1.0)),
            )

        last_error = next(
            (
                line.strip()
                for line in reversed(log.splitlines())
                if re.search(r"error|failed|exception", line, re.IGNORECASE)
            ),
            "CI job failed",
        )
        root = f"The first failed job reports: {last_error}"
        if suspected:
            root += f"; the log points to {', '.join(suspected)}."
        return CIDiagnosisResult(
            failed_job=str(job.get("name", job.get("id", "unknown"))),
            failure_summary=last_error,
            relevant_log=relevant_log,
            suspected_files=suspected,
            probable_root_cause=root,
            suggested_fix="Inspect the cited lines and apply any fix through the code-change workflow, followed by static verification.",
            confidence=0.8 if suspected else 0.45,
        )

    @staticmethod
    def _paths_from_log(log: str) -> list[str]:
        matches = re.findall(r"(?<![\w.-])([\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|json|toml|ya?ml))(?::\d+)?", log)
        return list(dict.fromkeys(path.lstrip("./") for path in matches))

    @staticmethod
    def _error_token(log: str) -> str:
        identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", log)
        stop = {"error", "failed", "failure", "exception", "traceback", "assertion"}
        candidates = [item for item in reversed(identifiers) if item.casefold() not in stop]
        return candidates[0] if candidates else "TODO"

    @staticmethod
    def _relevant_log(log: str) -> str:
        lines = log.splitlines()
        error_indexes = [
            index
            for index, line in enumerate(lines)
            if re.search(r"error|failed|exception|traceback", line, re.IGNORECASE)
        ]
        if not error_indexes:
            return "\n".join(lines[-40:])[-12_000:]
        start = max(0, error_indexes[0] - 5)
        end = min(len(lines), error_indexes[-1] + 8)
        return "\n".join(lines[start:end])[-12_000:]
