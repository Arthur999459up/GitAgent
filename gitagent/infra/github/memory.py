"""Deterministic in-memory fake for the pure GitHub API adapter."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import PurePosixPath
from threading import Lock
from typing import Any

from gitagent.domain.errors import ResourceNotFoundError, ValidationError
from gitagent.harness.file_access import (
    parse_file_read_requests,
    safe_repository_path,
    select_file_lines,
)

from .errors import GitHubAPIError


class InMemoryGitHubClient:
    """由显式测试数据驱动的远程仓库语义，不执行 clone。"""

    def __init__(self, repositories: dict[str, dict[str, Any]] | None = None) -> None:
        self.repositories: dict[str, dict[str, Any]] = deepcopy(repositories or {})
        self._lock = Lock()

    def _repo(self, repository: str) -> dict[str, Any]:
        try:
            repo = self.repositories[repository]
        except KeyError as exc:
            raise ResourceNotFoundError(f"repository not found: {repository}") from exc
        repo.setdefault("files", {})
        repo.setdefault("issues", {})
        repo.setdefault("milestones", {})
        repo.setdefault("prs", {})
        repo.setdefault("workflow_runs", {})
        repo.setdefault("comments", [])
        repo.setdefault("reviews", [])
        repo.setdefault("branches", {"main": {"pushed": True}})
        repo.setdefault("draft_prs", [])
        repo.setdefault("history", {})
        return repo

    # Repository namespace -------------------------------------------------

    def get_default_branch(self, repository: str) -> dict[str, Any]:
        repo = self._repo(repository)
        branch = str(repo.get("default_branch") or "main")
        state = repo["branches"].setdefault(branch, {"pushed": True, "commits": []})
        commit_sha = str(state.get("head_sha") or f"fixture-head-{len(state.get('commits', []))}")
        state["head_sha"] = commit_sha
        return {"repository": repository, "branch": branch, "commit_sha": commit_sha}

    def get_file_status(
        self,
        repository: str,
        paths: list[str],
        ref: str | None = None,
    ) -> dict[str, Any]:
        del ref
        if not paths or len(paths) > 20:
            raise ValidationError("file status requires between 1 and 20 paths")
        safe_paths = [safe_repository_path(path) for path in paths]
        if len(set(safe_paths)) != len(safe_paths):
            raise ValidationError("file status paths must be unique")
        files = self._repo(repository)["files"]
        return {
            "existing_files": sorted(path for path in safe_paths if path in files),
            "missing_files": sorted(path for path in safe_paths if path not in files),
        }

    def get_repo_tree(
        self,
        repository: str,
        path: str = "",
        depth: int = 2,
        max_entries: int = 300,
        ref: str | None = None,
    ) -> dict[str, Any]:
        repo = self._repo(repository)
        prefix = "" if not path else safe_repository_path(path).rstrip("/") + "/"
        depth = max(1, min(depth, 8))
        max_entries = max(1, min(max_entries, 500))
        paths = []
        omitted_by_depth = 0
        for file_path in sorted(repo["files"]):
            if not file_path.startswith(prefix):
                continue
            relative = file_path[len(prefix) :]
            if len(PurePosixPath(relative).parts) <= depth:
                paths.append(file_path)
            else:
                omitted_by_depth += 1
            if len(paths) >= max_entries:
                break
        return {
            "repository": repository,
            "path": path,
            "entries": paths,
            "truncated": len(paths) >= max_entries or omitted_by_depth > 0,
            "depth": depth,
            "omitted_by_depth": omitted_by_depth,
            "ref": ref or "fixture",
        }

    def search_code(
        self,
        repository: str,
        query: str,
        path: str = "",
        max_results: int = 20,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValidationError("search query cannot be empty")
        repo = self._repo(repository)
        prefix = "" if not path else safe_repository_path(path).rstrip("/") + "/"
        max_results = max(1, min(max_results, 50))
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        truncated = False
        for file_path, content in sorted(repo["files"].items()):
            if prefix and not file_path.startswith(prefix):
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if needle in line.casefold():
                    results.append({"path": file_path, "line": line_number, "snippet": line[:500]})
                    if len(results) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        return {
            "query": query,
            "results": results,
            "truncated": truncated,
            "complete": not truncated,
            "total_count": len(results),
            "backend": "fixture_scan",
            "ref": "fixture",
        }

    def read_file(
        self,
        repository: str,
        path: str,
        start_line: int = 1,
        limit: int = 200,
        ref: str | None = None,
    ) -> dict[str, Any]:
        del ref  # fixture server has one current remote snapshot
        repo = self._repo(repository)
        safe = safe_repository_path(path)
        try:
            content = repo["files"][safe]
        except KeyError as exc:
            raise ResourceNotFoundError(f"file not found: {safe}") from exc
        return {"path": safe, **select_file_lines(content, start_line=start_line, limit=limit)}

    def read_files(
        self,
        repository: str,
        requests: list[dict[str, Any]],
        ref: str | None = None,
    ) -> dict[str, Any]:
        parsed = parse_file_read_requests(requests)
        return {
            "files": [
                self.read_file(
                    repository,
                    request["path"],
                    start_line=request["start_line"],
                    limit=request["limit"],
                    ref=ref,
                )
                for request in parsed
            ]
        }

    def find_symbol(self, repository: str, symbol: str, max_results: int = 20) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_$][\w$.:<>-]*", symbol):
            raise ValidationError("symbol must be an identifier-like value")
        repo = self._repo(repository)
        pattern = re.compile(
            rf"^\s*(?:(?:async\s+)?def|class|function|interface|type|const|let|var|"
            rf"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn|func)\s+{re.escape(symbol)}\b"
        )
        results = []
        for file_path, content in sorted(repo["files"].items()):
            for line_number, line in enumerate(content.splitlines(), 1):
                if pattern.search(line):
                    results.append({"path": file_path, "line": line_number, "snippet": line[:500]})
                    if len(results) >= min(max_results, 50):
                        return {
                            "symbol": symbol,
                            "results": results,
                            "truncated": True,
                            "complete": False,
                            "backend": "fixture_symbol_scan",
                            "ref": "fixture",
                        }
        return {
            "symbol": symbol,
            "results": results,
            "truncated": False,
            "complete": True,
            "backend": "fixture_symbol_scan",
            "ref": "fixture",
        }

    def find_references(self, repository: str, symbol: str, max_results: int = 50) -> dict[str, Any]:
        return self.search_code(repository, symbol, max_results=max_results)

    def get_pr_diff(self, repository: str, pr_number: int, max_chars: int = 160_000) -> dict[str, Any]:
        pr = self._get_numbered(self._repo(repository)["prs"], pr_number, "pull request")
        diff = str(pr.get("diff", ""))
        limit = max(1, min(max_chars, 300_000))
        return {"pr_number": pr_number, "diff": diff[:limit], "truncated": len(diff) > limit}

    def get_changed_files(self, repository: str, pr_number: int) -> dict[str, Any]:
        pr = self._get_numbered(self._repo(repository)["prs"], pr_number, "pull request")
        files = list(pr.get("changed_files") or self._paths_from_diff(str(pr.get("diff", ""))))
        return {"pr_number": pr_number, "files": files[:300], "truncated": len(files) > 300}

    def get_file_history(self, repository: str, path: str, limit: int = 20) -> dict[str, Any]:
        history = self._repo(repository)["history"].get(safe_repository_path(path), [])
        return {"path": path, "commits": deepcopy(history[: max(1, min(limit, 50))])}

    # GitHub namespace -----------------------------------------------------

    def get_issue(self, repository: str, issue_number: int) -> dict[str, Any]:
        return deepcopy(self._get_numbered(self._repo(repository)["issues"], issue_number, "issue"))

    def list_issues(
        self,
        repository: str,
        state: str = "open",
        labels: list[str] | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        if state not in {"open", "closed", "all"}:
            raise ValidationError("issue state must be open, closed, or all")
        requested_labels = {label.casefold() for label in labels or []}
        issues = []
        for issue in self._repo(repository)["issues"].values():
            issue_state = str(issue.get("state", "open")).casefold()
            issue_labels = {str(label).casefold() for label in issue.get("labels", [])}
            if state != "all" and issue_state != state:
                continue
            if requested_labels and not requested_labels.issubset(issue_labels):
                continue
            issues.append(issue)
        issues = issues[: max(1, min(limit, 100))]
        return {"issues": deepcopy(issues)}

    def get_issue_comments(self, repository: str, issue_number: int, limit: int = 30) -> dict[str, Any]:
        repo = self._repo(repository)
        issue = self._get_numbered(repo["issues"], issue_number, "issue")
        comments = list(issue.get("comments", []))
        comments.extend(item for item in repo["comments"] if int(item.get("issue_number", -1)) == issue_number)
        return {"comments": deepcopy(comments[: max(1, min(limit, 100))])}

    def list_milestones(self, repository: str, state: str = "open", limit: int = 100) -> dict[str, Any]:
        if state not in {"open", "closed", "all"}:
            raise ValidationError("milestone state must be open, closed, or all")
        milestones = []
        for milestone in self._repo(repository)["milestones"].values():
            if state != "all" and str(milestone.get("state", "open")).casefold() != state:
                continue
            milestones.append(milestone)
        return {"milestones": deepcopy(milestones[: max(1, min(limit, 100))])}

    def get_pr(self, repository: str, pr_number: int) -> dict[str, Any]:
        return deepcopy(self._get_numbered(self._repo(repository)["prs"], pr_number, "pull request"))

    def list_pull_requests(
        self,
        repository: str,
        state: str = "open",
        base: str = "",
        head: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        if state not in {"open", "closed", "all"}:
            raise ValidationError("pull-request state must be open, closed, or all")
        pull_requests = []
        for pull_request in self._repo(repository)["prs"].values():
            pr_state = str(pull_request.get("state", "open")).casefold()
            pr_base = self._branch_name(pull_request.get("base"))
            pr_head = self._branch_name(pull_request.get("head"))
            if state != "all" and pr_state != state:
                continue
            if base and pr_base != base:
                continue
            if head and pr_head != head:
                continue
            pull_requests.append(pull_request)
        return {"pull_requests": deepcopy(pull_requests[: max(1, min(limit, 100))])}

    def get_pr_comments(self, repository: str, pr_number: int) -> dict[str, Any]:
        pr = self._get_numbered(self._repo(repository)["prs"], pr_number, "pull request")
        return {"comments": deepcopy(pr.get("comments", []))}

    @staticmethod
    def _branch_name(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("ref", ""))
        return str(value or "")

    def get_workflow_runs(
        self,
        repository: str,
        pr_number: int | None = None,
        workflow_run_id: int | None = None,
    ) -> dict[str, Any]:
        runs = list(self._repo(repository)["workflow_runs"].values())
        if workflow_run_id is not None:
            runs = [run for run in runs if int(run.get("id", -1)) == workflow_run_id]
        if pr_number is not None:
            runs = [run for run in runs if int(run.get("pr_number", -1)) == pr_number]
        return {"runs": deepcopy(runs[:50])}

    def get_job_logs(
        self, repository: str, run_id: int, job_id: int | None = None, max_chars: int = 80_000
    ) -> dict[str, Any]:
        run = self._get_numbered(self._repo(repository)["workflow_runs"], run_id, "workflow run")
        jobs = run.get("jobs", [])
        if job_id is not None:
            jobs = [job for job in jobs if int(job.get("id", -1)) == job_id]
        bounded = []
        limit = max(1, min(max_chars, 120_000))
        for job in jobs[:20]:
            copied = deepcopy(job)
            log = str(copied.get("log", ""))
            copied["log"] = log[:limit]
            copied["log_truncated"] = len(log) > limit
            bounded.append(copied)
        return {"run_id": run_id, "jobs": bounded}

    def post_comment(self, repository: str, issue_number: int, body: str) -> dict[str, Any]:
        if not body.strip():
            raise ValidationError("comment body cannot be empty")
        with self._lock:
            repo = self._repo(repository)
            self._get_numbered(repo["issues"], issue_number, "issue")
            comment = {"id": len(repo["comments"]) + 1, "issue_number": issue_number, "body": body}
            repo["comments"].append(comment)
        return deepcopy(comment)

    def create_issue(
        self,
        repository: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone_number: int | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValidationError("issue title cannot be empty")
        with self._lock:
            repo = self._repo(repository)
            existing_numbers = [int(number) for number in repo["issues"]]
            existing_numbers.extend(int(number) for number in repo["prs"])
            issue_number = max(existing_numbers, default=0) + 1
            issue = {
                "number": issue_number,
                "title": title,
                "body": body,
                "state": "open",
                "labels": list(labels or []),
                "assignees": list(assignees or []),
                "milestone": self._issue_milestone(repo, milestone_number),
                "locked": False,
                "active_lock_reason": None,
                "comments": [],
            }
            repo["issues"][issue_number] = issue
        return deepcopy(issue)

    def get_pr_reviews(self, repository: str, pr_number: int) -> dict[str, Any]:
        repo = self._repo(repository)
        self._get_numbered(repo["prs"], pr_number, "pull request")
        reviews = [item for item in repo["reviews"] if int(item.get("pr_number", -1)) == pr_number]
        return {"pr_number": pr_number, "reviews": deepcopy(reviews)}

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
        if milestone_number is not None and clear_milestone:
            raise ValidationError("milestone_number and clear_milestone cannot be used together")
        with self._lock:
            repo = self._repo(repository)
            issue = self._get_numbered(repo["issues"], issue_number, "issue")
            if title is not None:
                if not title.strip():
                    raise ValidationError("issue title cannot be empty")
                issue["title"] = title
            if body is not None:
                issue["body"] = body
            if state is not None:
                if state not in {"open", "closed"}:
                    raise ValidationError("issue state must be open or closed")
                issue["state"] = state
            if labels is not None:
                issue["labels"] = list(labels)
            if assignees is not None:
                issue["assignees"] = list(assignees)
            if milestone_number is not None:
                issue["milestone"] = self._issue_milestone(repo, milestone_number)
            elif clear_milestone:
                issue["milestone"] = None
        return deepcopy(issue)

    def set_issue_lock(
        self,
        repository: str,
        issue_number: int,
        locked: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if locked and reason is not None and reason not in {"off-topic", "too heated", "resolved", "spam"}:
            raise ValidationError("issue lock reason must be off-topic, too heated, resolved, or spam")
        with self._lock:
            issue = self._get_numbered(self._repo(repository)["issues"], issue_number, "issue")
            issue["locked"] = locked
            issue["active_lock_reason"] = reason if locked else None
        return {"number": issue_number, "locked": locked, "active_lock_reason": reason if locked else None}

    @staticmethod
    def _issue_milestone(repo: dict[str, Any], milestone_number: int | None) -> dict[str, Any] | None:
        if milestone_number is None:
            return None
        return deepcopy(InMemoryGitHubClient._get_numbered(repo["milestones"], milestone_number, "milestone"))

    def update_pr(self, repository: str, pr_number: int, state: str | None = None) -> dict[str, Any]:
        with self._lock:
            repo = self._repo(repository)
            pull_request = self._get_numbered(repo["prs"], pr_number, "pull request")
            if state is not None:
                if state not in {"open", "closed"}:
                    raise ValidationError("pull-request state must be open or closed")
                pull_request["state"] = state
        return deepcopy(pull_request)

    def create_branch(
        self,
        repository: str,
        base: str,
        branch: str,
        expected_head_sha: str,
    ) -> dict[str, Any]:
        self._validate_branch(branch)
        with self._lock:
            repo = self._repo(repository)
            if base not in repo["branches"]:
                raise ResourceNotFoundError(f"base branch not found: {base}")
            if branch in repo["branches"]:
                raise GitHubAPIError(f"branch already exists: {branch}", status_code=409, request_sent=False)
            base_state = repo["branches"][base]
            actual_head_sha = str(
                base_state.get("head_sha")
                or f"fixture-head-{len(base_state.get('commits', []))}"
            )
            if not expected_head_sha or expected_head_sha != actual_head_sha:
                raise GitHubAPIError(
                    "base branch changed after the candidate was prepared",
                    status_code=409,
                    request_sent=False,
                )
            repo["branches"][branch] = {
                "base": base,
                "head_sha": expected_head_sha,
                "commits": [],
                "pushed": False,
            }
        return {
            "repository": repository,
            "branch": branch,
            "base": base,
            "head_sha": expected_head_sha,
        }

    def commit(
        self,
        repository: str,
        branch: str,
        files: dict[str, str],
        deleted_files: list[str],
        message: str,
        expected_head_sha: str,
    ) -> dict[str, Any]:
        if (not files and not deleted_files) or not message.strip():
            raise ValidationError("commit requires file changes and a message")
        safe_files = {safe_repository_path(path): content for path, content in files.items()}
        safe_deletions = {safe_repository_path(path) for path in deleted_files}
        if set(safe_files) & safe_deletions:
            raise ValidationError("commit cannot write and delete the same file")
        with self._lock:
            repo = self._repo(repository)
            if branch not in repo["branches"]:
                raise ResourceNotFoundError(f"branch not found: {branch}")
            state = repo["branches"][branch]
            actual_head_sha = self._branch_head_sha(repo, branch)
            if not expected_head_sha or expected_head_sha != actual_head_sha:
                raise GitHubAPIError(
                    "branch changed after the candidate was prepared",
                    status_code=409,
                    request_sent=False,
                )
            missing = safe_deletions - set(repo["files"])
            if missing:
                raise ResourceNotFoundError(f"cannot delete missing file: {min(missing)}")
            commit_id = f"commit-{len(state.setdefault('commits', [])) + 1}"
            state["commits"].append(
                {
                    "id": commit_id,
                    "message": message,
                    "files": deepcopy(safe_files),
                    "deleted_files": sorted(safe_deletions),
                }
            )
            state["head_sha"] = commit_id
            for pull_request in repo["prs"].values():
                head = pull_request.get("head") or {}
                source = (head.get("repo") or {}) if isinstance(head, dict) else {}
                source_name = (
                    str(source.get("full_name") or "")
                    if isinstance(source, dict)
                    else ""
                )
                if (
                    isinstance(head, dict)
                    and str(head.get("ref") or "") == branch
                    and source_name in {"", repository}
                ):
                    head["sha"] = commit_id
            repo["files"].update(safe_files)
            for path in safe_deletions:
                del repo["files"][path]
        return {
            "commit": commit_id,
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
        if (not files and not deleted_files) or not message.strip():
            raise ValidationError("default-branch commit requires file changes and a message")
        safe_files = {safe_repository_path(path): content for path, content in files.items()}
        safe_deletions = {safe_repository_path(path) for path in deleted_files}
        if set(safe_files) & safe_deletions:
            raise ValidationError("default-branch commit cannot write and delete the same file")
        with self._lock:
            repo = self._repo(repository)
            branch = str(repo.get("default_branch") or "main")
            state = repo["branches"].setdefault(branch, {"pushed": True, "commits": []})
            actual_head_sha = str(state.get("head_sha") or f"fixture-head-{len(state.get('commits', []))}")
            if expected_head_sha != actual_head_sha:
                raise GitHubAPIError(
                    "default branch changed after the candidate was prepared",
                    status_code=409,
                    request_sent=False,
                )
            missing = safe_deletions - set(repo["files"])
            if missing:
                raise ResourceNotFoundError(f"cannot delete missing file: {min(missing)}")
            added_files = sorted(set(safe_files) - set(repo["files"]))
            modified_files = sorted(set(safe_files) & set(repo["files"]))
            commit_id = f"commit-{len(state.setdefault('commits', [])) + 1}"
            state["commits"].append(
                {
                    "id": commit_id,
                    "message": message,
                    "files": deepcopy(safe_files),
                    "deleted_files": sorted(safe_deletions),
                }
            )
            state["head_sha"] = commit_id
            repo["files"].update(safe_files)
            for path in safe_deletions:
                del repo["files"][path]
        return {
            "repository": repository,
            "branch": branch,
            "commit": commit_id,
            "added_files": added_files,
            "modified_files": modified_files,
            "deleted_files": sorted(safe_deletions),
            "files": sorted({*safe_files, *safe_deletions}),
        }

    def push(self, repository: str, branch: str) -> dict[str, Any]:
        with self._lock:
            repo = self._repo(repository)
            if branch not in repo["branches"]:
                raise ResourceNotFoundError(f"branch not found: {branch}")
            if not repo["branches"][branch].get("commits"):
                raise GitHubAPIError(
                    "cannot push a branch without a candidate commit",
                    status_code=409,
                    request_sent=False,
                )
            repo["branches"][branch]["pushed"] = True
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
        if draft is not True:
            raise ValidationError("GitAgent only creates draft pull requests")
        with self._lock:
            repo = self._repo(repository)
            if head not in repo["branches"] or not repo["branches"][head].get("pushed"):
                raise GitHubAPIError("head branch must exist and be pushed", status_code=409, request_sent=False)
            number = max([0, *[int(key) for key in repo["prs"]], *[int(pr["number"]) for pr in repo["draft_prs"]]]) + 1
            result = {"number": number, "title": title, "body": body, "base": base, "head": head, "draft": True}
            repo["draft_prs"].append(result)
        return deepcopy(result)

    def post_review(self, repository: str, pr_number: int, event: str, body: str) -> dict[str, Any]:
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValidationError("invalid review event")
        with self._lock:
            repo = self._repo(repository)
            self._get_numbered(repo["prs"], pr_number, "pull request")
            review = {"pr_number": pr_number, "event": event, "body": body}
            repo["reviews"].append(review)
        return deepcopy(review)

    def merge(self, repository: str, pr_number: int, expected_head_sha: str) -> dict[str, Any]:
        pull_request = self._get_numbered(self._repo(repository)["prs"], pr_number, "pull request")
        if str(pull_request.get("state", "open")).casefold() != "open":
            raise GitHubAPIError("only an open Pull Request can be merged", status_code=409, request_sent=False)
        if bool(pull_request.get("draft")):
            raise GitHubAPIError("a draft Pull Request cannot be merged", status_code=409, request_sent=False)
        head = pull_request.get("head") or {}
        actual_sha = str(head.get("sha", "")) if isinstance(head, dict) else ""
        if not expected_head_sha or expected_head_sha != actual_sha:
            raise GitHubAPIError("pull request head changed after review", status_code=409, request_sent=False)
        pull_request["state"] = "closed"
        pull_request["merged"] = True
        return {
            "repository": repository,
            "pr_number": pr_number,
            "head_sha": actual_sha,
            "merged": True,
        }

    # Helpers -------------------------------------------------------------

    @staticmethod
    def _get_numbered(items: dict[Any, Any], number: int, label: str) -> dict[str, Any]:
        item = items.get(number, items.get(str(number)))
        if item is None:
            raise ResourceNotFoundError(f"{label} not found: {number}")
        return item

    @staticmethod
    def _branch_head_sha(repo: dict[str, Any], branch: str) -> str:
        state = repo["branches"][branch]
        if state.get("head_sha"):
            return str(state["head_sha"])
        for pull_request in repo["prs"].values():
            head = pull_request.get("head") or {}
            if (
                isinstance(head, dict)
                and str(head.get("ref") or "") == branch
                and head.get("sha")
            ):
                return str(head["sha"])
        return f"fixture-head-{len(state.get('commits', []))}"

    @staticmethod
    def _paths_from_diff(diff: str) -> list[str]:
        paths = []
        for match in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE):
            if match.group(1) != "/dev/null":
                paths.append(match.group(1))
        return list(dict.fromkeys(paths))

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch) or ".." in branch or branch.endswith("/"):
            raise ValidationError(f"invalid branch name: {branch!r}")
