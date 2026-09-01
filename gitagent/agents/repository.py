"""Repository Domain Agent using native Capability and Coding Agent calls."""

from __future__ import annotations

from typing import Any

from gitagent.agent_loop import (
    AgentCall,
    AgentResult,
    ModelResponse,
    WaitForUser,
    explicit_wait,
    rejection_feedback,
    wait_for_user_tool,
)
from gitagent.domain.errors import WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    ChangeRequest,
    CodingTask,
    RepositoryOperation,
    RepositoryResult,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness, ExecutionProfile
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

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
    output_schema=("operation", "answer", "files", "symbols", "reasoning"),
    agent_depth=1,
    execution_profile=ExecutionProfile.concurrent(),
)


class RepositoryAgent:
    def __init__(
        self,
        harness: AgentHarness,
        reasoner: Reasoner,
    ) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(REPOSITORY_SPEC)

    def agent_schemas(self) -> dict[str, dict[str, Any]]:
        return {"coding": _CODING_SCHEMA}

    def step(self, context: AgentContext) -> ModelResponse | WaitForUser:
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
            wait_for_user_tool(),
        ]
        return explicit_wait(context.reason(self.reasoner, tools=tools))

    def prepare_child(
        self,
        context: AgentContext,
        call: AgentCall,
        child: AgentContext,
    ) -> None:
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
        child.coding_task = CodingTask(
            mode=mode,
            task=task,
            evidence={
                "observations": self._evidence(context),
                "changed_files": self._evidence_paths(self._evidence(context)),
            },
            change_request=request,
        )

    @staticmethod
    def after_agent_result(
        context: AgentContext,
        call: AgentCall,
        result: AgentResult,
        child: AgentContext,
        dispatcher: Any,
    ) -> None:
        context.change_request = child.change_request
        context.code_candidate = child.code_candidate
        context.verification = child.verification
        context.code_explanation = child.code_explanation
        context.code_plan = child.code_plan
        context.observations.extend(
            observation
            for observation in child.observations
            if observation.get("kind") == "capability_error"
        )
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
        return ModelResponse(content, [], message)

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
