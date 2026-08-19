"""Natural-language UX tests over Session context without task identifiers."""

from typing import Any

from AGENT.GitAgent.gitagent.core.models import DraftResult, IssueOperation
from AGENT.GitAgent.gitagent.runtime import AgentAction, AgentActionKind
from AGENT.GitAgent.tests.support import build_test_service, handle


def test_user_can_edit_and_publish_issue_reply_using_only_natural_language():
    service = build_test_service()
    draft = handle(service, "帮我处理 Issue #1，先写个回复草稿")
    assert isinstance(draft.output, DraftResult)

    shorter = handle(service, "再短一点")
    assert isinstance(shorter.output, DraftResult)

    proposal = handle(service, "可以，发布吧")
    assert proposal.output.pending is not None
    assert proposal.output.pending.calls[0].arguments["body"] == shorter.output.body

    done = handle(service, "可以")
    assert done.agent == "issues"
    assert service.harness.server.repositories["sample/widgets"]["comments"][-1]["body"] == shorter.output.body


def test_rejecting_publish_returns_to_draft_review_without_write():
    service = build_test_service()
    draft = handle(service, "处理 Issue #1，给我回复草稿")
    handle(service, "发布吧")
    repo = service.harness.server.repositories["sample/widgets"]
    before = len(repo.get("comments", []))

    rejected = handle(service, "算了，不要发布")

    assert isinstance(rejected.output, DraftResult)
    assert rejected.output.body == draft.output.body
    assert len(repo.get("comments", [])) == before


def test_simple_repository_question_uses_domain_agent_but_greeting_does_not():
    service = build_test_service()
    greeting = handle(service, "你好")
    assert greeting.agent is None

    question = handle(service, "format_name 在哪里？")
    assert question.agent == "repo_qa"
    assert "src/formatting.py" in question.output.answer


def test_issue_detail_result_wins_over_historical_issue_list():
    service = build_test_service()
    issue = {
        "number": 1,
        "title": "上下文压缩策略",
        "state": "open",
        "body": "需要展开分析这个具体问题。",
        "labels": [],
    }
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="我要展开看一下问题1",
        entity_type="issue",
        entity_id="1",
    )
    context.observations = [
        {
            "kind": "tool",
            "payload": {
                "tool": "github.list_issues",
                "data": {"issues": [issue, {"number": 2, "title": "另一个问题", "state": "open", "labels": []}]},
            },
        },
        {"kind": "tool", "payload": {"tool": "github.get_issue", "data": issue}},
    ]

    result = service.issue_agent.build_result(context)

    assert result.operation == IssueOperation.GET
    assert result.issue_number == 1
    assert len(result.issues) == 1
    assert "需要展开分析这个具体问题" in result.answer


def test_file_read_stops_after_five_reads():
    service = build_test_service()
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="分析 Issue #1",
        entity_type="issue",
        entity_id="1",
    )
    context.result_required = False
    for line in range(1, 6):
        context.observations.append(
            {
                "kind": "tool",
                "payload": {
                    "tool": "repository.read_file",
                    "arguments": {"path": "docs/paged.txt", "start_line": line, "limit": 1},
                    "data": {"path": "docs/paged.txt", "start_line": line, "end_line": line, "content": "line\n"},
                },
            }
        )

    action = service.issue_agent._normalize_file_read(
        context,
        AgentAction(
            AgentActionKind.TOOL,
            tool="repository.read_file",
            arguments={"path": "docs/paged.txt", "start_line": 1, "limit": 1},
        ),
    )

    assert action.kind == AgentActionKind.FINISH
    assert "5 次读取上限" in action.summary


def test_issue_detail_answer_keeps_repository_evidence_already_read():
    class CaptureAnswerReasoner:
        def __init__(self) -> None:
            self.prompt = ""

        def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"unexpected structured call: {kwargs}")

        def complete_text(self, *, system: str, prompt: str) -> str:
            del system
            self.prompt = prompt
            return "answer from repository evidence"

    reasoner = CaptureAnswerReasoner()
    service = build_test_service(agent_reasoner=reasoner)
    issue = {"number": 1, "title": "关于上下文压缩策略", "body": "上下文压缩策略是怎么做的？", "labels": []}
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="帮我分析 Issue #1",
        entity_type="issue",
        entity_id="1",
    )
    context.observations = [
        {"kind": "tool", "payload": {"tool": "github.get_issue", "data": issue}},
        {
            "kind": "tool",
            "payload": {
                "tool": "repository.read_file",
                "arguments": {"path": "corecoder/context.py"},
                "data": {"path": "corecoder/context.py", "content": "def compact_context():\n    return 'summary'\n"},
            },
        },
    ]

    answer = service.issue_agent._detail_answer(context, issue, [])

    assert answer == "answer from repository evidence"
    assert "repository_evidence" in reasoner.prompt
    assert "corecoder/context.py" in reasoner.prompt
