"""Candidate-patch agent. It has no GitHub mutation capabilities or test runner."""

from __future__ import annotations

import difflib
import json
from typing import Any

from gitagent.agent_loop import (
    CapabilityCall,
    ModelResponse,
    WaitForUser,
    explicit_wait,
    wait_for_user_tool,
)
from gitagent.capability import AccessLevel
from gitagent.capability.schema import validate_schema
from gitagent.domain.errors import (
    LLMProviderError,
    StructuredOutputError,
    ValidationError,
    WorkflowError,
)
from gitagent.domain.models import (
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    CandidatePreparationResult,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    CodingTask,
    Recommendation,
)
from gitagent.domain.reviews import canonical_review_event
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import (
    AgentHarness,
    ExecutionProfile,
    ResourceClaims,
)
from gitagent.harness.file_access import safe_repository_path
from gitagent.harness.structured_call_dispatcher import StructuredCallDispatcher
from gitagent.model import Reasoner, structured_tools
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
                },
                "required": ["action", "path"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["changes"],
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
    "required": [
        "behavior_changes",
        "key_symbols",
        "call_relationships",
        "impact_scope",
    ],
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
        "goal_alignment": {
            "type": "string",
            "enum": ["ALIGNED", "PARTIAL", "MISMATCH", "UNKNOWN"],
        },
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

_DIALOGUE_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved": {"type": "array", "items": {"type": "string"}},
        "explained": {"type": "array", "items": {"type": "string"}},
        "needs_changes": {"type": "array", "items": {"type": "string"}},
        "discussion": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "reply_draft": {"type": "string"},
    },
    "required": [
        "resolved",
        "explained",
        "needs_changes",
        "discussion",
        "conflicts",
        "reply_draft",
    ],
    "additionalProperties": False,
}

_CI_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {"type": "string"}},
        "suspected_causes": {"type": "array", "items": {"type": "string"}},
        "related_changes": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["facts", "suspected_causes", "related_changes", "actions"],
    "additionalProperties": False,
}

_EVIDENCE_READY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
    },
    "required": ["summary"],
    "additionalProperties": False,
}


class _CodingCapabilityFailure(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(
            str(payload.get("message") or payload.get("error") or "capability failed")
        )
        self.payload = payload


CODING_SPEC = AgentSpec(
    name="coding",
    role=(
        "Produce typed explanation, review, plan, PR dialogue, or CI analysis, or prepare "
        "a minimal candidate patch from targeted evidence."
    ),
    system_prompt=_PROMPTS.text("system.coding"),
    output_schema=(),
    agent_depth=2,
    execution_profile=ExecutionProfile.exclusive(),
)


class CodingAgent:
    def __init__(
        self,
        harness: AgentHarness,
        reasoner: Reasoner | None = None,
        verifier: Any | None = None,
    ) -> None:
        self.harness = harness
        self.reasoner = reasoner
        self.verifier = verifier
        self.dispatcher = StructuredCallDispatcher(harness)
        harness.register(CODING_SPEC)

    def step(self, context: AgentContext) -> ModelResponse | WaitForUser:
        """Execute one typed Coding task inside the shared Agent Runtime."""

        task = context.coding_task
        if task is None:
            raise WorkflowError("Coding context is missing its typed task")
        if not context.coding_task_completed:
            claims = ResourceClaims(write=(f"workspace:{context.repository}",))
            with self.harness.coordinator.claim_resources(claims):
                artifact = self._run_task(context, task)
                self._store_artifact(context, task.mode, artifact)
                context.coding_task_completed = True
        if self.reasoner is None:
            raise WorkflowError("Coding Agent calls require a configured reasoner")
        return explicit_wait(
            context.reason(self.reasoner, tools=[wait_for_user_tool()])
        )

    @staticmethod
    def build_result(context: AgentContext) -> str:
        return context.final_message

    def _run_task(self, context: AgentContext, task: CodingTask) -> Any:
        mode = task.mode
        if mode == "explain":
            return self._explain(context, task.task, task.evidence, context.guidance)
        if mode == "review":
            return self._review(context, task.task, task.evidence, context.guidance)
        if mode == "plan":
            return self._plan(context, task.task, task.evidence, context.guidance)
        if mode == "review_dialogue":
            return self._summarize_review_dialogue(
                context, task.task, task.evidence, context.guidance
            )
        if mode == "ci":
            return self._analyze_ci(
                context, task.task, task.evidence, context.guidance
            )
        if mode == "patch":
            if task.change_request is None or self.verifier is None:
                raise WorkflowError(
                    "Coding patch call requires a ChangeRequest and StaticVerifier"
                )
            context.change_request = task.change_request
            return self._prepare_verified_in_context(
                context,
                task.change_request,
                self.verifier,
                context.guidance,
            )
        raise ValidationError(f"unknown Coding Agent mode: {mode}")

    @staticmethod
    def _store_artifact(context: AgentContext, mode: str, artifact: Any) -> None:
        if isinstance(artifact, CandidatePreparationResult):
            context.code_candidate = artifact.candidate
            context.verification = artifact.verification
            if artifact.capability_error:
                context.observations.append(
                    {"kind": "capability_error", "payload": artifact.capability_error}
                )
        elif isinstance(artifact, CodeExplanationResult):
            context.code_explanation = artifact
        elif isinstance(artifact, CodeReviewResult):
            context.code_review = artifact
        elif isinstance(artifact, CodePlanResult):
            context.code_plan = artifact
        elif mode == "review_dialogue" and isinstance(artifact, dict):
            context.review_dialogue = artifact
        elif mode == "ci" and isinstance(artifact, dict):
            context.ci_analysis = artifact

    def _prepare_verified_in_context(
        self,
        context: AgentContext,
        request: ChangeRequest,
        verifier: Any,
        guidance: AgentGuidance | None,
    ) -> CandidatePreparationResult:
        try:
            prepared = self._create(context, request, guidance)
        except _CodingCapabilityFailure as failure:
            return CandidatePreparationResult(None, capability_error=failure.payload)
        if prepared.candidate is None:
            return prepared
        candidate = prepared.candidate
        report = verifier.verify(candidate, session_id=context.session_id, attempts=1)
        if not report.passed:
            failures = [
                check.details for check in report.checks if check.status == "FAIL"
            ]
            try:
                repaired = self._repair(
                    context, request, candidate, failures, guidance
                )
            except _CodingCapabilityFailure as failure:
                return CandidatePreparationResult(None, capability_error=failure.payload)
            if repaired.candidate is None:
                return repaired
            candidate = repaired.candidate
            report = verifier.verify(
                candidate, session_id=context.session_id, attempts=2
            )
        return CandidatePreparationResult(candidate, report)

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
        value = self._complete_structured_task(
            context,
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
            call_relationships=[
                str(item) for item in value.get("call_relationships", [])
            ],
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
        value = self._complete_structured_task(
            context,
            prompt=_PROMPTS.render(
                "agents.pr_review",
                request=request,
                evidence=json.dumps(evidence, ensure_ascii=False),
                guidance=guidance_section(guidance),
            ),
            schema=_REVIEW_SCHEMA,
            tool_name="review_code_change",
        )
        recommendation = Recommendation(
            str(value.get("recommendation", "NEEDS_HUMAN_REVIEW"))
        )
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
        value = self._complete_structured_task(
            context,
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

    def _summarize_review_dialogue(
        self,
        context: AgentContext,
        request: str,
        evidence: dict[str, Any],
        guidance: AgentGuidance | None,
    ) -> dict[str, Any]:
        reviews = [
            item for item in evidence.get("reviews", []) if isinstance(item, dict)
        ]
        comments = [
            item for item in evidence.get("comments", []) if isinstance(item, dict)
        ]
        if self.reasoner is None:
            return {
                "resolved": [
                    str(item.get("body") or "已批准")
                    for item in reviews
                    if canonical_review_event(item) == "APPROVE"
                ],
                "explained": [],
                "needs_changes": [
                    str(item.get("body") or "Review 要求修改")
                    for item in reviews
                    if canonical_review_event(item) == "REQUEST_CHANGES"
                ],
                "discussion": [
                    str(item.get("body") or "")
                    for item in [*reviews, *comments]
                    if item.get("body")
                ],
                "conflicts": [],
                "reply_draft": "已查看现有 Review；请确认待处理意见后再发布回复。",
            }
        value = self._complete_structured_task(
            context,
            prompt=_PROMPTS.render(
                "agents.pull_request_dialogue",
                request=request,
                evidence=json.dumps(evidence, ensure_ascii=False),
                guidance=guidance_section(guidance),
            ),
            schema=_DIALOGUE_SCHEMA,
            tool_name="summarize_review_dialogue",
        )
        return {
            "resolved": [str(item) for item in value.get("resolved", [])],
            "explained": [str(item) for item in value.get("explained", [])],
            "needs_changes": [str(item) for item in value.get("needs_changes", [])],
            "discussion": [str(item) for item in value.get("discussion", [])],
            "conflicts": [str(item) for item in value.get("conflicts", [])],
            "reply_draft": str(value.get("reply_draft", "")),
        }

    def _analyze_ci(
        self,
        context: AgentContext,
        request: str,
        evidence: dict[str, Any],
        guidance: AgentGuidance | None,
    ) -> dict[str, Any]:
        runs = [
            item for item in evidence.get("workflow_runs", []) if isinstance(item, dict)
        ]
        job_results = [
            item for item in evidence.get("job_logs", []) if isinstance(item, dict)
        ]
        jobs = [
            job
            for result in job_results
            for job in result.get("jobs", [])
            if isinstance(job, dict)
        ]
        run_facts = [
            f"workflow run #{run.get('id', '?')}：{run.get('conclusion') or run.get('status') or 'unknown'}"
            for run in runs
        ]
        unavailable_facts = [
            f"job {job.get('name', job.get('id', '?'))}："
            f"{job.get('conclusion') or job.get('status') or 'unknown'}，日志暂不可用。"
            for job in jobs
            if job.get("log_unavailable")
        ]
        if self.reasoner is not None:
            value = self._complete_structured_task(
                context,
                prompt=_PROMPTS.render(
                    "agents.pull_request_ci",
                    request=request,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(guidance),
                ),
                schema=_CI_SCHEMA,
                tool_name="analyze_pull_request_ci",
            )
            analysis = {
                key: [str(item) for item in value.get(key, [])]
                for key in _CI_SCHEMA["required"]
            }
            analysis["facts"].extend(
                fact
                for fact in [*run_facts, *unavailable_facts]
                if fact not in analysis["facts"]
            )
            return analysis
        facts = list(run_facts)
        for job in jobs:
            if not job.get("log_unavailable"):
                facts.append(
                    f"job {job.get('name', job.get('id', '?'))}："
                    f"{str(job.get('log') or '').strip()}"
                )
        facts.extend(unavailable_facts)
        changed = [str(path) for path in evidence.get("changed_files", [])]
        return {
            "facts": facts or ["没有找到符合条件的 workflow run。"],
            "suspected_causes": ["需要结合失败日志与本次 Diff 验证根因。"]
            if facts
            else [],
            "related_changes": changed,
            "actions": ["针对失败 job 运行对应检查，并验证相关变更文件。"]
            if facts
            else [],
        }

    def _complete_structured_task(
        self,
        context: AgentContext,
        *,
        prompt: str,
        schema: dict[str, Any],
        tool_name: str,
    ) -> dict[str, Any]:
        """Allow bounded read-only tool use before one typed Coding result."""

        if self.reasoner is None:
            raise WorkflowError("autonomous Coding task requires a reasoner")
        context.start_message_thread()
        if not any(
            message.get("role") == "user" and message.get("content") == prompt
            for message in context.messages
        ):
            context.append_message({"role": "user", "content": prompt})
        tools = self.harness.llm_tools(context, read_only=True)
        allowed = {
            capability.id
            for capability in self.harness.discover(context)
            if capability.access == AccessLevel.READ
            and capability.input_schema is not None
        }
        final_tools = structured_tools(tool_name, schema, tools)
        for _ in range(context.max_steps):
            response = context.reason(self.reasoner, tools=final_tools)
            if len(response.calls) > self.harness.max_calls_per_turn:
                raise StructuredOutputError(
                    "CodingAgent response exceeds execution.max_calls_per_turn",
                    max_calls_per_turn=self.harness.max_calls_per_turn,
                    actual_calls=len(response.calls),
                )
            call_ids = [call.call_id for call in response.calls]
            if any(not call_id for call_id in call_ids) or len(set(call_ids)) != len(
                call_ids
            ):
                raise StructuredOutputError(
                    "CodingAgent calls must have unique non-empty call_id values"
                )
            historical_ids = [
                str(call.get("id") or "") for call in context.provider_tool_calls()
            ]
            if any(historical_ids.count(call_id) != 1 for call_id in call_ids):
                raise StructuredOutputError(
                    "CodingAgent call_id values must be unique in the message thread"
                )

            final_calls = [call for call in response.calls if call.name == tool_name]
            if final_calls:
                if len(response.calls) != 1:
                    raise StructuredOutputError(
                        f"{tool_name} must be the only structured call in its response",
                        expected_tool=tool_name,
                        actual_tools=[call.name for call in response.calls],
                    )
                call = final_calls[0]
                try:
                    validate_schema(
                        call.arguments, schema, label=f"{tool_name} result"
                    )
                except ValidationError as exc:
                    raise StructuredOutputError(str(exc)) from exc
                context.append_tool_result(
                    {"status": "accepted"}, call_id=call.call_id
                )
                return dict(call.arguments)

            if not response.calls:
                raise StructuredOutputError(
                    "CodingAgent returned Text where a typed structured result was required",
                    expected_tool=tool_name,
                )
            if any(call.name.startswith("agent__") for call in response.calls):
                raise WorkflowError("CodingAgent may not delegate another Agent")

            resolved_calls: list[CapabilityCall] = []
            for call in response.calls:
                try:
                    resolved = self.harness.resolve_model_call(call, context)
                except ValidationError as exc:
                    raise StructuredOutputError(str(exc)) from exc
                if not isinstance(resolved, CapabilityCall):
                    raise WorkflowError("CodingAgent may not delegate another Agent")
                if resolved.capability_id not in allowed:
                    raise WorkflowError(
                        "CodingAgent evidence gathering may use only visible READ capabilities"
                    )
                try:
                    permission = self.harness.capability_permission_decision(
                        context, resolved
                    )
                except ValidationError as exc:
                    raise StructuredOutputError(str(exc)) from exc
                if permission != "ALLOW":
                    raise WorkflowError(
                        "CodingAgent evidence capability is not directly permitted"
                    )
                resolved_calls.append(resolved)

            try:
                completed = self.dispatcher.execute_capability_batch(
                    context, resolved_calls, summary=response.text
                )
            except ValidationError as exc:
                raise WorkflowError(
                    f"CodingAgent capability batch violated the Runtime contract: {exc}"
                ) from exc
            if not completed:
                raise WorkflowError("CodingAgent evidence gathering was cancelled")
        raise WorkflowError(
            f"CodingAgent exceeded the {context.max_steps}-step evidence limit"
        )

    @staticmethod
    def _invoke_capability(
        context: AgentContext,
        capability_id: str,
        *,
        call_id: str,
        **arguments: Any,
    ) -> Any:
        record = context.invoke(
            capability_id, call_id=call_id, **arguments
        )
        if record.result.status == "failed":
            error = record.result.error
            if error is None:
                raise WorkflowError(
                    "Coding capability failed without a structured error"
                )
            context.append_tool_result(
                {
                    "status": "failed",
                    "capability_id": record.result.capability_id,
                    "error": error.type.value,
                    "message": error.message,
                },
                call_id=call_id,
            )
            context.observations.append(
                {
                    "kind": "capability_error",
                    "payload": {
                        "capability_id": record.result.capability_id,
                        "arguments": dict(arguments),
                        "error": error.type.value,
                        "message": error.message,
                        "details": error.details,
                        "attempts": record.result.attempts,
                    },
                }
            )
            raise _CodingCapabilityFailure(
                {
                    "capability_id": record.result.capability_id,
                    "arguments": dict(arguments),
                    "error": error.type.value,
                    "message": error.message,
                    "details": error.details,
                    "attempts": record.result.attempts,
                }
            )
        if record.result.status != "success":
            raise WorkflowError(
                f"Coding capability returned unsupported status: {record.result.status}"
            )
        context.append_tool_result(record.observation_data, call_id=call_id)
        context.observations.append(
            {
                "kind": "capability",
                "payload": {
                    "capability_id": capability_id,
                    "arguments": dict(arguments),
                    "data": record.observation_data,
                },
            }
        )
        return record.result.content

    def _create(
        self,
        context: AgentContext,
        request: ChangeRequest,
        guidance: AgentGuidance | None,
    ) -> CandidatePreparationResult:
        if request.source_ref is None:
            default = self._invoke_capability(context, "repository.get_default_branch")
            request.base_branch = str(default["branch"])
            request.source_ref = str(default["commit_sha"])
        explicit_request = bool(
            request.proposed_files or request.replacements or request.deleted_files
        )
        if explicit_request:
            explicit_paths = list(
                dict.fromkeys(
                    [
                        *request.proposed_files,
                        *[item.path for item in request.replacements],
                        *request.deleted_files,
                    ]
                )
            )
            existing_files = self._existing_files(context, request, explicit_paths)
            planned_changes = self._explicit_change_plan(request, existing_files)
        elif self.reasoner is not None:
            planned_changes = self._reasoned_change_plan(context, request, guidance)
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
        existing_paths = [
            change["path"] for change in planned_changes if change["action"] != "ADD"
        ]
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
            raise WorkflowError(
                "a target file exceeds the safe fetch bound; refusing to risk a truncated overwrite"
            )
        originals = {item["path"]: item["content"] for item in fetched}

        deleted_files = [
            change["path"] for change in planned_changes if change["action"] == "DELETE"
        ]
        if explicit_request:
            new_files = dict(request.proposed_files)
            for replacement in request.replacements:
                if replacement.path not in originals:
                    raise WorkflowError(
                        f"replacement target was not fetched: {replacement.path}"
                    )
                content = new_files.get(replacement.path, originals[replacement.path])
                count = content.count(replacement.old)
                if count != 1:
                    raise WorkflowError(
                        f"replacement anchor in {replacement.path} must match exactly once; found {count}"
                    )
                new_files[replacement.path] = content.replace(
                    replacement.old, replacement.new, 1
                )
        else:
            planned_operations = [
                {
                    "id": f"change-{index}",
                    "action": change["action"],
                    "path": change["path"],
                }
                for index, change in enumerate(planned_changes, start=1)
            ]
            writable_operations = [
                operation
                for operation in planned_operations
                if operation["action"] in {"ADD", "MODIFY"}
            ]
            evidence = self._capability_evidence(context)
            new_files = {}
            for operation in writable_operations:
                try:
                    content = context.complete_text(
                        self.reasoner,
                        prompt=_PROMPTS.render(
                            "agents.coding_create",
                            repository=request.repository,
                            description=request.description,
                            operation=json.dumps(operation, ensure_ascii=False),
                            current_content=originals.get(operation["path"], ""),
                            evidence=json.dumps(evidence, ensure_ascii=False),
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
        guidance: AgentGuidance | None,
    ) -> list[dict[str, Any]]:
        if self.reasoner is None:
            raise WorkflowError("repository change planning requires a reasoner")
        mentioned = list(
            dict.fromkeys(safe_repository_path(path) for path in request.target_files)
        )
        value = self._complete_structured_task(
            context,
            prompt=json.dumps(
                {
                    "request": request.description,
                    "source_ref": request.source_ref,
                    "mentioned_paths": mentioned,
                    "guidance": guidance_section(guidance),
                    "instruction": (
                        "Use the available read-only capabilities autonomously until you can return every requested "
                        "file operation in one plan. ADD is only for a new path; MODIFY and DELETE target existing "
                        "files. Keep explicitly supplied paths; path existence is validated deterministically next."
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
                raise ValidationError(
                    f"repository change plan contains duplicate path: {path}"
                )
            seen.add(path)
            changes.append(
                {
                    "action": action,
                    "path": path,
                }
            )
        return changes

    @staticmethod
    def _explicit_change_plan(
        request: ChangeRequest, existing_files: set[str]
    ) -> list[dict[str, str]]:
        actions: dict[str, str] = {}

        def add(path: str, action: str) -> None:
            safe = safe_repository_path(path)
            previous = actions.get(safe)
            if previous is not None and previous != action:
                raise ValidationError(
                    f"conflicting repository operations for {safe}: {previous} and {action}"
                )
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
    def _validate_change_plan(
        changes: list[dict[str, Any]], existing_files: set[str]
    ) -> None:
        for change in changes:
            action = change["action"]
            path = change["path"]
            if action == "ADD" and path in existing_files:
                raise ValidationError(
                    f"repository change plan cannot add an existing file: {path}"
                )
            if action in {"MODIFY", "DELETE"} and path not in existing_files:
                raise ValidationError(
                    f"repository change plan cannot {action.casefold()} a missing file: {path}"
                )

    @staticmethod
    def _capability_evidence(context: AgentContext) -> list[dict[str, Any]]:
        return [
            dict(observation.get("payload") or {})
            for observation in context.observations
            if observation.get("kind") in {"capability", "capability_error"}
        ][-40:]

    def _repair(
        self,
        context: AgentContext,
        request: ChangeRequest,
        candidate: CandidatePatch,
        errors: list[str],
        guidance: AgentGuidance | None,
    ) -> CandidatePreparationResult:
        self._complete_structured_task(
            context,
            prompt=json.dumps(
                {
                    "task": (
                        "Inspect any additional read-only repository, RAG, Context7, or Skill evidence needed "
                        "to repair this verified candidate. Finish when the reported errors can be addressed."
                    ),
                    "request": request.description,
                    "changed_files": candidate.changed_files,
                    "verification_errors": errors,
                },
                ensure_ascii=False,
            ),
            schema=_EVIDENCE_READY_SCHEMA,
            tool_name="record_repair_evidence",
        )
        evidence = self._capability_evidence(context)
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
            content = context.complete_text(
                self.reasoner,
                prompt=_PROMPTS.render(
                    "agents.coding_repair",
                    description=request.description,
                    operation=json.dumps(
                        {key: operation[key] for key in ("id", "action", "path")},
                        ensure_ascii=False,
                    ),
                    current_content=operation["content"],
                    errors=json.dumps(errors, ensure_ascii=False),
                    evidence=json.dumps(evidence, ensure_ascii=False),
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
            raise WorkflowError(
                "a target file exceeds the safe fetch bound; refusing a truncated repair"
            )
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
            raise WorkflowError(
                f"deletion target was not fetched: {', '.join(sorted(missing_deletions))}"
            )
        overlap = set(proposed) & set(deleted)
        if overlap:
            raise ValidationError(
                f"candidate cannot write and delete the same file: {', '.join(sorted(overlap))}"
            )
        added = sorted(path for path in proposed if path not in originals)
        modified = sorted(
            path
            for path, content in proposed.items()
            if path in originals and originals[path] != content
        )
        unchanged = sorted(
            path
            for path in proposed
            if path in originals and originals[path] == proposed[path]
        )
        if unchanged:
            raise WorkflowError(
                f"candidate does not modify the requested file(s): {', '.join(unchanged)}"
            )
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
        return any(
            segment in {"test", "tests", "spec", "specs"} for segment in segments[:-1]
        ) or any(marker in segments[-1] for marker in ("_test.", ".test.", ".spec."))
