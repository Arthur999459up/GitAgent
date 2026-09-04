"""Per-agent execution state owned by the Harness."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from gitagent.agent_loop.models import WaitForUser
from gitagent.capability import AccessLevel, CapabilityKind, CapabilityResult
from gitagent.capability.errors import CapabilityErrorType, capability_error
from gitagent.domain.errors import ValidationError
from gitagent.domain.models import (
    AgentGuidance,
    AgentSpec,
    CandidatePatch,
    ChangeRequest,
    CodingTask,
    IssueReplyWorkflow,
    VerificationReport,
)
from gitagent.harness.context.builder import compact_messages
from gitagent.harness.context.messages import (
    assistant_tool_call,
    canonical_message,
    tool_result_message,
)
from gitagent.harness.file_reads import (
    FileReadLedger,
    FileReadOutputValidationError,
    PreparedFileRead,
)
from gitagent.prompts import get_prompt_library

if TYPE_CHECKING:
    from gitagent.harness.coding_workspace import CodingWorkspace
    from gitagent.harness.execution import AgentHarness

_PROMPTS = get_prompt_library()


@dataclass(frozen=True)
class CapabilityCallRecord:
    call_id: str
    arguments: dict[str, Any]
    observation_data: Any
    result: CapabilityResult
    cached: bool = False
    covered: bool = False
    execution_arguments: dict[str, Any] | None = None
    prepared_file_read: PreparedFileRead | None = None


@dataclass(frozen=True)
class _PreparedCapabilityInvocation:
    call_id: str
    capability_id: str
    arguments: dict[str, Any]
    execution_arguments: dict[str, Any] | None
    file_read: PreparedFileRead | None
    cached_result: dict[str, Any] | None


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
        max_steps: int,
    ) -> None:
        self._harness = harness
        self.spec = spec
        self.run_id = f"run-{uuid.uuid4().hex}"
        self.origin_turn_seq = 0
        self.parent_run_id = ""
        self.parent_call_id = ""
        self.parent_call_name = ""
        self.session_id = session_id
        self.repository = repository
        self.goal = goal
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.guidance = guidance
        self.steps = 0
        self.max_steps = max_steps
        self.observations: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.model_tools: list[dict[str, Any]] | None = None
        self.pending: Any = None
        self.user_input_request: WaitForUser | None = None
        self.active_children: dict[str, AgentContext] = {}
        self.last_completed_child: AgentContext | None = None
        self.parent_call_arguments: dict[str, Any] = {}
        self.result: Any = None
        self.final_message = ""
        self.code_candidate: CandidatePatch | None = None
        self.change_request: ChangeRequest | None = None
        self.verification: VerificationReport | None = None
        self.issue_reply: IssueReplyWorkflow | None = None
        self.coding_task: CodingTask | None = None
        self.coding_workspace: CodingWorkspace | None = None
        self.coding_task_completed = False
        self.code_explanation: Any = None
        self.code_review: Any = None
        self.code_plan: Any = None
        self.review_dialogue: dict[str, Any] | None = None
        self.ci_analysis: dict[str, Any] | None = None
        self.merge_readiness: dict[str, Any] | None = None
        self.read_cache: dict[str, Any] = {}
        self._ephemeral_memory_reads: list[dict[str, str]] = []
        self.file_reads = FileReadLedger()
        self.uncommitted_capability_results: dict[str, CapabilityCallRecord] = {}
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
        return (
            self.pending is not None
            or self.user_input_request is not None
            or any(child.waiting for child in self.active_children.values())
        )

    @property
    def waiting_question(self) -> str:
        child = self.first_waiting_child()
        if child is not None:
            return child.waiting_question
        if self.user_input_request is not None:
            return self.user_input_request.question
        return ""

    @property
    def context_window_tokens(self) -> int:
        return self._harness.context_window_for(self.agent)

    def first_waiting_child(self) -> AgentContext | None:
        for provider_call in self.open_tool_calls():
            child = self.active_children.get(str(provider_call.get("id") or ""))
            if child is not None and child.waiting:
                return child
        return next(
            (child for child in self.active_children.values() if child.waiting),
            None,
        )

    def invoke(
        self,
        capability_id: str,
        *,
        call_id: str,
        **arguments: Any,
    ) -> CapabilityCallRecord:
        """Invoke one explicitly correlated call without approval authority."""

        prepared = self.prepare_capability_call(call_id, capability_id, arguments)
        return self._harness.execute_capability_invocation(
            self, prepared, approval_id=None
        )

    def invoke_approved(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        approval_id: str,
        call_id: str,
    ) -> CapabilityCallRecord:
        """Invoke one exact Harness-owned call after an explicit approval decision."""

        prepared = self.prepare_capability_call(
            call_id, capability_id, dict(arguments)
        )
        return self._harness.execute_capability_invocation(
            self, prepared, approval_id=approval_id
        )

    def prepare_capability_call(
        self,
        call_id: str,
        capability_id: str,
        arguments: dict[str, Any],
    ) -> _PreparedCapabilityInvocation:
        if not call_id:
            raise ValidationError("Capability invocation requires an explicit call_id")
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
        capability = next(
            (item for item in self._harness.discover(self) if item.id == capability_id),
            None,
        )
        cache_key = (
            json.dumps(
                [capability_id, actual_arguments],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if actual_arguments is not None
            else None
        )
        cacheable = (
            capability is not None
            and capability.access == AccessLevel.READ
            and prepared is None
            and not _memory_read(capability_id, actual_arguments)
        )
        cached_result = self.read_cache.get(cache_key) if cacheable else None
        return _PreparedCapabilityInvocation(
            call_id,
            capability_id,
            dict(arguments),
            actual_arguments,
            prepared,
            dict(cached_result) if cached_result is not None else None,
        )

    def execute_capability_call(
        self,
        prepared: _PreparedCapabilityInvocation,
        *,
        approval_id: str | None = None,
        preflighted: bool = False,
    ) -> CapabilityCallRecord:
        actual_arguments = prepared.execution_arguments
        cached = prepared.cached_result is not None
        covered = actual_arguments is None
        if covered:
            invocation_result = CapabilityResult(
                prepared.capability_id, "success", "data", None, attempts=0
            )
        elif cached:
            invocation_result = CapabilityResult(
                prepared.capability_id,
                "success",
                str(prepared.cached_result["type"]),
                prepared.cached_result["content"],
                attempts=0,
            )
        else:
            workspace = self.coding_workspace
            bash_state = (
                workspace.worktree_state()
                if workspace is not None and prepared.capability_id == "native.bash"
                else None
            )
            try:
                invocation_result = self._harness.invoke(
                    self,
                    prepared.capability_id,
                    actual_arguments,
                    approval_id=approval_id,
                    call_id=prepared.call_id,
                    preflighted=preflighted,
                )
            finally:
                if bash_state is not None and workspace is not None:
                    current_state = workspace.worktree_state()
                    if current_state != bash_state:
                        workspace.record_mutation()
                        self.read_cache.clear()
                        self.file_reads.clear()
        return CapabilityCallRecord(
            prepared.call_id,
            prepared.arguments,
            invocation_result.content if invocation_result.status == "success" else None,
            invocation_result,
            cached=cached or covered,
            covered=covered or bool(
                prepared.file_read and prepared.file_read.covered_indexes
            ),
            execution_arguments=(
                dict(actual_arguments) if actual_arguments is not None else None
            ),
            prepared_file_read=prepared.file_read,
        )

    def commit_capability_call(
        self,
        invocation: CapabilityCallRecord,
        *,
        approval_id: str | None = None,
    ) -> CapabilityCallRecord:
        capability_id = invocation.result.capability_id
        actual_arguments = invocation.execution_arguments
        invocation_result = invocation.result
        raw_result = invocation_result.content
        prepared = invocation.prepared_file_read
        if invocation_result.status != "success":
            observation_data = None
            content: Any = raw_result
        elif actual_arguments is not None and _memory_read(
            capability_id, actual_arguments
        ):
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
            try:
                content, observation_data = self.file_reads.complete(
                    prepared, raw_result
                )
            except FileReadOutputValidationError as exc:
                invocation_result = self._invalid_output_result(invocation, exc)
                content = None
                observation_data = None
        else:
            content = raw_result
            observation_data = (
                {
                    "already_observed": True,
                    "capability_id": capability_id,
                    "arguments": actual_arguments,
                }
                if invocation.cached
                else raw_result
            )
        cache_key = (
            json.dumps(
                [capability_id, actual_arguments],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if actual_arguments is not None
            else None
        )
        capability = next(
            (item for item in self._harness.discover(self) if item.id == capability_id),
            None,
        )
        if (
            capability is not None
            and capability.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
            and invocation_result.status == "success"
        ):
            self.read_cache.clear()
            self.file_reads.clear()
        workspace = self.coding_workspace
        verification_command = bool(
            workspace is not None
            and capability_id == "native.bash"
            and actual_arguments is not None
            and self._harness.is_bash_verification(
                self, str(actual_arguments.get("command") or "")
            )
        )
        if workspace is not None:
            if (
                invocation_result.status == "success"
                and capability is not None
                and capability.kind == CapabilityKind.NATIVE_TOOL
                and capability.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
                and isinstance(capability.input_schema, dict)
                and isinstance(capability.input_schema.get("properties"), dict)
                and "path" in capability.input_schema["properties"]
                and isinstance(raw_result, dict)
                and bool(raw_result.get("changed"))
            ):
                workspace.record_mutation()
            if (
                verification_command
                and invocation_result.status == "success"
                and isinstance(raw_result, dict)
            ):
                workspace.record_validation()
                self.observations.append(
                    {
                        "kind": "coding_verification",
                        "payload": {
                            "revision": workspace.revision,
                            "command": str(actual_arguments.get("command") or ""),
                            "exit_code": int(raw_result.get("exit_code", 1)),
                            "stdout_tail": str(raw_result.get("stdout_tail") or ""),
                            "stderr_tail": str(raw_result.get("stderr_tail") or ""),
                        },
                    }
                )
            elif verification_command and invocation_result.status == "failed":
                error = invocation_result.error
                self.observations.append(
                    {
                        "kind": "coding_verification",
                        "payload": {
                            "revision": workspace.revision,
                            "command": str(actual_arguments.get("command") or ""),
                            "unavailable_reason": (
                                f"{error.type.value}: {error.message}"
                                if error is not None
                                else "verification command could not execute"
                            ),
                        },
                    }
                )
        cacheable = (
            capability is not None
            and capability.access == AccessLevel.READ
            and prepared is None
            and actual_arguments is not None
            and not _memory_read(capability_id, actual_arguments)
        )
        if (
            cacheable
            and not invocation.cached
            and invocation_result.status == "success"
            and cache_key is not None
        ):
            self.read_cache[cache_key] = {
                "type": invocation_result.type,
                "content": raw_result,
            }
        result = replace(invocation_result, content=content)
        committed = replace(
            invocation, observation_data=observation_data, result=result
        )
        self._harness.commit_capability_failure_guard(self, committed)
        self._harness.audit_capability_result(
            self, committed, approval_id=approval_id
        )
        return committed

    def _invalid_output_result(
        self, invocation: CapabilityCallRecord, error: Exception
    ) -> CapabilityResult:
        capability_error_result = capability_error(
            CapabilityErrorType.INVALID_OUTPUT,
            str(error),
            details={
                "provider_executed": True,
                "side_effect_possible": False,
            },
        )
        result = CapabilityResult(
            invocation.result.capability_id,
            "failed",
            "none",
            error=capability_error_result,
            attempts=invocation.result.attempts,
        )
        from gitagent.infra.observability import TraceCategory, TraceStatus

        try:
            self._harness.trace.emit(
                session_id=self.session_id,
                category=TraceCategory.CAPABILITY,
                name=invocation.result.capability_id,
                status=TraceStatus.FAILED,
                details={
                    "agent": self.agent,
                    "run_id": self.run_id,
                    "call_id": invocation.call_id,
                    "event": "output_validation.failed",
                    "error": CapabilityErrorType.INVALID_OUTPUT.value,
                    **(capability_error_result.details or {}),
                },
            )
        except Exception:  # noqa: BLE001, S110 - observability is best effort
            pass
        return result

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
        result = compact_messages(
            request,
            tools,
            context_window_tokens=self.context_window_tokens,
        )
        if result.changed:
            sink = self._harness.compaction_sink
            if sink is not None and self.origin_turn_seq > 0:
                sink(
                    self,
                    result.plan,
                    result.level,
                    result.before_tokens,
                    result.after_tokens,
                )
            durable = [dict(message) for message in result.messages]
            if durable and durable[0].get("role") == "system":
                durable[0] = {"role": "system", "content": self.system_prompt}
            self.messages[:] = durable
        return result.messages

    def reason(
        self,
        reasoner: Any,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Run one native Text/structured-call model step and persist it verbatim."""

        request = self.model_messages(tools)
        response = reasoner.complete_messages(
            messages=request,
            tools=tools,
            context_window_tokens=self.context_window_tokens,
        )
        self.append_message(response.assistant_message)
        return response

    def ensure_capability_tool_call(
        self, capability_id: str, arguments: dict[str, Any]
    ) -> str:
        expected_name = self._harness.function_name(capability_id)
        matching = []
        open_calls = self.open_tool_calls()
        for open_call in open_calls:
            function = open_call.get("function") or {}
            actual_name = (
                str(function.get("name") or "") if isinstance(function, dict) else ""
            )
            raw_arguments = (
                function.get("arguments") if isinstance(function, dict) else None
            )
            try:
                actual_arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else dict(raw_arguments or {})
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    "unresolved Capability call arguments are invalid"
                ) from exc
            if actual_name == expected_name and actual_arguments == arguments:
                matching.append(open_call)
        if len(matching) > 1:
            raise ValidationError(
                f"multiple unresolved calls match capability {expected_name}"
            )
        if matching:
            return str(matching[0]["id"])
        if open_calls:
            raise ValidationError(
                "cannot synthesize a Capability call while another provider call is unresolved"
            )
        call_id = f"call-{uuid.uuid4().hex}"
        self.append_message(assistant_tool_call(call_id, expected_name, arguments))
        return call_id

    def append_tool_result(self, content: Any, *, call_id: str) -> dict[str, Any]:
        if not call_id:
            raise ValidationError("Domain tool result has no matching assistant call")
        return self.append_message(tool_result_message(call_id, content))

    def open_tool_calls(self) -> list[dict[str, Any]]:
        resolved: set[str] = {
            str(message.get("tool_call_id") or "")
            for message in self.messages
            if message.get("role") == "tool"
        }
        return [
            call
            for call in self.provider_tool_calls()
            if str(call.get("id") or "") not in resolved
        ]

    def provider_tool_calls(self) -> list[dict[str, Any]]:
        return [
            call
            for message in self.messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if str(call.get("id") or "")
        ]

    def unresolved_tool_call(self, call_id: str) -> dict[str, Any] | None:
        return next(
            (
                call
                for call in self.open_tool_calls()
                if str(call.get("id") or "") == call_id
            ),
            None,
        )

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
        if (not has_guidance and not self._ephemeral_memory_reads) or not messages:
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
