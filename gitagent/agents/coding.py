"""Candidate-patch agent. It has no GitHub mutation capabilities or test runner."""

from __future__ import annotations

import json
from dataclasses import asdict
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
    StructuredOutputError,
    ValidationError,
    WorkflowError,
)
from gitagent.domain.models import (
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    CodeReviewResult,
    CodingTask,
    Recommendation,
    VerificationCheck,
    VerificationReport,
)
from gitagent.domain.reviews import canonical_review_event
from gitagent.harness.coding_workspace import CodingWorkspace
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import (
    AgentHarness,
    ExecutionProfile,
    ResourceClaims,
)
from gitagent.harness.structured_call_dispatcher import StructuredCallDispatcher
from gitagent.harness.validation.code import deterministic_code_checks
from gitagent.model import Reasoner, structured_tools
from gitagent.prompts import get_prompt_library

from .guidance import guidance_section

_PROMPTS = get_prompt_library()

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

_FINISH_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "runtime__finish_coding_patch",
        "description": (
            "Finish the patch only after the worktree has real changes and the current "
            "revision has been covered by a real validation command."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


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
        github: Any | None = None,
    ) -> None:
        self.harness = harness
        self.reasoner = reasoner
        self.github = github
        self.dispatcher = StructuredCallDispatcher(harness)
        harness.register(CODING_SPEC)

    def step(self, context: AgentContext) -> ModelResponse | WaitForUser:
        """Execute one typed Coding task inside the shared Agent Runtime."""

        task = context.coding_task
        if task is None:
            raise WorkflowError("Coding context is missing its typed task")
        if task.mode == "patch":
            return self._step_patch(context, task)
        if not self._nonpatch_prepared(context, task.mode):
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

    @staticmethod
    def _nonpatch_prepared(context: AgentContext, mode: str) -> bool:
        return {
            "explain": context.code_explanation is not None,
            "review": context.code_review is not None,
            "plan": context.code_plan is not None,
            "review_dialogue": context.review_dialogue is not None,
            "ci": context.ci_analysis is not None,
        }.get(mode, False)

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
        raise ValidationError(f"unknown Coding Agent mode: {mode}")

    @staticmethod
    def _store_artifact(context: AgentContext, mode: str, artifact: Any) -> None:
        if isinstance(artifact, CodeExplanationResult):
            context.code_explanation = artifact
        elif isinstance(artifact, CodeReviewResult):
            context.code_review = artifact
        elif isinstance(artifact, CodePlanResult):
            context.code_plan = artifact
        elif mode == "review_dialogue" and isinstance(artifact, dict):
            context.review_dialogue = artifact
        elif mode == "ci" and isinstance(artifact, dict):
            context.ci_analysis = artifact

    def _step_patch(self, context: AgentContext, task: CodingTask) -> ModelResponse:
        if self.reasoner is None:
            raise WorkflowError("Coding patch calls require a configured reasoner")
        request = task.change_request
        if request is None:
            raise WorkflowError("Coding patch call requires a ChangeRequest")
        if context.coding_task_completed:
            message = context.append_message(
                {"role": "assistant", "content": "Patch workspace already finalized."}
            )
            return ModelResponse("Patch workspace already finalized.", [], message)
        if context.coding_workspace is None:
            self._initialize_patch_workspace(context, request)

        tools = [*self.harness.llm_tools(context), _FINISH_PATCH_TOOL]
        response = context.reason(self.reasoner, tools=tools)
        finish_calls = [
            call for call in response.calls if call.name == "runtime__finish_coding_patch"
        ]
        if finish_calls:
            if len(response.calls) != 1:
                raise StructuredOutputError(
                    "runtime__finish_coding_patch must be the only structured call in its response"
                )
            call = finish_calls[0]
            if call.arguments:
                raise StructuredOutputError(
                    "runtime__finish_coding_patch accepts no arguments"
                )
            rejection = self._finalize_patch(context, request)
            if rejection is not None:
                context.append_tool_result(
                    {"status": "rejected", "reason": rejection}, call_id=call.call_id
                )
                context.append_message(
                    {
                        "role": "user",
                        "content": f"Runtime patch finalization rejected: {rejection}",
                    }
                )
                return ModelResponse("", [], {"role": "assistant", "content": ""})
            context.append_tool_result(
                {"status": "accepted"}, call_id=call.call_id
            )
            terminal = "Patch workspace finalized and cleaned up."
            message = context.append_message({"role": "assistant", "content": terminal})
            return ModelResponse(terminal, [], message)

        if not response.calls:
            context.append_message(
                {
                    "role": "user",
                    "content": (
                        "Patch mode is not complete. Continue using the available worktree tools, "
                        "then call runtime__finish_coding_patch after validating the current revision."
                    ),
                }
            )
        return response

    def _initialize_patch_workspace(
        self, context: AgentContext, request: ChangeRequest
    ) -> None:
        if self.github is None:
            raise WorkflowError("Coding patch calls require the configured GitHub client")
        context.change_request = request
        if request.source_ref is None:
            default = self._initialization_capability(
                context, "repository.get_default_branch", {}
            )
            request.base_branch = str(default["branch"])
            request.source_ref = str(default["commit_sha"])
        context.coding_workspace = CodingWorkspace.create(
            self.github,
            repository=request.repository,
            source_ref=str(request.source_ref),
            task_id=context.run_id,
            coordinator=self.harness.coordinator,
        )
        context.append_message(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "runtime_patch_request": asdict(request),
                        "instruction": (
                            "Work only through the available tools. Inspect before editing, make the smallest "
                            "change, run relevant real validation, fix failures in the same loop, and finish "
                            "with runtime__finish_coding_patch. Do not request approval during this patch stage."
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )

    @staticmethod
    def _initialization_capability(
        context: AgentContext, capability_id: str, arguments: dict[str, Any]
    ) -> Any:
        call_id = context.ensure_capability_tool_call(capability_id, arguments)
        record = context.invoke(capability_id, call_id=call_id, **arguments)
        if record.result.status != "success":
            error = record.result.error
            context.append_tool_result(
                {
                    "status": record.result.status,
                    "message": error.message if error is not None else "capability failed",
                },
                call_id=call_id,
            )
            raise WorkflowError(
                error.message if error is not None else f"{capability_id} failed"
            )
        context.append_tool_result(record.observation_data, call_id=call_id)
        return record.result.content

    def _finalize_patch(
        self, context: AgentContext, request: ChangeRequest
    ) -> str | None:
        workspace = context.coding_workspace
        if workspace is None:
            raise WorkflowError("Coding patch finalization has no active workspace")
        snapshot = workspace.snapshot()
        if not snapshot["changed_files"]:
            return "the worktree has no real changes"
        validation_events = [
            dict(observation.get("payload") or {})
            for observation in context.observations
            if observation.get("kind") == "coding_verification"
        ]
        current_validation_events = [
            event
            for event in validation_events
            if int(event.get("revision", -1)) == workspace.revision
        ]
        if not current_validation_events:
            return "the current revision has no recorded real validation result"
        unavailable_validation = any(
            bool(event.get("unavailable_reason")) for event in current_validation_events
        )
        if (
            workspace.last_validated_revision != workspace.revision
            and not unavailable_validation
        ):
            return (
                f"revision {workspace.revision} is not covered by a real validation command; "
                "run a relevant test, lint, type-check, or build command after the last edit"
            )

        checks: list[VerificationCheck] = []
        skipped: list[str] = []
        for index, event in enumerate(validation_events, start=1):
            unavailable = str(event.get("unavailable_reason") or "")
            exit_code = event.get("exit_code")
            event_revision = int(event.get("revision", -1))
            current_revision = event_revision == workspace.revision
            passed = not unavailable and exit_code == 0
            command = str(event.get("command") or "")
            details = unavailable or (
                f"revision={event_revision}; command={command!r}; exit_code={exit_code}; "
                f"stdout={str(event.get('stdout_tail') or '')[-1200:]}; "
                f"stderr={str(event.get('stderr_tail') or '')[-1200:]}"
            )
            if not current_revision and not passed:
                details = f"superseded by later edits; {details}"
            status = "PASS" if passed else ("FAIL" if current_revision else "WARN")
            checks.append(
                VerificationCheck(
                    name=f"real_validation_{index}",
                    status=status,
                    details=details,
                    files=list(snapshot["changed_files"]),
                )
            )
            if unavailable:
                skipped.append(f"{command}: {unavailable}")
        checks.extend(deterministic_code_checks(snapshot["files"]))
        report = VerificationReport(
            passed=all(check.status != "FAIL" for check in checks),
            checks=checks,
            skipped=skipped,
            attempts=len(validation_events),
        )
        follow_up = [
            f"Resolve failing validation: {check.name}"
            for check in checks
            if check.status == "FAIL"
        ]
        candidate = CandidatePatch(
            summary=request.suggested_title or request.description,
            root_cause=request.description,
            added_files=list(snapshot["added_files"]),
            modified_files=list(snapshot["modified_files"]),
            deleted_files=list(snapshot["deleted_files"]),
            patch=str(snapshot["patch"]),
            files=dict(snapshot["files"]),
            risks=["Model-authored changes require review before remote mutation."],
            verification_required=follow_up,
        )
        request.target_files = list(snapshot["changed_files"])

        workspace.cleanup()
        context.coding_workspace = None
        context.code_candidate = candidate
        context.verification = report
        context.coding_task_completed = True
        return None

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
    def _is_test_path(path: str) -> bool:
        lowered = path.casefold()
        segments = lowered.split("/")
        return any(
            segment in {"test", "tests", "spec", "specs"} for segment in segments[:-1]
        ) or any(marker in segments[-1] for marker in ("_test.", ".test.", ".spec."))
