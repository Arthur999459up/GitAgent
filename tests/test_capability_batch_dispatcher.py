from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gitagent.agent_loop import CapabilityCall
from gitagent.capability import CapabilityResult
from gitagent.harness.context.state import CapabilityCallRecord
from gitagent.harness.execution import ExecutionCoordinator, ExecutionProfile
from gitagent.harness.structured_call_dispatcher import StructuredCallDispatcher


class _Context:
    def __init__(self, calls: list[CapabilityCall]) -> None:
        self.observations: list[dict[str, Any]] = []
        self.uncommitted_capability_results: dict[str, CapabilityCallRecord] = {}
        self.pending = None
        self.issue_reply = None
        self.resolved: set[str] = set()
        self.provider_calls = {
            call.call_id: {
                "id": call.call_id,
                "function": {
                    "name": "capability__" + call.capability_id.replace(".", "__"),
                    "arguments": call.arguments,
                },
            }
            for call in calls
        }

    def unresolved_tool_call(self, call_id: str) -> dict[str, Any] | None:
        return None if call_id in self.resolved else self.provider_calls.get(call_id)

    @staticmethod
    def prepare_capability_call(
        call_id: str, capability_id: str, arguments: dict[str, Any]
    ) -> Any:
        return SimpleNamespace(
            call_id=call_id,
            capability_id=capability_id,
            arguments=arguments,
            execution_arguments=arguments,
        )

    @staticmethod
    def execute_capability_call(prepared: Any, **_: Any) -> CapabilityCallRecord:
        content = {"value": prepared.capability_id}
        return CapabilityCallRecord(
            prepared.call_id,
            dict(prepared.arguments),
            content,
            CapabilityResult(prepared.capability_id, "success", "data", content),
            execution_arguments=dict(prepared.arguments),
        )

    @staticmethod
    def commit_capability_call(record: CapabilityCallRecord) -> CapabilityCallRecord:
        return record

    def append_tool_result(self, _: Any, *, call_id: str) -> None:
        self.resolved.add(call_id)


class _Harness:
    def __init__(self) -> None:
        self.coordinator = ExecutionCoordinator(
            capability_max_concurrency=2,
            provider_concurrency={"test": 2},
            domain_agent_max_concurrency=1,
        )

    @staticmethod
    def describe_capability_execution(_: Any, __: Any) -> ExecutionProfile:
        return ExecutionProfile.concurrent()

    @staticmethod
    def capability_permission_decision(_: Any, __: Any) -> str:
        return "ALLOW"

    @staticmethod
    def capability_failure_blocked(_: Any, __: Any, **___: Any) -> bool:
        return False

    @staticmethod
    def provider_id(_: str) -> str:
        return "test"

    @staticmethod
    def function_name(capability_id: str) -> str:
        return "capability__" + capability_id.replace(".", "__")


def test_dispatcher_executes_and_commits_multiple_read_calls() -> None:
    calls = [
        CapabilityCall("first", "repository.read_first", {}),
        CapabilityCall("second", "repository.read_second", {}),
    ]
    harness = _Harness()
    context = _Context(calls)
    try:
        completed = StructuredCallDispatcher(harness).execute_capability_batch(
            context, calls
        )
    finally:
        harness.coordinator.close()

    assert completed
    assert context.resolved == {"first", "second"}
    assert [item["payload"]["capability_id"] for item in context.observations] == [
        "repository.read_first",
        "repository.read_second",
    ]
