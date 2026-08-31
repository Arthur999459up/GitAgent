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
from gitagent.harness.context.builder import fit_messages_with_plan
from gitagent.harness.context.messages import (
    assistant_tool_call,
    canonical_message,
    tool_result_message,
)
from gitagent.harness.file_reads import FileReadLedger
from gitagent.model import structured_tools
from gitagent.prompts import get_prompt_library

if TYPE_CHECKING:
    from gitagent.harness.execution import AgentHarness

_PROMPTS = get_prompt_library()


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
        self.steps = 0
        self.max_steps = max_steps
        self.delegation_depth = 0
        self.observations: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.pending: Any = None
        self.question = ""
        self.result: Any = None
        self.final_message = ""
        self.code_candidate: CandidatePatch | None = None
        self.change_request: ChangeRequest | None = None
        self.verification: VerificationReport | None = None
        self.reply_draft: str | None = None
        self.result_required = True
        self.read_cache: dict[str, Any] = {}
        self._ephemeral_memory_reads: list[dict[str, str]] = []
        self.file_reads = FileReadLedger()
        self.last_capability_call: CapabilityCallRecord | None = None
        self.error: str | None = None
        self.finished = False
        self.structured_retry_instruction = ""

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
    def context_window_tokens(self) -> int:
        return self._harness.context_window_for(self.agent)

    def invoke(self, capability_id: str, **arguments: Any) -> Any:
        """Invoke without approval; model-authored arguments can never supply approval authority."""

        return self._invoke(capability_id, arguments, approval_id=None)

    def invoke_approved(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        approval_id: str,
    ) -> Any:
        """Invoke one exact Harness-owned call after an explicit approval decision."""

        return self._invoke(capability_id, dict(arguments), approval_id=approval_id)

    def _invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        approval_id: str | None,
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
            and not _memory_read(capability_id, actual_arguments)
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
        elif _memory_read(capability_id, actual_arguments):
            root = str(actual_arguments.get("root") or "")
            path = str(actual_arguments.get("path") or "")
            memory_content = (
                str(raw_result.get("content") or "")
                if isinstance(raw_result, dict)
                else str(raw_result or "")
            )
            self._record_ephemeral_memory_read(root, path, memory_content)
            content = raw_result
            observation_data = {
                "memory_page_loaded": True,
                "root": root,
                "path": path,
            }
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

    def start_message_thread(self) -> None:
        if self.messages:
            return
        self.append_message({"role": "system", "content": self.system_prompt})
        delegated = {
            "task": self.goal,
            "repository": self.repository,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
        }
        self.append_message(
            {
                "role": "user",
                "content": json.dumps(
                    delegated, ensure_ascii=False, separators=(",", ":"), default=str
                ),
            }
        )

    def append_message(self, message: dict[str, Any]) -> dict[str, Any]:
        safe = canonical_message(message)
        sink = self._harness.message_sink
        if sink is not None and self.origin_turn_seq > 0:
            safe = sink(self, safe)
        self.messages.append(safe)
        return safe

    def model_messages(
        self, tools: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        self.start_message_thread()
        request = self._ephemeral_messages()
        fitted, _, _, plan = fit_messages_with_plan(
            request,
            tools,
            context_window_tokens=self.context_window_tokens,
        )
        if plan.changed:
            sink = self._harness.compaction_sink
            if sink is not None and self.origin_turn_seq > 0:
                sink(self, plan)
            durable = [dict(message) for message in fitted]
            if durable and durable[0].get("role") == "system":
                durable[0] = {"role": "system", "content": self.system_prompt}
            self.messages[:] = durable
        return fitted

    def reason_structured(
        self,
        reasoner: Any,
        *,
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run inference with one final tool payload used for fitting and transport."""

        final_tools = structured_tools(tool_name, schema, tools)
        request = self.model_messages(final_tools)
        return reasoner.complete_structured_messages(
            messages=request,
            schema=schema,
            tool_name=tool_name,
            final_tools=final_tools,
            context_window_tokens=self.context_window_tokens,
        )

    def record_model_response(
        self,
        value: dict[str, Any],
        *,
        tool_name: str,
    ) -> dict[str, Any]:
        self.structured_retry_instruction = ""
        message = getattr(value, "assistant_message", None)
        if not isinstance(message, dict):
            message = assistant_tool_call(f"call-{uuid.uuid4().hex}", tool_name, value)
        return self.append_message(message)

    def ensure_capability_tool_call(
        self, capability_id: str, arguments: dict[str, Any]
    ) -> str:
        expected_name = self._harness.function_name(capability_id)
        open_call = self.open_tool_call()
        if open_call is not None:
            function = open_call.get("function") or {}
            actual_name = (
                str(function.get("name") or "") if isinstance(function, dict) else ""
            )
            if actual_name != expected_name:
                raise ValidationError(
                    f"open tool call {actual_name or '<unnamed>'} does not match capability {expected_name}"
                )
            return str(open_call["id"])
        call_id = f"call-{uuid.uuid4().hex}"
        self.append_message(assistant_tool_call(call_id, expected_name, arguments))
        return call_id

    def append_tool_result(self, content: Any) -> dict[str, Any]:
        call_id = self.open_tool_call_id()
        if not call_id:
            raise ValidationError("Domain tool result has no matching assistant call")
        return self.append_message(tool_result_message(call_id, content))

    def open_tool_calls(self) -> list[dict[str, Any]]:
        resolved: set[str] = {
            str(message.get("tool_call_id") or "")
            for message in self.messages
            if message.get("role") == "tool"
        }
        open_calls: list[dict[str, Any]] = []
        for message in self.messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                call_id = str(call.get("id") or "")
                if call_id and call_id not in resolved:
                    open_calls.append(call)
        return open_calls

    def open_tool_call(self) -> dict[str, Any] | None:
        open_calls = self.open_tool_calls()
        if len(open_calls) > 1:
            raise ValidationError("Domain thread has multiple unresolved tool calls")
        return open_calls[0] if open_calls else None

    def open_tool_call_id(self) -> str:
        call = self.open_tool_call()
        return str(call.get("id") or "") if call is not None else ""

    def complete_control_call(self, outcome: Any) -> None:
        if self.open_tool_call() is not None:
            self.append_tool_result(outcome)

    def complete_structured(
        self,
        reasoner: Any,
        *,
        prompt: str,
        schema: dict[str, Any] | None = None,
        tool_name: str = "respond",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.start_message_thread()
        self.append_message({"role": "user", "content": prompt})
        value = self.reason_structured(
            reasoner,
            schema=schema,
            tool_name=tool_name,
            tools=tools,
        )
        self.record_model_response(value, tool_name=tool_name)
        self.complete_control_call({"status": "accepted", "result": value})
        return value

    def complete_text(self, reasoner: Any, *, prompt: str) -> str:
        self.start_message_thread()
        self.append_message({"role": "user", "content": prompt})
        content = reasoner.complete_text_messages(
            messages=self.model_messages(),
            context_window_tokens=self.context_window_tokens,
        ).strip()
        self.append_message({"role": "assistant", "content": content})
        return content

    def _ephemeral_messages(self) -> list[dict[str, Any]]:
        messages = [dict(message) for message in self.messages]
        has_guidance = self.guidance is not None and not self.guidance.empty
        if (
            not has_guidance
            and not self._ephemeral_memory_reads
            and not self.structured_retry_instruction
        ) or not messages:
            return messages
        payload = json.dumps(
            {
                "guidance": asdict(self.guidance) if has_guidance else None,
                "additional_memory_reads": list(self._ephemeral_memory_reads),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        policy = "\n\n" + _PROMPTS.render("agents.guidance_section", payload=payload)
        if self.structured_retry_instruction:
            policy += "\n\n## Required correction\n" + self.structured_retry_instruction
        first = dict(messages[0])
        first["content"] = str(first.get("content") or "") + policy
        messages[0] = first
        return messages

    def _record_ephemeral_memory_read(self, root: str, path: str, content: str) -> None:
        identity = (root, path)
        if any(
            (item["root"], item["path"]) == identity
            for item in self._ephemeral_memory_reads
        ):
            return
        remaining = 20_000 - sum(
            len(item["content"].encode()) for item in self._ephemeral_memory_reads
        )
        if remaining <= 0 or len(self._ephemeral_memory_reads) >= 5:
            return
        encoded = content.encode()
        clipped = encoded[:remaining].decode("utf-8", errors="ignore")
        self._ephemeral_memory_reads.append(
            {"root": root, "path": path, "content": clipped}
        )


def _memory_read(capability_id: str, arguments: dict[str, Any]) -> bool:
    return capability_id == "native.read" and arguments.get("root") in {
        "private_memory",
        "project_memory",
    }
