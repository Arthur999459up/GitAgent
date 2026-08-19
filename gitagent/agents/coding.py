"""Candidate-patch agent. It has no GitHub mutation tools or test runner."""

from __future__ import annotations

import difflib
import json
from typing import Any

from ..core.errors import ValidationError, WorkflowError
from ..core.models import (
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    Route,
    VerificationReport,
)
from ..prompts import get_prompt_library
from ..reasoning import Reasoner
from ..runtime import AgentContext, AgentHarness
from .guidance import guidance_section

_PROMPTS = get_prompt_library()

CODING_SPEC = AgentSpec(
    name="coding",
    role="Read targeted repository context and return a minimal candidate patch.",
    system_prompt=_PROMPTS.text("system.coding"),
    allowed_tools=frozenset(
        {
            "repository.get_repo_tree",
            "repository.search_code",
            "repository.read_file",
            "repository.read_files",
            "repository.find_symbol",
            "repository.find_references",
        }
    ),
    output_schema=(
        "summary",
        "root_cause",
        "changed_files",
        "patch",
        "static_checks",
        "risks",
        "verification_required",
    ),
    capabilities=frozenset({Route.CODE_CHANGE}),
    required_context=("repository",),
    routing_examples=(
        "修复登录接口的空指针问题",
        "为配置加载器增加超时参数",
    ),
)


class CodingAgent:
    def __init__(self, harness: AgentHarness, reasoner: Reasoner | None = None) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(CODING_SPEC)

    def create_candidate(
        self,
        request: ChangeRequest,
        *,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> CandidatePatch:
        return self.harness.run(
            "coding",
            session_id=session_id,
            operation=lambda context: self._create(context, request, guidance),
            repository=request.repository,
            goal=request.description,
            entity_type="issue" if request.issue_number is not None else None,
            entity_id=str(request.issue_number) if request.issue_number is not None else None,
            guidance=guidance,
        )

    def repair_candidate(
        self,
        request: ChangeRequest,
        candidate: CandidatePatch,
        errors: list[str],
        *,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> CandidatePatch:
        if not self.reasoner:
            return candidate
        return self.harness.run(
            "coding",
            session_id=session_id,
            operation=lambda context: self._repair(context, request, candidate, errors, guidance),
            repository=request.repository,
            goal=f"Repair candidate: {request.description}",
            entity_type="issue" if request.issue_number is not None else None,
            entity_id=str(request.issue_number) if request.issue_number is not None else None,
            guidance=guidance,
        )

    def _create(
        self,
        context: AgentContext,
        request: ChangeRequest,
        guidance: AgentGuidance | None,
    ) -> CandidatePatch:
        tree = context.tool("repository.get_repo_tree", repository=request.repository, depth=4)
        known_paths = set(tree["entries"])
        paths = list(dict.fromkeys(request.target_files + [replacement.path for replacement in request.replacements]))
        existing_paths = [path for path in paths if path in known_paths]
        if not existing_paths and not request.proposed_files:
            query = self._search_term(request.description)
            hits = context.tool("repository.search_code", repository=request.repository, query=query, max_results=15)["results"]
            existing_paths = list(dict.fromkeys(hit["path"] for hit in hits))[:6]
        fetched = (
            context.tool(
                "repository.read_files",
                repository=request.repository,
                paths=existing_paths,
                limit_per_file=400,
            )["files"]
            if existing_paths
            else []
        )
        if any(item.get("truncated") for item in fetched):
            raise WorkflowError("a target file exceeds the safe fetch bound; refusing to risk a truncated overwrite")
        originals = {item["path"]: item["content"] for item in fetched}

        if request.proposed_files:
            new_files = dict(request.proposed_files)
            missing_context = [path for path in new_files if path in known_paths and path not in originals]
            if missing_context:
                more = context.tool(
                    "repository.read_files",
                    repository=request.repository,
                    paths=missing_context,
                    limit_per_file=400,
                )["files"]
                if any(item.get("truncated") for item in more):
                    raise WorkflowError(
                        "a target file exceeds the safe fetch bound; refusing to risk a truncated overwrite"
                    )
                originals.update({item["path"]: item["content"] for item in more})
        elif request.replacements:
            new_files = dict(originals)
            for replacement in request.replacements:
                if replacement.path not in new_files:
                    raise WorkflowError(f"replacement target was not fetched: {replacement.path}")
                count = new_files[replacement.path].count(replacement.old)
                if count != 1:
                    raise WorkflowError(
                        f"replacement anchor in {replacement.path} must match exactly once; found {count}"
                    )
                new_files[replacement.path] = new_files[replacement.path].replace(replacement.old, replacement.new, 1)
        elif self.reasoner:
            value = self.reasoner.complete_structured(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.coding_create",
                    repository=request.repository,
                    description=request.description,
                    files=json.dumps(originals, ensure_ascii=False),
                    guidance=guidance_section(guidance),
                ),
            )
            new_files = {str(path): str(content) for path, content in value.get("files", {}).items()}
            outside = set(new_files) - set(originals)
            if outside:
                raise ValidationError(f"reasoner changed unfetched files: {', '.join(sorted(outside))}")
            return self._candidate(
                originals,
                new_files,
                summary=str(value.get("summary", request.description)),
                root_cause=str(value.get("root_cause", request.description)),
                risks=[str(item) for item in value.get("risks", [])],
                verification_required=[str(item) for item in value.get("verification_required", [])],
            )
        else:
            raise WorkflowError(
                "coding without a reasoner requires explicit replacements/proposed_files; configure LLMReasoner otherwise"
            )

        return self._candidate(
            originals,
            new_files,
            summary=request.description,
            root_cause=f"The requested behavior requires a minimal targeted change: {request.description}",
            risks=["Only static checks are performed; runtime behavior still needs human verification."],
            verification_required=["Human review", "Project tests after the draft PR is created"],
        )

    def _repair(
        self,
        context: AgentContext,
        request: ChangeRequest,
        candidate: CandidatePatch,
        errors: list[str],
        guidance: AgentGuidance | None,
    ) -> CandidatePatch:
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.coding_repair",
                description=request.description,
                files=json.dumps(candidate.files, ensure_ascii=False),
                # Keep the default json.dumps flags here: the error text is ASCII-safe
                # by construction and the byte identity of the rendered prompt must not change.
                errors=json.dumps(errors),
                guidance=guidance_section(guidance),
            ),
        )
        repaired = {str(path): str(content) for path, content in value.get("files", {}).items()}
        if set(repaired) != set(candidate.files):
            raise ValidationError("repair changed the candidate file scope")
        fetched = context.tool(
            "repository.read_files",
            repository=request.repository,
            paths=candidate.changed_files,
            limit_per_file=400,
        )["files"]
        if any(item.get("truncated") for item in fetched):
            raise WorkflowError("a target file exceeds the safe fetch bound; refusing a truncated repair")
        originals = {item["path"]: item["content"] for item in fetched}
        return self._candidate(
            originals,
            repaired,
            summary=str(value.get("summary", candidate.summary)),
            root_cause=str(value.get("root_cause", candidate.root_cause)),
            risks=[str(item) for item in value.get("risks", candidate.risks)],
            verification_required=[
                str(item) for item in value.get("verification_required", candidate.verification_required)
            ],
        )

    @staticmethod
    def _candidate(
        originals: dict[str, str],
        proposed: dict[str, str],
        *,
        summary: str,
        root_cause: str,
        risks: list[str],
        verification_required: list[str],
    ) -> CandidatePatch:
        changed = sorted(path for path, content in proposed.items() if originals.get(path, "") != content)
        if not changed:
            raise WorkflowError("candidate patch does not change any file")
        chunks = []
        for path in changed:
            chunks.extend(
                difflib.unified_diff(
                    originals.get(path, "").splitlines(keepends=True),
                    proposed[path].splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        return CandidatePatch(
            summary=summary,
            root_cause=root_cause,
            changed_files=changed,
            patch="".join(chunks),
            files={path: proposed[path] for path in changed},
            static_checks=[],
            risks=risks,
            verification_required=verification_required,
        )

    def _search_term(self, description: str) -> str:
        if self.reasoner is None:
            raise WorkflowError("coding requires target_files when no reasoner is configured")
        query = self.reasoner.complete_text(
            system="Return one concise repository search term for the requested code change. Return only the term.",
            prompt=description,
        ).strip()
        if not query:
            raise WorkflowError("coding could not determine a repository search term")
        return query[:120]


def prepare_verified_candidate(
    coding: CodingAgent,
    verifier: Any,
    request: ChangeRequest,
    *,
    session_id: str,
    guidance: AgentGuidance | None = None,
    max_repair_attempts: int = 1,
) -> tuple[CandidatePatch, VerificationReport]:
    """Generate a candidate synchronously and gate it behind static verification.

    Read-only throughout: no GitHub mutation is proposed unless every static
    check passes.  One repair attempt is allowed, matching the previous
    workflow's ``MAX_REPAIR_ATTEMPTS``.
    """
    candidate = coding.create_candidate(request, session_id=session_id, guidance=guidance)
    for attempt in range(1, max_repair_attempts + 2):
        report = verifier.verify(candidate, session_id=session_id, attempts=attempt)
        if report.passed:
            return candidate, report
        if attempt <= max_repair_attempts:
            failures = [check.details for check in report.checks if check.status == "FAIL"]
            candidate = coding.repair_candidate(
                request,
                candidate,
                failures,
                session_id=session_id,
                guidance=guidance,
            )
    return candidate, report
