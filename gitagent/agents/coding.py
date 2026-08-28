"""Candidate-patch agent. It has no GitHub mutation capabilities or test runner."""

from __future__ import annotations

import difflib
import json
import re
from typing import Any

from gitagent.domain.errors import LLMProviderError, ValidationError, WorkflowError
from gitagent.domain.models import (
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    CandidatePreparationResult,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    Recommendation,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.file_access import safe_repository_path
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .guidance import guidance_section

_PROMPTS = get_prompt_library()

_CHANGE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["ADD", "MODIFY", "DELETE"]},
                    "path": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence_queries": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {"type": "string", "minLength": 1, "maxLength": 120},
                    },
                },
                "required": ["action", "path", "evidence_queries"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["changes"],
    "additionalProperties": False,
}

_EXPLICIT_PATH = re.compile(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+")


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

class _CodingCapabilityFailure(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or payload.get("error") or "capability failed"))
        self.payload = payload


CODING_SPEC = AgentSpec(
    name="coding",
    role="Explain, review, plan, or prepare a minimal candidate patch from targeted repository evidence.",
    system_prompt=_PROMPTS.text("system.coding"),
    output_schema=(),
    routes=frozenset({"coding"}),
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
    ) -> CandidatePreparationResult:
        def operation(context: AgentContext) -> CandidatePreparationResult:
            try:
                return self._create(context, request, guidance)
            except _CodingCapabilityFailure as failure:
                return CandidatePreparationResult(None, capability_error=failure.payload)

        return self.harness.run(
            "coding",
            session_id=session_id,
            operation=operation,
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
    ) -> CandidatePreparationResult:
        if not self.reasoner:
            return CandidatePreparationResult(candidate)

        def operation(context: AgentContext) -> CandidatePreparationResult:
            try:
                return self._repair(context, request, candidate, errors, guidance)
            except _CodingCapabilityFailure as failure:
                return CandidatePreparationResult(None, capability_error=failure.payload)

        return self.harness.run(
            "coding",
            session_id=session_id,
            operation=operation,
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

    @staticmethod
    def _invoke_capability(context: AgentContext, capability_id: str, **arguments: Any) -> Any:
        content = context.invoke(capability_id, **arguments)
        call = context.last_capability_call
        if call is None:
            raise WorkflowError("Coding capability invocation did not record its result")
        if call.result.status == "failed":
            error = call.result.error
            if error is None:
                raise WorkflowError("Coding capability failed without a structured error")
            raise _CodingCapabilityFailure(
                {
                    "capability_id": call.result.capability_id,
                    "arguments": dict(arguments),
                    "error": error.type.value,
                    "message": error.message,
                    "details": error.details,
                    "attempts": call.result.attempts,
                }
            )
        if call.result.status != "success":
            raise WorkflowError(f"Coding capability returned unsupported status: {call.result.status}")
        return content

    def _create(
        self,
        context: AgentContext,
        request: ChangeRequest,
        guidance: AgentGuidance | None,
    ) -> CandidatePreparationResult:
        context.phase = "discovering_targets"
        if request.source_ref is None:
            default = self._invoke_capability(context, "repository.get_default_branch")
            request.base_branch = str(default["branch"])
            request.source_ref = str(default["commit_sha"])
        tree = self._invoke_capability(
            context,
            "repository.get_repo_tree",
            depth=8,
            max_entries=500,
            ref=request.source_ref,
        )
        tree_paths = [str(path) for path in tree["entries"]]
        explicit_request = bool(request.proposed_files or request.replacements or request.deleted_files)
        if explicit_request:
            explicit_paths = list(
                dict.fromkeys(
                    [*request.proposed_files, *[item.path for item in request.replacements], *request.deleted_files]
                )
            )
            existing_files = self._existing_files(context, request, explicit_paths)
            planned_changes = self._explicit_change_plan(request, existing_files)
        elif self.reasoner is not None:
            planned_changes = self._reasoned_change_plan(context, request, tree_paths, guidance)
            existing_files = self._existing_files(
                context,
                request,
                [change["path"] for change in planned_changes],
            )
        else:
            raise WorkflowError(
                "coding without a reasoner requires explicit replacements, proposed_files, or deleted_files"
            )
        self._validate_change_plan(planned_changes, existing_files)

        request.target_files = [change["path"] for change in planned_changes]
        existing_paths = [change["path"] for change in planned_changes if change["action"] != "ADD"]
        context.phase = "reading_targets"
        fetched = (
            self._invoke_capability(
                context,
                "repository.read_files",
                requests=[{"path": path, "limit": 400} for path in existing_paths],
                ref=request.source_ref,
            )["files"]
            if existing_paths
            else []
        )
        if any(item.get("truncated") for item in fetched):
            raise WorkflowError("a target file exceeds the safe fetch bound; refusing to risk a truncated overwrite")
        originals = {item["path"]: item["content"] for item in fetched}

        deleted_files = [change["path"] for change in planned_changes if change["action"] == "DELETE"]
        if explicit_request:
            new_files = dict(request.proposed_files)
            for replacement in request.replacements:
                if replacement.path not in originals:
                    raise WorkflowError(f"replacement target was not fetched: {replacement.path}")
                content = new_files.get(replacement.path, originals[replacement.path])
                count = content.count(replacement.old)
                if count != 1:
                    raise WorkflowError(
                        f"replacement anchor in {replacement.path} must match exactly once; found {count}"
                    )
                new_files[replacement.path] = content.replace(replacement.old, replacement.new, 1)
        else:
            planned_operations = [
                {
                    "id": f"change-{index}",
                    "action": change["action"],
                    "path": change["path"],
                    "evidence_queries": change["evidence_queries"],
                }
                for index, change in enumerate(planned_changes, start=1)
            ]
            writable_operations = [
                operation for operation in planned_operations if operation["action"] in {"ADD", "MODIFY"}
            ]
            evidence = self._collect_operation_evidence(
                context,
                request,
                writable_operations,
                existing_paths=set(originals),
            )
            evidence_by_id = {item["operation_id"]: item for item in evidence}
            context.phase = "generating_candidate"
            new_files = {}
            for operation in writable_operations:
                try:
                    content = self.reasoner.complete_text(
                        system=context.system_prompt,
                        prompt=_PROMPTS.render(
                            "agents.coding_create",
                            repository=request.repository,
                            description=request.description,
                            operation=json.dumps(operation, ensure_ascii=False),
                            current_content=originals.get(operation["path"], ""),
                            evidence=json.dumps(evidence_by_id[operation["id"]], ensure_ascii=False),
                            guidance=guidance_section(guidance),
                        ),
                    )
                except LLMProviderError as exc:
                    raise LLMProviderError(f"候选文件生成阶段失败：{exc}") from exc
                if not content:
                    return CandidatePreparationResult(
                        None,
                        message=(
                            f"模型没有为 `{operation['path']}` 返回文件内容，本次未生成审批，也没有写入仓库。"
                            "请补充文件要求后重试。"
                        ),
                    )
                new_files[operation["path"]] = content

        return CandidatePreparationResult(
            self._candidate(
                originals,
                new_files,
                deleted_files=deleted_files,
                summary=request.suggested_title or request.description,
                root_cause=request.description,
                risks=["模型生成的文件内容需要人工审阅。"],
                verification_required=["人工审阅", "提交默认分支后运行项目测试"],
            )
        )

    def _reasoned_change_plan(
        self,
        context: AgentContext,
        request: ChangeRequest,
        tree_paths: list[str],
        guidance: AgentGuidance | None,
    ) -> list[dict[str, Any]]:
        if self.reasoner is None:
            raise WorkflowError("repository change planning requires a reasoner")
        mentioned = list(
            dict.fromkeys(
                [safe_repository_path(path) for path in request.target_files]
                + _EXPLICIT_PATH.findall(request.description)
            )
        )
        mentioned.extend(path for path in tree_paths if path in request.description and path not in mentioned)
        context.phase = "planning_changes"
        value = self.reasoner.complete_structured(
            system=context.system_prompt,
            prompt=json.dumps(
                {
                    "request": request.description,
                    "repository_tree": tree_paths,
                    "mentioned_paths": mentioned,
                    "guidance": guidance_section(guidance),
                    "instruction": (
                        "Return every requested file operation in one plan. ADD is only when the user requests "
                        "a new path; MODIFY and DELETE target existing files. repository_tree is bounded, so keep "
                        "an explicitly mentioned path even when it is not listed there; path status is validated next. "
                        "For each ADD or MODIFY operation, return one to three short, independent literal "
                        "evidence_queries likely to occur in repository source. Each query must concern only that "
                        "operation; never combine unrelated requested topics in one query. DELETE uses an empty list."
                    ),
                },
                ensure_ascii=False,
            ),
            schema=_CHANGE_PLAN_SCHEMA,
            tool_name="plan_repository_file_changes",
        )
        changes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in value.get("changes", []):
            path = safe_repository_path(str(raw.get("path") or ""))
            action = str(raw.get("action") or "")
            if path in seen:
                raise ValidationError(f"repository change plan contains duplicate path: {path}")
            seen.add(path)
            changes.append(
                {
                    "action": action,
                    "path": path,
                    "evidence_queries": [str(item) for item in raw["evidence_queries"]],
                }
            )
        return changes

    @staticmethod
    def _explicit_change_plan(request: ChangeRequest, existing_files: set[str]) -> list[dict[str, str]]:
        actions: dict[str, str] = {}

        def add(path: str, action: str) -> None:
            safe = safe_repository_path(path)
            previous = actions.get(safe)
            if previous is not None and previous != action:
                raise ValidationError(f"conflicting repository operations for {safe}: {previous} and {action}")
            actions[safe] = action

        for path in request.proposed_files:
            add(path, "MODIFY" if path in existing_files else "ADD")
        for replacement in request.replacements:
            add(replacement.path, "MODIFY")
        for path in request.deleted_files:
            add(path, "DELETE")
        return [{"action": action, "path": path} for path, action in actions.items()]

    @staticmethod
    def _existing_files(
        context: AgentContext,
        request: ChangeRequest,
        paths: list[str],
    ) -> set[str]:
        status = CodingAgent._invoke_capability(
            context,
            "repository.get_file_status",
            paths=paths,
            ref=request.source_ref,
        )
        return {str(path) for path in status.get("existing_files", [])}

    @staticmethod
    def _validate_change_plan(changes: list[dict[str, Any]], existing_files: set[str]) -> None:
        for change in changes:
            action = change["action"]
            path = change["path"]
            if action == "ADD" and path in existing_files:
                raise ValidationError(f"repository change plan cannot add an existing file: {path}")
            if action in {"MODIFY", "DELETE"} and path not in existing_files:
                raise ValidationError(f"repository change plan cannot {action.casefold()} a missing file: {path}")

    @staticmethod
    def _collect_operation_evidence(
        context: AgentContext,
        request: ChangeRequest,
        operations: list[dict[str, Any]],
        *,
        existing_paths: set[str],
    ) -> list[dict[str, Any]]:
        context.phase = "collecting_evidence"
        evidence: list[dict[str, Any]] = []
        for operation in operations:
            selected: dict[str, int] = {}
            queries = [str(query) for query in operation["evidence_queries"]]
            for query in queries:
                result = CodingAgent._invoke_capability(
                    context,
                    "repository.search_code",
                    query=query,
                    max_results=5,
                )
                for hit in result["results"]:
                    path = str(hit["path"])
                    if path not in existing_paths and path not in selected:
                        selected[path] = int(hit["line"])
                        break
            requests = [{"path": path, "start_line": max(1, line - 40), "limit": 81} for path, line in selected.items()]
            sources = (
                CodingAgent._invoke_capability(
                    context,
                    "repository.read_files",
                    requests=requests,
                    ref=request.source_ref,
                )["files"]
                if requests
                else []
            )
            evidence.append(
                {
                    "operation_id": operation["id"],
                    "queries": queries,
                    "sources": sources,
                }
            )
        return evidence

    def _repair(
        self,
        context: AgentContext,
        request: ChangeRequest,
        candidate: CandidatePatch,
        errors: list[str],
        guidance: AgentGuidance | None,
    ) -> CandidatePreparationResult:
        writable_operations = [
            {
                "id": f"change-{index}",
                "action": "ADD" if path in candidate.added_files else "MODIFY",
                "path": path,
                "content": content,
            }
            for index, (path, content) in enumerate(candidate.files.items(), start=1)
        ]
        repaired: dict[str, str] = {}
        for operation in writable_operations:
            content = self.reasoner.complete_text(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.coding_repair",
                    description=request.description,
                    operation=json.dumps(
                        {key: operation[key] for key in ("id", "action", "path")},
                        ensure_ascii=False,
                    ),
                    current_content=operation["content"],
                    errors=json.dumps(errors, ensure_ascii=False),
                    guidance=guidance_section(guidance),
                ),
            )
            if not content:
                return CandidatePreparationResult(
                    None,
                    message=(
                        f"模型没有为 `{operation['path']}` 返回修复后的文件内容，本次未生成审批，"
                        "也没有写入仓库。请补充文件要求后重试。"
                    ),
                )
            repaired[operation["path"]] = content
        existing_paths = [*candidate.modified_files, *candidate.deleted_files]
        fetched = (
            self._invoke_capability(
                context,
                "repository.read_files",
                requests=[{"path": path, "limit": 400} for path in existing_paths],
                ref=request.source_ref,
            )["files"]
            if existing_paths
            else []
        )
        if any(item.get("truncated") for item in fetched):
            raise WorkflowError("a target file exceeds the safe fetch bound; refusing a truncated repair")
        originals = {item["path"]: item["content"] for item in fetched}
        return CandidatePreparationResult(
            self._candidate(
                originals,
                repaired,
                deleted_files=candidate.deleted_files,
                summary=candidate.summary,
                root_cause=candidate.root_cause,
                risks=candidate.risks,
                verification_required=candidate.verification_required,
            )
        )

    @staticmethod
    def _candidate(
        originals: dict[str, str],
        proposed: dict[str, str],
        *,
        deleted_files: list[str],
        summary: str,
        root_cause: str,
        risks: list[str],
        verification_required: list[str],
    ) -> CandidatePatch:
        deleted = sorted(set(deleted_files))
        missing_deletions = set(deleted) - set(originals)
        if missing_deletions:
            raise WorkflowError(f"deletion target was not fetched: {', '.join(sorted(missing_deletions))}")
        overlap = set(proposed) & set(deleted)
        if overlap:
            raise ValidationError(f"candidate cannot write and delete the same file: {', '.join(sorted(overlap))}")
        added = sorted(path for path in proposed if path not in originals)
        modified = sorted(
            path for path, content in proposed.items() if path in originals and originals[path] != content
        )
        unchanged = sorted(path for path in proposed if path in originals and originals[path] == proposed[path])
        if unchanged:
            raise WorkflowError(f"candidate does not modify the requested file(s): {', '.join(unchanged)}")
        if not (added or modified or deleted):
            raise WorkflowError("candidate patch does not change any file")
        chunks = []
        for path in [*added, *modified]:
            chunks.extend(
                difflib.unified_diff(
                    originals.get(path, "").splitlines(keepends=True),
                    proposed[path].splitlines(keepends=True),
                    fromfile="/dev/null" if path in added else f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        for path in deleted:
            chunks.extend(
                difflib.unified_diff(
                    originals[path].splitlines(keepends=True),
                    [],
                    fromfile=f"a/{path}",
                    tofile="/dev/null",
                )
            )
        return CandidatePatch(
            summary=summary,
            root_cause=root_cause,
            added_files=added,
            modified_files=modified,
            deleted_files=deleted,
            patch="".join(chunks),
            files={path: proposed[path] for path in [*added, *modified]},
            static_checks=[],
            risks=risks,
            verification_required=verification_required,
        )

    @staticmethod
    def _is_test_path(path: str) -> bool:
        lowered = path.casefold()
        segments = lowered.split("/")
        return any(segment in {"test", "tests", "spec", "specs"} for segment in segments[:-1]) or any(
            marker in segments[-1] for marker in ("_test.", ".test.", ".spec.")
        )


def record_candidate_capability_error(context: AgentContext, prepared: CandidatePreparationResult) -> bool:
    payload = prepared.capability_error
    if payload is None:
        return False
    context.observations.append({"kind": "capability_error", "payload": dict(payload)})
    return True


def prepare_verified_candidate(
    coding: CodingAgent,
    verifier: Any,
    request: ChangeRequest,
    *,
    session_id: str,
    guidance: AgentGuidance | None = None,
    max_repair_attempts: int = 1,
) -> CandidatePreparationResult:
    """Generate a candidate synchronously and gate it behind static verification.

    Candidate preparation emits no GitHub mutation call unless every static
    check passes. One repair attempt is allowed, matching the previous
    workflow's ``MAX_REPAIR_ATTEMPTS``.
    """
    prepared = coding.create_candidate(request, session_id=session_id, guidance=guidance)
    if prepared.candidate is None:
        return prepared
    candidate = prepared.candidate
    for attempt in range(1, max_repair_attempts + 2):
        report = verifier.verify(candidate, session_id=session_id, attempts=attempt)
        if report.passed:
            return CandidatePreparationResult(candidate, report)
        if attempt <= max_repair_attempts:
            failures = [check.details for check in report.checks if check.status == "FAIL"]
            prepared = coding.repair_candidate(
                request,
                candidate,
                failures,
                session_id=session_id,
                guidance=guidance,
            )
            if prepared.candidate is None:
                return prepared
            candidate = prepared.candidate
    return CandidatePreparationResult(candidate, report)
