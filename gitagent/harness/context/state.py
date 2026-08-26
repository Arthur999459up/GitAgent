"""Per-agent execution state owned by the Harness."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from gitagent.domain.models import AccessLevel, AgentGuidance, AgentSpec, CandidatePatch, ChangeRequest, VerificationReport
from gitagent.harness.context import estimate_tokens
from gitagent.harness.tools.file_reads import FileReadLedger

if TYPE_CHECKING:
    from gitagent.harness.execution import AgentHarness


@dataclass(frozen=True)
class ToolCallRecord:
    arguments: dict[str, Any]
    observation_data: Any
    cached: bool = False
    covered: bool = False


class AgentContext:
    """Working state and Harness capabilities for one agent invocation."""

    def __init__(
        self,
        harness: AgentHarness,
        spec: AgentSpec,
        session_id: str,
        *,
        repository: str = "",
        goal: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        guidance: AgentGuidance | None = None,
        max_steps: int = 20,
    ) -> None:
        self._harness = harness
        self.spec = spec
        self.session_id = session_id
        self.repository = repository
        self.goal = goal
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.guidance = guidance
        self.operation = ""
        self.requested_outcome = ""
        self.phase = ""
        self.steps = 0
        self.max_steps = max_steps
        self.observations: list[dict[str, Any]] = []
        self.pending: Any = None
        self.question = ""
        self.result: Any = None
        self.final_message = ""
        self.code_candidate: CandidatePatch | None = None
        self.change_request: ChangeRequest | None = None
        self.verification: VerificationReport | None = None
        self.reply_draft: str | None = None
        self.repository_search_plan: dict[str, Any] | None = None
        self.repository_history_path = ""
        self.read_only = False
        self.result_required = True
        self.read_cache: dict[str, Any] = {}
        self.file_reads = FileReadLedger()
        self.last_tool_call: ToolCallRecord | None = None
        self.error: str | None = None
        self.finished = False

    @property
    def agent(self) -> str:
        return self.spec.name

    @property
    def system_prompt(self) -> str:
        return self.spec.system_prompt

    @property
    def waiting(self) -> bool:
        return self.pending is not None or bool(self.question)

    @property
    def context_budget(self) -> int:
        return self._harness.context_budget

    def tool(self, name: str, *, approval_id: str | None = None, **arguments: Any) -> Any:
        self.last_tool_call = None
        prepared = self.file_reads.prepare(name, arguments)
        actual_arguments = prepared.actual_arguments if prepared is not None else dict(arguments)
        if actual_arguments is None:
            result, observation_data = self.file_reads.complete(prepared, None)
            self.last_tool_call = ToolCallRecord(dict(arguments), observation_data, cached=True, covered=True)
            return result

        tool = self._harness.server.get_tool(name)
        cache_key = json.dumps([name, actual_arguments], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cacheable = tool.access == AccessLevel.READ and prepared is None
        cached = cacheable and cache_key in self.read_cache
        if cached:
            raw_result = self.read_cache[cache_key]
        else:
            raw_result = self._harness.execute_tool(self, name, actual_arguments, approval_id=approval_id)
            if cacheable:
                self.read_cache[cache_key] = raw_result

        if prepared is not None:
            result, observation_data = self.file_reads.complete(prepared, raw_result)
        else:
            result = raw_result
            observation_data = {"already_observed": True, "tool": name, "arguments": actual_arguments} if cached else raw_result
        self.last_tool_call = ToolCallRecord(
            actual_arguments,
            observation_data,
            cached=cached,
            covered=bool(prepared and prepared.covered_indexes),
        )
        return result

    def prompt_overhead(self) -> int:
        value = {
            "system": self.system_prompt,
            "goal": self.goal,
            "repository": self.repository,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "guidance": asdict(self.guidance) if self.guidance is not None else None,
            "tools": self._harness.client.llm_tools(self.spec.allowed_tools),
        }
        return estimate_tokens(json.dumps(value, ensure_ascii=False, default=str)) + 512
