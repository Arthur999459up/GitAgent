"""Natural-language UX tests over Session context without task identifiers."""

from typing import Any

from AGENT.GitAgent.gitagent.core.models import DraftResult, IssueOperation
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


def test_single_and_batched_file_reads_share_coverage_and_continue_from_the_first_gap():
    service = build_test_service()
    service.harness.server.repositories["sample/widgets"]["files"]["docs/paged.txt"] = "".join(
        f"line {number}\n" for number in range(1, 451)
    )
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="分析 Issue #1",
        entity_type="issue",
        entity_id="1",
    )
    first = context.tool("repository.read_file", repository="sample/widgets", path="docs/paged.txt", limit=200)
    second = context.tool(
        "repository.read_files",
        repository="sample/widgets",
        requests=[{"path": "docs/paged.txt", "limit": 200}],
    )["files"][0]
    third = context.tool("repository.read_file", repository="sample/widgets", path="docs/paged.txt", limit=200)

    assert (first["start_line"], first["end_line"], first["truncated"]) == (1, 200, True)
    assert (second["start_line"], second["end_line"], second["truncated"]) == (201, 400, True)
    assert (third["start_line"], third["end_line"], third["truncated"]) == (401, 450, False)
    assert context.file_reads.summaries() == [
        {
            "repository": "sample/widgets",
            "path": "docs/paged.txt",
            "ref": None,
            "ranges": [[1, 450]],
            "eof": True,
            "eof_line": 450,
        }
    ]


def test_implicit_read_stops_before_a_later_covered_range_instead_of_refetching_it():
    service = build_test_service()
    service.harness.server.repositories["sample/widgets"]["files"]["docs/gaps.txt"] = "".join(
        f"line {number}\n" for number in range(1, 301)
    )
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="分析 Issue #1",
        entity_type="issue",
        entity_id="1",
    )

    middle = context.tool(
        "repository.read_file",
        repository="sample/widgets",
        path="docs/gaps.txt",
        start_line=101,
        limit=100,
    )
    beginning = context.tool("repository.read_file", repository="sample/widgets", path="docs/gaps.txt", limit=200)
    end = context.tool("repository.read_file", repository="sample/widgets", path="docs/gaps.txt", limit=200)

    assert (middle["start_line"], middle["end_line"]) == (101, 200)
    assert (beginning["start_line"], beginning["end_line"]) == (1, 100)
    assert (end["start_line"], end["end_line"], end["truncated"]) == (201, 300, False)
    assert context.file_reads.summaries()[0]["ranges"] == [[1, 300]]


def test_covered_file_range_is_not_fetched_or_returned_again():
    service = build_test_service()
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="分析 Issue #1",
        entity_type="issue",
        entity_id="1",
    )
    first = context.tool(
        "repository.read_files",
        repository="sample/widgets",
        requests=[{"path": "src/formatting.py", "limit": 200}],
    )
    trace_before = len(service.harness.trace.events(context.session_id))
    repeated = context.tool(
        "repository.read_file",
        repository="sample/widgets",
        path="src/formatting.py",
        start_line=1,
        limit=200,
    )

    assert first["files"][0]["content"].startswith("def format_name")
    assert repeated["already_read"] is True
    assert "content" not in repeated
    assert context.last_tool_call is not None and context.last_tool_call.covered is True
    assert context.last_tool_call.observation_data == {
        "already_read": True,
        "coverage": {
            "repository": "sample/widgets",
            "path": "src/formatting.py",
            "ref": None,
            "ranges": [[1, 2]],
            "eof": True,
            "eof_line": 2,
        },
    }
    assert len(service.harness.trace.events(context.session_id)) == trace_before


def test_file_coverage_and_observed_content_survive_agent_context_persistence():
    service = build_test_service()
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="分析 Issue #1",
        entity_type="issue",
        entity_id="1",
    )
    original = context.tool(
        "repository.read_file",
        repository="sample/widgets",
        path="src/formatting.py",
        limit=200,
    )
    context.observations.append(
        {
            "kind": "tool",
            "payload": {
                "tool": "repository.read_file",
                "arguments": {"repository": "sample/widgets", "path": "src/formatting.py", "limit": 200},
                "data": original,
            },
        }
    )
    restored = service._restore_context(service._serialize_context(context))
    trace_before = len(service.harness.trace.events(context.session_id))

    repeated = restored.tool(
        "repository.read_files",
        repository="sample/widgets",
        requests=[{"path": "src/formatting.py", "limit": 200}],
    )["files"][0]

    assert repeated["already_read"] is True
    assert "content" not in repeated
    assert restored.observations[0]["payload"]["data"]["content"] == original["content"]
    assert restored.file_reads.summaries() == context.file_reads.summaries()
    assert len(service.harness.trace.events(context.session_id)) == trace_before


def test_character_bound_file_reads_stop_on_a_line_boundary_before_continuing():
    service = build_test_service()
    line = "x" * 1_999 + "\n"
    service.harness.server.repositories["sample/widgets"]["files"]["docs/wide.txt"] = line * 100
    context = service.harness.context(
        "issues",
        service.session_scope.session_id,
        repository="sample/widgets",
        goal="分析 Issue #1",
        entity_type="issue",
        entity_id="1",
    )

    first = context.tool("repository.read_file", repository="sample/widgets", path="docs/wide.txt", limit=200)
    second = context.tool(
        "repository.read_files",
        repository="sample/widgets",
        requests=[{"path": "docs/wide.txt", "limit": 200}],
    )["files"][0]

    assert first["end_line"] == 60
    assert len(first["content"]) == 120_000
    assert first["truncated"] is True
    assert second["start_line"] == 61
    assert second["end_line"] == 100
    assert second["truncated"] is False


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
