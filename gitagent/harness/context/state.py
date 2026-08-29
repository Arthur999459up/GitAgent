"""Per-agent execution state owned by the Harness."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from gitagent.capability import AccessLevel, CapabilityResult
from gitagent.domain.errors import ValidationError
from gitagent.domain.models import (
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    VerificationReport,
)
from gitagent.harness.context import estimate_tokens
from gitagent.harness.file_reads import FileReadLedger

if TYPE_CHECKING:
    from gitagent.harness.execution import AgentHarness


@dataclass(frozen=True)
class CapabilityCallRecord:
    arguments: dict[str, Any]
    observation_data: Any
    result: CapabilityResult
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
        self.run_id = f"run-{uuid.uuid4().hex}"
        self.origin_turn_seq = 0
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
        self.delegation_depth = 0
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
        self.result_required = True
        self.read_cache: dict[str, Any] = {}
        self.file_reads = FileReadLedger()
        self.last_capability_call: CapabilityCallRecord | None = None
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

    def invoke(
        self, capability_id: str, *, approval_id: str | None = None, **arguments: Any
    ) -> Any:
        self.last_capability_call = None
        try:
            prepared = self.file_reads.prepare(
                capability_id,
                arguments,
                repository=self.repository,
            )
        except ValidationError:
            prepared = None
        actual_arguments = (
            prepared.actual_arguments if prepared is not None else dict(arguments)
        )
        if actual_arguments is None:
            content, observation_data = self.file_reads.complete(prepared, None)
            result = CapabilityResult(
                capability_id, "success", "data", content, attempts=0
            )
            self.last_capability_call = CapabilityCallRecord(
                dict(arguments), observation_data, result, cached=True, covered=True
            )
            return content

        capability = next(
            (item for item in self._harness.discover(self) if item.id == capability_id),
            None,
        )
        cache_key = json.dumps(
            [capability_id, actual_arguments],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cacheable = (
            capability is not None
            and capability.access == AccessLevel.READ
            and prepared is None
        )
        cached = cacheable and cache_key in self.read_cache
        if cached:
            cached_result = self.read_cache[cache_key]
            raw_result = cached_result["content"]
            invocation_result = CapabilityResult(
                capability_id,
                "success",
                str(cached_result["type"]),
                raw_result,
                attempts=0,
            )
        else:
            invocation_result = self._harness.invoke(
                self, capability_id, actual_arguments, approval_id=approval_id
            )
            raw_result = invocation_result.content
            if cacheable and invocation_result.status == "success":
                self.read_cache[cache_key] = {
                    "type": invocation_result.type,
                    "content": raw_result,
                }

        if invocation_result.status != "success":
            observation_data = None
            content: Any = invocation_result
        elif prepared is not None:
            content, observation_data = self.file_reads.complete(prepared, raw_result)
        else:
            content = raw_result
            observation_data = (
                {
                    "already_observed": True,
                    "capability_id": capability_id,
                    "arguments": actual_arguments,
                }
                if cached
                else raw_result
            )
        self.last_capability_call = CapabilityCallRecord(
            actual_arguments,
            observation_data,
            invocation_result,
            cached=cached,
            covered=bool(prepared and prepared.covered_indexes),
        )
        return content

    def prompt_overhead(self) -> int:
        value = {
            "system": self.system_prompt,
            "goal": self.goal,
            "repository": self.repository,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "guidance": asdict(self.guidance) if self.guidance is not None else None,
            "capabilities": self._harness.llm_tools(self),
        }
        return estimate_tokens(json.dumps(value, ensure_ascii=False, default=str)) + 512
