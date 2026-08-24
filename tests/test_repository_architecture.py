from __future__ import annotations

import unittest

from gitagent.agents.code_change_controller import CodeChangeController
from gitagent.agents.coding import CodingAgent
from gitagent.agents.main import MainAgent
from gitagent.agents.repository import RepositoryAgent
from gitagent.app.service import GitAgentService
from gitagent.core.approval import ApprovalStore
from gitagent.core.errors import ApprovalRequired, RoutingError, ValidationError
from gitagent.core.models import (
    ApprovalIntent,
    ChangeRequest,
    PlannedToolCall,
    RepositoryOperation,
    Route,
    SessionScope,
    WorkflowTurnDecision,
)
from gitagent.mcp.memory import InMemoryMCPServer
from gitagent.runtime import AgentHarness, AgentLoop, register_github_mutator
from gitagent.verification import StaticVerifier


class StubReasoner:
    def complete_structured(self, **_: object) -> dict[str, object]:
        return {
            "target_agent": "",
            "request": "",
            "message": "ok",
            "clarify": False,
            "requested_reply": False,
        }

    def complete_text(self, **_: object) -> str:
        return "ok"


def repository_fixture() -> dict[str, dict[str, object]]:
    return {
        "acme/demo": {
            "files": {
                "README.md": "demo foo\n",
                "src/a.py": "def foo():\n    return 1\n",
            },
            "history": {
                "src/a.py": [
                    {"sha": "abc", "message": "add foo"},
                ]
            },
        }
    }


def repository_agent() -> tuple[InMemoryMCPServer, AgentHarness, RepositoryAgent]:
    server = InMemoryMCPServer(repository_fixture())
    harness = AgentHarness(server)
    register_github_mutator(harness)
    coding = CodingAgent(harness)
    verifier = StaticVerifier(harness)
    agent = RepositoryAgent(harness, coding, CodeChangeController(coding, verifier))
    return server, harness, agent


class RepositoryArchitectureTests(unittest.TestCase):
    def test_route_and_repository_operation_model(self) -> None:
        self.assertEqual([item.value for item in Route], ["ISSUE", "PULL_REQUEST", "REPOSITORY"])
        self.assertEqual(
            [item.value for item in RepositoryOperation],
            ["EXPLORE", "SEARCH", "EXPLAIN", "IMPACT_ANALYZE", "PLAN", "HISTORY", "MODIFY"],
        )

    def test_main_accepts_repository_and_rejects_old_domain_names(self) -> None:
        server = InMemoryMCPServer(repository_fixture())
        harness = AgentHarness(server)
        main = MainAgent(harness, StubReasoner())
        decision = main._validate(
            {
                "target_agent": "repository",
                "request": "change README",
                "message": "",
                "clarify": False,
                "requested_reply": False,
            },
            "change README",
        )
        self.assertEqual(decision.target_agent, "repository")
        with self.assertRaises(ValidationError):
            main._validate(
                {
                    "target_agent": "code_change",
                    "request": "change README",
                    "message": "",
                    "clarify": False,
                    "requested_reply": False,
                },
                "change README",
            )

    def test_repository_agent_registers_without_code_change_agent(self) -> None:
        _, harness, _ = repository_agent()
        spec = harness.spec("repository")
        self.assertEqual(spec.name, "repository")
        self.assertIn("repository.get_file_history", spec.allowed_tools)
        self.assertFalse(any(tool.startswith("github.") for tool in spec.allowed_tools))
        with self.assertRaises(ValidationError):
            harness.spec("code_change")

    def test_repository_read_operations_use_bounded_read_tools(self) -> None:
        _, _, agent = repository_agent()
        explore = agent.answer(
            "acme/demo",
            "浏览目录",
            session_id="session-read",
            operation=RepositoryOperation.EXPLORE,
        )
        search = agent.answer(
            "acme/demo",
            "foo",
            session_id="session-read",
            operation=RepositoryOperation.SEARCH,
        )
        history = agent.answer(
            "acme/demo",
            "src/a.py 历史",
            session_id="session-read",
            operation=RepositoryOperation.HISTORY,
        )

        self.assertEqual(explore.files, ["README.md", "src/a.py"])
        self.assertEqual(search.files, ["README.md", "src/a.py"])
        self.assertEqual(search.symbols, ["foo"])
        self.assertEqual(history.files, ["src/a.py"])
        self.assertEqual(len(history.history), 1)

    def test_repository_modify_owns_pause_state_and_approval(self) -> None:
        server, harness, agent = repository_agent()
        context = harness.context(
            "repository",
            "session-modify",
            repository="acme/demo",
            goal="set foo return value to 2",
            entity_type="repository",
        )
        context.operation = RepositoryOperation.MODIFY.value
        context.change_request = ChangeRequest(
            repository="acme/demo",
            description="set foo return value to 2",
            target_files=["src/a.py"],
            proposed_files={"src/a.py": "def foo():\n    return 2\n"},
        )
        loop = AgentLoop(harness)

        loop.start(context, agent)
        self.assertEqual(context.agent, "repository")
        self.assertEqual(context.operation, RepositoryOperation.MODIFY.value)
        self.assertIsNotNone(context.pending)
        self.assertTrue(context.verification and context.verification.passed)
        self.assertEqual(server.repositories["acme/demo"].get("draft_prs", []), [])

        loop.resume(context, agent, WorkflowTurnDecision(ApprovalIntent.APPROVE))
        self.assertTrue(context.finished)
        self.assertEqual(type(context.result).__name__, "RepositoryResult")
        self.assertEqual(len(server.repositories["acme/demo"]["draft_prs"]), 1)

    def test_approval_matches_exact_tool_arguments(self) -> None:
        store = ApprovalStore()
        call = PlannedToolCall(
            "github.post_comment",
            {"repository": "acme/demo", "issue_number": 1, "body": "approved body"},
        )
        approval = store.create(
            session_id="session-approval",
            repository="acme/demo",
            summary="post comment",
            calls=[call],
        )
        store.decide(approval.approval_id, "Approve")

        with self.assertRaises(ApprovalRequired):
            store.authorize(
                approval_id=approval.approval_id,
                session_id="session-approval",
                tool=call.tool,
                arguments={**call.arguments, "body": "changed body"},
            )

        store.authorize(
            approval_id=approval.approval_id,
            session_id="session-approval",
            tool=call.tool,
            arguments=call.arguments,
        )
        self.assertTrue(store.complete(approval.approval_id))

    def test_service_restores_only_three_domain_contexts(self) -> None:
        service = GitAgentService(
            InMemoryMCPServer(repository_fixture()),
            main_reasoner=StubReasoner(),
            session_scope=SessionScope("account", "repo-key", "session-service"),
        )
        restored = service._restore_context(
            {
                "agent": "repository",
                "repository": "acme/demo",
                "goal": "change repository",
                "operation": RepositoryOperation.MODIFY.value,
            },
            repository="acme/demo",
        )
        self.assertEqual(restored.agent, "repository")
        self.assertIs(service._agent_for("repository"), service.repository_agent)

        with self.assertRaises(RoutingError):
            service._restore_context(
                {"agent": "code_change", "repository": "acme/demo", "goal": "old nested workflow"},
                repository="acme/demo",
            )
        with self.assertRaises(RoutingError):
            service._agent_for("code_change")


if __name__ == "__main__":
    unittest.main()
