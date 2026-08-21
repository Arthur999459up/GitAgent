"""Session routing and direct parent-to-child agent handoff acceptance tests."""

from __future__ import annotations

import json
from typing import Any

from AGENT.GitAgent.gitagent.core.errors import LLMProviderError
from AGENT.GitAgent.gitagent.core.models import DraftResult, RoutingContext
from AGENT.GitAgent.gitagent.mcp.memory import InMemoryMCPServer
from AGENT.GitAgent.gitagent.runtime import AgentContext
from AGENT.GitAgent.tests.support import StubMainReasoner, build_test_service, handle, sample_repositories


class IssueFixReasoner:
    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Any = None,
        tool_name: str = "respond",
        tools: Any = None,
    ) -> dict[str, Any]:
        del system, prompt, schema, tools
        if tool_name == "decide_action":
            return {
                "kind": "apply_code_change",
                "summary": "prepare the issue fix",
                "tool": "",
                "arguments": {},
                "specialist": "",
                "question": "",
                "message": "",
            }
        raise AssertionError(f"unexpected structured call: {tool_name}")

    def complete_text(self, *, system: str, prompt: str) -> str:
        del system, prompt
        return "repository"


class ConfirmationThenFixReasoner:
    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Any = None,
        tool_name: str = "respond",
        tools: Any = None,
    ) -> dict[str, Any]:
        del system, schema, tools
        if tool_name != "decide_action":
            raise AssertionError(f"unexpected structured call: {tool_name}")
        if '"user": "继续，允许修改"' in prompt:
            return {
                "kind": "apply_code_change",
                "summary": "prepare the confirmed issue fix",
                "awaiting_user_confirmation": False,
            }
        return {
            "kind": "finish",
            "summary": "explain the proposed change",
            "message": "建议修改 src/math_utils.py。是否继续？",
            "question": "建议修改 src/math_utils.py。是否继续？",
            "awaiting_user_confirmation": True,
        }

    def complete_text(self, *, system: str, prompt: str) -> str:
        del system, prompt
        return "repository"


class IssueThreeWorkflowReasoner:
    def __init__(self, original_session: str, fixed_session: str, fixed_test: str) -> None:
        self.original_session = original_session
        self.fixed_session = fixed_session
        self.fixed_test = fixed_test
        self.fix_prompt = ""
        self.candidate_prompt = ""

    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Any = None,
        tool_name: str = "respond",
        tools: Any = None,
    ) -> dict[str, Any]:
        del system, schema, tools
        if tool_name == "decide_action":
            if '"user": "继续，允许修改"' in prompt:
                return {
                    "kind": "apply_code_change",
                    "summary": "prepare the confirmed Issue #3 fix",
                    "awaiting_user_confirmation": False,
                }
            if '"tool": "repository.read_file"' not in prompt:
                return {
                    "kind": "tool",
                    "summary": "read the affected session implementation",
                    "tool": "repository.read_file",
                    "arguments": {"path": "corecoder/session.py"},
                    "awaiting_user_confirmation": False,
                }
            return {
                "kind": "ask",
                "summary": "confirm the Issue #3 code change",
                "question": "已定位 list_sessions()，是否继续修改并补测试？",
                "awaiting_user_confirmation": True,
            }
        if tool_name == "prepare_fix_guide":
            self.fix_prompt = prompt
            return {
                "description": "Catch OSError in list_sessions and add regression coverage",
                "target_files": ["corecoder/session.py", "tests/test_session.py"],
                "suggested_title": "Handle unreadable session files",
            }
        if tool_name == "prepare_candidate":
            self.candidate_prompt = prompt
            return {
                "summary": "Handle unreadable session files",
                "root_cause": "list_sessions did not catch OSError",
                "files": {
                    "corecoder/session.py": self.fixed_session,
                    "tests/test_session.py": self.fixed_test,
                },
                "risks": [],
                "verification_required": ["pytest tests/test_session.py"],
            }
        raise AssertionError(f"unexpected structured call: {tool_name}")

    def complete_text(self, *, system: str, prompt: str) -> str:
        del system, prompt
        return "list_sessions"


class ConfirmationThenTimeoutReasoner(ConfirmationThenFixReasoner):
    def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("tool_name", "respond") == "prepare_candidate":
            raise LLMProviderError("模型提供方请求超时（单次读取超时 30 秒）")
        return super().complete_structured(**kwargs)


def test_main_agent_receives_every_history_unit_selected_by_the_shared_budget():
    main_reasoner = StubMainReasoner()
    service = build_test_service(main_reasoner=main_reasoner)
    history = tuple(
        {
            "seq": seq,
            "status": "completed",
            "history_text": f"history {seq}",
            "assistant_text": f"answer {seq}",
        }
        for seq in range(1, 14)
    )
    context = RoutingContext(
        scope=service.session_scope,
        repository_full_name="sample/widgets",
        history_units=history,
    )

    service.main_agent.decide("继续", repository="sample/widgets", context=context)

    payload = json.loads(main_reasoner.prompts[-1].split("\n", 1)[1])
    assert [item["seq"] for item in payload["session"]["recent_history"]] == list(range(1, 14))


def test_issue_draft_revision_and_publish_are_session_scoped():
    service = build_test_service()

    first = handle(service, "处理 Issue #1，先给我一版回复草稿")
    assert isinstance(first.output, DraftResult)
    initial_draft = first.output.body
    stored = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert stored is not None
    assert stored.agent_context["agent"] == "issues"
    assert stored.agent_context["reply_draft"] == initial_draft

    revised = handle(service, "再短一点")
    assert isinstance(revised.output, DraftResult)
    assert revised.output.body != ""

    proposal = handle(service, "可以，发布吧")
    assert isinstance(proposal.output, AgentContext)
    assert proposal.output.pending is not None
    assert proposal.output.pending.calls[0].tool == "github.post_comment"
    assert proposal.output.pending.calls[0].arguments["body"] == revised.output.body

    repo = service.harness.server.repositories["sample/widgets"]
    before = len(repo.get("comments", []))
    completed = handle(service, "可以")
    assert len(repo["comments"]) == before + 1
    assert repo["comments"][-1]["body"] == revised.output.body
    assert completed.agent == "issues"

    stored = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert stored is not None and stored.agent_context == {}


def test_simple_conversation_answers_without_child_context():
    service = build_test_service()
    result = handle(service, "你好")

    assert result.agent is None
    assert result.output == "你好，我可以帮你处理这个仓库。"
    session = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert session is not None and session.agent_context == {}


def test_issue_calls_coding_directly_and_parent_context_continues_to_approval():
    service = build_test_service(agent_reasoner=IssueFixReasoner())
    main = StubMainReasoner(
        [
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "2",
                "request": "修复 Issue #2",
                "message": "",
                "clarify": False,
                "requested_fix": True,
                "requested_reply": False,
            }
        ]
    )
    service.main_agent.reasoner = main

    result = handle(service, "修复 Issue #2")

    assert isinstance(result.output, AgentContext)
    parent = result.output
    assert parent.agent == "issues"
    assert not parent.finished
    assert parent.pending is not None
    assert parent.code_candidate is not None
    assert parent.verification is not None and parent.verification.passed
    child_summaries = [item for item in parent.observations if item.get("kind") == "agent"]
    assert child_summaries
    assert child_summaries[-1]["payload"]["agent"] == "coding"
    assert child_summaries[-1]["payload"]["verification_passed"] is True
    assert all("observations" not in item["payload"] for item in child_summaries)
    assert parent.pending.calls[-1].tool == "github.create_draft_pr"
    pr_call = parent.pending.calls[-1]
    assert pr_call.arguments["title"].startswith("Fix #2:")
    assert "## Summary" in pr_call.arguments["body"]
    assert "## Static verification" in pr_call.arguments["body"]
    assert "## Related issue\n#2" in pr_call.arguments["body"]


def test_issue_fix_creates_a_separately_approved_modification_report_after_the_draft_pr():
    service = build_test_service(
        main_responses=[
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "2",
                "request": "修复 Issue #2",
                "message": "",
                "clarify": False,
                "requested_fix": True,
                "requested_reply": False,
            }
        ],
        agent_reasoner=IssueFixReasoner(),
    )

    proposal = handle(service, "修复 Issue #2")
    assert isinstance(proposal.output, AgentContext)
    assert proposal.output.pending is not None
    assert proposal.output.pending.calls[-1].tool == "github.create_draft_pr"

    report_proposal = handle(service, "可以")

    assert isinstance(report_proposal.output, AgentContext)
    assert report_proposal.output.pending is not None
    assert [call.tool for call in report_proposal.output.pending.calls] == ["github.post_comment"]
    report = report_proposal.output.pending.calls[0].arguments["body"]
    assert report_proposal.output.reply_draft == report
    assert "Draft PR #" in report
    assert "## 修改摘要" in report
    assert "## 变更文件" in report
    assert "## 静态验证" in report
    assert "## 后续验证" in report
    repository = service.harness.server.repositories["sample/widgets"]
    assert len(repository["draft_prs"]) == 1
    assert repository.get("comments", []) == []

    completed = handle(service, "可以")

    assert len(repository["comments"]) == 1
    assert repository["comments"][0]["issue_number"] == 2
    assert repository["comments"][0]["body"] == report
    assert "并发布修改报告到 Issue #2" in completed.output.answer


def test_issue_confirmation_resumes_the_same_context_and_keeps_structured_handoff():
    service = build_test_service(
        main_responses=[
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "2",
                "request": "分析并处理 Issue #2",
                "message": "",
                "clarify": False,
                "requested_fix": True,
                "requested_reply": False,
            }
        ],
        agent_reasoner=ConfirmationThenFixReasoner(),
    )

    first = handle(service, "分析并处理 Issue #2")

    assert isinstance(first.output, AgentContext)
    assert first.output.question == "建议修改 src/math_utils.py。是否继续？"
    assert first.output.finished is False
    stored = service._test_sessions.get_session(
        service.session_scope.account_key,
        service.session_scope.repository_key,
        service.session_scope.session_id,
    )
    assert stored is not None
    assert stored.agent_context["agent"] == "issues"

    second = handle(service, "继续，允许修改")

    assert isinstance(second.output, AgentContext)
    assert second.output.session_id == first.output.session_id
    assert second.output.pending is not None
    assert second.output.change_request is not None
    assert second.output.change_request.issue_number == 2
    assert second.output.change_request.target_files == ["src/math_utils.py"]
    assert any(
        item.get("kind") == "user" and item.get("payload") == "继续，允许修改"
        for item in second.output.observations
    )


def test_main_routes_issue_scoped_code_changes_through_the_issue_agent():
    service = build_test_service(
        main_responses=[
            {
                "target_agent": "code_change",
                "entity_type": "issue",
                "entity_id": "2",
                "request": "继续修复 Issue #2",
                "message": "",
                "clarify": False,
                "requested_fix": True,
                "requested_reply": False,
            }
        ],
        agent_reasoner=ConfirmationThenFixReasoner(),
    )

    result = handle(service, "继续修复 Issue #2")

    assert result.agent == "issues"


def test_issue_three_shaped_two_turn_flow_preserves_late_file_content_and_reaches_approval():
    padding = "".join(f"# padding line {index} keeps the source projection realistic\n" for index in range(70))
    original_session = (
        padding
        + "\ndef list_sessions():\n"
        + "    try:\n"
        + "        return load_files()\n"
        + "    except (ValueError, KeyError):\n"
        + "        return []\n"
    )
    fixed_session = original_session.replace(
        "except (ValueError, KeyError):",
        "except (ValueError, KeyError, OSError):",
    )
    original_test = "def test_existing():\n    assert True\n"
    fixed_test = original_test + "\ndef test_unreadable_session_is_skipped():\n    assert True\n"
    repositories = sample_repositories()
    repository = repositories["sample/widgets"]
    repository["files"]["corecoder/session.py"] = original_session
    repository["files"]["tests/test_session.py"] = original_test
    repository["issues"][3] = {
        "number": 3,
        "title": "list_sessions should skip unreadable files",
        "body": "Catch OSError in corecoder/session.py and add tests/test_session.py coverage.",
        "labels": ["bug"],
        "comments": [],
    }
    reasoner = IssueThreeWorkflowReasoner(original_session, fixed_session, fixed_test)
    service = build_test_service(
        main_responses=[
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "3",
                "request": "分析并处理 Issue #3",
                "message": "",
                "clarify": False,
                "requested_fix": True,
                "requested_reply": False,
            }
        ],
        agent_reasoner=reasoner,
        server=InMemoryMCPServer(repositories),
    )

    first = handle(service, "分析并处理 Issue #3")
    second = handle(service, "继续，允许修改")

    assert isinstance(first.output, AgentContext) and first.output.question
    assert isinstance(second.output, AgentContext) and second.output.pending is not None
    assert "def list_sessions" in reasoner.fix_prompt
    assert '"corecoder/session.py"' in reasoner.candidate_prompt
    assert "def list_sessions" in reasoner.candidate_prompt
    assert "padding line 69" in reasoner.candidate_prompt
    assert second.output.change_request is not None
    assert second.output.change_request.issue_number == 3
    assert second.output.change_request.target_files == ["corecoder/session.py", "tests/test_session.py"]
    assert second.output.code_candidate is not None
    assert second.output.code_candidate.changed_files == ["corecoder/session.py", "tests/test_session.py"]


def test_issue_code_candidate_timeout_is_not_swallowed_by_the_decision_fallback():
    repositories = sample_repositories()
    repositories["sample/widgets"]["issues"][2]["change_request"] = {
        "description": "Correct add so it returns the sum of both arguments",
        "target_files": ["src/math_utils.py"],
    }
    service = build_test_service(
        main_responses=[
            {
                "target_agent": "issues",
                "entity_type": "issue",
                "entity_id": "2",
                "request": "分析并处理 Issue #2",
                "message": "",
                "clarify": False,
                "requested_fix": True,
                "requested_reply": False,
            }
        ],
        agent_reasoner=ConfirmationThenTimeoutReasoner(),
        server=InMemoryMCPServer(repositories),
    )

    handle(service, "分析并处理 Issue #2")
    result = handle(service, "继续，允许修改")

    assert isinstance(result.output, AgentContext)
    assert result.output.error is not None
    assert "code candidate preparation failed" in result.output.error
    assert "候选补丁生成阶段失败" in result.output.error
    assert result.output.code_candidate is None


def test_main_agent_routes_from_session_context_without_task_lifecycle_fields():
    reasoner = StubMainReasoner()
    service = build_test_service(main_reasoner=reasoner)
    result = handle(service, "看看 Issue #1")

    assert result.agent == "issues"
    routing_prompt = reasoner.prompts[0]
    assert "session" in routing_prompt
    assert "working_state" in routing_prompt
