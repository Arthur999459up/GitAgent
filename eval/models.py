"""Small, JSON-friendly contracts shared by the eval runner and grader."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

SUPPORTED_DATASET_SCHEMA = "1.0"
TRIAL_SCHEMA_VERSION = "1.0"
METRIC_GROUPS = frozenset({"M1", "M2-A", "M2-B", "M3", "M4", "M5", "M6", "M7"})


@dataclass(frozen=True, slots=True)
class EvalSample:
    sample_key: str
    task_name: str
    id: str
    metric_group: str
    user_input: tuple[str, ...]
    setup_ref: str | None
    label: dict[str, Any]
    answer_reference: tuple[str, ...]

    @classmethod
    def from_mapping(cls, sample_key: str, value: Mapping[str, Any]) -> EvalSample:
        expected = {
            "task_name",
            "id",
            "metric_group",
            "user_input",
            "setup_ref",
            "label",
            "answer_reference",
        }
        unknown = set(value) - expected
        missing = expected - set(value)
        if missing:
            raise ValueError(
                f"{sample_key} is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise ValueError(
                f"{sample_key} has unknown fields: {', '.join(sorted(unknown))}"
            )
        task_name = _nonempty(value["task_name"], f"{sample_key}.task_name")
        identifier = _nonempty(value["id"], f"{sample_key}.id")
        if sample_key != f"{task_name}:{identifier}":
            raise ValueError(
                f"sample map key does not match task_name:id: {sample_key}"
            )
        metric_group = _nonempty(value["metric_group"], f"{sample_key}.metric_group")
        if metric_group not in METRIC_GROUPS:
            raise ValueError(
                f"{sample_key} has unsupported metric_group {metric_group!r}"
            )
        raw_inputs = value["user_input"]
        if not isinstance(raw_inputs, list) or not raw_inputs:
            raise ValueError(f"{sample_key}.user_input must be a non-empty array")
        user_input = tuple(
            _nonempty(item, f"{sample_key}.user_input[{index}]")
            for index, item in enumerate(raw_inputs)
        )
        setup_ref = value["setup_ref"]
        if setup_ref is not None and (
            not isinstance(setup_ref, str) or not setup_ref.strip()
        ):
            raise ValueError(
                f"{sample_key}.setup_ref must be null or a non-empty string"
            )
        label = value["label"]
        if not isinstance(label, dict):
            raise TypeError(f"{sample_key}.label must be an object")
        label_fields = {"route", "trace", "must_not", "final_state"}
        if set(label) != label_fields:
            raise ValueError(
                f"{sample_key}.label fields must be {sorted(label_fields)}"
            )
        if not isinstance(label["route"], list) or not label["route"]:
            raise ValueError(f"{sample_key}.label.route must be a non-empty array")
        if not isinstance(label["trace"], list):
            raise TypeError(f"{sample_key}.label.trace must be an array")
        if not isinstance(label["must_not"], list):
            raise TypeError(f"{sample_key}.label.must_not must be an array")
        if not isinstance(label["final_state"], str):
            raise TypeError(f"{sample_key}.label.final_state must be a string")
        references = value["answer_reference"]
        if not isinstance(references, list):
            raise TypeError(f"{sample_key}.answer_reference must be an array")
        return cls(
            sample_key=sample_key,
            task_name=task_name,
            id=identifier,
            metric_group=metric_group,
            user_input=user_input,
            setup_ref=setup_ref,
            label=dict(label),
            answer_reference=tuple(str(item) for item in references),
        )

    def gold(self) -> dict[str, Any]:
        return {"label": self.label, "answer_reference": list(self.answer_reference)}


@dataclass(frozen=True, slots=True)
class TrialPlan:
    sample_key: str
    metric_group: str
    variant: str = "normal"
    replicate: int = 1
    warmup: bool = False
    account: str = "A"

    @property
    def trial_id(self) -> str:
        suffix = ":warmup" if self.warmup else ""
        return f"{self.sample_key}:{self.variant}:{self.replicate}{suffix}"


@dataclass(frozen=True, slots=True)
class EventSlice:
    session_id: str
    after_seq: int
    end_seq: int


@dataclass(slots=True)
class TrialRecord:
    run_id: str
    trial_id: str
    sample_key: str
    metric_group: str
    variant: str
    replicate: int
    status: str
    invalid_reason: str | None = None
    session_ids: list[str] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)
    latency_ms: float = 0.0
    final_answer: str = ""
    event_slices: list[EventSlice] = field(default_factory=list)
    events_path: str = ""
    observer_path: str = ""
    fault: dict[str, Any] | None = None
    warmup: bool = False
    action_log: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    schema_version: str = TRIAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TrialRecord:
        data = dict(value)
        data["event_slices"] = [
            EventSlice(**item) for item in data.get("event_slices", [])
        ]
        return cls(**data)


@dataclass(slots=True)
class ObserverSnapshot:
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    diff: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ObserverSnapshot:
        return cls(
            before=dict(value.get("before") or {}),
            after=dict(value.get("after") or {}),
            diff=dict(value.get("diff") or {}),
        )


@dataclass(slots=True)
class DeterministicResult:
    sample_key: str
    metric_group: str
    valid: bool
    trial_ids: list[str]
    deterministic: dict[str, Any]
    judge_required: list[str]
    status: str = "completed"
    invalid_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DeterministicResult:
        return cls(**dict(value))


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value
