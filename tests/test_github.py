from __future__ import annotations

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
        GitHubMCPServer().read_files("sample/widgets", ["corecoder/session.py"])
