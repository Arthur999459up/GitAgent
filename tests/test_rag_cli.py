from __future__ import annotations

from gitagent.application import cli
from gitagent.capability.rag import RAGUnavailableError


class EmptyManager:
    @staticmethod
    def list():
        return ()


class FailingManager:
    @staticmethod
    def register_knowledge_base(knowledge_base_id, description):
        del knowledge_base_id, description
        raise RAGUnavailableError("local model is missing")


def test_rag_list_does_not_enter_interactive_credential_flow(monkeypatch) -> None:
    monkeypatch.setattr(cli, "KnowledgeBaseManager", EmptyManager)
    monkeypatch.setattr(
        cli.RuntimeConfig,
        "from_file",
        lambda path: (_ for _ in ()).throw(AssertionError(path)),
    )

    assert cli.main(["rag", "list"]) == 0


def test_rag_management_reports_runtime_failure_without_traceback(monkeypatch) -> None:
    monkeypatch.setattr(cli, "KnowledgeBaseManager", FailingManager)

    result = cli.main(
        [
            "rag",
            "register",
            "engineering",
            "--description",
            "Engineering standards",
        ]
    )

    assert result == 1
