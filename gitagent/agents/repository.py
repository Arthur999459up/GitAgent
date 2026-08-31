"""Autonomous Repository Domain Agent with protected code mutation."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop import AgentAction, AgentActionKind, rejection_feedback
from gitagent.domain.errors import ValidationError, WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    ChangeRequest,
    DomainAction,
    RepositoryOperation,
    RepositoryResult,
    Route,
)
from gitagent.harness.context import capability_attempted
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.validation.static import StaticVerifier
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .coding import (
    CodingAgent,
    prepare_verified_candidate,
    record_candidate_capability_error,
)
from .decide import decide_action

_PROMPTS = get_prompt_library()
_OPERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": [operation.value for operation in RepositoryOperation],
        },
    },
    "required": ["operation"],
    "additionalProperties": False,
}


REPOSITORY_SPEC = AgentSpec(
    name="repository",
    role=(
        "Autonomously explore, search, explain, analyze, plan, inspect history, "
        "and coordinate verified repository modifications."
    ),
    system_prompt=_PROMPTS.text("system.repository"),
    output_schema=("action", "operation", "answer", "files", "symbols", "reasoning"),
    routes=frozenset({Route.REPOSITORY}),
    required_context=("repository",),
    routing_examples=(
        "解释认证模块的调用链",
        "format_name 在哪里实现？",
        "分析修改配置加载器的影响范围",
        "这个文件最近为什么改过？",
        "为配置加载器增加超时参数",
    ),
)


class RepositoryAgent:
    """Choose repository capabilities autonomously; protect actual code writes."""

    def __init__(
        self,
        harness: AgentHarness,
        coding: CodingAgent,
        verifier: StaticVerifier,
        reasoner: Reasoner | None = None,
    ) -> None:
        self.harness = harness
        self.coding = coding
        self.verifier = verifier
        self.reasoner = reasoner
        harness.register(REPOSITORY_SPEC)

    def decide(self, context: AgentContext) -> AgentAction:
        if not context.operation:
            self._select_operation(context)
        operation = self._operation(context)
        applied = self._last_capability(context, "github.commit_to_default_branch")
        if applied is not None:
            branch = str(applied.get("branch") or "default branch")
            commit = str(applied.get("commit") or "")
            return AgentAction(
                AgentActionKind.FINISH,
                summary="仓库变更已提交",
                message=f"已直接提交到 `{branch}`"
                + (f"，Commit `{commit}`。" if commit else "。"),
            )

        feedback = rejection_feedback(context)
        if feedback is not None and not feedback:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="已放弃",
                message="已按你的要求放弃，未执行任何仓库写入。",
            )

        if self.reasoner is None:
            return self._minimal_fallback(context, operation)
        protected = (
            (AgentActionKind.PREPARE_CODE_CHANGE,)
            if operation == RepositoryOperation.MODIFY
            else ()
        )
        action = decide_action(
            context,
            self.harness,
            self.reasoner,
            protected_kinds=protected,
        )
        if action.kind == AgentActionKind.PREPARE_CODE_CHANGE:
            return self._prepare_change(context)
        if action.capability_id == "github.commit_to_default_branch":
            raise WorkflowError(
                "default-branch writes require a verified CandidatePatch and protected mutation action"
            )
        return action

    def _select_operation(self, context: AgentContext) -> None:
        if self.reasoner is None:
            context.operation = RepositoryOperation.SEARCH.value
            return
        raw = context.reason_structured(
            self.reasoner,
            schema=_OPERATION_SCHEMA,
            tool_name="select_repository_operation",
        )
        context.record_model_response(raw, tool_name="select_repository_operation")
        try:
            operation = RepositoryOperation(str(raw.get("operation") or ""))
        except ValueError as exc:
            raise ValidationError(
                "Repository Agent selected an unknown operation"
            ) from exc
        context.operation = operation.value
        if operation == RepositoryOperation.MODIFY and context.change_request is None:
            context.change_request = ChangeRequest(
                repository=context.repository,
                description=context.goal,
            )
        context.complete_control_call({"operation": operation.value})

    def build_result(self, context: AgentContext) -> RepositoryResult:
        operation = self._operation(context)
        if operation == RepositoryOperation.MODIFY:
            files = (
                list(context.code_candidate.changed_files)
                if context.code_candidate is not None
                else []
            )
            return RepositoryResult(
                action=DomainAction.ANSWER,
                operation=operation,
                answer=context.final_message or "代码变更流程已完成。",
                files=files,
                candidate=context.code_candidate,
                verification=context.verification,
                reasoning=(
                    "CodingAgent produced the candidate; StaticVerifier and explicit approval "
                    "guarded the default-branch mutation."
                ),
            )

        evidence = self._evidence(context)
        files = self._evidence_paths(evidence)
        symbols = self._evidence_symbols(evidence)
        answer = context.final_message or "仓库证据收集已完成。"
        if operation in {
            RepositoryOperation.EXPLAIN,
            RepositoryOperation.IMPACT_ANALYZE,
        }:
            interpretation = self.coding.explain(
                context.repository,
                context.goal,
                {"observations": evidence, "changed_files": files},
                session_id=context.session_id,
                guidance=context.guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                answer,
                files=files,
                symbols=list(dict.fromkeys([*symbols, *interpretation.key_symbols])),
                interpretation=interpretation,
                reasoning="The model selected evidence; CodingAgent produced the typed interpretation.",
            )
        if operation == RepositoryOperation.PLAN:
            plan = self.coding.plan(
                context.repository,
                context.goal,
                {"observations": evidence, "changed_files": files},
                session_id=context.session_id,
                guidance=context.guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                answer,
                files=list(dict.fromkeys([*files, *plan.files])),
                symbols=symbols,
                plan=plan,
                reasoning="The model selected evidence; CodingAgent produced the non-mutating plan.",
            )
        if operation == RepositoryOperation.HISTORY:
            history = (
                self._last_capability(context, "repository.get_file_history") or {}
            )
            path = str(history.get("path") or "")
            commits = list(history.get("commits") or [])
            return RepositoryResult(
                DomainAction.ANSWER if path else DomainAction.CLARIFY,
                operation,
                answer if path else "需要明确要查看历史的文件路径。",
                files=[path] if path else files,
                symbols=symbols,
                history=commits,
                question="" if path else "请指定要查看提交历史的文件路径。",
                reasoning="The model autonomously selected repository history evidence.",
            )
        return RepositoryResult(
            DomainAction.ANSWER,
            operation,
            answer,
            files=files,
            symbols=symbols,
            reasoning="Repository capabilities were selected autonomously from current observations.",
        )

    def _prepare_change(self, context: AgentContext) -> AgentAction:
        if context.change_request is None:
            context.change_request = ChangeRequest(
                repository=context.repository,
                description=context.goal,
            )
        prepared = prepare_verified_candidate(
            self.coding,
            self.verifier,
            context.change_request,
            session_id=context.session_id,
            guidance=context.guidance,
        )
        if record_candidate_capability_error(context, prepared):
            context.complete_control_call(
                {"status": "failed", "capability_error": prepared.capability_error}
            )
            if self.reasoner is None:
                return AgentAction(
                    AgentActionKind.FINISH,
                    summary="候选补丁准备失败",
                    message=str(
                        (prepared.capability_error or {}).get("message")
                        or "候选补丁准备失败。"
                    ),
                )
            action = decide_action(
                context,
                self.harness,
                self.reasoner,
                protected_kinds=(AgentActionKind.PREPARE_CODE_CHANGE,),
            )
            if action.kind == AgentActionKind.PREPARE_CODE_CHANGE:
                return AgentAction(
                    AgentActionKind.FINISH,
                    summary="候选补丁准备失败",
                    message="Coding capability 失败后不能在没有新证据时立即重复相同候选生成。",
                )
            return action
        if prepared.candidate is None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="模型未生成文件内容",
                message=prepared.message,
            )
        if prepared.verification is None or not prepared.verification.passed:
            raise WorkflowError("静态验证失败；拒绝生成默认分支写入提案")
        context.code_candidate = prepared.candidate
        context.verification = prepared.verification
        context.observations.append(
            {
                "kind": "agent",
                "payload": {
                    "agent": "coding",
                    "summary": prepared.candidate.summary,
                    "changed_files": list(prepared.candidate.changed_files),
                    "verification_passed": True,
                },
            }
        )
        return AgentAction(
            AgentActionKind.APPLY_REPOSITORY_CHANGE,
            summary="将已验证的多文件变更提交到默认分支",
        )

    def _minimal_fallback(
        self,
        context: AgentContext,
        operation: RepositoryOperation,
    ) -> AgentAction:
        """A deliberately small provider-less degradation, not a second workflow."""

        if operation == RepositoryOperation.MODIFY:
            return self._prepare_change(context)
        if operation == RepositoryOperation.HISTORY:
            return AgentAction(
                AgentActionKind.ASK,
                summary="缺少文件路径",
                question="请指定要查看提交历史的文件路径。",
            )
        arguments = {"depth": 4, "max_entries": 300}
        if not capability_attempted(
            context, "repository.get_repo_tree", arguments=arguments
        ):
            return AgentAction(
                AgentActionKind.CAPABILITY,
                capability_id="repository.get_repo_tree",
                arguments=arguments,
                summary="读取有界仓库结构",
            )
        return AgentAction(
            AgentActionKind.FINISH,
            summary="仓库结构证据已收集",
            message="仓库结构证据已收集；当前未配置可继续自主分析的模型。",
        )

    @staticmethod
    def _operation(context: AgentContext) -> RepositoryOperation:
        try:
            return RepositoryOperation(context.operation)
        except ValueError as exc:
            raise WorkflowError("Repository Agent requires a valid operation") from exc

    @staticmethod
    def _evidence(context: AgentContext) -> list[dict[str, Any]]:
        return [
            dict(observation.get("payload") or {})
            for observation in context.observations
            if observation.get("kind") in {"capability", "capability_error"}
        ]

    @staticmethod
    def _evidence_paths(evidence: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []
        for payload in evidence:
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                continue
            if data.get("path"):
                paths.append(str(data["path"]))
            paths.extend(str(item) for item in data.get("entries", []) if item)
            for item in data.get("results", []):
                if isinstance(item, dict) and item.get("path"):
                    paths.append(str(item["path"]))
            for item in data.get("files", []):
                if isinstance(item, dict) and item.get("path"):
                    paths.append(str(item["path"]))
                elif isinstance(item, str):
                    paths.append(item)
        return list(dict.fromkeys(paths))[:120]

    @staticmethod
    def _evidence_symbols(evidence: list[dict[str, Any]]) -> list[str]:
        symbols = []
        for payload in evidence:
            data = payload.get("data") or {}
            if isinstance(data, dict) and data.get("symbol"):
                symbols.append(str(data["symbol"]))
        return list(dict.fromkeys(symbols))

    @staticmethod
    def _last_capability(
        context: AgentContext, capability_id: str
    ) -> dict[str, Any] | None:
        for observation in reversed(context.observations):
            payload = observation.get("payload") or {}
            if (
                observation.get("kind") == "capability"
                and payload.get("capability_id") == capability_id
            ):
                data = payload.get("data")
                return dict(data) if isinstance(data, dict) else None
        return None
