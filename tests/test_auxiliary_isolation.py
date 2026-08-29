from copy import deepcopy

from gitagent.agents.main import MainAgent
from gitagent.capability import CapabilityLayer, PermissionPolicy
from gitagent.domain.learning import ReflectionInput
from gitagent.domain.models import AgentSpec, CandidatePatch, SessionScope
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.validation.static import StaticVerifier


class _ReflectionReasoner:
    def __init__(self) -> None:
        self.requests = []

    def complete_structured_messages(self, *, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        return {"add": [], "replace": [], "delete": []}

    def complete_text_messages(self, **kwargs):
        raise AssertionError(f"unexpected text inference: {kwargs}")


def test_reflection_and_verifier_do_not_mutate_main_or_domain_threads() -> None:
    harness = AgentHarness(CapabilityLayer(policy=PermissionPolicy({})))
    reasoner = _ReflectionReasoner()
    main = MainAgent(harness, reasoner)
    verifier = StaticVerifier(harness)
    scope = SessionScope(
        "https://api.github.com#user:71",
        "https://api.github.com#repo:72",
        "session-" + "3" * 32,
    )
    main_messages = [
        {"role": "system", "content": "main live system"},
        {"role": "user", "content": "main live user"},
        {"role": "assistant", "content": "main live assistant"},
    ]
    domain = AgentContext(
        harness,
        AgentSpec("domain_probe", "role", "domain live system", (), frozenset()),
        scope.session_id,
        repository="owner/repo",
        goal="domain live task",
    )
    domain.start_message_thread()
    domain.append_message({"role": "assistant", "content": "domain live assistant"})
    before_main = deepcopy(main_messages)
    before_domain = deepcopy(domain.messages)

    changes = main.reflect(
        ReflectionInput(
            scope=scope,
            repository_full_name="owner/repo",
            trigger="turn_completed",
            memory_index="",
            conversation_units=({"role": "user", "content": "learning evidence"},),
        )
    )
    report = verifier.verify(
        CandidatePatch(
            summary="candidate",
            root_cause="test",
            added_files=[],
            modified_files=["example.py"],
            deleted_files=[],
            patch="",
            files={"example.py": "value = 1\n"},
        ),
        session_id=scope.session_id,
    )

    assert changes.changed is False
    assert report.passed is True
    assert main_messages == before_main
    assert domain.messages == before_domain
    encoded_reflection = repr(reasoner.requests)
    assert "main live user" not in encoded_reflection
    assert "domain live assistant" not in encoded_reflection
