from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Self

import AGENT.GitAgent.gitagent.mcp.github as github_module
import pytest
from AGENT.GitAgent.gitagent.core.errors import ToolExecutionError
from AGENT.GitAgent.gitagent.mcp.github import GitHubMCPServer


class FakeResponse:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self) -> bytes:
        return b'{"ok": true}'


class PayloadResponse(FakeResponse):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


def test_get_retries_transient_connection_timeouts(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def flaky_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        nonlocal calls
        del request, timeout
        calls += 1
        if calls < 3:
            raise urllib.error.URLError(TimeoutError("timed out"))
        return FakeResponse()

    monkeypatch.setattr(github_module.urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(github_module.time, "sleep", sleeps.append)

    result = GitHubMCPServer()._request("GET", "/repos/sample/widgets")

    assert result == {"ok": True}
    assert calls == 3
    assert sleeps == [0.25, 0.5]


def test_write_request_is_not_retried_after_connection_timeout(monkeypatch):
    calls = 0

    def failing_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        nonlocal calls
        del request, timeout
        calls += 1
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(github_module.urllib.request, "urlopen", failing_urlopen)

    with pytest.raises(ToolExecutionError, match="connection failed: timed out"):
        GitHubMCPServer()._request("POST", "/repos/sample/widgets/issues", {"title": "test"})

    assert calls == 1


def test_read_files_reports_the_path_that_exhausted_retries(monkeypatch):
    monkeypatch.setattr(github_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        github_module.urllib.request,
        "urlopen",
        lambda request, *, timeout: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError("timed out"))),
    )

    with pytest.raises(ToolExecutionError, match=r"failed to read corecoder/session\.py:.*after 3 attempts"):
        GitHubMCPServer().read_files("sample/widgets", [{"path": "corecoder/session.py"}])


def test_get_job_logs_preserves_job_status_when_log_download_fails(monkeypatch):
    jobs = {
        "jobs": [
            {
                "id": 4201,
                "name": "static-check",
                "status": "completed",
                "conclusion": "failure",
            }
        ]
    }

    def urlopen(request: Any, *, timeout: float) -> PayloadResponse:
        del timeout
        if "/actions/runs/42/jobs?" in request.full_url:
            return PayloadResponse(json.dumps(jobs).encode())
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(github_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(github_module.time, "sleep", lambda _seconds: None)

    result = GitHubMCPServer().get_job_logs("sample/widgets", 42)

    assert result == {
        "run_id": 42,
        "jobs": [
            {
                **jobs["jobs"][0],
                "log": "",
                "log_truncated": False,
                "log_unavailable": True,
            }
        ],
    }


def test_get_pr_returns_none_when_github_reports_not_found(monkeypatch):
    response = urllib.error.HTTPError(
        "https://api.github.com/repos/sample/widgets/pulls/999999",
        404,
        "Not Found",
        {},
        io.BytesIO(b'{"message":"Not Found"}'),
    )
    monkeypatch.setattr(
        github_module.urllib.request,
        "urlopen",
        lambda request, *, timeout: (_ for _ in ()).throw(response),
    )

    assert GitHubMCPServer().get_pr("sample/widgets", 999999) is None


def test_github_http_error_keeps_raw_debug_message_and_exposes_business_reason(monkeypatch):
    response = urllib.error.HTTPError(
        "https://api.github.com/repos/sample/widgets/pulls/11/reviews",
        422,
        "Unprocessable Entity",
        {},
        io.BytesIO(
            json.dumps(
                {
                    "message": "Unprocessable Entity",
                    "errors": ["Review Can not approve your own pull request"],
                    "documentation_url": "https://docs.github.com/rest/pulls/reviews#create-a-review-for-a-pull-request",
                    "status": "422",
                }
            ).encode()
        ),
    )
    monkeypatch.setattr(
        github_module.urllib.request,
        "urlopen",
        lambda request, *, timeout: (_ for _ in ()).throw(response),
    )

    with pytest.raises(ToolExecutionError) as captured:
        GitHubMCPServer()._request("POST", "/repos/sample/widgets/pulls/11/reviews", {"event": "APPROVE"})

    assert "documentation_url" in str(captured.value)
    assert captured.value.user_message == (
        "GitHub 拒绝了该操作（HTTP 422）：Review Can not approve your own pull request"
    )


def test_review_responses_normalize_github_states_to_request_events(monkeypatch):
    calls: list[tuple[str, Any]] = []

    def capture_urlopen(request: Any, *, timeout: float) -> PayloadResponse:
        del timeout
        payload = json.loads(request.data) if request.data is not None else None
        calls.append((request.get_method(), payload))
        if request.get_method() == "POST":
            return PayloadResponse(b'{"id": 6, "state": "APPROVED"}')
        return PayloadResponse(
            json.dumps(
                [
                    {"id": 1, "state": "APPROVED"},
                    {"id": 2, "state": "CHANGES_REQUESTED"},
                    {"id": 3, "state": "COMMENTED"},
                    {"id": 4, "state": "PENDING"},
                    {"id": 5, "state": "DISMISSED"},
                ]
            ).encode()
        )

    monkeypatch.setattr(github_module.urllib.request, "urlopen", capture_urlopen)
    server = GitHubMCPServer(token="token")

    listed = server.get_pr_reviews("sample/widgets", 11)
    posted = server.post_review("sample/widgets", 11, "APPROVE", "Ready")

    assert [review.get("event") for review in listed["reviews"]] == [
        "APPROVE",
        "REQUEST_CHANGES",
        "COMMENT",
        None,
        None,
    ]
    assert posted == {"id": 6, "state": "APPROVED", "event": "APPROVE"}
    assert calls[-1] == ("POST", {"event": "APPROVE", "body": "Ready"})


def test_issue_management_uses_the_expected_rest_methods_and_payloads(monkeypatch):
    calls: list[tuple[str, str, Any]] = []

    def capture_urlopen(request: Any, *, timeout: float) -> PayloadResponse:
        del timeout
        payload = json.loads(request.data) if request.data is not None else None
        calls.append((request.get_method(), request.full_url, payload))
        response = b"[]" if "/milestones?" in request.full_url else b'{"number": 8}'
        return PayloadResponse(response)

    monkeypatch.setattr(github_module.urllib.request, "urlopen", capture_urlopen)
    server = GitHubMCPServer(token="token")

    server.list_milestones("sample/widgets", state="all", limit=25)
    server.create_issue(
        "sample/widgets",
        "New issue",
        body="Body",
        labels=["bug"],
        assignees=["alice"],
        milestone_number=4,
    )
    server.update_issue(
        "sample/widgets",
        8,
        title="Renamed",
        body="Updated",
        state="closed",
        labels=["bug", "resolved"],
        assignees=[],
        clear_milestone=True,
    )
    server.set_issue_lock("sample/widgets", 8, True, "resolved")
    server.set_issue_lock("sample/widgets", 8, False)

    assert calls == [
        ("GET", "https://api.github.com/repos/sample/widgets/milestones?state=all&per_page=25", None),
        (
            "POST",
            "https://api.github.com/repos/sample/widgets/issues",
            {
                "title": "New issue",
                "body": "Body",
                "labels": ["bug"],
                "assignees": ["alice"],
                "milestone": 4,
            },
        ),
        (
            "PATCH",
            "https://api.github.com/repos/sample/widgets/issues/8",
            {
                "title": "Renamed",
                "body": "Updated",
                "state": "closed",
                "labels": ["bug", "resolved"],
                "assignees": [],
                "milestone": None,
            },
        ),
        (
            "PUT",
            "https://api.github.com/repos/sample/widgets/issues/8/lock",
            {"lock_reason": "resolved"},
        ),
        ("DELETE", "https://api.github.com/repos/sample/widgets/issues/8/lock", None),
    ]
