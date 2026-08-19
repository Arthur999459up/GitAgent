"""Session projection tests for direct child-agent results."""

from AGENT.GitAgent.gitagent.app.projection import project_service_result
from AGENT.GitAgent.gitagent.core.models import DraftResult
from AGENT.GitAgent.gitagent.runtime import AgentContext
from AGENT.GitAgent.tests.support import build_test_service, handle


def test_draft_projection_contains_reviewable_artifact():
    service = build_test_service()
    result = handle(service, "处理 Issue #1，先给我回复草稿")
    projection = project_service_result(result, turn_seq=1)

    assert isinstance(result.output, DraftResult)
    assert result.output.body in projection.assistant_text
    assert projection.focus == {"type": "issue", "id": "1", "short_label": "Issue #1"}
    assert projection.route_summary[0]["route"] == "issues"


def test_pending_write_projection_includes_exact_tool_arguments_and_body():
    service = build_test_service()
    first = handle(service, "处理 Issue #1，先给我回复草稿")
    proposal = handle(service, "发布吧")

    assert isinstance(proposal.output, AgentContext)
    assert proposal.output.pending is not None
    projection = project_service_result(proposal, turn_seq=2)
    assert "github.post_comment" in projection.assistant_text
    assert first.output.body in projection.assistant_text
    assert '"issue_number": 1' in projection.assistant_text


def test_completed_write_projection_confirms_the_applied_mutation():
    service = build_test_service()
    first = handle(service, "处理 Issue #1，先给我回复草稿")
    handle(service, "发布吧")
    completed = handle(service, "可以")
    projection = project_service_result(completed, turn_seq=3)

    assert "已发布回复" in projection.assistant_text
    assert service.harness.server.repositories["sample/widgets"]["comments"][-1]["body"] == first.output.body
