"""Pure GitHub REST API adapter with no Agent capability metadata."""

from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from copy import deepcopy
from typing import Any

from gitagent.domain.errors import (
    ExternalExecutionError,
    ResourceNotFoundError,
    ValidationError,
)
from gitagent.domain.models import RepositoryRef
from gitagent.domain.reviews import normalize_review
from gitagent.harness.file_access import (
    parse_file_read_requests,
    safe_repository_path,
    select_file_lines,
)

from .errors import GitHubAPIError, GitHubTransportError

_SEARCH_CACHE_SECONDS = 60.0
_FALLBACK_FILE_LIMIT = 500
_FALLBACK_BYTE_LIMIT = 20_000_000
_FALLBACK_FILE_BYTE_LIMIT = 512_000


class GitHubClient:
    """Read and mutate GitHub through the REST API without cloning."""

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
        self._search_cache: dict[tuple[str, str, str, str, int], tuple[float, dict[str, Any]]] = {}
        self._text_cache: dict[tuple[str, str, str], str] = {}

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
                raise ExternalExecutionError("GitHub repository list returned an unexpected response")
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
            raise ExternalExecutionError(f"GitHub returned no stable numeric ID for {label}")
        if isinstance(raw, int):
            identifier = raw
        elif isinstance(raw, str) and raw.isascii() and raw.isdecimal():
            identifier = int(raw)
        else:
            raise ExternalExecutionError(f"GitHub returned no stable numeric ID for {label}")
        if identifier < 1:
            raise ExternalExecutionError(f"GitHub returned an invalid numeric ID for {label}")
        return identifier

    # Repository namespace -------------------------------------------------

    def get_default_branch(self, repository: str) -> dict[str, Any]:
        repository = self._repository(repository)
        branch = self._default_branch(repository)
        ref = self._request("GET", f"/repos/{repository}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        commit_sha = str((ref.get("object") or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha):
            raise ExternalExecutionError(f"GitHub returned no commit SHA for {repository}:{branch}")
        return {"repository": repository, "branch": branch, "commit_sha": commit_sha}

    def get_file_status(
        self,
        repository: str,
        paths: list[str],
        ref: str | None = None,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        if not paths or len(paths) > 20:
            raise ValidationError("file status requires between 1 and 20 paths")
        safe_paths = [safe_repository_path(path) for path in paths]
        if len(set(safe_paths)) != len(safe_paths):
            raise ValidationError("file status paths must be unique")
        actual_ref = ref or self._default_branch(repository)
        existing: list[str] = []
        missing: list[str] = []
        for path in safe_paths:
            encoded_path = urllib.parse.quote(path, safe="/")
            query = urllib.parse.urlencode({"ref": actual_ref})
            try:
                value = self._request("GET", f"/repos/{repository}/contents/{encoded_path}?{query}")
            except ResourceNotFoundError:
                missing.append(path)
                continue
            if value.get("type") != "file":
                raise ExternalExecutionError(f"repository path is not a file: {path}")
            existing.append(path)
        return {"existing_files": sorted(existing), "missing_files": sorted(missing)}

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
        omitted_by_depth = 0
        for item in value.get("tree", []):
            file_path = str(item.get("path", ""))
            if item.get("type") != "blob" or (prefix and not file_path.startswith(prefix)):
                continue
            relative = file_path[len(prefix) :]
            if len(relative.split("/")) <= depth:
                paths.append(file_path)
            else:
                omitted_by_depth += 1
            if len(paths) >= max_entries:
                break
        return {
            "repository": repository,
            "path": path,
            "entries": paths,
            "truncated": bool(value.get("truncated")) or len(paths) >= max_entries or omitted_by_depth > 0,
            "depth": depth,
            "omitted_by_depth": omitted_by_depth,
            "ref": actual_ref,
            "tree_sha": str(value.get("sha") or ""),
        }

    def search_code(
        self,
        repository: str,
        query: str,
        path: str = "",
        max_results: int = 20,
    ) -> dict[str, Any]:
        repository = self._repository(repository)
        query = query.strip()
        if not query:
            raise ValidationError("search query cannot be empty")
        max_results = max(1, min(max_results, 30))
        safe_path = safe_repository_path(path) if path else ""
        head_sha = self._default_head_sha(repository)
        cache_key = (repository, head_sha, query.casefold(), safe_path, max_results)
        cached = self._search_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] <= _SEARCH_CACHE_SECONDS:
            return deepcopy(cached[1])

        qualifier = f"{query} repo:{repository}" + (f" path:{safe_path}" if safe_path else "")
        encoded = urllib.parse.urlencode({"q": qualifier, "per_page": max_results})
        try:
            value = self._request("GET", f"/search/code?{encoded}")
        except ResourceNotFoundError:
            raise
        except ExternalExecutionError as exc:
            result = self._scan_default_branch(
                repository,
                head_sha,
                query,
                path=safe_path,
                max_results=max_results,
                native_error=exc.user_message,
            )
            self._search_cache[cache_key] = (time.monotonic(), deepcopy(result))
            return result

        if bool(value.get("incomplete_results")):
            result = self._scan_default_branch(
                repository,
                head_sha,
                query,
                path=safe_path,
                max_results=max_results,
                native_incomplete=True,
            )
            self._search_cache[cache_key] = (time.monotonic(), deepcopy(result))
            return result

        items = list(value.get("items", []))[:max_results]
        results: list[dict[str, Any]] = []
        fetch_errors: list[dict[str, str]] = []
        files_checked = 0
        for item in items:
            file_path = str(item.get("path", ""))
            try:
                content = self._read_text(repository, file_path, head_sha)
            except ExternalExecutionError as exc:
                fetch_errors.append({"path": file_path, "error": str(exc)[:500]})
                continue
            files_checked += 1
            results.extend(self._literal_matches(file_path, content, query, limit=min(2, max_results - len(results))))
            if len(results) >= max_results:
                break
        if items and files_checked == 0:
            details = "; ".join(
                f"{item['path'] or '<unknown>'}: {item['error']}" for item in fetch_errors[:3]
            )
            raise ExternalExecutionError(
                f"GitHub code search returned {len(items)} candidate file(s), but none could be read"
                + (f": {details}" if details else "")
            )
        total_count = int(value.get("total_count") or 0)
        truncated = total_count > len(items) or len(results) >= max_results
        result = {
            "query": query,
            "results": results,
            "truncated": truncated,
            "complete": not truncated and not fetch_errors,
            "total_count": total_count,
            "candidates": len(items),
            "files_checked": files_checked,
            "fetch_errors": fetch_errors,
            "backend": "github_code_search",
            "ref": head_sha,
            "native_incomplete": False,
        }
        self._search_cache[cache_key] = (time.monotonic(), deepcopy(result))
        return result

    def _scan_default_branch(
        self,
        repository: str,
        head_sha: str,
        query: str,
        *,
        path: str,
        max_results: int,
        native_incomplete: bool = False,
        native_error: str = "",
    ) -> dict[str, Any]:
        value = self._request(
            "GET",
            f"/repos/{repository}/git/trees/{urllib.parse.quote(head_sha, safe='')}?recursive=1",
        )
        prefix = "" if not path else path.rstrip("/") + "/"
        results: list[dict[str, Any]] = []
        fetch_errors: list[dict[str, str]] = []
        files_considered = 0
        files_scanned = 0
        bytes_scanned = 0
        skipped_binary = 0
        skipped_large = 0
        budget_exhausted = False
        result_limit_reached = False
        for item in value.get("tree", []):
            file_path = str(item.get("path") or "")
            if item.get("type") != "blob" or (prefix and not file_path.startswith(prefix)):
                continue
            if files_considered >= _FALLBACK_FILE_LIMIT:
                budget_exhausted = True
                break
            files_considered += 1
            size = item.get("size")
            if isinstance(size, int) and size > _FALLBACK_FILE_BYTE_LIMIT:
                skipped_large += 1
                continue
            if isinstance(size, int) and bytes_scanned + size > _FALLBACK_BYTE_LIMIT:
                budget_exhausted = True
                break
            try:
                content = self._read_text(repository, file_path, head_sha)
            except ExternalExecutionError as exc:
                if "not UTF-8 text" in str(exc):
                    skipped_binary += 1
                else:
                    fetch_errors.append({"path": file_path, "error": str(exc)[:500]})
                continue
            files_scanned += 1
            bytes_scanned += len(content.encode("utf-8"))
            results.extend(self._literal_matches(file_path, content, query, limit=min(2, max_results - len(results))))
            if len(results) >= max_results:
                result_limit_reached = True
                break
        truncated = (
            bool(value.get("truncated"))
            or budget_exhausted
            or result_limit_reached
            or bool(fetch_errors)
            or skipped_large > 0
        )
        result = {
            "query": query,
            "results": results,
            "truncated": truncated,
            "complete": not truncated,
            "total_count": len(results),
            "candidates": files_scanned,
            "files_checked": files_scanned,
            "fetch_errors": fetch_errors,
            "backend": "github_tree_scan",
            "ref": head_sha,
            "native_incomplete": native_incomplete,
            "fallback": {
                "files_scanned": files_scanned,
                "files_considered": files_considered,
                "bytes_scanned": bytes_scanned,
                "skipped_binary": skipped_binary,
                "skipped_large": skipped_large,
                "budget_exhausted": budget_exhausted,
            },
        }
        if native_error:
            result["native_error"] = native_error[:500]
        return result

    @staticmethod
    def _literal_matches(path: str, content: str, query: str, *, limit: int) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        needle = query.casefold()
        results = []
        for line_number, line in enumerate(content.splitlines(), 1):
            if needle not in line.casefold():
                continue
            results.append({"path": path, "line": line_number, "snippet": line[:500]})
            if len(results) >= limit:
                break
        return results

    def _read_text(self, repository: str, path: str, ref: str) -> str:
        safe = safe_repository_path(path)
        cache_key = (repository, ref, safe)
        cacheable = bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", ref))
        if cacheable and cache_key in self._text_cache:
            return self._text_cache[cache_key]
        encoded_path = urllib.parse.quote(safe, safe="/")
        query = urllib.parse.urlencode({"ref": ref})
        value = self._request("GET", f"/repos/{repository}/contents/{encoded_path}?{query}")
        if value.get("type") != "file" or value.get("encoding") != "base64":
            raise ExternalExecutionError(f"GitHub did not return base64 file content for {safe}")
        try:
            content = base64.b64decode(str(value.get("content", "")), validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ExternalExecutionError(f"file is not UTF-8 text: {safe}") from exc
        if cacheable:
            self._text_cache[cache_key] = content
        return content

    def _default_head_sha(self, repository: str) -> str:
        branch = self._default_branch(repository)
        value = self._request(
            "GET",
            f"/repos/{repository}/commits/{urllib.parse.quote(branch, safe='')}",
        )
        sha = str(value.get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise ExternalExecutionError(f"GitHub returned no commit SHA for {repository}:{branch}")
        return sha

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
        content = self._read_text(repository, safe, actual_ref)
        return {"path": safe, **select_file_lines(content, start_line=start_line, limit=limit)}

    def read_files(
        self,
        repository: str,
        requests: list[dict[str, Any]],
        ref: str | None = None,
    ) -> dict[str, Any]:
        parsed = parse_file_read_requests(requests)
        files: list[dict[str, Any]] = []
        for request in parsed:
            path = request["path"]
            try:
                files.append(
                    self.read_file(
                        repository,
                        path,
                        start_line=request["start_line"],
                        limit=request["limit"],
                        ref=ref,
                    )
                )
            except ExternalExecutionError as exc:
                raise ExternalExecutionError(f"failed to read {path}: {exc}") from exc
        return {"files": files}

    def find_symbol(self, repository: str, symbol: str, max_results: int = 20) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_$][\w$.:<>-]*", symbol):
            raise ValidationError("symbol must be an identifier-like value")
        repository = self._repository(repository)
        search = self.search_code(repository, symbol, max_results=max_results)
        pattern = re.compile(
            rf"^\s*(?:(?:async\s+)?def|class|function|interface|type|const|let|var|"
            rf"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn|func)\s+{re.escape(symbol)}\b"
        )
        results: list[dict[str, Any]] = []
        for path in dict.fromkeys(str(item.get("path") or "") for item in search["results"]):
            if not path:
                continue
            content = self._read_text(repository, path, str(search["ref"]))
            for line_number, line in enumerate(content.splitlines(), 1):
                if not pattern.search(line):
                    continue
                results.append({"path": path, "line": line_number, "snippet": line[:500]})
                if len(results) >= min(max_results, 30):
                    break
            if len(results) >= min(max_results, 30):
                break
        return {
            "symbol": symbol,
            "results": results,
            "truncated": search["truncated"],
            "complete": search["complete"],
            "backend": search["backend"],
            "ref": search["ref"],
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

    def list_milestones(self, repository: str, state: str = "open", limit: int = 100) -> dict[str, Any]:
        repository = self._repository(repository)
        if state not in {"open", "closed", "all"}:
            raise ValidationError("milestone state must be open, closed, or all")
        query = urllib.parse.urlencode({"state": state, "per_page": max(1, min(limit, 100))})
        values = self._request("GET", f"/repos/{repository}/milestones?{query}")
        return {"milestones": values}

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
        reviews = [normalize_review(item) for item in values if isinstance(item, dict)]
        return {"pr_number": pr_number, "reviews": reviews}

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
            try:
                log = self._request_bytes("GET", f"/repos/{repository}/actions/jobs/{int(job['id'])}/logs")
            except ExternalExecutionError:
                bounded.append({**job, "log": "", "log_truncated": False, "log_unavailable": True})
                continue
            text = self._decode_log(log)
            bounded.append({**job, "log": text[:limit], "log_truncated": len(text) > limit})
        return {"run_id": run_id, "jobs": bounded}

    def post_comment(self, repository: str, issue_number: int, body: str) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if not body.strip():
            raise ValidationError("comment body cannot be empty")
        return self._request("POST", f"/repos/{repository}/issues/{issue_number}/comments", {"body": body})

    def create_issue(
        self,
        repository: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone_number: int | None = None,
    ) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if not title.strip():
            raise ValidationError("issue title cannot be empty")
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels is not None:
            payload["labels"] = labels
        if assignees is not None:
            payload["assignees"] = assignees
        if milestone_number is not None:
            payload["milestone"] = milestone_number
        return self._request("POST", f"/repos/{repository}/issues", payload)

    def update_issue(
        self,
        repository: str,
        issue_number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone_number: int | None = None,
        clear_milestone: bool = False,
    ) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if milestone_number is not None and clear_milestone:
            raise ValidationError("milestone_number and clear_milestone cannot be used together")
        payload: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise ValidationError("issue title cannot be empty")
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            if state not in {"open", "closed"}:
                raise ValidationError("issue state must be open or closed")
            payload["state"] = state
        if labels is not None:
            payload["labels"] = labels
        if assignees is not None:
            payload["assignees"] = assignees
        if milestone_number is not None:
            payload["milestone"] = milestone_number
        elif clear_milestone:
            payload["milestone"] = None
        return self._request("PATCH", f"/repos/{repository}/issues/{issue_number}", payload)

    def set_issue_lock(
        self,
        repository: str,
        issue_number: int,
        locked: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        path = f"/repos/{repository}/issues/{issue_number}/lock"
        if locked:
            if reason is not None and reason not in {"off-topic", "too heated", "resolved", "spam"}:
                raise ValidationError("issue lock reason must be off-topic, too heated, resolved, or spam")
            self._request("PUT", path, {"lock_reason": reason} if reason else {})
        else:
            self._request("DELETE", path)
        return {"number": issue_number, "locked": locked, "active_lock_reason": reason if locked else None}

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

    def commit(
        self,
        repository: str,
        branch: str,
        files: dict[str, str],
        deleted_files: list[str],
        message: str,
    ) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        self._validate_branch(branch)
        if (not files and not deleted_files) or not message.strip():
            raise ValidationError("commit requires file changes and a message")
        ref = self._request("GET", f"/repos/{repository}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        parent_sha = ref["object"]["sha"]
        parent = self._request("GET", f"/repos/{repository}/git/commits/{parent_sha}")
        base_tree_sha = str(parent["tree"]["sha"])
        existing_entries = self._tree_entries(repository, base_tree_sha)
        tree_entries = []
        safe_files = {safe_repository_path(path): content for path, content in files.items()}
        safe_deletions = {safe_repository_path(path) for path in deleted_files}
        if set(safe_files) & safe_deletions:
            raise ValidationError("commit cannot write and delete the same file")
        for safe, content in safe_files.items():
            blob = self._request("POST", f"/repos/{repository}/git/blobs", {"content": content, "encoding": "utf-8"})
            current = existing_entries.get(safe) or {}
            tree_entries.append(
                {
                    "path": safe,
                    "mode": str(current.get("mode") or "100644"),
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        tree_entries.extend(
            {
                "path": path,
                "mode": str((existing_entries.get(path) or {}).get("mode") or "100644"),
                "type": "blob",
                "sha": None,
            }
            for path in sorted(safe_deletions)
        )
        tree = self._request(
            "POST",
            f"/repos/{repository}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
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
        return {
            "commit": commit["sha"],
            "branch": branch,
            "files": sorted({*safe_files, *safe_deletions}),
            "deleted_files": sorted(safe_deletions),
        }

    def commit_to_default_branch(
        self,
        repository: str,
        expected_head_sha: str,
        files: dict[str, str],
        deleted_files: list[str],
        message: str,
    ) -> dict[str, Any]:
        self._require_token()
        repository = self._repository(repository)
        if (not files and not deleted_files) or not message.strip():
            raise ValidationError("default-branch commit requires file changes and a message")
        branch = self._default_branch(repository)
        encoded_branch = urllib.parse.quote(branch, safe="")
        ref = self._request("GET", f"/repos/{repository}/git/ref/heads/{encoded_branch}")
        actual_head_sha = str((ref.get("object") or {}).get("sha") or "")
        if not expected_head_sha.strip() or actual_head_sha != expected_head_sha.strip():
            raise GitHubAPIError(
                "default branch changed after the candidate was prepared",
                status_code=409,
                request_sent=False,
            )
        parent = self._request("GET", f"/repos/{repository}/git/commits/{actual_head_sha}")
        base_tree_sha = str(parent["tree"]["sha"])
        existing_entries = self._tree_entries(repository, base_tree_sha)
        safe_files = {safe_repository_path(path): content for path, content in files.items()}
        safe_deletions = {safe_repository_path(path) for path in deleted_files}
        if set(safe_files) & safe_deletions:
            raise ValidationError("default-branch commit cannot write and delete the same file")
        tree_entries = []
        for path, content in safe_files.items():
            blob = self._request(
                "POST",
                f"/repos/{repository}/git/blobs",
                {"content": content, "encoding": "utf-8"},
            )
            current = existing_entries.get(path) or {}
            tree_entries.append(
                {
                    "path": path,
                    "mode": str(current.get("mode") or "100644"),
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        tree_entries.extend(
            {
                "path": path,
                "mode": str((existing_entries.get(path) or {}).get("mode") or "100644"),
                "type": "blob",
                "sha": None,
            }
            for path in sorted(safe_deletions)
        )
        tree = self._request(
            "POST",
            f"/repos/{repository}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
        )
        commit = self._request(
            "POST",
            f"/repos/{repository}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [actual_head_sha]},
        )
        self._request(
            "PATCH",
            f"/repos/{repository}/git/refs/heads/{encoded_branch}",
            {"sha": commit["sha"], "force": False},
        )
        self._search_cache.clear()
        self._text_cache.clear()
        return {
            "repository": repository,
            "branch": branch,
            "commit": commit["sha"],
            "files": sorted({*safe_files, *safe_deletions}),
            "deleted_files": sorted(safe_deletions),
        }

    def _tree_entries(self, repository: str, tree_sha: str) -> dict[str, dict[str, Any]]:
        value = self._request(
            "GET",
            f"/repos/{repository}/git/trees/{urllib.parse.quote(tree_sha, safe='')}?recursive=1",
        )
        if value.get("truncated"):
            raise ExternalExecutionError("repository tree is too large to preserve file modes safely")
        return {
            str(item["path"]): dict(item)
            for item in value.get("tree", [])
            if item.get("type") == "blob" and item.get("path")
        }

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
        result = self._request(
            "POST",
            f"/repos/{repository}/pulls/{pr_number}/reviews",
            {"event": event, "body": body},
        )
        return normalize_review(result)

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
            raise ExternalExecutionError(f"GitHub returned invalid JSON for {method} {path}") from exc

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
            if exc.code == 404:
                raise ResourceNotFoundError(f"GitHub resource not found: {method} {path}") from exc
            retry_header = exc.headers.get("Retry-After") if exc.headers is not None else None
            retry_after = float(retry_header) if retry_header and retry_header.isdecimal() else None
            reason = _github_error_reason(details)
            raise GitHubAPIError(
                f"GitHub API {method} {path} failed ({exc.code}): {details}",
                status_code=exc.code,
                retry_after=retry_after,
                user_message=f"GitHub 拒绝了该操作（HTTP {exc.code}）：{reason}",
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            raise GitHubTransportError(
                f"GitHub API connection failed: {reason}",
                timed_out=timed_out,
            ) from exc

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
            raise GitHubAPIError(
                "GitHub write requires github_token in config.json",
                status_code=401,
                request_sent=False,
            )

    @staticmethod
    def _repository(repository: str) -> str:
        return str(RepositoryRef.parse(repository))

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch) or ".." in branch or branch.endswith("/"):
            raise ValidationError(f"invalid branch name: {branch!r}")


def _github_error_reason(details: str) -> str:
    """Extract the actionable reason while keeping raw response details in debug traces."""

    try:
        payload = json.loads(details)
    except json.JSONDecodeError:
        return details.strip() or "GitHub 未提供具体原因"
    if not isinstance(payload, dict):
        return details.strip() or "GitHub 未提供具体原因"
    errors = payload.get("errors")
    if isinstance(errors, list):
        reasons = []
        for item in errors:
            if isinstance(item, str) and item.strip():
                reasons.append(item.strip())
            elif isinstance(item, dict):
                message = str(item.get("message") or item.get("code") or "").strip()
                if message:
                    reasons.append(message)
        if reasons:
            return "; ".join(reasons)
    message = str(payload.get("message") or "").strip()
    return message or "GitHub 未提供具体原因"
