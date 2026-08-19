"""GitHub REST API 的 MCP 后端：按需读取仓库，并执行已审批写操作。"""

from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any

from ..core.errors import ToolExecutionError, ValidationError
from ..core.models import RepositoryRef
from .base import safe_repository_path
from .memory import InMemoryMCPServer


class GitHubMCPServer(InMemoryMCPServer):
    """与内存后端暴露相同工具契约的 GitHub REST 实现。

    继承只复用工具注册、静态验证与参数安全检查；repository.* 和
    github.* 方法全部在本类中走远程 API，不会 clone 仓库。
    """

    def __init__(
        self,
        token: str = "",
        *,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._default_branches: dict[str, str] = {}
        super().__init__({})

    # Account namespace ----------------------------------------------------

    def get_authenticated_user(self) -> dict[str, Any]:
        """返回当前 Token 对应账号的最小公开身份信息。"""
        self._require_token()
        value = self._request("GET", "/user")
        return {
            "id": self._numeric_id(value, "authenticated user"),
            "login": str(value.get("login", "")),
            "name": str(value.get("name") or ""),
            "avatar_url": str(value.get("avatar_url") or ""),
        }

    def list_repositories(self, *, max_repositories: int = 1000) -> list[dict[str, Any]]:
        """分页读取当前 Token 可访问的个人、协作与组织仓库。"""
        self._require_token()
        limit = max(1, min(max_repositories, 1000))
        repositories: list[dict[str, Any]] = []
        per_page = min(100, limit)
        for page in range(1, 11):
            query = urllib.parse.urlencode(
                {
                    "affiliation": "owner,collaborator,organization_member",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": per_page,
                    "page": page,
                }
            )
            values = self._request("GET", f"/user/repos?{query}")
            if not isinstance(values, list):
                raise ToolExecutionError("GitHub repository list returned an unexpected response")
            for value in values:
                full_name = str(value.get("full_name") or "")
                if not full_name:
                    continue
                repositories.append(
                    {
                        "id": self._numeric_id(value, f"repository {full_name}"),
                        "full_name": full_name,
                        "private": bool(value.get("private")),
                        "archived": bool(value.get("archived")),
                        "default_branch": str(value.get("default_branch") or "main"),
                        "updated_at": str(value.get("updated_at") or ""),
                        "permissions": dict(value.get("permissions") or {}),
                    }
                )
                if len(repositories) >= limit:
                    return repositories
            if len(values) < per_page:
                break
        return repositories

    @staticmethod
    def _numeric_id(value: dict[str, Any], label: str) -> int:
        raw = value.get("id")
        if isinstance(raw, bool):
            raise ToolExecutionError(f"GitHub returned no stable numeric ID for {label}")
        if isinstance(raw, int):
            identifier = raw
        elif isinstance(raw, str) and raw.isascii() and raw.isdecimal():
            identifier = int(raw)
        else:
            raise ToolExecutionError(f"GitHub returned no stable numeric ID for {label}")
        if identifier < 1:
            raise ToolExecutionError(f"GitHub returned an invalid numeric ID for {label}")
        return identifier

    # Repository namespace -------------------------------------------------

    def get_repo_tree(
        self,
        repository: str,
        path: str = "",
        depth: int = 2,
        max_entries: int = 300,
        ref: str | None = None,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        prefix = "" if not path else safe_repository_path(path).rstrip("/") + "/"
        depth = max(1, min(depth, 8))
        max_entries = max(1, min(max_entries, 500))
        actual_ref = ref or self._default_branch(repository)
        value = self._request(
            "GET",
            f"/repos/{repository}/git/trees/{urllib.parse.quote(actual_ref, safe='')}?recursive=1",
        )
        paths: list[str] = []
        for item in value.get("tree", []):
            file_path = str(item.get("path", ""))
            if item.get("type") != "blob" or (prefix and not file_path.startswith(prefix)):
                continue
            relative = file_path[len(prefix) :]
            if len(relative.split("/")) <= depth:
                paths.append(file_path)
            if len(paths) >= max_entries:
                break
        return {
            "repository": repository,
            "path": path,
            "entries": paths,
            "truncated": bool(value.get("truncated")) or len(paths) >= max_entries,
        }

    def search_code(
        self,
        repository: str,
        query: str,
        path: str = "",
        max_results: int = 20,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        if not query.strip():
            raise ValidationError("search query cannot be empty")
        max_results = max(1, min(max_results, 30))
        qualifier = f"{query} repo:{repository}" + (f" path:{safe_repository_path(path)}" if path else "")
        encoded = urllib.parse.urlencode({"q": qualifier, "per_page": max_results})
        value = self._request("GET", f"/search/code?{encoded}")
        results: list[dict[str, Any]] = []
        for item in value.get("items", [])[:max_results]:
            file_path = str(item.get("path", ""))
            try:
                fetched = self.read_file(repository, file_path, limit=400)
            except ToolExecutionError:
                continue
            for line_number, line in enumerate(str(fetched["content"]).splitlines(), 1):
                if query.casefold() in line.casefold():
                    results.append({"path": file_path, "line": line_number, "snippet": line[:500]})
                    if len(results) >= max_results:
                        return {"query": query, "results": results, "truncated": True}
        return {"query": query, "results": results, "truncated": len(value.get("items", [])) >= max_results}

    def read_file(
        self,
        repository: str,
        path: str,
        start_line: int = 1,
        limit: int = 200,
        ref: str | None = None,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        safe = safe_repository_path(path)
        actual_ref = ref or self._default_branch(repository)
        encoded_path = urllib.parse.quote(safe, safe="/")
        query = urllib.parse.urlencode({"ref": actual_ref})
        value = self._request("GET", f"/repos/{repository}/contents/{encoded_path}?{query}")
        if value.get("type") != "file" or value.get("encoding") != "base64":
            raise ToolExecutionError(f"GitHub did not return base64 file content for {safe}")
        try:
            content = base64.b64decode(str(value.get("content", "")), validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolExecutionError(f"file is not UTF-8 text: {safe}") from exc
        start_line = max(1, start_line)
        limit = max(1, min(limit, 400))
        lines = content.splitlines(keepends=True)
        end_line = min(len(lines), start_line - 1 + limit)
        selected = "".join(lines[start_line - 1 : end_line])
        char_truncated = len(selected) > 120_000
        if char_truncated:
            selected = selected[:120_000]
        return {
            "path": safe,
            "start_line": start_line,
            "end_line": end_line,
            "content": selected,
            "truncated": char_truncated or end_line < len(lines),
        }

    def read_files(
        self,
        repository: str,
        paths: list[str],
        limit_per_file: int = 200,
        ref: str | None = None,
    ) -> dict[str, Any]:
        if len(paths) > 20:
            raise ValidationError("read_files is limited to 20 targeted paths")
        return {"files": [self.read_file(repository, path, limit=limit_per_file, ref=ref) for path in paths]}

    def find_symbol(self, repository: str, symbol: str, max_results: int = 20) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_$][\w$.:<>-]*", symbol):
            raise ValidationError("symbol must be an identifier-like value")
        search = self.search_code(repository, symbol, max_results=max_results)
        pattern = re.compile(
            rf"^\s*(?:async\s+def|def|class|function|interface|type|const|let|var)\s+{re.escape(symbol)}\b"
        )
        return {
            "symbol": symbol,
            "results": [item for item in search["results"] if pattern.search(item["snippet"])],
            "truncated": search["truncated"],
        }

    def find_references(self, repository: str, symbol: str, max_results: int = 50) -> dict[str, Any]:
        return self.search_code(repository, symbol, max_results=min(max_results, 30))

    def get_pr_diff(self, repository: str, pr_number: int, max_chars: int = 160_000) -> dict[str, Any]:
        repository = self._repository(repository)
        diff = self._request(
            "GET",
            f"/repos/{repository}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
            raw=True,
        )
        limit = max(1, min(max_chars, 300_000))
        return {"pr_number": pr_number, "diff": diff[:limit], "truncated": len(diff) > limit}

    def get_changed_files(self, repository: str, pr_number: int) -> dict[str, Any]:
        repository = self._repository(repository)
        values = self._request("GET", f"/repos/{repository}/pulls/{pr_number}/files?per_page=100")
        files = [str(item.get("filename")) for item in values]
        return {"pr_number": pr_number, "files": files, "truncated": len(files) >= 100}

    def get_file_history(self, repository: str, path: str, limit: int = 20) -> dict[str, Any]:
        repository = self._repository(repository)
        safe = safe_repository_path(path)
        limit = max(1, min(limit, 50))
        query = urllib.parse.urlencode({"path": safe, "per_page": limit})
        values = self._request("GET", f"/repos/{repository}/commits?{query}")
        commits = [
            {
                "sha": item.get("sha"),
                "message": item.get("commit", {}).get("message", ""),
                "author": item.get("commit", {}).get("author", {}),
            }
            for item in values
        ]
        return {"path": safe, "commits": commits}

    # GitHub namespace -----------------------------------------------------

    def get_issue(self, repository: str, issue_number: int) -> dict[str, Any]:
        repository = self._repository(repository)
        value = self._request("GET", f"/repos/{repository}/issues/{issue_number}")
        value["labels"] = [
            item.get("name", "") if isinstance(item, dict) else str(item) for item in value.get("labels", [])
        ]
        return value

    def list_issues(
        self,
        repository: str,
        state: str = "open",
        labels: list[str] | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        if state not in {"open", "closed", "all"}:
            raise ValidationError("issue state must be open, closed, or all")
        limit = max(1, min(limit, 100))
        query = urllib.parse.urlencode(
            {
                "state": state,
                "labels": ",".join(labels or []),
                "sort": "updated",
                "direction": "desc",
                "per_page": limit,
            }
        )
        values = self._request("GET", f"/repos/{repository}/issues?{query}")
        return {"issues": [item for item in values if "pull_request" not in item]}

    def get_issue_comments(self, repository: str, issue_number: int, limit: int = 30) -> dict[str, Any]:
        repository = self._repository(repository)
        limit = max(1, min(limit, 100))
        values = self._request("GET", f"/repos/{repository}/issues/{issue_number}/comments?per_page={limit}")
        return {"comments": values}

    def get_pr(self, repository: str, pr_number: int) -> dict[str, Any]:
        repository = self._repository(repository)
        return self._request("GET", f"/repos/{repository}/pulls/{pr_number}")

    def list_pull_requests(
        self,
        repository: str,
        state: str = "open",
        base: str = "",
        head: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        if state not in {"open", "closed", "all"}:
            raise ValidationError("pull-request state must be open, closed, or all")
        query = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": max(1, min(limit, 100)),
        }
        if base:
            query["base"] = base
        if head:
            query["head"] = head
        values = self._request("GET", f"/repos/{repository}/pulls?{urllib.parse.urlencode(query)}")
        return {"pull_requests": values}

    def get_pr_comments(self, repository: str, pr_number: int) -> dict[str, Any]:
        repository = self._repository(repository)
        values = self._request("GET", f"/repos/{repository}/issues/{pr_number}/comments?per_page=100")
        return {"comments": values}

    def get_pr_reviews(self, repository: str, pr_number: int) -> dict[str, Any]:
        repository = self._repository(repository)
        values = self._request("GET", f"/repos/{repository}/pulls/{pr_number}/reviews?per_page=100")
        return {"pr_number": pr_number, "reviews": values}

    def get_workflow_runs(
        self,
        repository: str,
        pr_number: int | None = None,
        workflow_run_id: int | None = None,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        if workflow_run_id is not None:
            values = [self._request("GET", f"/repos/{repository}/actions/runs/{workflow_run_id}")]
        else:
            response = self._request("GET", f"/repos/{repository}/actions/runs?per_page=50")
            values = response.get("workflow_runs", [])
        if pr_number is not None:
            values = [
                run
                for run in values
                if any(int(item.get("number", -1)) == pr_number for item in run.get("pull_requests", []))
            ]
        return {"runs": values[:50]}

    def get_job_logs(
        self, repository: str, run_id: int, job_id: int | None = None, max_chars: int = 80_000
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        response = self._request("GET", f"/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100")
        jobs = response.get("jobs", [])
        if job_id is not None:
            jobs = [job for job in jobs if int(job.get("id", -1)) == job_id]
        bounded: list[dict[str, Any]] = []
        limit = max(1, min(max_chars, 120_000))
        for job in jobs[:20]:
            if str(job.get("conclusion", "")).casefold() not in {"failure", "failed"} and job_id is None:
                continue
            log = self._request_bytes("GET", f"/repos/{repository}/actions/jobs/{int(job['id'])}/logs")
            text = self._decode_log(log)
            bounded.append({**job, "log": text[:limit], "log_truncated": len(text) > limit})
        return {"run_id": run_id, "jobs": bounded}

    def post_comment(self, repository: str, issue_number: int, body: str) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if not body.strip():
            raise ValidationError("comment body cannot be empty")
        return self._request("POST", f"/repos/{repository}/issues/{issue_number}/comments", {"body": body})

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        state: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone: str | None = None,
    ) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        payload: dict[str, Any] = {}
        if state is not None:
            if state not in {"open", "closed"}:
                raise ValidationError("issue state must be open or closed")
            payload["state"] = state
        if labels is not None:
            payload["labels"] = [str(label) for label in labels]
        if assignees is not None:
            payload["assignees"] = [str(item) for item in assignees]
        if milestone is not None:
            payload["milestone"] = None if milestone in {"", "none"} else milestone
        return self._request("PATCH", f"/repos/{repository}/issues/{issue_number}", payload)

    def update_pr(self, repository: str, pr_number: int, state: str | None = None) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if state is not None and state not in {"open", "closed"}:
            raise ValidationError("pull-request state must be open or closed")
        return self._request("PATCH", f"/repos/{repository}/pulls/{pr_number}", {"state": state})

    def create_branch(self, repository: str, base: str, branch: str) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        self._validate_branch(branch)
        base_ref = self._request("GET", f"/repos/{repository}/git/ref/heads/{urllib.parse.quote(base, safe='')}")
        self._request(
            "POST",
            f"/repos/{repository}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_ref["object"]["sha"]},
        )
        return {"repository": repository, "branch": branch, "base": base}

    def commit(self, repository: str, branch: str, files: dict[str, str], message: str) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        self._validate_branch(branch)
        if not files or not message.strip():
            raise ValidationError("commit requires exact file contents and a message")
        ref = self._request("GET", f"/repos/{repository}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        parent_sha = ref["object"]["sha"]
        parent = self._request("GET", f"/repos/{repository}/git/commits/{parent_sha}")
        tree_entries = []
        for path, content in files.items():
            safe = safe_repository_path(path)
            blob = self._request("POST", f"/repos/{repository}/git/blobs", {"content": content, "encoding": "utf-8"})
            tree_entries.append({"path": safe, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._request(
            "POST",
            f"/repos/{repository}/git/trees",
            {"base_tree": parent["tree"]["sha"], "tree": tree_entries},
        )
        commit = self._request(
            "POST",
            f"/repos/{repository}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
        )
        self._request(
            "PATCH",
            f"/repos/{repository}/git/refs/heads/{urllib.parse.quote(branch, safe='')}",
            {"sha": commit["sha"], "force": False},
        )
        return {"commit": commit["sha"], "branch": branch, "files": sorted(files)}

    def push(self, repository: str, branch: str) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        self._request("GET", f"/repos/{repository}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        return {"repository": repository, "branch": branch, "pushed": True}

    def create_draft_pr(
        self,
        repository: str,
        title: str,
        body: str,
        base: str,
        head: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if draft is not True:
            raise ValidationError("GitAgent only creates draft pull requests")
        return self._request(
            "POST",
            f"/repos/{repository}/pulls",
            {"title": title, "body": body, "base": base, "head": head, "draft": True},
        )

    def post_review(self, repository: str, pr_number: int, event: str, body: str) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValidationError("invalid review event")
        return self._request(
            "POST",
            f"/repos/{repository}/pulls/{pr_number}/reviews",
            {"event": event, "body": body},
        )

    def merge(self, repository: str, pr_number: int, expected_head_sha: str) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if not expected_head_sha.strip():
            raise ValidationError("merge requires the reviewed head SHA")
        return self._request(
            "PUT",
            f"/repos/{repository}/pulls/{pr_number}/merge",
            {"sha": expected_head_sha.strip()},
        )

    # HTTP helpers ---------------------------------------------------------

    def _default_branch(self, repository: str) -> str:
        if repository not in self._default_branches:
            value = self._request("GET", f"/repos/{repository}")
            self._default_branches[repository] = str(value.get("default_branch") or "main")
        return self._default_branches[repository]

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
    ) -> Any:
        data = self._request_bytes(method, path, payload, accept=accept)
        if raw:
            return data.decode("utf-8", errors="replace")
        if not data:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(f"GitHub returned invalid JSON for {method} {path}") from exc

    def _request_bytes(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        accept: str = "application/vnd.github+json",
    ) -> bytes:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GitAgent/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"{self.api_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ToolExecutionError(f"GitHub API {method} {path} failed ({exc.code}): {details}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"GitHub API connection failed: {exc.reason}") from exc

    @staticmethod
    def _decode_log(data: bytes) -> str:
        if data.startswith(b"PK"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    return "\n".join(
                        archive.read(name).decode("utf-8", errors="replace") for name in sorted(archive.namelist())
                    )
            except (OSError, zipfile.BadZipFile):
                pass
        return data.decode("utf-8", errors="replace")

    def _require_token(self) -> None:
        if not self.token:
            raise ToolExecutionError("GitHub write requires GITHUB_TOKEN or GH_TOKEN")

    @staticmethod
    def _repository(repository: str) -> str:
        return str(RepositoryRef.parse(repository))
