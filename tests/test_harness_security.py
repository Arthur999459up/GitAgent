import pytest
from AGENT.GitAgent.gitagent.core.errors import ApprovalRequired, PermissionDenied, ValidationError
from AGENT.GitAgent.gitagent.core.trace import TraceStatus
from AGENT.GitAgent.gitagent.runtime import AgentContext
from AGENT.GitAgent.tests.support import build_test_service, handle


def _issue_comment_proposal(service):
    handle(service, "处理 Issue #1，先给我回复草稿")
    result = handle(service, "发布吧")
    context = result.output
    assert isinstance(context, AgentContext)
    assert context.pending is not None
    return context


def test_agent_tool_allowlist_is_enforced_in_code():
    service = build_test_service()
    context = service.harness.context("coding", "session-unauthorized")

    with pytest.raises(PermissionDenied):
        context.tool("github.post_comment", repository="sample/widgets", issue_number=1, body="no")

    event = service.harness.audit.events("session-unauthorized")[-1]
    assert event.result == "DENIED"
    assert event.classification.value == "WRITE"
    trace = service.harness.trace.events("session-unauthorized")[-1]
    assert trace.name == "github.post_comment"
    assert trace.status == TraceStatus.DENIED


def test_write_requires_exact_approved_scope_and_arguments():
    service = build_test_service()
    context = _issue_comment_proposal(service)
    approval = service.harness.approvals.decide(context.pending.approval_id, "Approve")
    mutator = service.harness.context("github_mutator", context.session_id)

    call = approval.calls[0]
    changed_arguments = dict(call.arguments, body=call.arguments["body"] + " changed")
    with pytest.raises(ApprovalRequired):
        mutator.tool(call.tool, approval_id=approval.approval_id, **changed_arguments)

    assert service.harness.server.repositories["sample/widgets"].get("comments", []) == []


def test_only_exact_human_decisions_are_accepted():
    service = build_test_service()
    context = _issue_comment_proposal(service)

    with pytest.raises(ValidationError):
        service.harness.approvals.decide(context.pending.approval_id, "yes")

    assert context.pending is not None
