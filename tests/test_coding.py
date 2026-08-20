from __future__ import annotations

from typing import Any

import pytest
from AGENT.GitAgent.gitagent.core.errors import LLMProviderError, ToolExecutionError, WorkflowError
from AGENT.GitAgent.gitagent.core.models import ChangeRequest
from AGENT.GitAgent.gitagent.mcp.github import GitHubMCPServer
from AGENT.GitAgent.tests.support import build_test_service


class PathAwareCodingReasoner:
    def __init__(self) -> None:
        self.search_calls = 0
        self.candidate_prompt = ""
        self.candidate_schema: dict[str, Any] | None = None
        self.tool_name = ""

    def complete_text(self, *, system: str, prompt: str) -> str:
        del system, prompt
        self.search_calls += 1
        return "missing_symbol"

    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Any = None,
        tool_name: str = "respond",
        tools: Any = None,
    ) -> dict[str, Any]:
        del system, tools
        self.candidate_prompt = prompt
        self.candidate_schema = schema
        self.tool_name = tool_name
        return {
            "summary": "correct addition",
            "root_cause": "the implementation subtracts",
            "files": {"src/math_utils.py": "def add(a: int, b: int) -> int:\n    return a + b\n"},
            "risks": [],
            "verification_required": ["tests"],
        }


class EmptySearchReasoner(PathAwareCodingReasoner):
    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"candidate generation must not run without editable files: {kwargs}")


def test_coding_uses_an_exact_repository_path_mentioned_in_the_description():
    reasoner = PathAwareCodingReasoner()
    service = build_test_service(agent_reasoner=reasoner)

    candidate = service.coding.create_candidate(
        ChangeRequest(
            repository="sample/widgets",
            description="Correct the implementation in src/math_utils.py",
        ),
        session_id=service.session_scope.session_id,
    )

    assert candidate.changed_files == ["src/math_utils.py"]
    assert reasoner.search_calls == 0
    assert "return a - b" in reasoner.candidate_prompt
    assert reasoner.tool_name == "prepare_candidate"
    assert reasoner.candidate_schema is not None
    assert reasoner.candidate_schema["properties"]["files"]["additionalProperties"] == {"type": "string"}


def test_coding_fails_before_generation_when_no_editable_file_can_be_found():
    reasoner = EmptySearchReasoner()
    service = build_test_service(agent_reasoner=reasoner)

    with pytest.raises(WorkflowError, match="no matches for 'missing_symbol'"):
        service.coding.create_candidate(
            ChangeRequest(repository="sample/widgets", description="Change an unknown implementation"),
            session_id=service.session_scope.session_id,
        )


class SearchCandidatesThatCannotBeRead(GitHubMCPServer):
    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        del method, path, payload, kwargs
        return {"items": [{"path": "src/first.py"}, {"path": "src/second.py"}]}

    def read_file(self, repository: str, path: str, **kwargs: Any) -> dict[str, Any]:
        del repository, kwargs
        raise ToolExecutionError(f"cannot read {path}")


def test_live_search_does_not_disguise_candidate_fetch_failures_as_no_matches():
    server = SearchCandidatesThatCannotBeRead(token="test-token")

    with pytest.raises(ToolExecutionError, match="2 candidate file.*none could be read"):
        server.search_code("sample/widgets", "target")


class CandidateTimeoutReasoner(PathAwareCodingReasoner):
    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise LLMProviderError("模型提供方请求超时（单次读取超时 30 秒）")


def test_coding_timeout_debug_identifies_candidate_generation_phase():
    service = build_test_service(agent_reasoner=CandidateTimeoutReasoner())

    with pytest.raises(LLMProviderError, match="候选补丁生成阶段失败"):
        service.coding.create_candidate(
            ChangeRequest(repository="sample/widgets", description="Fix src/math_utils.py"),
            session_id=service.session_scope.session_id,
        )

    failure = [
        event
        for event in service.harness.trace.events(service.session_scope.session_id)
        if event.name == "coding" and event.status.value == "failed"
    ][-1]
    assert failure.details["context"]["phase"] == "generating_candidate"
    assert "候选补丁生成阶段失败" in failure.details["context"]["error"]
