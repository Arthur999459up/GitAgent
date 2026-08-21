"""Shared in-memory repository fixtures and deterministic Session-routing test doubles."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from AGENT.GitAgent.gitagent.app.service import GitAgentService
from AGENT.GitAgent.gitagent.context import ContextBuilder
from AGENT.GitAgent.gitagent.core.models import RoutingContext
from AGENT.GitAgent.gitagent.mcp.memory import InMemoryMCPServer
from AGENT.GitAgent.gitagent.state import SessionManager, StateStore, build_account_key, build_repository_key


class StubMainReasoner:
    """Deterministic test double for Session routing, approval intent, and draft revision."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.prompts: list[str] = []

    def complete_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Any = None,
        tool_name: str = "respond",
        tools: Any = None,
    ) -> dict[str, Any]:
        del system, schema, tools
        self.prompts.append(prompt)
        if self.responses:
            return self.responses.pop(0)
        if tool_name == "classify_approval_intent":
            return self._approval(prompt)
        if tool_name == "route_session_turn":
            return self._main(prompt)
        raise AssertionError(f"unexpected structured test call: {tool_name}")

    def complete_text(self, *, system: str, prompt: str) -> str:
        if system.startswith("Return one concise repository search term"):
            if "format_name" in prompt:
                return "format_name"
            if "add" in prompt:
                return "add"
            return prompt.strip().split()[0] if prompt.strip() else "repository"
        if "src/formatting.py" in prompt:
            return "format_name is implemented in src/formatting.py."
        if prompt.startswith("User request:"):
            return "I checked the issue evidence and prepared this reply based on the available repository context."
        try:
            payload = json.loads(prompt)
        except json.JSONDecodeError:
            return "repository"
        current = str(payload.get("current_draft") or "")
        instruction = str(payload.get("instruction") or "")
        if "短" in instruction or "short" in instruction.casefold():
            first = current
            for mark in ("。", ".", "!", "！"):
                if mark in first:
                    first = first.split(mark, 1)[0]
            return (first.strip() or current[:80]).rstrip("。.!！") + "。"
        if "改成" in instruction:
            return instruction.split("改成", 1)[1].strip().strip("“”\"'")
        return current

    @staticmethod
    def _approval(prompt: str) -> dict[str, Any]:
        text = prompt.split("User turn:\n", 1)[1].split("\n\nOpen proposal context:", 1)[0].strip()
        lowered = text.casefold()
        if any(term in text for term in ("算了", "不要", "取消")) or "reject" in lowered or "cancel" in lowered:
            return {"action": "reject", "instruction": "", "message": ""}
        if any(term in text for term in ("可以", "同意", "批准", "就这么", "发布", "发出去")) or any(
            term in lowered for term in ("approve", "post it", "publish")
        ):
            return {"action": "approve", "instruction": "", "message": ""}
        if (
            any(term in text for term in ("为什么", "解释", "说明"))
            or ("草稿" in text and any(term in text for term in ("看", "显示", "展示")))
            or text.endswith(("?", "？"))
        ):
            return {"action": "question", "instruction": "", "message": ""}
        return {"action": "revise", "instruction": text, "message": ""}

    @staticmethod
    def _main(prompt: str) -> dict[str, Any]:
        payload = json.loads(prompt.split("\n", 1)[1])
        text = str(payload["user_input"])
        lowered = text.casefold()
        words = lowered.replace("?", " ").replace("!", " ").replace(".", " ").replace(",", " ").split()
        requested_reply = any(term in text for term in ("回复", "评论", "草稿")) or "reply" in words
        entity_type = None
        entity_id = None
        if "issue" in lowered or "问题单" in text:
            target = "issues"
            entity_type = "issue"
            entity_id = _number_after_marker(text, "#")
        elif "pull request" in lowered or "pr #" in lowered or "审查" in text:
            target = "pull_requests"
            entity_type = "pull_request"
            entity_id = _number_after_marker(text, "#")
        elif "workflow" in lowered or "诊断" in text or " ci" in f" {lowered}":
            target = "ci_diagnosis"
            entity_type = "workflow_run"
            entity_id = _number_after_marker(text, "#")
        elif any(term in words for term in ("fix", "implement", "refactor")) or any(
            term in text for term in ("修复", "实现", "重构")
        ):
            target = "code_change"
            entity_type = "repository"
        elif any(term in text for term in ("你好", "谢谢")) or lowered in {"hello", "hi", "thanks"}:
            return {
                "target_agent": "",
                "entity_type": "",
                "entity_id": "",
                "request": text,
                "message": "你好，我可以帮你处理这个仓库。",
                "clarify": False,
                "requested_fix": False,
                "requested_reply": False,
            }
        else:
            target = "repo_qa"
            entity_type = "repository"
        return {
            "target_agent": target,
            "entity_type": entity_type or "",
            "entity_id": entity_id or "",
            "request": text,
            "message": "",
            "clarify": False,
            "requested_fix": target == "ci_diagnosis" and ("fix" in lowered or "修" in text),
            "requested_reply": requested_reply,
        }


def _number_after_marker(text: str, marker: str) -> str | None:
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1].lstrip()
    digits = ""
    for character in tail:
        if character.isdigit():
            digits += character
        else:
            break
    return digits or None


def sample_repositories() -> dict[str, dict[str, Any]]:
    pr_diff = """diff --git a/src/math_utils.py b/src/math_utils.py
--- a/src/math_utils.py
+++ b/src/math_utils.py
@@ -1,5 +1,8 @@
 def add(a, b):
-    return a + b
+    expression = f"{a}+{b}"
+    return eval(expression)
"""
    return {
        "sample/widgets": {
            "files": {
                "README.md": "# Widgets\n\nA small sample repository.\n",
                "src/math_utils.py": "def add(a: int, b: int) -> int:\n    return a - b\n",
                "src/formatting.py": "def format_name(name: str) -> str:\n    return name.strip().title()\n",
                "tests/test_math_utils.py": "from src.math_utils import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                ".github/workflows/ci.yml": "name: CI\non: [push, pull_request]\n",
            },
            "issues": {
                1: {
                    "number": 1,
                    "title": "How do I format a widget name?",
                    "body": "How do I use format_name for a display label?",
                    "labels": ["question"],
                    "comments": [{"id": 11, "body": "I am using Python 3.12."}],
                },
                2: {
                    "number": 2,
                    "title": "add returns the wrong result",
                    "body": "Reproduction: add(2, 3). Expected 5, actual -1 under Python 3.12.",
                    "labels": ["bug"],
                    "classification": "code_change",
                    "change_request": {
                        "description": "Correct add so it returns the sum of both arguments",
                        "target_files": ["src/math_utils.py"],
                        "replacements": [
                            {"path": "src/math_utils.py", "old": "    return a - b\n", "new": "    return a + b\n"}
                        ],
                    },
                },
                3: {"number": 3, "title": "Crash", "body": "It crashes", "labels": ["bug"], "comments": []},
            },
            "prs": {
                7: {
                    "number": 7,
                    "title": "Compute addition from an expression",
                    "body": "Simplify arithmetic handling.",
                    "state": "open",
                    "user": {"login": "alice"},
                    "head": {"ref": "expression-add", "sha": "abc123"},
                    "base": {"ref": "main"},
                    "draft": False,
                    "updated_at": "2026-08-10T00:00:00Z",
                    "html_url": "https://github.com/sample/widgets/pull/7",
                    "diff": pr_diff,
                    "changed_files": ["src/math_utils.py"],
                    "comments": [],
                },
                3: {
                    "number": 3,
                    "title": "Docs cleanup",
                    "body": "Tidy documentation.",
                    "state": "open",
                    "user": {"login": "bob"},
                    "head": {"ref": "docs", "sha": "def456"},
                    "base": {"ref": "main"},
                    "draft": False,
                    "updated_at": "2026-08-11T00:00:00Z",
                    "html_url": "https://github.com/sample/widgets/pull/3",
                    "diff": "diff --git a/README.md b/README.md\n",
                    "changed_files": ["README.md"],
                    "comments": [],
                },
            },
            "workflow_runs": {
                42: {
                    "id": 42,
                    "pr_number": 7,
                    "status": "completed",
                    "conclusion": "failure",
                    "jobs": [
                        {
                            "id": 4201,
                            "name": "static-check",
                            "conclusion": "failure",
                            "log": "Running static checks\nsrc/math_utils.py:2: error: Returning Any\nexit code 1\n",
                        }
                    ],
                }
            },
            "branches": {"main": {"pushed": True, "commits": []}},
        }
    }


def build_test_service(
    *,
    main_responses: list[dict[str, Any]] | None = None,
    main_reasoner: Any = None,
    agent_reasoner: Any = None,
    server: InMemoryMCPServer | None = None,
) -> GitAgentService:
    tempdir = tempfile.TemporaryDirectory()
    store = StateStore(Path(tempdir.name) / "state.db")
    sessions = SessionManager(store)
    account_key = build_account_key("https://api.github.com", 1)
    repository_key = build_repository_key("https://api.github.com", 1)
    session = sessions.create_session(account_key, repository_key, "sample/widgets")
    reasoner = main_reasoner or StubMainReasoner(main_responses)
    service = GitAgentService(
        server or InMemoryMCPServer(sample_repositories()),
        main_reasoner=reasoner,
        agent_reasoner=agent_reasoner,
        session_manager=sessions,
        session_scope=session.scope,
    )
    if agent_reasoner is None:
        service.repo_qa.reasoner = reasoner
    service._test_tempdir = tempdir
    service._test_store = store
    service._test_sessions = sessions
    service._test_context_builder = ContextBuilder(sessions)
    return service


def routing_context(service: GitAgentService, user_input: str) -> RoutingContext:
    return service._test_context_builder.build(
        service.session_scope,
        "sample/widgets",
        user_input,
        prompt_renderer=lambda context: service.main_agent.render_input_context(
            user_input, "sample/widgets", context
        ),
    )


def handle(service: GitAgentService, user_input: str):
    return service.handle(
        user_input,
        repository="sample/widgets",
        routing_context=routing_context(service, user_input),
        session_scope=service.session_scope,
    )
