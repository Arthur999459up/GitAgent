"""供自动化测试使用的确定性 MCP 测试替身。"""

from __future__ import annotations

import ast
import json
import re
from copy import deepcopy
from pathlib import PurePosixPath
from threading import Lock
from typing import Any

import tomllib

from ..core.errors import ResourceNotFoundError, ToolExecutionError, ValidationError
from ..core.models import AccessLevel
from .base import parse_file_read_requests, safe_repository_path, select_file_lines
from .registry import tool_spec
from .server import MCPServer


class InMemoryMCPServer(MCPServer):
    """由显式测试数据驱动的远程仓库语义，不执行 clone。"""

    def __init__(self, repositories: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.repositories: dict[str, dict[str, Any]] = deepcopy(repositories or {})
        self._lock = Lock()
        self._register_repository_tools()
        self._register_github_tools()
        self._register_verification_tools()

    def _repo(self, repository: str) -> dict[str, Any]:
        try:
            repo = self.repositories[repository]
        except KeyError as exc:
            raise ToolExecutionError(f"repository not found: {repository}") from exc
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

    def _register_repository_tools(self) -> None:
        read = AccessLevel.READ
        self.register(
            tool_spec("repository.get_repo_tree", read, "List a bounded remote repository tree.", self.get_repo_tree)
        )
        self.register(tool_spec("repository.search_code", read, "Search remote file content.", self.search_code))
        self.register(tool_spec("repository.read_file", read, "Read a bounded line range from one file.", self.read_file))
        self.register(
            tool_spec(
                "repository.read_files",
                read,
                "Read targeted file ranges. Each request contains path and optional start_line/limit.",
                self.read_files,
            )
        )
        self.register(tool_spec("repository.find_symbol", read, "Find symbol definitions.", self.find_symbol))
        self.register(
            tool_spec("repository.find_references", read, "Find textual symbol references.", self.find_references)
        )
        self.register(tool_spec("repository.get_pr_diff", read, "Fetch a pull-request diff.", self.get_pr_diff))
        self.register(
            tool_spec(
                "repository.get_changed_files", read, "Fetch changed paths for a pull request.", self.get_changed_files
            )
        )
        self.register(
            tool_spec("repository.get_file_history", read, "Fetch bounded file history.", self.get_file_history)
        )

    def _register_github_tools(self) -> None:
        read = AccessLevel.READ
        write = AccessLevel.WRITE
        destructive = AccessLevel.DESTRUCTIVE
        self.register(tool_spec("github.get_issue", read, "Fetch one issue.", self.get_issue))
        self.register(tool_spec("github.list_issues", read, "List and filter bounded issue metadata.", self.list_issues))
        self.register(
            tool_spec("github.get_issue_comments", read, "Fetch bounded issue comments.", self.get_issue_comments)
        )
        self.register(
            tool_spec("github.list_milestones", read, "List milestones so an Issue can reference one by number.", self.list_milestones)
        )
        self.register(tool_spec("github.get_pr", read, "Fetch one pull request.", self.get_pr))
        self.register(
            tool_spec("github.list_pull_requests", read, "List and filter pull requests.", self.list_pull_requests)
        )
        self.register(tool_spec("github.get_pr_comments", read, "Fetch pull-request comments.", self.get_pr_comments))
        self.register(tool_spec("github.get_pr_reviews", read, "Fetch pull-request reviews.", self.get_pr_reviews))
        self.register(tool_spec("github.get_workflow_runs", read, "Fetch workflow-run metadata.", self.get_workflow_runs))
        self.register(tool_spec("github.get_job_logs", read, "Fetch a bounded failed-job log.", self.get_job_logs))
        self.register(tool_spec("github.post_comment", write, "Post an issue or PR comment.", self.post_comment))
        self.register(
            tool_spec(
                "github.create_issue",
                write,
                "Create an issue, optionally with labels, assignees, and a milestone number.",
                self.create_issue,
            )
        )
        self.register(
            tool_spec(
                "github.update_issue",
                write,
                "Update issue fields. Labels and assignees replace their complete current lists.",
                self.update_issue,
            )
        )
        self.register(
            tool_spec(
                "github.set_issue_lock",
                write,
                "Lock or unlock an issue discussion, with an optional GitHub lock reason.",
                self.set_issue_lock,
            )
        )
        self.register(tool_spec("github.update_pr", write, "Update pull-request state.", self.update_pr))
        self.register(tool_spec("github.create_branch", write, "Create a branch.", self.create_branch))
        self.register(tool_spec("github.commit", write, "Commit exact proposed file contents.", self.commit))
        self.register(tool_spec("github.push", write, "Publish a prepared branch.", self.push))
        self.register(tool_spec("github.create_draft_pr", write, "Create a draft pull request.", self.create_draft_pr))
        self.register(tool_spec("github.post_review", write, "Publish a pull-request review.", self.post_review))
        self.register(tool_spec("github.merge", destructive, "Merge a pull request.", self.merge))

    def _register_verification_tools(self) -> None:
        read = AccessLevel.READ
        self.register(tool_spec("verification.run_lint", read, "Run bounded, non-runtime lint checks.", self.run_lint))
        self.register(
            tool_spec(
                "verification.run_static_check",
                read,
                "Parse changed files and run static safety checks.",
                self.run_static_check,
            )
        )

    # Repository namespace -------------------------------------------------

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
            raise ToolExecutionError(f"file not found: {safe}") from exc
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

    def get_pr(self, repository: str, pr_number: int) -> dict[str, Any] | None:
        try:
            return deepcopy(self._get_numbered(self._repo(repository)["prs"], pr_number, "pull request"))
        except ResourceNotFoundError:
            return None

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
        return deepcopy(InMemoryMCPServer._get_numbered(repo["milestones"], milestone_number, "milestone"))

    def update_pr(self, repository: str, pr_number: int, state: str | None = None) -> dict[str, Any]:
        with self._lock:
            repo = self._repo(repository)
            pull_request = self._get_numbered(repo["prs"], pr_number, "pull request")
            if state is not None:
                if state not in {"open", "closed"}:
                    raise ValidationError("pull-request state must be open or closed")
                pull_request["state"] = state
        return deepcopy(pull_request)

    def create_branch(self, repository: str, base: str, branch: str) -> dict[str, Any]:
        self._validate_branch(branch)
        with self._lock:
            repo = self._repo(repository)
            if base not in repo["branches"]:
                raise ToolExecutionError(f"base branch not found: {base}")
            if branch in repo["branches"]:
                raise ToolExecutionError(f"branch already exists: {branch}")
            repo["branches"][branch] = {"base": base, "commits": [], "pushed": False}
        return {"repository": repository, "branch": branch, "base": base}

    def commit(self, repository: str, branch: str, files: dict[str, str], message: str) -> dict[str, Any]:
        if not files or not message.strip():
            raise ValidationError("commit requires exact file contents and a message")
        safe_files = {safe_repository_path(path): content for path, content in files.items()}
        with self._lock:
            repo = self._repo(repository)
            if branch not in repo["branches"]:
                raise ToolExecutionError(f"branch not found: {branch}")
            commit_id = f"commit-{len(repo['branches'][branch].setdefault('commits', [])) + 1}"
            repo["branches"][branch]["commits"].append(
                {"id": commit_id, "message": message, "files": deepcopy(safe_files)}
            )
            repo["files"].update(safe_files)
        return {"commit": commit_id, "branch": branch, "files": sorted(safe_files)}

    def push(self, repository: str, branch: str) -> dict[str, Any]:
        with self._lock:
            repo = self._repo(repository)
            if branch not in repo["branches"]:
                raise ToolExecutionError(f"branch not found: {branch}")
            if not repo["branches"][branch].get("commits"):
                raise ToolExecutionError("cannot push a branch without a candidate commit")
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
                raise ToolExecutionError("head branch must exist and be pushed")
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
            raise ToolExecutionError("only an open Pull Request can be merged")
        if bool(pull_request.get("draft")):
            raise ToolExecutionError("a draft Pull Request cannot be merged")
        head = pull_request.get("head") or {}
        actual_sha = str(head.get("sha", "")) if isinstance(head, dict) else ""
        if not expected_head_sha or expected_head_sha != actual_sha:
            raise ToolExecutionError("pull request head changed after review")
        pull_request["state"] = "closed"
        pull_request["merged"] = True
        return {
            "repository": repository,
            "pr_number": pr_number,
            "head_sha": actual_sha,
            "merged": True,
        }

    # Verification namespace ---------------------------------------------

    def run_lint(self, files: dict[str, str]) -> dict[str, Any]:
        errors: list[str] = []
        for path, content in files.items():
            safe = safe_repository_path(path)
            for number, line in enumerate(content.splitlines(), 1):
                if line.rstrip() != line:
                    errors.append(f"{safe}:{number}: trailing whitespace")
                if "\t" in line and safe.endswith(".py"):
                    errors.append(f"{safe}:{number}: tab indentation in Python")
                if len(line) > 160:
                    errors.append(f"{safe}:{number}: line exceeds 160 characters")
        return {"passed": not errors, "errors": errors, "files": sorted(files)}

    def run_static_check(self, files: dict[str, str]) -> dict[str, Any]:
        errors: list[str] = []
        skipped: list[str] = []
        for path, content in files.items():
            safe = safe_repository_path(path)
            try:
                if safe.endswith(".py"):
                    ast.parse(content, filename=safe)
                elif safe.endswith(".json"):
                    json.loads(content)
                elif safe.endswith(".toml"):
                    tomllib.loads(content)
                elif safe.endswith((".yaml", ".yml")):
                    skipped.append(f"{safe}: YAML parser is not installed")
            except (SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
                errors.append(f"{safe}: {exc}")
            if re.search(r"^(?:<{7}|={7}|>{7})", content, re.MULTILINE):
                errors.append(f"{safe}: unresolved merge-conflict marker")
        skipped.append("type check: no repository-specific type-checker configuration was provided")
        return {"passed": not errors, "errors": errors, "skipped": skipped, "files": sorted(files)}

    # Helpers -------------------------------------------------------------

    @staticmethod
    def _get_numbered(items: dict[Any, Any], number: int, label: str) -> dict[str, Any]:
        item = items.get(number, items.get(str(number)))
        if item is None:
            raise ResourceNotFoundError(f"{label} not found: {number}")
        return item

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
