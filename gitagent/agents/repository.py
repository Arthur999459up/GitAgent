"""Repository Domain Agent using native Capability and Coding Agent calls."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop import (
    AgentCall,
    AgentResult,
    ModelResponse,
    rejection_feedback,
)
from gitagent.domain.errors import WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    CandidatePreparationResult,
    ChangeRequest,
    CodeExplanationResult,
    CodePlanResult,
    DomainAction,
    RepositoryOperation,
    RepositoryResult,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.validation.static import StaticVerifier
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .coding import CodingAgent

_PROMPTS = get_prompt_library()
_CODING_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "minLength": 1},
        "mode": {"type": "string", "enum": ["explain", "plan", "patch"]},
    },
    "required": ["task", "mode"],
    "additionalProperties": False,
}


REPOSITORY_SPEC = AgentSpec(
    name="repository",
    role=(
        "Explore, explain, analyze, plan, inspect history, and coordinate "
        "verified repository modifications."
    ),
    system_prompt=_PROMPTS.text("system.repository"),
    output_schema=("action", "operation", "answer", "files", "symbols", "reasoning"),
)


class RepositoryAgent:
    def __init__(
        self,
        harness: AgentHarness,
        coding: CodingAgent,
        verifier: StaticVerifier,
        reasoner: Reasoner,
    ) -> None:
        self.harness = harness
        self.coding = coding
        self.verifier = verifier
        self.reasoner = reasoner
        harness.register(REPOSITORY_SPEC)

    def agent_schemas(self) -> dict[str, dict[str, Any]]:
        return {"coding": _CODING_SCHEMA}

    def step(self, context: AgentContext) -> ModelResponse:
        feedback = rejection_feedback(context)
        if feedback is not None and not feedback:
            return self._text(
                context,
                "已按你的要求放弃，未执行任何仓库写入。",
            )
        tools = [
            *self.harness.llm_tools(context),
            self.harness.agent_tool(
                "coding",
                (
                    "Delegate a self-contained repository explanation, plan, or verified "
                    "candidate-patch task to a fresh Coding Agent."
                ),
                _CODING_SCHEMA,
            ),
        ]
        return context.reason(self.reasoner, tools=tools)

    def invoke_child(
        self, context: AgentContext, call: AgentCall
    ) -> AgentResult:
        if call.agent_id != "coding":
            raise WorkflowError(f"RepositoryAgent cannot call {call.agent_id}")
        mode = str(call.arguments["mode"])
        task = str(call.arguments["task"])
        request = None
        if mode == "patch":
            request = context.change_request or ChangeRequest(
                repository=context.repository,
                description=task,
            )
            context.change_request = request
        result, artifact = self.coding.run_call(
            context,
            call_id=call.call_id,
            mode=mode,
            task=task,
            evidence={
                "observations": self._evidence(context),
                "changed_files": self._evidence_paths(self._evidence(context)),
            },
            change_request=request,
            verifier=self.verifier,
        )
        if isinstance(artifact, CandidatePreparationResult):
            context.code_candidate = artifact.candidate
            context.verification = artifact.verification
            if artifact.capability_error:
                context.observations.append(
                    {"kind": "capability_error", "payload": artifact.capability_error}
                )
        elif isinstance(artifact, CodeExplanationResult):
            context.code_explanation = artifact
        elif isinstance(artifact, CodePlanResult):
            context.code_plan = artifact
        return result

    @staticmethod
    def after_agent_result(
        context: AgentContext,
        call: AgentCall,
        result: AgentResult,
        dispatcher: Any,
    ) -> None:
        if call.arguments.get("mode") != "patch" or result.status != "completed":
            return
        if context.code_candidate is None:
            return
        if context.verification is None or not context.verification.passed:
            raise WorkflowError(
                "static verification failed; refusing a default-branch proposal"
            )
        dispatcher.queue_repository_change(context)

    def build_result(self, context: AgentContext) -> RepositoryResult:
        evidence = self._evidence(context)
        operation = self._result_operation(context, evidence)
        files = self._evidence_paths(evidence)
        if context.code_candidate is not None:
            files = list(dict.fromkeys([*files, *context.code_candidate.changed_files]))
        symbols = self._evidence_symbols(evidence)
        if context.code_explanation is not None:
            symbols = list(
                dict.fromkeys([*symbols, *context.code_explanation.key_symbols])
            )
        history = self._last_capability(context, "repository.get_file_history") or {}
        return RepositoryResult(
            action=DomainAction.ANSWER,
            operation=operation,
            answer=context.final_message or "仓库请求已处理。",
            files=files,
            symbols=symbols,
            reasoning="Model-native calls selected evidence and any Coding delegation.",
            history=list(history.get("commits") or []),
            interpretation=context.code_explanation,
            plan=context.code_plan,
            candidate=context.code_candidate,
            verification=context.verification,
        )

    @staticmethod
    def _text(context: AgentContext, content: str) -> ModelResponse:
        message = context.append_message({"role": "assistant", "content": content})
        return ModelResponse(content, None, message)

    @staticmethod
    def _result_operation(
        context: AgentContext, evidence: list[dict[str, Any]]
    ) -> RepositoryOperation:
        if context.code_candidate is not None:
            return RepositoryOperation.MODIFY
        if context.code_explanation is not None:
            return RepositoryOperation.EXPLAIN
        if context.code_plan is not None:
            return RepositoryOperation.PLAN
        capability_ids = [str(item.get("capability_id") or "") for item in evidence]
        if "repository.get_file_history" in capability_ids:
            return RepositoryOperation.HISTORY
        if any(
            item in {"repository.search_code", "repository.find_symbol", "repository.find_references"}
            for item in capability_ids
        ):
            return RepositoryOperation.SEARCH
        return RepositoryOperation.EXPLORE

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
            for key in ("results", "files"):
                for item in data.get(key, []):
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
