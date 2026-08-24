"""Candidate-patch agent. It has no GitHub mutation tools or test runner."""

from __future__ import annotations

import difflib
import json
from typing import Any

from ..core.errors import LLMProviderError, StructuredOutputError, ValidationError, WorkflowError
from ..core.models import (
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    Recommendation,
    VerificationReport,
)
from ..prompts import get_prompt_library
from ..reasoning import Reasoner
from ..runtime import AgentContext, AgentHarness
from .guidance import guidance_section

_PROMPTS = get_prompt_library()

_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "root_cause": {"type": "string"},
        "files": {
            "type": "object",
            "description": "Complete new UTF-8 content keyed by the supplied repository path.",
            "additionalProperties": {"type": "string"},
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "verification_required": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "root_cause", "files", "risks", "verification_required"],
    "additionalProperties": False,
}

_EXPLANATION_SCHEMA = {
    "type": "object",
    "properties": {
        "behavior_changes": {"type": "array", "items": {"type": "string"}},
        "key_symbols": {"type": "array", "items": {"type": "string"}},
        "call_relationships": {"type": "array", "items": {"type": "string"}},
        "impact_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["behavior_changes", "key_symbols", "call_relationships", "impact_scope"],
    "additionalProperties": False,
}

_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
        "impacts": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "test_assessment": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "recommendation": {
            "type": "string",
            "enum": ["APPROVE", "REQUEST_CHANGES", "NEEDS_HUMAN_REVIEW"],
        },
        "goal_alignment": {"type": "string", "enum": ["ALIGNED", "PARTIAL", "MISMATCH", "UNKNOWN"]},
    },
    "required": [
        "summary",
        "blocking_issues",
        "impacts",
        "suggestions",
        "test_assessment",
        "risk_level",
        "recommendation",
        "goal_alignment",
    ],
    "additionalProperties": False,
}

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "tradeoffs": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["direction", "files", "tradeoffs", "tests"],
    "additionalProperties": False,
}

CODING_SPEC = AgentSpec(
    name="coding",
    role="Explain, review, plan, or prepare a minimal candidate patch from targeted repository evidence.",
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
    output_schema=(),
    capabilities=frozenset({"coding"}),
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

    def explain(
        self,
        repository: str,
        request: str,
        evidence: dict[str, Any],
        *,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> CodeExplanationResult:
        return self.harness.run(
            "coding",
            session_id=session_id,
            operation=lambda context: self._explain(context, request, evidence, guidance),
            repository=repository,
            goal=request,
            guidance=guidance,
        )

    def review(
        self,
        repository: str,
        request: str,
        evidence: dict[str, Any],
        *,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> CodeReviewResult:
        return self.harness.run(
            "coding",
            session_id=session_id,
            operation=lambda context: self._review(context, request, evidence, guidance),
            repository=repository,
            goal=request,
            guidance=guidance,
        )

    def plan(
        self,
        repository: str,
        request: str,
        evidence: dict[str, Any],
        *,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> CodePlanResult:
        return self.harness.run(
            "coding",
            session_id=session_id,
            operation=lambda context: self._plan(context, request, evidence, guidance),
            repository=repository,
            goal=request,
            guidance=guidance,
        )

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

    def _explain(
        self,
        context: AgentContext,
        request: str,
        evidence: dict[str, Any],
        guidance: AgentGuidance | None,
    ) -> CodeExplanationResult:
        if self.reasoner is None:
            changed = [str(path) for path in evidence.get("changed_files", [])]
            return CodeExplanationResult(
                behavior_changes=[f"代码变更涉及 {path}" for path in changed],
                key_symbols=[],
                call_relationships=[],
                impact_scope=changed,
            )
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.coding_explain",
                request=request,
                evidence=json.dumps(evidence, ensure_ascii=False),
                guidance=guidance_section(guidance),
            ),
            schema=_EXPLANATION_SCHEMA,
            tool_name="explain_code_change",
        )
        return CodeExplanationResult(
            behavior_changes=[str(item) for item in value.get("behavior_changes", [])],
            key_symbols=[str(item) for item in value.get("key_symbols", [])],
            call_relationships=[str(item) for item in value.get("call_relationships", [])],
            impact_scope=[str(item) for item in value.get("impact_scope", [])],
        )

    def _review(
        self,
        context: AgentContext,
        request: str,
        evidence: dict[str, Any],
        guidance: AgentGuidance | None,
    ) -> CodeReviewResult:
        changed = [str(path) for path in evidence.get("changed_files", [])]
        if self.reasoner is None:
            tests = [path for path in changed if self._is_test_path(path)]
            return CodeReviewResult(
                summary=f"静态查看了 {len(changed)} 个变更文件。",
                blocking_issues=[],
                impacts=changed,
                suggestions=[],
                test_assessment=(
                    f"Diff 中包含 {len(tests)} 个测试文件；未执行测试。"
                    if tests
                    else "Diff 中没有测试文件变化；未执行测试。"
                ),
                risk_level="MEDIUM" if changed else "LOW",
                recommendation=Recommendation.NEEDS_HUMAN_REVIEW,
                goal_alignment="UNKNOWN",
            )
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.pr_review",
                request=request,
                evidence=json.dumps(evidence, ensure_ascii=False),
                guidance=guidance_section(guidance),
            ),
            schema=_REVIEW_SCHEMA,
            tool_name="review_code_change",
        )
        recommendation = Recommendation(str(value.get("recommendation", "NEEDS_HUMAN_REVIEW")))
        blocking_issues = (
            [str(item) for item in value.get("blocking_issues", [])]
            if recommendation == Recommendation.REQUEST_CHANGES
            else []
        )
        return CodeReviewResult(
            summary=str(value.get("summary", "")),
            blocking_issues=blocking_issues,
            impacts=[str(item) for item in value.get("impacts", [])],
            suggestions=[str(item) for item in value.get("suggestions", [])],
            test_assessment=str(value.get("test_assessment", "")),
            risk_level=str(value.get("risk_level", "MEDIUM")).upper(),
            recommendation=recommendation,
            goal_alignment=str(value.get("goal_alignment", "UNKNOWN")).upper(),
        )

    def _plan(
        self,
        context: AgentContext,
        request: str,
        evidence: dict[str, Any],
        guidance: AgentGuidance | None,
    ) -> CodePlanResult:
        changed = [str(path) for path in evidence.get("changed_files", [])]
        if self.reasoner is None:
            return CodePlanResult(
                direction=request,
                files=changed,
                tradeoffs=["需要结合运行时行为确认具体取舍。"],
                tests=["运行受影响模块的项目测试。"],
            )
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.coding_plan",
                request=request,
                evidence=json.dumps(evidence, ensure_ascii=False),
                guidance=guidance_section(guidance),
            ),
            schema=_PLAN_SCHEMA,
            tool_name="plan_code_change",
        )
        return CodePlanResult(
            direction=str(value.get("direction", request)),
            files=[str(item) for item in value.get("files", changed)],
            tradeoffs=[str(item) for item in value.get("tradeoffs", [])],
            tests=[str(item) for item in value.get("tests", [])],
        )

    def _create(
        self,
        context: AgentContext,
        request: ChangeRequest,
        guidance: AgentGuidance | None,
    ) -> CandidatePatch:
        context.phase = "discovering_targets"
        tree = context.tool(
            "repository.get_repo_tree",
            repository=request.repository,
            depth=4,
            ref=request.source_ref,
        )
        tree_paths = [str(path) for path in tree["entries"]]
        known_paths = set(tree_paths)
        paths = list(dict.fromkeys(request.target_files + [replacement.path for replacement in request.replacements]))
        if not paths:
            paths = [path for path in tree_paths if path in request.description]
        existing_paths = [path for path in paths if path in known_paths]
        if not existing_paths and not request.proposed_files:
            context.phase = "searching_repository"
            query = self._search_term(request.description)
            hits = context.tool("repository.search_code", repository=request.repository, query=query, max_results=15)["results"]
            existing_paths = list(dict.fromkeys(hit["path"] for hit in hits))[:6]
            if not existing_paths:
                raise WorkflowError(
                    "cannot determine a safe editable target file; "
                    f"repository search returned no matches for {query!r}"
                )
        context.phase = "reading_targets"
        fetched = (
            context.tool(
                "repository.read_files",
                repository=request.repository,
                requests=[{"path": path, "limit": 400} for path in existing_paths],
                ref=request.source_ref,
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
                    requests=[{"path": path, "limit": 400} for path in missing_context],
                    ref=request.source_ref,
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
            context.phase = "generating_candidate"
            try:
                value = self.reasoner.complete_structured(
                    system=context.system_prompt,
                    prompt=_PROMPTS.render(
                        "agents.coding_create",
                        repository=request.repository,
                        description=request.description,
                        files=json.dumps(originals, ensure_ascii=False),
                        guidance=guidance_section(guidance),
                    ),
                    schema=_CANDIDATE_SCHEMA,
                    tool_name="prepare_candidate",
                )
            except LLMProviderError as exc:
                raise LLMProviderError(f"候选补丁生成阶段失败：{exc}") from exc
            except StructuredOutputError as exc:
                raise StructuredOutputError(f"候选补丁结构化输出无效：{exc}") from exc
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
            schema=_CANDIDATE_SCHEMA,
            tool_name="repair_candidate",
        )
        repaired = {str(path): str(content) for path, content in value.get("files", {}).items()}
        if set(repaired) != set(candidate.files):
            raise ValidationError("repair changed the candidate file scope")
        fetched = context.tool(
            "repository.read_files",
            repository=request.repository,
            requests=[{"path": path, "limit": 400} for path in candidate.changed_files],
            ref=request.source_ref,
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
        try:
            query = self.reasoner.complete_text(
                system="Return one concise repository search term for the requested code change. Return only the term.",
                prompt=description,
            ).strip()
        except LLMProviderError as exc:
            raise LLMProviderError(f"目标文件搜索词生成阶段失败：{exc}") from exc
        if not query:
            raise WorkflowError("coding could not determine a repository search term")
        return query[:120]

    @staticmethod
    def _is_test_path(path: str) -> bool:
        lowered = path.casefold()
        segments = lowered.split("/")
        return any(segment in {"test", "tests", "spec", "specs"} for segment in segments[:-1]) or any(
            marker in segments[-1] for marker in ("_test.", ".test.", ".spec.")
        )


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
