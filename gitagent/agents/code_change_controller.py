"""Scripted controller for direct code-change routes and approved patch application."""

from __future__ import annotations

from typing import Any

from ..core.errors import WorkflowError
from ..core.models import AgentSpec
from ..runtime import AgentAction, AgentActionKind, AgentContext, rejection_feedback
from ..verification import StaticVerifier
from .coding import CodingAgent, prepare_verified_candidate

CODE_CHANGE_SPEC = AgentSpec(
    name="code_change",
    role="Coordinate candidate generation, static verification, and approved Draft PR application.",
    system_prompt="Coordinate an already-routed code change without independently selecting tools.",
    allowed_tools=frozenset(),
    output_schema=(),
    capabilities=frozenset(),
)


class CodeChangeController:
    def __init__(self, coding: CodingAgent, verifier: StaticVerifier) -> None:
        self.coding = coding
        self.verifier = verifier
        coding.harness.register(CODE_CHANGE_SPEC)

    def decide(self, context: AgentContext) -> AgentAction:
        if context.change_request is None:
            raise WorkflowError("code_change requires a change request")
        draft = self._last_tool(context, "github.create_draft_pr")
        if draft is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="代码变更已应用",
                message=f"已创建修复 Draft PR #{draft.get('number')}。",
            )
        if rejection_feedback(context) is not None and not rejection_feedback(context):
            return AgentAction(
                AgentActionKind.FINISH,
                summary="已放弃",
                message="已按你的要求放弃，未执行任何写入。",
            )
        if context.code_candidate is None:
            candidate, report = prepare_verified_candidate(
                self.coding,
                self.verifier,
                context.change_request,
                session_id=context.session_id,
                guidance=context.guidance,
            )
            context.code_candidate = candidate
            context.verification = report
            context.observations.append(
                {
                    "kind": "agent",
                    "payload": {
                        "agent": "coding",
                        "summary": candidate.summary,
                        "changed_files": list(candidate.changed_files),
                        "verification_passed": report.passed,
                    },
                }
            )
            if not report.passed:
                raise WorkflowError("静态验证失败；拒绝生成 GitHub 变更提案")
        return AgentAction(
            AgentActionKind.APPLY_CODE_CHANGE,
            summary="将已通过静态验证的补丁以 Draft PR 形式应用",
        )

    def build_result(self, context: AgentContext) -> dict[str, Any]:
        if context.change_request is None:
            raise WorkflowError("code_change requires a change request")
        draft = self._last_tool(context, "github.create_draft_pr")
        return {
            "draft_pr": {"number": draft.get("number")} if draft else None,
            "summary": context.change_request.description,
        }

    def run_specialist(self, context: AgentContext, specialist: str) -> dict[str, Any]:
        raise WorkflowError(f"code_change agent has no approved specialist: {specialist}")

    @staticmethod
    def _last_tool(context: AgentContext, tool: str) -> Any:
        for observation in reversed(context.observations):
            if observation["kind"] == "tool" and observation["payload"].get("tool") == tool:
                return observation["payload"].get("data")
        return None
