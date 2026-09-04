"""Suite/case/trial orchestration on top of GitAgent's public application facade."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gitagent.application import build_live_application
from gitagent.capability import CapabilityCatalog
from gitagent.infra.github import GitHubClient
from gitagent.infra.persistence import StateStore

try:
    from .environment import (
        FixtureManager,
        Observer,
        PrerequisiteUnavailable,
        RecoveryController,
        append_broken_event_tail,
        derive_runtime_config,
        durable_pending_capability_ids,
    )
    from .grader import (
        aggregate_metrics,
        build_judge_requests,
        finalize_metrics,
        grade_sample,
        read_jsonl,
    )
    from .models import (
        SUPPORTED_DATASET_SCHEMA,
        DeterministicResult,
        EvalSample,
        EventSlice,
        ObserverSnapshot,
        TrialPlan,
        TrialRecord,
    )
except ImportError:  # pragma: no cover - script entry point
    from environment import (
        FixtureManager,
        Observer,
        PrerequisiteUnavailable,
        RecoveryController,
        append_broken_event_tail,
        derive_runtime_config,
        durable_pending_capability_ids,
    )
    from grader import (
        aggregate_metrics,
        build_judge_requests,
        finalize_metrics,
        grade_sample,
        read_jsonl,
    )
    from models import (
        SUPPORTED_DATASET_SCHEMA,
        DeterministicResult,
        EvalSample,
        EventSlice,
        ObserverSnapshot,
        TrialPlan,
        TrialRecord,
    )


EXPECTED_SAMPLE_COUNT = 103
OFFICIAL_METRIC_GROUPS = frozenset({"M3", "M4", "M5", "M6", "M7"})
_APPROVE_INPUT = "同意，执行当前待审批提案。"
_ROOT_FIELDS = {
    "schema_version",
    "dataset_name",
    "description",
    "grounding",
    "trace_label_semantics",
    "samples",
}
_INFRASTRUCTURE_NAMES = {
    "ExternalExecutionError",
    "GitHubTransportError",
    "LLMProviderError",
    "RAGUnavailableError",
    "TimeoutError",
    "ConnectionError",
}


def load_dataset(path: str | Path) -> tuple[dict[str, Any], list[EvalSample]]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"dataset is not valid JSON: {source}:{exc.lineno}:{exc.colno}"
        ) from exc
    if not isinstance(value, dict):
        raise TypeError("dataset root must be an object")
    if set(value) != _ROOT_FIELDS:
        missing = sorted(_ROOT_FIELDS - set(value))
        unknown = sorted(set(value) - _ROOT_FIELDS)
        raise ValueError(
            f"dataset root fields mismatch; missing={missing}, unknown={unknown}"
        )
    if value.get("schema_version") != SUPPORTED_DATASET_SCHEMA:
        raise ValueError(
            f"unsupported dataset schema_version: {value.get('schema_version')!r}"
        )
    raw_samples = value.get("samples")
    if not isinstance(raw_samples, dict):
        raise TypeError("dataset.samples must be an object keyed by task_name:id")
    if len(raw_samples) != EXPECTED_SAMPLE_COUNT:
        raise ValueError(
            f"dataset must contain exactly {EXPECTED_SAMPLE_COUNT} samples"
        )
    samples = [EvalSample.from_mapping(key, item) for key, item in raw_samples.items()]
    if len({sample.sample_key for sample in samples}) != len(samples):
        raise ValueError("dataset sample keys must be unique")
    metadata = {key: item for key, item in value.items() if key != "samples"}
    metadata["source_path"] = str(source)
    return metadata, samples


class EvalRunner:
    def __init__(
        self,
        *,
        dataset_path: str | Path,
        config_path: str | Path,
        output_dir: str | Path,
        repetitions: int = 5,
        sample_key: str | None = None,
        group: str | None = None,
        resume: bool = False,
        keep_fixtures: bool = False,
    ) -> None:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        self.dataset_path = Path(dataset_path).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.repetitions = repetitions
        self.sample_filter = sample_key
        self.group_filter = group
        self.resume = resume
        self.keep_fixtures = keep_fixtures
        self.metadata, all_samples = load_dataset(self.dataset_path)
        if sample_key and group:
            raise ValueError("--sample and --group are mutually exclusive")
        if sample_key and sample_key not in {
            sample.sample_key for sample in all_samples
        }:
            raise ValueError(f"unknown sample: {sample_key}")
        self.samples = [
            sample
            for sample in all_samples
            if (not sample_key or sample.sample_key == sample_key)
            and (not group or sample.metric_group == group)
            and (
                sample_key is not None
                or group is not None
                or sample.metric_group in OFFICIAL_METRIC_GROUPS
            )
        ]
        if not self.samples:
            raise ValueError(f"no samples selected for group {group!r}")
        self.base_config = derive_runtime_config(
            self.config_path,
            variant="normal",
            state_root=self.output_dir / "state" / "normal",
        )
        self.run_id = f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self._redaction_store: StateStore | None = None
        self._trials: list[TrialRecord] = []
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._observers: dict[str, ObserverSnapshot] = {}
        self._errors: list[dict[str, Any]] = []
        self._applications: list[Any] = []
        self._application_specs: dict[int, dict[str, str]] = {}
        self._account_info: dict[str, dict[str, Any]] = {}
        self._repository_info: dict[str, dict[str, Any]] = {}
        self._fixture_manager: FixtureManager | None = None
        self._observer: Observer | None = None
        self._manifest: dict[str, Any] = {}
        self._context_chain: dict[str, Any] | None = None
        self._suite_prerequisites: dict[str, str] = {}

    def run(self) -> dict[str, Any]:
        self._prepare_output()
        self._redaction_store = StateStore(
            self.base_config.state_path,
            secret_values=self._all_secret_values(),
        )
        self._load_existing()
        self._initialize_environment()
        self._write_manifest()
        suite_error: Exception | None = None
        try:
            self._run_groups()
        except Exception as exc:  # noqa: BLE001 - artifacts and cleanup still run
            self._record_error("suite", exc, status="error")
            suite_error = exc
        finally:
            self._close_applications()
            if self._fixture_manager is not None:
                for error in self._fixture_manager.cleanup_suite(
                    keep_fixtures=self.keep_fixtures
                ):
                    self._errors.append(
                        {"scope": "cleanup", "status": "error", "error": error}
                    )
            self._manifest["finished_at"] = datetime.now(UTC).isoformat()
            if self._fixture_manager is not None:
                self._manifest["owned_resources"] = (
                    self._fixture_manager.manifest_resources()
                )
            self._write_manifest()
            self._write_jsonl("errors.jsonl", self._errors)
        try:
            results = self._grade()
            metrics = aggregate_metrics(self.run_id, self.samples, results)
            self._write_jsonl(
                "deterministic-results.jsonl",
                [result.to_dict() for result in results],
            )
            self._write_json("metrics.json", metrics)
            by_sample: dict[str, list[TrialRecord]] = defaultdict(list)
            for trial in self._trials:
                by_sample[trial.sample_key].append(trial)
            requests = build_judge_requests(
                self.samples,
                results,
                by_sample,
                self._events,
                self._observers,
                sanitizer=self._sanitize,
            )
            self._write_jsonl("judge-input.jsonl", requests)
            self._assert_reports_secret_free()
        finally:
            # A hard-killed run retains this directory for --resume. A run that
            # reaches artifact generation must not preserve private Memory/state.
            self._remove_runtime_state()
        if suite_error is not None:
            raise suite_error
        return metrics

    def _prepare_output(self) -> None:
        if self.output_dir.exists():
            material = [
                item for item in self.output_dir.iterdir() if item.name != ".gitkeep"
            ]
            if material and not self.resume:
                raise ValueError(
                    f"output directory is not empty; use --resume: {self.output_dir}"
                )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name in ("events", "observer", "state"):
            (self.output_dir / name).mkdir(parents=True, exist_ok=True)

    def _initialize_environment(self) -> None:
        github = GitHubClient(
            token=self.base_config.github_token,
            api_url=self.base_config.github_api_url,
            timeout=self.base_config.github_timeout,
        )
        account_a, repository_a = self._resolve_account_repository(github)
        self._account_info["A"] = account_a
        self._repository_info["A"] = repository_a
        github_b = None
        token_b = os.environ.get("GITAGENT_EVAL_GITHUB_TOKEN_B", "")
        if token_b:
            github_b = GitHubClient(
                token=token_b,
                api_url=self.base_config.github_api_url,
                timeout=self.base_config.github_timeout,
            )
            try:
                account_b, repository_b = self._resolve_account_repository(github_b)
            except Exception as exc:  # noqa: BLE001 - B remains a per-case prerequisite
                self._suite_prerequisites["account_b"] = f"{type(exc).__name__}: {exc}"
                github_b = None
            else:
                if int(account_b["id"]) == int(account_a["id"]):
                    self._suite_prerequisites["account_b"] = (
                        "account_b_identity_matches_account_a"
                    )
                    github_b = None
                else:
                    self._account_info["B"] = account_b
                    self._repository_info["B"] = repository_b
        repository = str(repository_a["full_name"])
        previous_manifest = dict(self._manifest)
        self._fixture_manager = FixtureManager(
            github,
            repository,
            self.run_id,
            github_b=github_b,
        )
        self._fixture_manager.owned.extend(
            dict(item)
            for item in previous_manifest.get("owned_resources", [])
            if isinstance(item, dict)
        )
        try:
            self._fixture_manager.prepare_suite(self.samples)
        except PrerequisiteUnavailable as exc:
            self._suite_prerequisites["M7"] = str(exc)
            self._record_error("fixture:M7", exc, status="invalid")
        grounding = self.metadata.get("grounding") or {}
        local = grounding.get("test_repository_local_mirror")
        local_commit = _git_commit(Path(local)) if local else None
        local_clean = _git_worktree_clean(Path(local)) if local else False
        remote_commit = str(
            github.get_default_branch(repository).get("commit_sha") or ""
        )
        if not local_commit:
            self._suite_prerequisites["grounding"] = (
                "test_repository_local_mirror_unavailable"
            )
        elif not local_clean:
            self._suite_prerequisites["grounding"] = (
                "test_repository_local_mirror_not_clean"
            )
        elif local_commit != remote_commit:
            self._suite_prerequisites["grounding"] = "test_repository_revision_mismatch"
        self._observer = Observer(
            github,
            repository,
            local_repository=local,
            secret_values=self._all_secret_values(),
        )
        self._manifest = {
            "run_id": self.run_id,
            "started_at": previous_manifest.get("started_at")
            or datetime.now(UTC).isoformat(),
            "finished_at": None,
            "dataset_hash": _sha256(self.dataset_path),
            "gitagent_commit": _git_commit(Path(__file__).resolve().parents[1]),
            "test_repo_commit": local_commit,
            "test_repo_remote_commit": remote_commit,
            "test_repo_worktree_clean": local_clean,
            "base_config_hash": _sha256(self.config_path),
            "model": self.base_config.model,
            "base_url_endpoint": _endpoint_identifier(self.base_config.base_url),
            "temperature": 0.0,
            "repetitions": self.repetitions,
            "account_a_available": True,
            "account_b_available": "B" in self._account_info,
            "account_a_identity_hash": _identity_hash(account_a),
            "account_b_identity_hash": _identity_hash(self._account_info.get("B")),
            "repository": repository,
            "selected_samples": [sample.sample_key for sample in self.samples],
            "owned_resources": self._fixture_manager.manifest_resources(),
            "prerequisites": dict(self._suite_prerequisites),
        }

    def _resolve_account_repository(
        self, github: GitHubClient
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        account = github.get_authenticated_user()
        repository_name = str(
            os.environ.get("GITAGENT_EVAL_REPOSITORY")
            or (self.metadata.get("grounding") or {}).get("test_repository")
            or ""
        )
        repositories = github.list_repositories(max_repositories=1000)
        repository = next(
            (
                item
                for item in repositories
                if str(item.get("full_name") or "").casefold()
                == repository_name.casefold()
            ),
            None,
        )
        if repository is None:
            raise PrerequisiteUnavailable(
                f"repository_not_accessible:{repository_name}"
            )
        return account, repository

    def _run_groups(self) -> None:
        grounding_error = self._suite_prerequisites.get("grounding")
        if grounding_error:
            for sample in self.samples:
                self._append_invalid(sample, grounding_error)
            return
        by_group: dict[str, list[EvalSample]] = defaultdict(list)
        for sample in self.samples:
            by_group[sample.metric_group].append(sample)
        for group in ("E2E", "M2-A", "M2-B", "M3", "M4", "M5", "M6", "M7"):
            samples = by_group.get(group, [])
            if not samples:
                continue
            try:
                if group == "M2-A":
                    self._run_context_chain(samples)
                elif group == "M2-B":
                    self._run_memory_chain(samples)
                elif group in {"M3", "M4"}:
                    self._run_performance(samples, group)
                elif group == "M6":
                    self._run_recovery(samples)
                else:
                    self._run_standard(samples, namespace=group)
            finally:
                self._close_applications()

    def _run_standard(self, samples: Sequence[EvalSample], *, namespace: str) -> None:
        if namespace in self._suite_prerequisites:
            for sample in samples:
                self._append_invalid(sample, self._suite_prerequisites[namespace])
            return
        app = self._build_application("normal", namespace=namespace)
        for sample in samples:
            if self._has_valid_trials(sample.sample_key, "normal", 1):
                continue
            plan = TrialPlan(sample.sample_key, sample.metric_group)
            self._append_trial(self._execute_trial(sample, plan, app, new_session=True))

    def _run_context_chain(self, samples: Sequence[EvalSample]) -> None:
        existing = [
            trial
            for trial in self._trials
            if trial.metric_group == "M2-A" and trial.status in {"completed", "failed"}
        ]
        if all(
            self._has_valid_trials(sample.sample_key, "smallctx", 1)
            for sample in samples
        ):
            ctx11 = next(
                (
                    trial
                    for trial in existing
                    if trial.sample_key == "context_compaction:CTX-11"
                ),
                None,
            )
            if ctx11 and ctx11.session_ids:
                self._context_chain = {
                    "session_id": ctx11.session_ids[-1],
                    "namespace": "M2-A",
                    "variant": "smallctx",
                    "auto_compact_observed": bool(
                        ctx11.fault and ctx11.fault.get("auto_compact_observed")
                    ),
                }
            return
        if existing and not (self.output_dir / "state/M2-A/A/state.db").is_file():
            for sample in samples:
                if not self._has_valid_trials(sample.sample_key, "smallctx", 1):
                    self._append_invalid(sample, "resume_context_state_unavailable")
            return
        app = self._build_application("smallctx", namespace="M2-A")
        if existing:
            session_id = existing[-1].session_ids[-1]
            app.resume_session(int(self._account_info["A"]["id"]), session_id)
        else:
            self._create_session(app, "A")
            app.config.memory_automation = False
            app.memory_hooks.enabled = False
        for sample in samples:
            if self._has_valid_trials(sample.sample_key, "smallctx", 1):
                continue
            plan = TrialPlan(sample.sample_key, sample.metric_group, "smallctx")
            self._append_trial(
                self._execute_trial(sample, plan, app, new_session=False)
            )
        scope = app.scope
        if scope is not None:
            all_events = [
                _event_to_dict(event)
                for event in app.sessions.event_log.iter_events(scope)
            ]
            compact_events = [
                event
                for event in all_events
                if str((event.get("data") or {}).get("name") or "") == "auto_compact"
            ]
            ctx11 = next(
                (
                    trial
                    for trial in self._trials
                    if trial.sample_key == "context_compaction:CTX-11"
                    and not trial.warmup
                ),
                None,
            )
            ctx11_compactions: list[dict[str, Any]] = []
            if ctx11 is not None:
                preconditions = {
                    event_slice.session_id: event_slice.after_seq
                    for event_slice in ctx11.event_slices
                }
                ctx11_compactions = [
                    event
                    for event in compact_events
                    if int(event.get("seq") or 0)
                    <= preconditions.get(str(event.get("session_id") or ""), -1)
                ]
                ctx11.fault = {
                    "kind": "context_chain_precondition",
                    "triggered": bool(ctx11_compactions),
                    "auto_compact_observed": bool(ctx11_compactions),
                    "compaction_count": len(ctx11_compactions),
                }
                self._write_jsonl(
                    "trials.jsonl", (trial.to_dict() for trial in self._trials)
                )
            self._context_chain = {
                "session_id": scope.session_id,
                "namespace": "M2-A",
                "variant": "smallctx",
                "auto_compact_observed": bool(ctx11_compactions),
            }

    def _run_memory_chain(self, samples: Sequence[EvalSample]) -> None:
        existing = [
            trial
            for trial in self._trials
            if trial.metric_group == "M2-B" and trial.status in {"completed", "failed"}
        ]
        if all(
            self._has_valid_trials(sample.sample_key, "normal", 1) for sample in samples
        ):
            return
        if existing and not (self.output_dir / "state/M2-B/state.db").is_file():
            for sample in samples:
                if not self._has_valid_trials(sample.sample_key, "normal", 1):
                    self._append_invalid(sample, "resume_memory_state_unavailable")
            return
        app_a = self._build_application("normal", namespace="M2-B")
        if app_a.scope is None:
            self._create_session(app_a, "A")
        app_b = None
        for sample in samples:
            if self._has_valid_trials(sample.sample_key, "normal", 1):
                continue
            if sample.id == "MEM-09":
                if "B" not in self._account_info:
                    self._append_invalid(
                        sample, "account_b_not_configured", account="B"
                    )
                    continue
                if app_b is None:
                    app_b = self._build_application(
                        "normal", namespace="M2-B", account="B"
                    )
                    self._create_session(app_b, "B")
                app = app_b
                account = "B"
            else:
                app = app_a
                account = "A"
            plan = TrialPlan(sample.sample_key, sample.metric_group, account=account)
            self._append_trial(
                self._execute_trial(sample, plan, app, new_session=False)
            )

    def _run_performance(self, samples: Sequence[EvalSample], group: str) -> None:
        variants = {
            "M3": {"serial": "tool_serial", "parallel": "tool_parallel"},
            "M4": {"serial": "agent_serial", "parallel": "agent_parallel"},
        }[group]
        apps = {
            label: self._build_application(config_variant, namespace=f"{group}-{label}")
            for label, config_variant in variants.items()
        }
        for sample in samples:
            for label in ("serial", "parallel"):
                if not self._has_warmup(sample.sample_key, label):
                    plan = TrialPlan(sample.sample_key, group, label, 0, warmup=True)
                    self._append_trial(
                        self._execute_trial(sample, plan, apps[label], new_session=True)
                    )
            valid_counts = {
                label: self._valid_trial_count(sample.sample_key, label)
                for label in variants
            }
            attempts = {
                label: max(
                    [
                        trial.replicate
                        for trial in self._trials
                        if trial.sample_key == sample.sample_key
                        and trial.variant == label
                    ]
                    or [0]
                )
                for label in variants
            }
            pass_index = 0
            while any(valid_counts[label] < self.repetitions for label in variants):
                order = (
                    ("serial", "parallel")
                    if pass_index % 2 == 0
                    else ("parallel", "serial")
                )
                progressed = False
                for label in order:
                    if (
                        valid_counts[label] >= self.repetitions
                        or attempts[label] >= self.repetitions + 2
                    ):
                        continue
                    attempts[label] += 1
                    plan = TrialPlan(sample.sample_key, group, label, attempts[label])
                    trial = self._execute_trial(
                        sample, plan, apps[label], new_session=True
                    )
                    self._append_trial(trial)
                    if trial.status in {"completed", "failed"}:
                        valid_counts[label] += 1
                    progressed = True
                if not progressed:
                    break
                pass_index += 1

    def _run_recovery(self, samples: Sequence[EvalSample]) -> None:
        for sample in samples:
            if self._has_valid_trials(sample.sample_key, "normal", 1):
                continue
            try:
                if sample.id in {"REC-01", "REC-02", "REC-03", "REC-04", "REC-08"}:
                    trial = self._execute_crash_recovery(sample)
                elif sample.id == "REC-09":
                    trial = self._execute_tail_recovery(sample)
                elif sample.id == "REC-11":
                    trial = self._execute_context_recovery(sample)
                else:
                    app = self._build_application("normal", namespace=f"M6-{sample.id}")
                    trial = self._execute_trial(
                        sample,
                        TrialPlan(sample.sample_key, sample.metric_group),
                        app,
                        new_session=True,
                    )
                self._append_trial(trial)
            finally:
                self._close_applications()

    def _execute_trial(
        self,
        sample: EvalSample,
        plan: TrialPlan,
        application: Any,
        *,
        new_session: bool,
    ) -> TrialRecord:
        started = time.perf_counter()
        action_log: list[dict[str, Any]] = []
        raw_exported_values: list[Any] = []
        fault: dict[str, Any] | None = None
        try:
            assert self._fixture_manager is not None and self._observer is not None
            fixture_targets = self._fixture_manager.prepare_case(sample)
            self._sync_owned_resources()
            if new_session:
                self._create_or_new_session(application, plan.account)
            elif application.scope is None:
                self._create_session(application, plan.account)
            tracker = _EventTracker(application)
            before = self._observer.capture(
                sample, application, fixture_targets=fixture_targets
            )
            self._observer.validate_baseline(sample, before)
            status = "completed"
            error: str | None = None
            context: dict[str, Any] = {
                "s1": application.session_id,
                "listed_sessions": (),
            }
            execution_started = time.perf_counter()
            for raw in sample.user_input:
                tracker.touch(application)
                if raw.startswith("<FAULT:"):
                    expected_capability = {
                        "REC-05": "github.merge",
                        "REC-06": "github.commit_to_default_branch",
                        "REC-07": "github.merge",
                    }.get(sample.id)
                    if expected_capability is not None:
                        pending = durable_pending_capability_ids(
                            application.store, application.session_id
                        )
                        if expected_capability not in pending:
                            raise PrerequisiteUnavailable(
                                f"fault_trigger_not_reached:{expected_capability}"
                            )
                    external = self._fixture_manager.apply_external_fault(sample)
                    self._sync_owned_resources()
                    fault = {
                        "kind": "external_state_change",
                        "triggered": True,
                        "description": raw,
                        "external_fingerprints": external,
                    }
                    action_log.append(
                        {
                            "action": "fault",
                            "status": "ok",
                            "session_id": application.session_id,
                        }
                    )
                    continue
                if raw.startswith("<SETUP:"):
                    action_log.append(
                        {
                            "action": "setup",
                            "status": "ok",
                            "session_id": application.session_id,
                        }
                    )
                    continue
                try:
                    action_result = self._dispatch(
                        application,
                        _execution_mode_input(sample, plan.variant, raw),
                        context,
                    )
                except Exception as exc:  # noqa: BLE001 - classify behavior vs infra
                    expected_error = _expected_turn_error(sample, raw)
                    action_error = f"{type(exc).__name__}: {exc}"
                    status = (
                        "invalid"
                        if _is_infrastructure(exc)
                        else status
                        if expected_error
                        else "failed"
                    )
                    if not expected_error:
                        error = action_error
                    action_log.append(
                        {
                            "action": _action_name(raw),
                            "status": "error",
                            "session_id": application.session_id,
                            "error": action_error,
                        }
                    )
                    if status == "invalid" or not expected_error:
                        break
                else:
                    action_entry = {
                        "action": _action_name(raw),
                        "status": "ok",
                        "session_id": application.session_id,
                    }
                    result_summary = _action_result_summary(raw, action_result)
                    if result_summary is not None:
                        action_entry["result"] = result_summary
                    action_log.append(action_entry)
                    raw_exported_values.append(action_result)
                tracker.touch(application)
            execution_latency_ms = (time.perf_counter() - execution_started) * 1000
            events, slices = tracker.collect(application)
            self._remember_context_chain(application, plan, events)
            final_answer = _final_answer(events)
            after = self._observer.capture(
                sample, application, fixture_targets=fixture_targets
            )
            snapshot = self._observer.compare(
                sample,
                before,
                after,
                exported_values=(events, final_answer, action_log, raw_exported_values),
            )
            if status != "invalid":
                self._fixture_manager.reset_case(sample, snapshot)
                self._sync_owned_resources()
            return self._finish_trial(
                sample,
                plan,
                status=status,
                invalid_reason=error if status == "invalid" else None,
                error=error,
                latency_ms=execution_latency_ms,
                events=events,
                slices=slices,
                snapshot=snapshot,
                action_log=action_log,
                fault=fault,
            )
        except PrerequisiteUnavailable as exc:
            return self._invalid_record(sample, plan, str(exc), started=started)
        except Exception as exc:  # noqa: BLE001 - invalid trial must remain reportable
            self._record_error(plan.trial_id, exc, status="invalid")
            return self._invalid_record(
                sample, plan, f"{type(exc).__name__}: {exc}", started=started
            )

    def _execute_crash_recovery(self, sample: EvalSample) -> TrialRecord:
        plan = TrialPlan(sample.sample_key, sample.metric_group)
        started = time.perf_counter()
        namespace = f"M6-{sample.id}"
        application = self._build_application("normal", namespace=namespace)
        try:
            assert self._fixture_manager is not None and self._observer is not None
            fixture_targets = self._fixture_manager.prepare_case(sample)
            self._sync_owned_resources()
            self._create_or_new_session(application, "A")
            tracker = _EventTracker(application)
            before = self._observer.capture(
                sample, application, fixture_targets=fixture_targets
            )
            self._observer.validate_baseline(sample, before)
            scope = application.scope
            assert scope is not None
            session_id = scope.session_id
            event_log = application.sessions.event_log
            account_id = int(self._account_info["A"]["id"])
            application.close()
            trigger = (
                "active_siblings"
                if sample.id == "REC-08"
                else "waiting_for_user"
                if sample.id == "REC-03"
                else "pending_approval"
            )
            controller = RecoveryController()
            fault = controller.crash_turn(
                config_path=self.config_path,
                variant="normal",
                state_root=self.output_dir / "state" / namespace / "A",
                account="A",
                authenticated_user_id=account_id,
                session_id=session_id,
                scope=scope,
                event_log=event_log,
                state_store=application.store,
                user_input=sample.user_input[0],
                trigger=trigger,
            )
            if not fault["triggered"]:
                return self._invalid_record(
                    sample,
                    plan,
                    "fault_trigger_not_reached",
                    started=started,
                    fault=fault,
                )
            restored = self._build_application("normal", namespace=namespace)
            restored.resume_session(account_id, session_id)
            tracker.touch(restored)
            action_log = [{"action": "fault", "status": "ok", "session_id": session_id}]
            context: dict[str, Any] = {"s1": session_id, "listed_sessions": ()}
            status = "completed"
            error = None
            for raw in sample.user_input[2:]:
                try:
                    self._dispatch(restored, raw, context)
                except Exception as exc:  # noqa: BLE001 - classify behavior vs infra
                    status = "invalid" if _is_infrastructure(exc) else "failed"
                    error = f"{type(exc).__name__}: {exc}"
                    action_log.append(
                        {
                            "action": _action_name(raw),
                            "status": "error",
                            "error": error,
                            "session_id": restored.session_id,
                        }
                    )
                    break
                else:
                    action_log.append(
                        {
                            "action": _action_name(raw),
                            "status": "ok",
                            "session_id": restored.session_id,
                        }
                    )
                tracker.touch(restored)
            events, slices = tracker.collect(restored)
            after = self._observer.capture(
                sample, restored, fixture_targets=fixture_targets
            )
            snapshot = self._observer.compare(
                sample, before, after, exported_values=(events, action_log)
            )
            self._fixture_manager.reset_case(sample, snapshot)
            self._sync_owned_resources()
            return self._finish_trial(
                sample,
                plan,
                status=status,
                invalid_reason=error if status == "invalid" else None,
                error=error,
                latency_ms=(time.perf_counter() - started) * 1000,
                events=events,
                slices=slices,
                snapshot=snapshot,
                action_log=action_log,
                fault=fault,
            )
        except Exception as exc:  # noqa: BLE001 - invalid recovery is reportable
            return self._invalid_record(
                sample, plan, f"{type(exc).__name__}: {exc}", started=started
            )

    def _execute_tail_recovery(self, sample: EvalSample) -> TrialRecord:
        plan = TrialPlan(sample.sample_key, sample.metric_group)
        started = time.perf_counter()
        namespace = "M6-REC-09"
        application = self._build_application("normal", namespace=namespace)
        try:
            assert self._observer is not None
            self._create_or_new_session(application, "A")
            tracker = _EventTracker(application)
            before = self._observer.capture(sample, application)
            for prompt in (
                "只读检查 README.md 并用一句话说明项目目标。",
                "只读检查 product_id.py 并告诉我合法数字长度范围。",
            ):
                application.handle(prompt)
            scope = application.scope
            assert scope is not None
            event_log = application.sessions.event_log
            session_id = scope.session_id
            account_id = int(self._account_info["A"]["id"])
            application.close()
            fault = append_broken_event_tail(event_log, scope)
            restored = self._build_application("normal", namespace=namespace)
            restored.resume_session(account_id, session_id)
            self._dispatch(
                restored,
                sample.user_input[-1],
                {"s1": session_id, "listed_sessions": ()},
            )
            tracker.touch(restored)
            events, slices = tracker.collect(restored)
            after = self._observer.capture(sample, restored)
            action_log = [
                {"action": "setup", "status": "ok", "session_id": session_id},
                {"action": "fault", "status": "ok", "session_id": session_id},
                {"action": "handle", "status": "ok", "session_id": session_id},
            ]
            snapshot = self._observer.compare(
                sample, before, after, exported_values=(events, action_log)
            )
            return self._finish_trial(
                sample,
                plan,
                status="completed",
                latency_ms=(time.perf_counter() - started) * 1000,
                events=events,
                slices=slices,
                snapshot=snapshot,
                action_log=action_log,
                fault=fault,
            )
        except Exception as exc:  # noqa: BLE001 - invalid recovery is reportable
            return self._invalid_record(
                sample, plan, f"{type(exc).__name__}: {exc}", started=started
            )

    def _execute_context_recovery(self, sample: EvalSample) -> TrialRecord:
        plan = TrialPlan(sample.sample_key, sample.metric_group)
        if not self._context_chain or not self._context_chain.get(
            "auto_compact_observed"
        ):
            return self._invalid_record(
                sample, plan, "no_natural_auto_compact_observed_before_rec_11"
            )
        started = time.perf_counter()
        chain = dict(self._context_chain)
        account = str(chain.get("account") or "A")
        namespace = str(chain.get("namespace") or "")
        config_variant = str(chain.get("config_variant") or chain.get("variant") or "normal")
        if not namespace:
            return self._invalid_record(
                sample, plan, "compacted_session_namespace_unavailable", started=started
            )
        application = self._build_application(
            config_variant, namespace=namespace, account=account
        )
        try:
            assert self._observer is not None
            session_id = str(chain["session_id"])
            account_id = int(self._account_info[account]["id"])
            application.resume_session(account_id, session_id)
            tracker = _EventTracker(application)
            before = self._observer.capture(sample, application)
            application.close()
            restored = self._build_application(
                config_variant, namespace=namespace, account=account
            )
            restored.resume_session(account_id, session_id)
            self._dispatch(
                restored,
                sample.user_input[-1],
                {"s1": session_id, "listed_sessions": ()},
            )
            tracker.touch(restored)
            events, slices = tracker.collect(restored)
            after = self._observer.capture(sample, restored)
            fault = {
                "kind": "clean_restart_after_natural_auto_compact",
                "triggered": True,
                "session_id": session_id,
                "source_sample_key": chain.get("sample_key"),
                "source_variant": chain.get("trial_variant"),
                "compaction_count": chain.get("compaction_count"),
            }
            action_log = [
                {"action": "restore", "status": "ok", "session_id": session_id}
            ]
            snapshot = self._observer.compare(
                sample, before, after, exported_values=(events, action_log)
            )
            return self._finish_trial(
                sample,
                plan,
                status="completed",
                latency_ms=(time.perf_counter() - started) * 1000,
                events=events,
                slices=slices,
                snapshot=snapshot,
                action_log=action_log,
                fault=fault,
            )
        except Exception as exc:  # noqa: BLE001 - invalid recovery is reportable
            return self._invalid_record(
                sample, plan, f"{type(exc).__name__}: {exc}", started=started
            )

    def _dispatch(self, application: Any, raw: str, context: dict[str, Any]) -> Any:
        text = raw.strip()
        if text.startswith("<EVAL_MEMORY_WRITE "):
            header, separator, content = text.partition("\n")
            scope = header.removeprefix("<EVAL_MEMORY_WRITE ").removesuffix(">")
            if not separator or scope.casefold() not in {"private", "project"}:
                raise ValueError("invalid EVAL_MEMORY_WRITE fixture")
            current = _active_scope(application)
            return application.memory.manual_write(
                current.account_key,
                current.repository_key,
                content.strip(),
                scope=scope.casefold(),
            )
        if text.startswith("<EVAL_MEMORY_FORGET "):
            header, separator, identifier = text.partition("\n")
            scope = header.removeprefix("<EVAL_MEMORY_FORGET ").removesuffix(">")
            if not separator or scope.casefold() not in {"private", "project"}:
                raise ValueError("invalid EVAL_MEMORY_FORGET fixture")
            current = _active_scope(application)
            return application.memory.forget(
                current.account_key,
                current.repository_key,
                identifier=identifier.strip(),
                scope=scope.casefold(),
            )
        if text.startswith("<EVAL_MEMORY_SEARCH>"):
            _, separator, query = text.partition("\n")
            if not separator or not query.strip():
                raise ValueError("invalid EVAL_MEMORY_SEARCH fixture")
            current = _active_scope(application)
            return application.memory_search.search(
                current.account_key,
                current.repository_key,
                query.strip(),
            )
        if text == "<EVAL_MEMORY_REBUILD>":
            current = _active_scope(application)
            return application.memory.rebuild_index(
                current.account_key, current.repository_key
            )
        if not text.startswith("/"):
            return application.handle(text)
        command, _, argument = text.partition(" ")
        normalized = command.casefold()
        if normalized == "/new":
            return application.new_session()
        if normalized == "/reset":
            return application.reset_session()
        if normalized == "/compact":
            return application.compact()
        if normalized == "/sessions":
            sessions = application.list_sessions()
            context["listed_sessions"] = tuple(item.session_id for item in sessions)
            return sessions
        if normalized == "/switch":
            sessions = application.list_sessions()
            target = context.get("s1") if context.get("s1") else None
            if target not in {item.session_id for item in sessions}:
                index = int(argument.strip()) - 1
                target = sessions[index].session_id
            return application.switch_session(str(target))
        raise ValueError(f"unsupported eval control action: {command}")

    def _finish_trial(
        self,
        sample: EvalSample,
        plan: TrialPlan,
        *,
        status: str,
        latency_ms: float,
        events: Sequence[Mapping[str, Any]],
        slices: Sequence[EventSlice],
        snapshot: ObserverSnapshot,
        action_log: list[dict[str, Any]],
        fault: dict[str, Any] | None = None,
        invalid_reason: str | None = None,
        error: str | None = None,
    ) -> TrialRecord:
        slug = _trial_slug(plan.trial_id)
        events_relative = f"events/{slug}.jsonl"
        observer_relative = f"observer/{slug}.json"
        self._write_jsonl(events_relative, events)
        self._write_json(observer_relative, snapshot.to_dict())
        trial = TrialRecord(
            run_id=self.run_id,
            trial_id=plan.trial_id,
            sample_key=sample.sample_key,
            metric_group=sample.metric_group,
            variant=plan.variant,
            replicate=plan.replicate,
            status=status,
            invalid_reason=invalid_reason,
            session_ids=list(dict.fromkeys(item.session_id for item in slices)),
            turns=sorted(
                {int(event["turn_seq"]) for event in events if event.get("turn_seq")}
            ),
            latency_ms=round(latency_ms, 3),
            final_answer=_final_answer(events),
            event_slices=list(slices),
            events_path=events_relative,
            observer_path=observer_relative,
            fault=fault,
            warmup=plan.warmup,
            action_log=action_log,
            error=error,
        )
        self._events[trial.trial_id] = [dict(event) for event in events]
        self._observers[trial.trial_id] = snapshot
        if error:
            self._errors.append(
                {
                    "scope": trial.trial_id,
                    "status": status,
                    "error": error,
                    "invalid_reason": invalid_reason,
                }
            )
        return trial

    def _invalid_record(
        self,
        sample: EvalSample,
        plan: TrialPlan,
        reason: str,
        *,
        started: float | None = None,
        fault: dict[str, Any] | None = None,
    ) -> TrialRecord:
        self._errors.append(
            {"scope": plan.trial_id, "status": "invalid", "error": reason}
        )
        return TrialRecord(
            run_id=self.run_id,
            trial_id=plan.trial_id,
            sample_key=sample.sample_key,
            metric_group=sample.metric_group,
            variant=plan.variant,
            replicate=plan.replicate,
            status="invalid",
            invalid_reason=reason,
            latency_ms=round((time.perf_counter() - started) * 1000, 3)
            if started
            else 0.0,
            fault=fault,
            warmup=plan.warmup,
        )

    def _append_invalid(
        self, sample: EvalSample, reason: str, *, account: str = "A"
    ) -> None:
        plan = TrialPlan(sample.sample_key, sample.metric_group, account=account)
        self._append_trial(self._invalid_record(sample, plan, reason))

    def _append_trial(self, trial: TrialRecord) -> None:
        for index, existing in enumerate(self._trials):
            if existing.trial_id != trial.trial_id:
                continue
            self._trials[index] = trial
            self._write_jsonl("trials.jsonl", (item.to_dict() for item in self._trials))
            return
        self._trials.append(trial)
        self._append_jsonl("trials.jsonl", trial.to_dict())

    def _build_application(
        self, variant: str, *, namespace: str, account: str = "A"
    ) -> Any:
        state_root = (
            self.output_dir / "state" / namespace
            if namespace == "M2-B"
            else self.output_dir / "state" / namespace / account
        )
        config = derive_runtime_config(
            self.config_path,
            variant=variant,
            state_root=state_root,
            account=account,
        )
        application = build_live_application(config)
        self._applications.append(application)
        self._application_specs[id(application)] = {
            "config_variant": variant,
            "namespace": namespace,
            "account": account,
        }
        return application

    def _remember_context_chain(
        self,
        application: Any,
        plan: TrialPlan,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        """Remember the latest naturally compacted measured session for REC-11."""

        if plan.warmup:
            return
        compactions = [
            event
            for event in events
            if str((event.get("data") or {}).get("name") or "") == "auto_compact"
        ]
        if not compactions:
            return
        spec = self._application_specs.get(id(application))
        if not spec or application.scope is None:
            return
        self._context_chain = {
            **spec,
            "session_id": application.scope.session_id,
            "sample_key": plan.sample_key,
            "trial_variant": plan.variant,
            "auto_compact_observed": True,
            "compaction_count": len(compactions),
        }

    def _create_or_new_session(self, application: Any, account: str) -> None:
        if application.scope is None:
            self._create_session(application, account)
        else:
            application.new_session()

    def _create_session(self, application: Any, account: str) -> None:
        user = self._account_info[account]
        repository = self._repository_info[account]
        application.create_session(
            authenticated_user_id=int(user["id"]),
            repository_id=int(repository["id"]),
            repository_full_name=str(repository["full_name"]),
        )

    def _grade(self) -> list[DeterministicResult]:
        access = _access_levels(
            Path(__file__).resolve().parents[1] / "capabilities.yaml"
        )
        by_sample: dict[str, list[TrialRecord]] = defaultdict(list)
        for trial in self._trials:
            by_sample[trial.sample_key].append(trial)
        return [
            grade_sample(
                sample,
                by_sample.get(sample.sample_key, ()),
                self._events,
                self._observers,
                access_levels=access,
                performance_repetitions=self.repetitions,
            )
            for sample in self.samples
        ]

    def _load_existing(self) -> None:
        if not self.resume:
            return
        manifest_path = self.output_dir / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._validate_resume_manifest(manifest)
            self._manifest = dict(manifest)
            self.run_id = str(manifest.get("run_id") or self.run_id)
        for row in read_jsonl(self.output_dir / "trials.jsonl"):
            trial = TrialRecord.from_dict(row)
            self._trials.append(trial)
            if trial.events_path:
                self._events[trial.trial_id] = read_jsonl(
                    self.output_dir / trial.events_path
                )
            if (
                trial.observer_path
                and (self.output_dir / trial.observer_path).is_file()
            ):
                raw = json.loads(
                    (self.output_dir / trial.observer_path).read_text(encoding="utf-8")
                )
                self._observers[trial.trial_id] = ObserverSnapshot.from_dict(raw)
        self._errors.extend(read_jsonl(self.output_dir / "errors.jsonl"))

    def _has_valid_trials(self, sample_key: str, variant: str, count: int) -> bool:
        return self._valid_trial_count(sample_key, variant) >= count

    def _valid_trial_count(self, sample_key: str, variant: str) -> int:
        return sum(
            trial.sample_key == sample_key
            and trial.variant == variant
            and not trial.warmup
            and trial.status in {"completed", "failed"}
            for trial in self._trials
        )

    def _has_warmup(self, sample_key: str, variant: str) -> bool:
        return any(
            trial.sample_key == sample_key and trial.variant == variant and trial.warmup
            for trial in self._trials
        )

    def _close_applications(self) -> None:
        for application in reversed(self._applications):
            try:
                application.close()
            except Exception as exc:  # noqa: BLE001
                self._record_error("application.close", exc, status="error")
            finally:
                self._application_specs.pop(id(application), None)
        self._applications.clear()

    def _sync_owned_resources(self) -> None:
        if self._fixture_manager is None or not self._manifest:
            return
        self._manifest["owned_resources"] = self._fixture_manager.manifest_resources()
        self._write_manifest()

    def _validate_resume_manifest(self, manifest: Mapping[str, Any]) -> None:
        expected = {
            "dataset_hash": _sha256(self.dataset_path),
            "base_config_hash": _sha256(self.config_path),
            "repetitions": self.repetitions,
            "selected_samples": [sample.sample_key for sample in self.samples],
        }
        mismatches = [
            key for key, value in expected.items() if manifest.get(key) != value
        ]
        if mismatches:
            raise ValueError(
                "resume configuration does not match manifest: " + ", ".join(mismatches)
            )

    def _remove_runtime_state(self) -> None:
        root = self.output_dir / "state"
        if root.exists():
            shutil.rmtree(root)

    def _write_manifest(self) -> None:
        if self._manifest:
            self._write_json("manifest.json", self._manifest)

    def _write_json(self, relative: str, value: Any) -> None:
        path = self.output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = self._sanitize(value)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                safe, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _write_jsonl(self, relative: str, values: Iterable[Mapping[str, Any]]) -> None:
        path = self.output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            json.dumps(
                self._sanitize(dict(value)),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            for value in values
        ]
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
        temporary.replace(path)

    def _append_jsonl(self, relative: str, value: Mapping[str, Any]) -> None:
        path = self.output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        row = json.dumps(
            self._sanitize(dict(value)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with path.open("a", encoding="utf-8") as stream:
            stream.write(row + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _sanitize(self, value: Any) -> Any:
        if self._redaction_store is None:
            return value
        return self._redaction_store.redact(value)

    def _all_secret_values(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.base_config.secret_values,
                    os.environ.get("GITAGENT_EVAL_GITHUB_TOKEN_B", ""),
                )
            )
        )

    def _record_error(self, scope: str, error: BaseException, *, status: str) -> None:
        self._errors.append(
            {
                "scope": scope,
                "status": status,
                "error": f"{type(error).__name__}: {error}",
            }
        )

    def _assert_reports_secret_free(self) -> None:
        secrets = [secret.encode() for secret in self._all_secret_values() if secret]
        paths = [
            *(self.output_dir / "events").glob("*.jsonl"),
            *(self.output_dir / "observer").glob("*.json"),
            *(
                self.output_dir / name
                for name in (
                    "manifest.json",
                    "trials.jsonl",
                    "deterministic-results.jsonl",
                    "metrics.json",
                    "judge-input.jsonl",
                    "errors.jsonl",
                )
            ),
        ]
        for path in paths:
            content = path.read_bytes() if path.is_file() else b""
            if any(secret in content for secret in secrets):
                raise RuntimeError(
                    f"secret_leak_detected:{path.relative_to(self.output_dir)}"
                )


class _EventTracker:
    def __init__(self, application: Any) -> None:
        self.starts: dict[str, tuple[Any, int]] = {}
        self.touch(application)

    def touch(self, application: Any) -> None:
        scope = application.scope
        if scope is None or scope.session_id in self.starts:
            return
        self.starts[scope.session_id] = (
            scope,
            application.sessions.event_log.last_seq(scope),
        )

    def collect(
        self, application: Any
    ) -> tuple[list[dict[str, Any]], list[EventSlice]]:
        self.touch(application)
        events: list[dict[str, Any]] = []
        slices: list[EventSlice] = []
        event_log = application.sessions.event_log
        for scope, after_seq in self.starts.values():
            end_seq = event_log.last_seq(scope)
            slices.append(EventSlice(scope.session_id, after_seq, end_seq))
            events.extend(
                _event_to_dict(event)
                for event in event_log.iter_events(scope, after_seq=after_seq)
                if event.seq <= end_seq
            )
        events.sort(
            key=lambda event: (
                str(event.get("time") or ""),
                str(event.get("session_id") or ""),
                int(event.get("seq") or 0),
            )
        )
        return events, slices


def finalize_run(run_dir: str | Path, judge_results: str | Path) -> dict[str, Any]:
    root = Path(run_dir).expanduser().resolve()
    metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    results = [
        DeterministicResult.from_dict(row)
        for row in read_jsonl(root / "deterministic-results.jsonl")
    ]
    judge = read_jsonl(judge_results)
    final = finalize_metrics(metrics, results, judge)
    path = root / "final-metrics.json"
    path.write_text(
        json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return final


def _event_to_dict(event: Any) -> dict[str, Any]:
    return {
        "v": event.version,
        "seq": event.seq,
        "type": event.type,
        "time": event.time,
        "session_id": event.session_id,
        "turn_seq": event.turn_seq,
        "agent": event.agent,
        "data": event.data,
    }


def _final_answer(events: Sequence[Mapping[str, Any]]) -> str:
    return next(
        (
            str((event.get("data") or {}).get("content") or "")
            for event in reversed(events)
            if event.get("type") == "assistant_message"
        ),
        "",
    )


def _access_levels(path: Path) -> dict[str, str]:
    catalog = CapabilityCatalog.from_file(path)
    values = {item.id: item.access.value for item in catalog.capabilities}
    values["rag.eval-rag"] = "READ"
    return values


def _is_infrastructure(error: BaseException) -> bool:
    if type(error).__name__ in _INFRASTRUCTURE_NAMES:
        return True
    text = str(error).casefold()
    return any(
        term in text
        for term in ("timeout", "rate limit", "connection", "network", "unavailable")
    )


def _active_scope(application: Any) -> Any:
    scope = application.scope
    if scope is None:
        raise ValueError("eval control action requires an active Session")
    return scope


def _expected_turn_error(sample: EvalSample, raw: str) -> bool:
    return raw.strip() == _APPROVE_INPUT and sample.id in {
        "SAFE-12",
        "SAFE-14",
        "REC-05",
        "REC-06",
        "REC-07",
    }


def _execution_mode_input(sample: EvalSample, variant: str, raw: str) -> str:
    """Add one concise execution-mode sentence to neutral M3/M4 task text."""

    if sample.metric_group not in {"M3", "M4"} or variant not in {"serial", "parallel"}:
        return raw
    if raw.startswith("<") or raw.startswith("/"):
        return raw
    if sample.metric_group == "M3":
        mode = (
            "执行方式：串行。下面的独立仓库操作请逐个执行。"
            if variant == "serial"
            else "执行方式：并行。下面的独立仓库操作请同时发起，完成后统一汇总。"
        )
    else:
        mode = (
            "执行方式：串行。下面的独立 Agent 子任务请逐个委派。"
            if variant == "serial"
            else "执行方式：并行。下面的独立 Agent 子任务请在同一轮发起，完成后统一汇总。"
        )
    return f"{mode}\n{raw}"


def _action_name(raw: str) -> str:
    text = raw.strip()
    if text.startswith("<EVAL_MEMORY_"):
        directive = text.partition(">")[0].removeprefix("<")
        return directive.split(maxsplit=1)[0].casefold()
    if not text.startswith("/"):
        return "handle"
    return text.split(maxsplit=1)[0].removeprefix("/").casefold()


def _action_result_summary(raw: str, result: Any) -> dict[str, Any] | None:
    """Keep only bounded eval-control evidence; never persist Memory bodies."""

    text = raw.strip()
    if text.startswith("<EVAL_MEMORY_SEARCH>"):
        hits: list[dict[str, Any]] = []
        if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
            for item in result:
                name = str(getattr(item, "name", "") or "")
                if not name:
                    continue
                score = getattr(item, "score", None)
                hits.append(
                    {
                        "name": name,
                        "scope": str(getattr(item, "scope", "") or ""),
                        "stale": bool(getattr(item, "stale", False)),
                        "score": round(float(score), 6)
                        if isinstance(score, (int, float)) and not isinstance(score, bool)
                        else None,
                    }
                )
        return {"hits": hits}
    if text == "/sessions":
        values = (
            result
            if isinstance(result, Sequence)
            and not isinstance(result, (str, bytes, bytearray))
            else ()
        )
        return {"session_ids": [str(getattr(item, "session_id", "")) for item in values]}
    if text == "/new" or text.startswith("/switch "):
        session_id = str(getattr(result, "session_id", "") or "")
        return {"session_id": session_id} if session_id else None
    return None


def _trial_slug(trial_id: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_." else "__"
        for character in trial_id
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def _git_worktree_clean(root: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return not completed.stdout.strip()


def _endpoint_identifier(base_url: str | None) -> str:
    if not base_url:
        return "default"
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}".rstrip("/")


def _identity_hash(account: Mapping[str, Any] | None) -> str | None:
    if not account:
        return None
    return hashlib.sha256(str(account.get("id") or "").encode()).hexdigest()
