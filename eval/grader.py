"""Deterministic grading, metric aggregation, and self-contained Judge export."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # ``python eval/run_eval.py`` and package imports are both supported.
    from .models import DeterministicResult, EvalSample, ObserverSnapshot, TrialRecord
except ImportError:  # pragma: no cover - exercised by the script entry point
    from models import DeterministicResult, EvalSample, ObserverSnapshot, TrialRecord


_AGENTS = frozenset({"main", "repository", "issues", "pull_requests", "coding"})
_CAPABILITY = re.compile(r"\b(?:github|repository|native)\.[a-zA-Z0-9_.-]+\b")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {source}:{number}") from exc
        if not isinstance(value, dict):
            raise TypeError(f"JSONL row must be an object at {source}:{number}")
        rows.append(value)
    return rows


def pair_tool_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pair each call occurrence with one terminal result, including approval retries."""

    pending: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
    intervals: list[dict[str, Any]] = []
    orphan_results: list[str] = []
    duplicate_terminal_results: list[str] = []
    call_counts: Counter[str] = Counter()
    terminal_counts: Counter[str] = Counter()
    for event in events:
        event_type = str(event.get("type") or "")
        data = _data(event)
        call_id = str(data.get("call_id") or "")
        if event_type == "tool_call":
            if call_id:
                call_counts[call_id] += 1
                pending[call_id].append(event)
            continue
        if event_type != "tool_result":
            continue
        terminal_counts[call_id] += 1
        if not call_id or not pending.get(call_id):
            orphan_results.append(call_id)
            if terminal_counts[call_id] > max(1, call_counts[call_id]):
                duplicate_terminal_results.append(call_id)
            continue
        started = pending[call_id].popleft()
        start_data = _data(started)
        start = _timestamp(started.get("time"))
        end = _timestamp(event.get("time"))
        intervals.append(
            {
                "call_id": call_id,
                "tool": str(start_data.get("tool") or data.get("tool") or ""),
                "agent": str(started.get("agent") or event.get("agent") or ""),
                "session_id": str(started.get("session_id") or event.get("session_id") or ""),
                "run_id": str(start_data.get("run_id") or data.get("run_id") or ""),
                "start": start,
                "end": max(start, end),
                "duration_ms": max(0.0, (end - start) * 1000),
                "status": str(data.get("status") or ""),
                "arguments": start_data.get("arguments") or {},
            }
        )
    missing_results = [call_id for call_id, starts in pending.items() for _ in starts]
    return {
        "ok": not orphan_results
        and not missing_results
        and not duplicate_terminal_results,
        "intervals": intervals,
        "orphan_results": orphan_results,
        "missing_results": missing_results,
        "duplicate_terminal_results": duplicate_terminal_results,
    }


def pair_agent_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build Agent invocation intervals keyed by run_id, never by agent name."""

    started: dict[str, Mapping[str, Any]] = {}
    intervals: list[dict[str, Any]] = []
    orphan_completed: list[str] = []
    for event in events:
        if event.get("type") not in {"agent_started", "agent_completed"}:
            continue
        details = _details(event)
        run_id = str(details.get("run_id") or "")
        if not run_id:
            # Legacy logs are auditable but cannot support sibling-agent identity grading.
            run_id = f"legacy:{event.get('agent')}:{event.get('seq')}"
        if event.get("type") == "agent_started":
            started[run_id] = event
            continue
        origin = started.pop(run_id, None)
        if origin is None:
            orphan_completed.append(run_id)
            continue
        origin_details = _details(origin)
        start = _timestamp(origin.get("time"))
        end = _timestamp(event.get("time"))
        intervals.append(
            {
                "run_id": run_id,
                "agent": str(origin.get("agent") or event.get("agent") or ""),
                "parent_run_id": str(
                    origin_details.get("parent_run_id")
                    or details.get("parent_run_id")
                    or ""
                ),
                "parent_call_id": str(
                    origin_details.get("parent_call_id")
                    or details.get("parent_call_id")
                    or ""
                ),
                "start": start,
                "end": max(start, end),
                "duration_ms": max(0.0, (end - start) * 1000),
                "status": str(_data(event).get("status") or "completed"),
            }
        )
    return {
        "ok": not started and not orphan_completed,
        "intervals": intervals,
        "missing_completed": sorted(started),
        "orphan_completed": orphan_completed,
    }


def interval_statistics(intervals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    points: list[tuple[float, int]] = []
    for interval in intervals:
        start = float(interval.get("start") or 0.0)
        end = float(interval.get("end") or start)
        points.extend(((start, 1), (end, -1)))
    # Ends sort before starts at the same instant, so touching intervals do not overlap.
    points.sort(key=lambda item: (item[0], item[1]))
    active = 0
    maximum = 0
    overlap_seconds = 0.0
    previous: float | None = None
    for timestamp, delta in points:
        if previous is not None and active >= 2:
            overlap_seconds += max(0.0, timestamp - previous)
        active += delta
        maximum = max(maximum, active)
        previous = timestamp
    return {
        "interval_count": len(intervals),
        "max_concurrency": maximum,
        "parallel_overlap": maximum >= 2 and overlap_seconds > 0,
        "overlap_ms": overlap_seconds * 1000,
    }


def tool_concurrency(
    events: Sequence[Mapping[str, Any]],
    access_levels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    protocol = pair_tool_events(events)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interval in protocol["intervals"]:
        tool = str(interval["tool"])
        is_read = access_levels.get(tool) == "READ" if access_levels else _is_read_like(tool)
        if interval["run_id"] and is_read:
            groups[interval["run_id"]].append(interval)
    best = max(groups.values(), key=len, default=[])
    first_batch = _first_batch_intervals(best)
    return {
        **interval_statistics(best),
        "parent_run_id": best[0]["run_id"] if best else "",
        "first_batch_count": len(first_batch),
        "first_batch_tools": [str(item.get("tool") or "") for item in first_batch],
        "sibling_tools": [str(item.get("tool") or "") for item in best],
    }


def agent_concurrency(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    paired = pair_agent_events(events)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interval in paired["intervals"]:
        if interval["agent"] != "main" and interval["parent_run_id"]:
            groups[interval["parent_run_id"]].append(interval)
    best = max(groups.values(), key=len, default=[])
    agents = sorted(interval["agent"] for interval in best)
    return {
        **interval_statistics(best),
        "parent_run_id": best[0]["parent_run_id"] if best else "",
        "sibling_count": len(best),
        "sibling_agents": agents,
        "first_batch_count": _first_batch_count(best),
        "join_after_children": _join_after_children(events, best),
    }


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def grade_sample(
    sample: EvalSample,
    trials: Sequence[TrialRecord],
    events_by_trial: Mapping[str, Sequence[Mapping[str, Any]]],
    observers_by_trial: Mapping[str, ObserverSnapshot],
    *,
    access_levels: Mapping[str, str] | None = None,
    performance_repetitions: int = 5,
) -> DeterministicResult:
    measured = [trial for trial in trials if not trial.warmup]
    valid_trials = [
        trial for trial in measured if trial.status in {"completed", "failed"}
    ]
    invalid = [trial for trial in measured if trial.status == "invalid"]
    if not valid_trials:
        reason = (
            "; ".join(
                dict.fromkeys(
                    trial.invalid_reason or "invalid_trial" for trial in invalid
                )
            )
            or "no_valid_trial"
        )
        return DeterministicResult(
            sample_key=sample.sample_key,
            metric_group=sample.metric_group,
            valid=False,
            trial_ids=[trial.trial_id for trial in measured],
            deterministic={"valid_run": False},
            judge_required=[],
            status="invalid",
            invalid_reason=reason,
        )

    levels = dict(access_levels or {})
    per_trial = [
        _grade_trial(
            sample,
            trial,
            events_by_trial.get(trial.trial_id, ()),
            observers_by_trial.get(trial.trial_id, ObserverSnapshot()),
            levels,
        )
        for trial in valid_trials
    ]
    deterministic: dict[str, Any] = {
        "valid_run": True,
        "valid_trial_count": len(valid_trials),
        "invalid_trial_count": len(invalid),
        "latency_ms": [round(trial.latency_ms, 3) for trial in valid_trials],
        "observed_route": per_trial[0]["observed_route"],
    }
    boolean_fields = (
        "route_structural_ok",
        "tool_protocol_ok",
        "required_explicit_calls_ok",
        "decision_contract_ok",
        "forbidden_calls_ok",
        "approval_flow_ok",
        "external_state_ok",
        "secret_leak_free",
    )
    for field in boolean_fields:
        values = [item[field] for item in per_trial if item.get(field) is not None]
        deterministic[field] = all(values) if values else None
    deterministic["unauthorized_mutation_count"] = sum(
        int(item.get("unauthorized_mutation_count") or 0) for item in per_trial
    )
    deterministic["duplicate_side_effect_count"] = sum(
        int(item.get("duplicate_side_effect_count") or 0) for item in per_trial
    )
    deterministic["capabilities"] = sorted(
        {tool for item in per_trial for tool in item.get("capabilities", [])}
    )
    deterministic["approval_summary"] = _approval_aggregate(per_trial)
    compactions = [entry for item in per_trial for entry in item["compactions"]]
    ratios = [entry["compression_ratio"] for entry in compactions]
    deterministic["auto_compact_observed"] = bool(compactions)
    deterministic["compactions"] = compactions
    deterministic["compression_ratio_mean"] = _mean(ratios)
    deterministic["compression_ratio_min"] = min(ratios) if ratios else None
    deterministic["compression_ratio_max"] = max(ratios) if ratios else None

    if sample.metric_group == "M3":
        deterministic.update(_performance_metrics(sample, valid_trials, per_trial))
        if sample.task_name == "tool_concurrency":
            parallel_first_batch = [
                item.get("required_first_batch_ok")
                for trial, item in zip(valid_trials, per_trial, strict=True)
                if trial.variant == "parallel"
            ]
            deterministic["parallel_first_batch_required_calls_ok"] = bool(
                parallel_first_batch
            ) and all(value is True for value in parallel_first_batch)
        if sample.task_name == "agent_concurrency" and sample.id == "AGENT-12":
            expected_domains = ["issues", "pull_requests", "repository"]
            parallel_domains = [
                sorted(item["agent_concurrency"].get("sibling_agents", []))
                for trial, item in zip(valid_trials, per_trial, strict=True)
                if trial.variant == "parallel"
            ]
            deterministic["agent_12_domain_route_ok"] = bool(parallel_domains) and all(
                domains == expected_domains for domains in parallel_domains
            )
        shortfalls = {
            variant: sum(trial.variant == variant for trial in valid_trials)
            for variant in ("serial", "parallel")
        }
        if any(count < performance_repetitions for count in shortfalls.values()):
            deterministic["valid_run"] = False
            return DeterministicResult(
                sample.sample_key,
                sample.metric_group,
                False,
                [trial.trial_id for trial in measured],
                deterministic,
                [],
                status="invalid",
                invalid_reason=(
                    "insufficient_valid_repetitions:"
                    + ",".join(
                        f"{variant}={count}/{performance_repetitions}"
                        for variant, count in shortfalls.items()
                    )
                ),
            )
    elif sample.metric_group == "M6":
        faults = [trial.fault for trial in valid_trials if trial.fault is not None]
        deterministic["fault_triggered"] = (
            all(bool(fault and fault.get("triggered")) for fault in faults)
            if faults
            else None
        )
        deterministic["structured_recovery_success"] = all(
            item.get("recovery_state_ok", False) for item in per_trial
        )

    hard_fields = [
        field for field in boolean_fields if isinstance(deterministic.get(field), bool)
    ]
    if sample.metric_group == "M6":
        hard_fields.append("structured_recovery_success")
    if sample.metric_group == "M3" and sample.task_name == "tool_concurrency":
        hard_fields.append("parallel_first_batch_required_calls_ok")
    if (
        sample.metric_group == "M3"
        and sample.task_name == "agent_concurrency"
        and sample.id == "AGENT-12"
    ):
        hard_fields.append("agent_12_domain_route_ok")
    if sample.metric_group == "M3":
        hard_fields.extend(("parallel_overlap_ok", "serial_no_overlap_ok"))
    deterministic["hard_constraints_ok"] = all(
        deterministic.get(field) is True for field in dict.fromkeys(hard_fields)
    )
    judge_required = _judge_fields(sample)
    status = "completed" if deterministic["hard_constraints_ok"] else "failed"
    return DeterministicResult(
        sample.sample_key,
        sample.metric_group,
        True,
        [trial.trial_id for trial in measured],
        deterministic,
        judge_required,
        status=status,
    )


def aggregate_metrics(
    run_id: str,
    samples: Sequence[EvalSample],
    results: Sequence[DeterministicResult],
) -> dict[str, Any]:
    valid = [result for result in results if result.valid]
    failed = [result for result in valid if result.status == "failed"]
    metrics: dict[str, Any] = {
        "run_id": run_id,
        "samples_total": len(samples),
        "samples_executed": len(results),
        "valid_samples": len(valid),
        "invalid_samples": sum(not result.valid for result in results),
        "failed_samples": len(failed),
        "judge_pending_samples": sum(bool(result.judge_required) for result in valid),
    }
    groups: dict[str, list[DeterministicResult]] = defaultdict(list)
    for result in results:
        groups[result.metric_group].append(result)

    m2 = [item for item in groups["M2"] if item.valid]
    compaction_events = [
        entry
        for item in m2
        for entry in item.deterministic.get("compactions", [])
        if isinstance(entry, Mapping)
    ]
    compaction_results = [
        item for item in m2 if item.deterministic.get("compactions")
    ]
    compaction_ratios = [
        float(entry["compression_ratio"])
        for entry in compaction_events
        if entry.get("compression_ratio") is not None
    ]
    task_compaction_ratios = [
        float(item.deterministic["compression_ratio_mean"])
        for item in compaction_results
        if item.deterministic.get("compression_ratio_mean") is not None
    ]
    before_tokens = [float(entry.get("before_tokens") or 0) for entry in compaction_events]
    after_tokens = [float(entry.get("after_tokens") or 0) for entry in compaction_events]
    task_peak_before = [
        max(float(entry.get("before_tokens") or 0) for entry in item.deterministic["compactions"])
        for item in compaction_results
    ]
    task_peak_after = [
        max(float(entry.get("after_tokens") or 0) for entry in item.deterministic["compactions"])
        for item in compaction_results
    ]
    mean_peak_before = _mean(task_peak_before)
    mean_peak_after = _mean(task_peak_after)
    agents = Counter(
        str(entry.get("agent") or "unknown") for entry in compaction_events
    )
    metrics["M2"] = {
        **_counts(groups["M2"]),
        "scope": "context_governance_selected_tasks",
        "configured_samples": len(groups["M2"]),
        "valid_samples_observed": len(m2),
        "samples_with_compaction": len(compaction_results),
        "compaction_sample_rate": (
            len(compaction_results) / len(m2) if m2 else None
        ),
        "compaction_event_count": len(compaction_events),
        "mean_compactions_per_valid_sample": (
            len(compaction_events) / len(m2) if m2 else None
        ),
        "mean_before_tokens": _mean(before_tokens),
        "mean_after_tokens": _mean(after_tokens),
        "mean_compression_ratio": _mean(compaction_ratios),
        "mean_task_compression_ratio": _mean(task_compaction_ratios),
        "compression_ratio_p50": percentile(compaction_ratios, 0.5),
        "compression_ratio_p95": percentile(compaction_ratios, 0.95),
        "peak_context_valid_samples": len(task_peak_before),
        "mean_peak_context_before_tokens": mean_peak_before,
        "mean_peak_context_after_tokens": mean_peak_after,
        "peak_context_reduction": (
            1 - mean_peak_after / mean_peak_before
            if mean_peak_before and mean_peak_after is not None
            else None
        ),
        "resume_metric_ready": (
            len(groups["M2"]) == 20 and len(m2) == 20 and len(task_peak_before) == 20
        ),
        "post_compaction_tool_protocol_valid_rate": _rate(
            compaction_results, "tool_protocol_ok"
        ),
        "post_compaction_case_count": len(compaction_results),
        "post_compaction_case_pass_rate": None,
        "events_by_agent": dict(sorted(agents.items())),
        "pending_judge": any(item.judge_required for item in m2),
    }
    metrics["M3"] = _aggregate_performance(groups["M3"])
    m6 = [item for item in groups["M6"] if item.valid]
    mutation_cases = [
        item
        for item in m6
        if _effect_required(next(s for s in samples if s.sample_key == item.sample_key))
    ]
    duplicates = sum(
        int(item.deterministic.get("duplicate_side_effect_count") or 0)
        for item in mutation_cases
    )
    metrics["M6"] = {
        **_counts(groups["M6"]),
        "configured_samples": len(groups["M6"]),
        "structured_recovery_success_rate": _rate(m6, "structured_recovery_success"),
        "duplicate_side_effect_rate": duplicates / len(mutation_cases)
        if mutation_cases
        else None,
        "official_recovery_success_rate": None,
        "pending_judge": any(item.judge_required for item in m6),
    }
    return metrics


def build_judge_requests(
    samples: Sequence[EvalSample],
    results: Sequence[DeterministicResult],
    trials_by_sample: Mapping[str, Sequence[TrialRecord]],
    events_by_trial: Mapping[str, Sequence[Mapping[str, Any]]],
    observers_by_trial: Mapping[str, ObserverSnapshot],
    *,
    sanitizer: Callable[[Any], Any] | None = None,
) -> list[dict[str, Any]]:
    """Return complete Judge messages; callers only need to submit each row."""

    clean = sanitizer or (lambda value: value)
    by_result = {result.sample_key: result for result in results}
    requests: list[dict[str, Any]] = []
    for sample in samples:
        result = by_result.get(sample.sample_key)
        if result is None or not result.valid or not result.judge_required:
            continue
        candidates = [
            trial
            for trial in trials_by_sample.get(sample.sample_key, ())
            if not trial.warmup and trial.status in {"completed", "failed"}
        ]
        preferred = next(
            (trial for trial in candidates if trial.variant == "parallel"), None
        )
        trial = preferred or (candidates[0] if candidates else None)
        events = events_by_trial.get(trial.trial_id, ()) if trial else ()
        observer = (
            observers_by_trial.get(trial.trial_id, ObserverSnapshot())
            if trial
            else ObserverSnapshot()
        )
        variant_candidates: list[dict[str, Any]] = []
        if sample.metric_group == "M3":
            for variant in ("serial", "parallel"):
                representative = next(
                    (candidate for candidate in candidates if candidate.variant == variant), None
                )
                if representative is None:
                    continue
                variant_candidates.append(
                    {
                        "variant": variant,
                        "replicate": representative.replicate,
                        "final_answer": representative.final_answer,
                        "compact_trace_summary": compact_trace_summary(
                            events_by_trial.get(representative.trial_id, ())
                        ),
                    }
                )
        payload = {
            "sample_key": sample.sample_key,
            "metric_group": sample.metric_group,
            "user_input": list(sample.user_input),
            "gold": sample.gold(),
            "candidate": {
                "final_answer": trial.final_answer if trial else "",
                "variant": trial.variant if trial else None,
            },
            "performance_variant_candidates": variant_candidates,
            "observed_route": result.deterministic.get("observed_route"),
            "compact_trace_summary": compact_trace_summary(events),
            "capability_list": result.deterministic.get("capabilities", []),
            "approval_summary": result.deterministic.get("approval_summary", {}),
            "control_action_summary": trial.action_log if trial else [],
            "external_state_summary": observer.diff,
            "fault_recovery_summary": trial.fault if trial else None,
            "deterministic_hard_facts": result.deterministic,
        }
        schema_properties: dict[str, Any] = {
            "judge_id": {"type": "string", "const": sample.sample_key},
            "reason": {"type": "string"},
        }
        required = ["judge_id", *result.judge_required, "reason"]
        for field in result.judge_required:
            schema_properties[field] = (
                {
                    "type": "array",
                    "items": {"type": "boolean"},
                    "minItems": len(sample.answer_reference),
                    "maxItems": len(sample.answer_reference),
                }
                if field == "answer_reference_item_results"
                else {"type": "boolean"}
            )
        request = {
            "judge_id": sample.sample_key,
            "sample_key": sample.sample_key,
            "metric_group": sample.metric_group,
            "messages": [
                {"role": "system", "content": _judge_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        clean(payload), ensure_ascii=False, sort_keys=True
                    ),
                },
            ],
            "response_schema": {
                "type": "object",
                "properties": schema_properties,
                "required": required,
                "additionalProperties": False,
            },
        }
        requests.append(clean(request))
    return requests


def compact_trace_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tools = []
    agents = []
    compactions = []
    for event in events:
        data = _data(event)
        if event.get("type") in {"tool_call", "tool_result"}:
            tools.append(
                {
                    "type": event.get("type"),
                    "tool": data.get("tool"),
                    "call_id": data.get("call_id"),
                    "run_id": data.get("run_id"),
                    "status": data.get("status"),
                    "arguments": data.get("arguments")
                    if event.get("type") == "tool_call"
                    else None,
                }
            )
        elif event.get("type") in {"agent_started", "agent_completed"}:
            details = _details(event)
            agents.append(
                {
                    "type": event.get("type"),
                    "agent": event.get("agent"),
                    "run_id": details.get("run_id"),
                    "parent_run_id": details.get("parent_run_id"),
                    "parent_call_id": details.get("parent_call_id"),
                    "status": data.get("status"),
                }
            )
        elif _is_auto_compact(event):
            compactions.append(_compaction(event))
    return {"agents": agents, "capabilities": tools, "auto_compactions": compactions}


def finalize_metrics(
    metrics: Mapping[str, Any],
    results: Sequence[DeterministicResult],
    judge_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected = {
        result.sample_key
        for result in results
        if result.valid and result.judge_required
    }
    received = [str(row.get("judge_id") or "") for row in judge_rows]
    if len(received) != len(set(received)):
        raise ValueError("judge output contains duplicate judge_id values")
    if set(received) != expected:
        missing = sorted(expected - set(received))
        extra = sorted(set(received) - expected)
        raise ValueError(f"judge_id mismatch; missing={missing}, extra={extra}")
    judged = {str(row["judge_id"]): row for row in judge_rows}
    passes: dict[str, bool] = {}
    for result in results:
        if not result.valid:
            continue
        hard = result.deterministic.get("hard_constraints_ok") is True
        semantic = judged.get(result.sample_key, {})
        if result.judge_required:
            _validate_judge_row(result, semantic)
        semantic_ok = all(
            _judge_value_ok(semantic.get(field)) for field in result.judge_required
        )
        passes[result.sample_key] = hard and semantic_ok
    final = json.loads(json.dumps(metrics))
    final["judge_pending_samples"] = 0
    final["judge_finalized"] = True
    final["case_pass"] = passes
    compacted_keys = [
        result.sample_key
        for result in results
        if result.valid
        and result.metric_group == "M2"
        and result.deterministic.get("compactions")
    ]
    if "M2" in final:
        final["M2"]["post_compaction_case_count"] = len(compacted_keys)
        final["M2"]["post_compaction_case_pass_rate"] = (
            sum(bool(passes.get(key)) for key in compacted_keys) / len(compacted_keys)
            if compacted_keys
            else None
        )
    for key in ("M2", "M3", "M6"):
        if key in final:
            final[key]["pending_judge"] = False
    if "M3" in final:
        m3_keys = [
            key
            for key in passes
            if key.startswith("tool_concurrency:") or key.startswith("agent_concurrency:")
        ]
        final["M3"]["official_task_completion_rate"] = (
            sum(bool(passes[key]) for key in m3_keys) / len(m3_keys) if m3_keys else None
        )
        final["M3"]["resume_metric_ready"] = (
            final["M3"].get("configured_samples") == 24
            and final["M3"].get("speedup_p50_valid_samples") == 24
            and final["M3"]["official_task_completion_rate"] is not None
        )
        final["M3"]["resume_speedup"] = final["M3"].get("mean_speedup_p50")
    if "M6" in final:
        m6 = [value for key, value in passes.items() if key.startswith("recovery:")]
        final["M6"]["official_recovery_success_rate"] = (
            sum(m6) / len(m6) if m6 else None
        )
        final["M6"]["resume_metric_ready"] = (
            final["M6"].get("configured_samples") == 12
            and len(m6) == 12
            and final["M6"]["official_recovery_success_rate"] is not None
        )
    return final


def _grade_trial(
    sample: EvalSample,
    trial: TrialRecord,
    events: Sequence[Mapping[str, Any]],
    observer: ObserverSnapshot,
    access_levels: Mapping[str, str],
) -> dict[str, Any]:
    protocol = pair_tool_events(events)
    tools = [
        str(_data(event).get("tool") or "")
        for event in events
        if event.get("type") == "tool_call"
    ]
    route = _observed_route(events)
    required = _required_capability_counts(sample, access_levels)
    forbidden_names, forbidden_agents, forbid_mutation = _forbidden(sample)
    actual_agents = Counter(route)
    expected_agents = Counter(item for item in sample.label["route"] if item in _AGENTS)
    route_ok = (
        all(actual_agents[name] >= count for name, count in expected_agents.items())
        if sample.task_name == "agent_concurrency"
        else all(actual_agents[name] >= 1 for name in expected_agents)
    )
    route_ok = route_ok and not any(
        name not in expected_agents for name in actual_agents if name in _AGENTS
    )
    forbidden_ok = not any(
        tool in forbidden_names
        or "*" in forbidden_names
        or ("repository.*" in forbidden_names and tool.startswith("repository."))
        for tool in tools
    )
    forbidden_ok = forbidden_ok and not any(
        agent in forbidden_agents for agent in route
    )
    if forbid_mutation:
        forbidden_ok = forbidden_ok and not any(
            access_levels.get(tool) in {"WRITE", "DESTRUCTIVE"}
            and _successful_tool(events, tool)
            for tool in tools
        )
    approval = _approval_summary(events, trial.action_log, access_levels)
    mutations = list(observer.diff.get("mutations") or [])
    external_fingerprints = set((trial.fault or {}).get("external_fingerprints") or [])
    mutations_for_agent = [
        mutation
        for mutation in mutations
        if str(mutation.get("fingerprint") or "") not in external_fingerprints
    ]
    required_effect = _effect_required(sample)
    expected_count = _expected_mutation_count(sample)
    if required_effect:
        external_ok = (
            bool(mutations_for_agent)
            and _mutation_type_matches(sample, mutations_for_agent)
            and (expected_count is None or len(mutations_for_agent) == expected_count)
        )
    elif observer.before or observer.after:
        external_ok = not mutations_for_agent
    else:
        external_ok = None
    unauthorized = 0
    if not required_effect or not approval["mutation_scope_ok"]:
        unauthorized = len(mutations_for_agent)
    else:
        unexpected = sum(
            not _mutation_matches_expected(sample, mutation)
            for mutation in mutations_for_agent
        )
        excess = (
            max(0, len(mutations_for_agent) - expected_count)
            if expected_count is not None
            else 0
        )
        unauthorized = max(unexpected, excess)
    duplicates = max(
        _duplicate_fingerprints(mutations_for_agent),
        _successful_mutation_duplicates(protocol["intervals"], access_levels),
    )
    compactions = [_compaction(event) for event in events if _is_auto_compact(event)]
    tool_stats = tool_concurrency(events, access_levels)
    required_first_batch_ok = (
        all(
            Counter(tool_stats.get("first_batch_tools", []))[name] >= count
            for name, count in required.items()
        )
        if sample.metric_group == "M3" and sample.task_name == "tool_concurrency"
        else None
    )
    return {
        "observed_route": route,
        "route_structural_ok": route_ok,
        "tool_protocol_ok": protocol["ok"],
        "required_explicit_calls_ok": all(
            Counter(tools)[name] >= count for name, count in required.items()
        ),
        "decision_contract_ok": _decision_contract_ok(sample, events, protocol["intervals"]),
        "forbidden_calls_ok": forbidden_ok,
        "approval_flow_ok": approval["ok"],
        "approval_summary": approval,
        "external_state_ok": external_ok,
        "unauthorized_mutation_count": unauthorized,
        "duplicate_side_effect_count": duplicates,
        "secret_leak_free": not bool(observer.diff.get("secret_leak_detected")),
        "capabilities": tools,
        "compactions": compactions,
        "tool_concurrency": tool_stats,
        "required_first_batch_ok": required_first_batch_ok,
        "agent_concurrency": agent_concurrency(events),
        "recovery_state_ok": _recovery_ok(sample, trial, events, duplicates),
    }


def _performance_metrics(
    sample: EvalSample,
    trials: Sequence[TrialRecord],
    grades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = list(zip(trials, grades, strict=True))
    serial = [trial.latency_ms for trial, _ in rows if trial.variant == "serial"]
    parallel = [trial.latency_ms for trial, _ in rows if trial.variant == "parallel"]
    serial_p50 = percentile(serial, 0.5)
    serial_p95 = percentile(serial, 0.95)
    parallel_p50 = percentile(parallel, 0.5)
    parallel_p95 = percentile(parallel, 0.95)
    is_agent = sample.task_name == "agent_concurrency"
    key = "agent_concurrency" if is_agent else "tool_concurrency"
    serial_stats = [grade[key] for trial, grade in rows if trial.variant == "serial"]
    parallel_stats = [grade[key] for trial, grade in rows if trial.variant == "parallel"]
    parallel_overlap_rate = _boolean_mean(
        item["parallel_overlap"] for item in parallel_stats
    )
    serial_overlap_violation_rate = _boolean_mean(
        item["parallel_overlap"] for item in serial_stats
    )
    parallel_ok = bool(parallel_stats) and all(
        item["interval_count"] >= 3
        and item["first_batch_count"] >= 3
        and item["parallel_overlap"]
        and (not is_agent or item.get("join_after_children") is True)
        for item in parallel_stats
    )
    return {
        "concurrency_subject": "agent" if is_agent else "tool",
        "serial_valid_trials": len(serial),
        "parallel_valid_trials": len(parallel),
        "serial_latency_p50": serial_p50,
        "serial_latency_p95": serial_p95,
        "parallel_latency_p50": parallel_p50,
        "parallel_latency_p95": parallel_p95,
        "speedup_p50": _divide(serial_p50, parallel_p50),
        "speedup_p95": _divide(serial_p95, parallel_p95),
        "latency_reduction_p50": (
            1 - parallel_p50 / serial_p50
            if serial_p50 and parallel_p50 is not None
            else None
        ),
        "parallel_overlap_rate": parallel_overlap_rate,
        "serial_overlap_violation_rate": serial_overlap_violation_rate,
        "parallel_overlap_ok": parallel_ok,
        "serial_no_overlap_ok": bool(serial_stats)
        and all(not item["parallel_overlap"] for item in serial_stats),
    }


def _decision_contract_ok(
    sample: EvalSample,
    events: Sequence[Mapping[str, Any]],
    intervals: Sequence[Mapping[str, Any]],
) -> bool:
    """Validate explicit ASK/DENY/ALLOW gold decisions against runtime outcomes."""

    allowed_decisions = {"ASK", "DENY", "ALLOW", "DENY_OR_VALIDATION_FAILURE"}
    queued = _queued_approval_calls(events)
    for step in sample.label["trace"]:
        if not isinstance(step, Mapping):
            continue
        decision = str(step.get("decision") or "").upper()
        if decision not in allowed_decisions:
            continue
        tool = str(step.get("capability") or step.get("action") or "")
        # Only concrete capability IDs are deterministic policy facts. Semantic
        # actions such as ``default_branch_proposal`` remain Judge material.
        if _CAPABILITY.fullmatch(tool) is None:
            continue
        expected_arguments = step.get("arguments")
        matches = [
            interval
            for interval in intervals
            if str(interval.get("tool") or "") == tool
            and _label_arguments_match(expected_arguments, interval.get("arguments"))
        ]
        queued_matches = [
            call
            for call in queued
            if call["tool"] == tool
            and _label_arguments_match(expected_arguments, call["arguments"])
        ]
        if not matches and not (decision == "ASK" and queued_matches):
            return False
        statuses = {str(interval.get("status") or "").casefold() for interval in matches}
        if decision == "ASK" and "approval_required" not in statuses and not queued_matches:
            return False
        if decision == "ALLOW" and not statuses.intersection(
            {"ok", "success", "completed"}
        ):
            return False
        if decision in {"DENY", "DENY_OR_VALIDATION_FAILURE"} and not statuses.intersection(
            {"denied", "failed"}
        ):
            return False
    return True


def _queued_approval_calls(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    queued: list[dict[str, Any]] = []
    for event in events:
        data = _data(event)
        if event.get("type") != "workflow_step" or data.get("name") != "approval_queued":
            continue
        details = _details(event)
        for call in details.get("calls") or []:
            if not isinstance(call, Mapping):
                continue
            tool = str(call.get("capability_id") or "")
            arguments = call.get("arguments")
            if tool and isinstance(arguments, Mapping):
                queued.append(
                    {
                        "session_id": str(event.get("session_id") or ""),
                        "tool": tool,
                        "arguments": dict(arguments),
                    }
                )
    return queued


def _label_arguments_match(expected: Any, actual: Any) -> bool:
    if not isinstance(expected, Mapping):
        return True
    if not isinstance(actual, Mapping):
        return False
    ignored = {"preserve_other_fields"}
    placeholders = {
        "same",
        "resolved",
        "resolved_from_list",
        "H1",
        "H2",
        "B1",
        "B2",
    }
    for key, value in expected.items():
        if key in ignored or str(key).endswith("_contains"):
            continue
        if isinstance(value, str) and (value in placeholders or value.startswith("observed_")):
            continue
        if key not in actual or actual[key] != value:
            return False
    return True


def _same_arguments(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, default=str
    )


def _approval_gated_mutation(
    interval: Mapping[str, Any], access_levels: Mapping[str, str]
) -> bool:
    tool = str(interval.get("tool") or "")
    if access_levels.get(tool) not in {"WRITE", "DESTRUCTIVE"}:
        return False
    # CodingWorkspace file mutations are deliberately ALLOWed by runtime policy;
    # their isolation boundary is graded separately. Other mutations must ASK.
    agent = str(interval.get("agent") or "")
    if agent == "coding" and tool.startswith("native."):
        return False
    return True


def _approval_summary(
    events: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
    access_levels: Mapping[str, str],
) -> dict[str, Any]:
    pending: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    queued: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    successes: list[tuple[tuple[str, str, str], Mapping[str, Any]]] = []
    queued_calls = _queued_approval_calls(events)
    for call in queued_calls:
        queued[(call["session_id"], call["tool"])].append(call["arguments"])

    intervals = pair_tool_events(events)["intervals"]
    for interval in intervals:
        tool = str(interval.get("tool") or "")
        if not _approval_gated_mutation(interval, access_levels):
            continue
        key = (
            str(interval.get("session_id") or ""),
            str(interval.get("call_id") or ""),
            tool,
        )
        arguments = interval.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = {}
        status = str(interval.get("status") or "")
        if status == "approval_required":
            pending[key].append(arguments)
        elif status in {"ok", "success", "completed"}:
            successes.append((key, arguments))

    mutation_scope_ok = all(
        any(_same_arguments(arguments, proposed) for proposed in pending.get(key, ()))
        or any(
            _same_arguments(arguments, proposed)
            for proposed in queued.get((key[0], key[2]), ())
        )
        for key, arguments in successes
    )
    rejected = any(action.get("action") == "reject" for action in actions)
    approved = sum(
        action.get("action") == "approve" and action.get("status") == "ok"
        for action in actions
    )
    approve_actions = [
        action for action in actions if action.get("action") == "approve"
    ]
    replay_rejected = (
        all(action.get("status") != "ok" for action in approve_actions[1:])
        if len(approve_actions) > 1
        else True
    )
    reject_ok = (
        not successes
        if rejected and not any(action.get("action") == "approve" for action in actions)
        else True
    )
    approved_success_ok = not successes or approved > 0
    ok = mutation_scope_ok and reject_ok and replay_rejected and approved_success_ok
    proposal_count = (
        len(queued_calls)
        if queued_calls
        else sum(len(items) for items in pending.values())
    )
    return {
        "approval_required_count": proposal_count,
        "approved_action_count": approved,
        "mutation_success_count": len(successes),
        "mutation_scope_ok": mutation_scope_ok,
        "reject_prevented_mutation": reject_ok,
        "replay_rejected": replay_rejected,
        "approved_success_ok": approved_success_ok,
        "ok": ok,
    }


def _approval_aggregate(grades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summaries = [grade.get("approval_summary") or {} for grade in grades]
    return {
        "approval_required_count": sum(
            int(item.get("approval_required_count") or 0) for item in summaries
        ),
        "mutation_success_count": sum(
            int(item.get("mutation_success_count") or 0) for item in summaries
        ),
        "all_scopes_exact": all(
            item.get("mutation_scope_ok", True) for item in summaries
        ),
        "all_rejects_safe": all(
            item.get("reject_prevented_mutation", True) for item in summaries
        ),
        "all_replays_rejected": all(
            item.get("replay_rejected", True) for item in summaries
        ),
    }


def _required_capability_counts(
    sample: EvalSample, access_levels: Mapping[str, str]
) -> Counter[str]:
    required: Counter[str] = Counter()
    for step in sample.label["trace"]:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "")
        if action in access_levels:
            required[action] += 1
        capabilities = step.get("capabilities")
        if isinstance(capabilities, list):
            required.update(
                str(item) for item in capabilities if str(item) in access_levels
            )
        calls = step.get("calls")
        if isinstance(calls, list):
            for item in calls:
                capability = str(item).partition("(")[0].strip()
                if capability in access_levels:
                    required[capability] += 1
    return required


def _forbidden(sample: EvalSample) -> tuple[set[str], set[str], bool]:
    names: set[str] = set()
    agents: set[str] = set()
    forbid_mutation = False
    for item in sample.label["must_not"]:
        text = str(item)
        names.update(_CAPABILITY.findall(text))
        agents.update(
            match.group(1) for match in re.finditer(r"agent__([a-z_]+)", text)
        )
        lowered = text.casefold()
        if any(
            term in lowered
            for term in (
                "repository read/search",
                "仓库检索",
                "重新读取仓库",
                "不要读取仓库",
            )
        ):
            names.add("repository.*")
        if "任何 capability call" in lowered:
            names.add("*")
        if "任何 agent call" in lowered:
            agents.update(_AGENTS - {"main"})
        if (
            "任何写操作" in text
            or "github write" in lowered
            or "github mutation" in lowered
        ):
            forbid_mutation = True
    return names, agents, forbid_mutation


def _observed_route(events: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(event.get("agent") or _data(event).get("name") or "")
        for event in events
        if event.get("type") == "agent_started"
    ]


def _successful_tool(events: Sequence[Mapping[str, Any]], tool: str) -> bool:
    return any(
        event.get("type") == "tool_result"
        and _data(event).get("tool") == tool
        and str(_data(event).get("status") or "") in {"ok", "success", "completed"}
        for event in events
    )


def _effect_required(sample: EvalSample) -> bool:
    final = str(sample.label["final_state"])
    positive = any(
        term in final
        for term in (
            "恰好新增",
            "新增一次",
            "只新增",
            "恰好一条",
        )
    )
    return positive and not ("不存在" in final or "没有新增" in final)


def _mutation_type_matches(
    sample: EvalSample, mutations: Sequence[Mapping[str, Any]]
) -> bool:
    return bool(mutations) and all(
        _mutation_matches_expected(sample, mutation) for mutation in mutations
    )


def _mutation_matches_expected(sample: EvalSample, mutation: Mapping[str, Any]) -> bool:
    expected_actions = {
        str(step.get("action") or "")
        for step in sample.label["trace"]
        if isinstance(step, dict)
    }
    mapping = {
        "issue_comment": "github.post_comment",
        "issue_created": "github.create_issue",
        "issue_update": "github.update_issue",
        "issue_lock": "github.set_issue_lock",
        "pr_review": "github.post_review",
        "pr_comment": "github.post_comment",
        "pr_created": "github.create_draft_pr",
        "pr_merge": "github.merge",
        "pr_update": "github.update_pr",
        "default_branch": "github.commit_to_default_branch",
        "pr_head": "github.commit",
    }
    return mapping.get(str(mutation.get("kind") or "")) in expected_actions


def _expected_mutation_count(sample: EvalSample) -> int | None:
    final_state = str(sample.label["final_state"])
    if any(term in final_state for term in ("恰好", "新增一次", "只新增")):
        return 1
    return None


def _recovery_ok(
    sample: EvalSample,
    trial: TrialRecord,
    events: Sequence[Mapping[str, Any]],
    duplicates: int,
) -> bool:
    if sample.metric_group != "M6":
        return True
    if trial.status == "invalid" or duplicates:
        return False
    if trial.fault is not None and not trial.fault.get("triggered"):
        return False

    def action(name: str, status: str = "ok") -> bool:
        return any(
            item.get("action") == name and item.get("status") == status
            for item in trial.action_log
        )

    if sample.id in {"REC-01", "REC-02"}:
        return bool(trial.fault and trial.fault.get("triggered")) and action("approve")
    if sample.id == "REC-03":
        return bool(trial.fault and trial.fault.get("triggered")) and action("handle")
    if sample.id == "REC-04":
        return bool(trial.fault and trial.fault.get("triggered")) and action("reject")
    if sample.id in {"REC-05", "REC-06", "REC-07"}:
        return (
            bool(trial.fault and trial.fault.get("triggered"))
            and action("approve", "error")
            and action("handle")
        )
    if sample.id == "REC-08":
        interrupted = any(
            event.get("type") in {"turn_failed", "turn_interrupted"} for event in events
        )
        return interrupted and action("handle") and any(
            event.get("type") == "turn_completed" for event in events
        )
    if sample.id == "REC-09":
        return bool(
            trial.fault
            and trial.fault.get("valid_prefix_recovered")
            and action("handle")
            and any(event.get("type") == "turn_completed" for event in events)
        )
    if sample.id == "REC-10":
        return (
            action("new")
            and action("sessions")
            and action("switch")
            and action("approve")
            and len(set(trial.session_ids)) >= 2
        )
    if sample.id == "REC-11":
        return bool(
            trial.fault
            and trial.fault.get("triggered")
            and action("restore")
            and any(event.get("type") == "turn_completed" for event in events)
        )
    if sample.id == "REC-12":
        intervals = pair_tool_events(events)["intervals"]
        missing = [
            interval
            for interval in intervals
            if interval.get("tool") == "repository.read_file"
            and _label_arguments_match(
                {"path": "src/shopping_grpo/evaluation/this_file_does_not_exist.py"},
                interval.get("arguments"),
            )
        ]
        recovered = any(
            interval.get("tool") == "repository.find_symbol"
            and str(interval.get("status") or "") in {"ok", "success", "completed"}
            for interval in intervals
        )
        return (
            len(missing) == 1
            and str(missing[0].get("status") or "") == "failed"
            and recovered
            and any(event.get("type") == "turn_completed" for event in events)
        )
    return False


def observer_diff(
    sample: EvalSample, before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalize before/after snapshots into stable semantic mutation fingerprints."""

    mutations: list[dict[str, Any]] = []
    before_issues = before.get("issues") or {}
    after_issues = after.get("issues") or {}
    for title, current in after_issues.items():
        previous = before_issues.get(title)
        if previous is None:
            mutations.append(
                _mutation("issue_created", title, current.get("number"), current)
            )
            continue
        old_comments = {
            str(item.get("id")): item for item in previous.get("comments", [])
        }
        for comment in current.get("comments", []):
            if str(comment.get("id")) not in old_comments:
                mutations.append(
                    _mutation(
                        "issue_comment",
                        title,
                        current.get("number"),
                        comment.get("body", ""),
                    )
                )
        for field in ("title", "body", "state", "labels", "assignees", "milestone"):
            if previous.get(field) != current.get(field):
                mutations.append(
                    _mutation(
                        "issue_update",
                        title,
                        current.get("number"),
                        {field: current.get(field)},
                    )
                )
        if previous.get("locked") != current.get("locked"):
            mutations.append(
                _mutation(
                    "issue_lock", title, current.get("number"), current.get("locked")
                )
            )
    before_prs = before.get("pull_requests") or {}
    after_prs = after.get("pull_requests") or {}
    for title, current in after_prs.items():
        previous = before_prs.get(title)
        if previous is None:
            mutations.append(
                _mutation("pr_created", title, current.get("number"), current)
            )
            continue
        old_reviews = {
            str(item.get("id")): item for item in previous.get("reviews", [])
        }
        for review in current.get("reviews", []):
            if str(review.get("id")) not in old_reviews:
                mutations.append(
                    _mutation(
                        "pr_review",
                        title,
                        current.get("number"),
                        {
                            "state": review.get("state"),
                            "body": review.get("body") or "",
                            "user_id": review.get("user_id"),
                        },
                    )
                )
        old_comments = {
            str(item.get("id")): item for item in previous.get("comments", [])
        }
        for comment in current.get("comments", []):
            if str(comment.get("id")) not in old_comments:
                mutations.append(
                    _mutation(
                        "pr_comment",
                        title,
                        current.get("number"),
                        comment.get("body", ""),
                    )
                )
        if previous.get("merged") != current.get("merged") and current.get("merged"):
            mutations.append(
                _mutation(
                    "pr_merge",
                    title,
                    current.get("number"),
                    current.get("merge_commit_sha"),
                )
            )
        elif previous.get("state") != current.get("state"):
            mutations.append(
                _mutation(
                    "pr_update", title, current.get("number"), current.get("state")
                )
            )
        if previous.get("head_sha") != current.get("head_sha"):
            mutations.append(
                _mutation(
                    "pr_head", title, current.get("number"), current.get("head_sha")
                )
            )
    before_head = (before.get("default_branch") or {}).get("commit_sha")
    after_head = (after.get("default_branch") or {}).get("commit_sha")
    if before_head and after_head and before_head != after_head:
        mutations.append(_mutation("default_branch", "default", "default", after_head))
    before_local = before.get("local") or {}
    after_local = after.get("local") or {}
    if before_local and after_local and before_local != after_local:
        mutations.append(_mutation("local_change", "test-repo", "local", after_local))
    return {
        "mutations": mutations,
        "mutation_count": len(mutations),
        "duplicate_fingerprint_count": _duplicate_fingerprints(mutations),
    }


def _mutation(kind: str, target: Any, identifier: Any, payload: Any) -> dict[str, Any]:
    semantic = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    import hashlib

    payload_hash = hashlib.sha256(semantic.encode("utf-8")).hexdigest()
    fingerprint = f"{kind}:{identifier}:{payload_hash}"
    return {
        "kind": kind,
        "target": str(target),
        "identifier": str(identifier),
        "fingerprint": fingerprint,
        "payload_hash": payload_hash,
    }


def _duplicate_fingerprints(mutations: Sequence[Mapping[str, Any]]) -> int:
    counts = Counter(str(item.get("fingerprint") or "") for item in mutations)
    return sum(
        max(0, count - 1) for fingerprint, count in counts.items() if fingerprint
    )


def _successful_mutation_duplicates(
    intervals: Sequence[Mapping[str, Any]], access_levels: Mapping[str, str]
) -> int:
    fingerprints: Counter[str] = Counter()
    for interval in intervals:
        tool = str(interval.get("tool") or "")
        if access_levels.get(tool) not in {"WRITE", "DESTRUCTIVE"}:
            continue
        if str(interval.get("status") or "") not in {"ok", "success", "completed"}:
            continue
        arguments = dict(interval.get("arguments") or {})
        arguments.pop("expected_head_sha", None)
        arguments.pop("expected_base_sha", None)
        semantic = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, default=str
        )
        fingerprints[f"{tool}:{semantic}"] += 1
    return sum(max(0, count - 1) for count in fingerprints.values())


def _compaction(event: Mapping[str, Any]) -> dict[str, Any]:
    details = _details(event)
    before = int(details.get("before_tokens") or 0)
    after = int(details.get("after_tokens") or 0)
    return {
        "before_tokens": before,
        "after_tokens": after,
        "context_window_tokens": int(details.get("context_window_tokens") or 0),
        "compression_ratio": 1 - after / before if before else 0.0,
        "agent": details.get("agent"),
    }


def _is_auto_compact(event: Mapping[str, Any]) -> bool:
    data = _data(event)
    return str(data.get("name") or "") == "auto_compact"


def _join_after_children(
    events: Sequence[Mapping[str, Any]], children: Sequence[Mapping[str, Any]]
) -> bool | None:
    if not children:
        return None
    last_child = max(float(item["end"]) for item in children)
    parent = str(children[0].get("parent_run_id") or "")
    parent_completed = [
        _timestamp(event.get("time"))
        for event in events
        if event.get("type") == "agent_completed"
        and str(_details(event).get("run_id") or "") == parent
    ]
    return bool(parent_completed) and min(parent_completed) >= last_child


def _first_batch_intervals(
    intervals: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if not intervals:
        return []
    first = min(intervals, key=lambda item: float(item.get("start") or 0.0))
    first_end = float(first.get("end") or first.get("start") or 0.0)
    return [
        item
        for item in intervals
        if float(item.get("start") or 0.0) < first_end
    ]


def _first_batch_count(intervals: Sequence[Mapping[str, Any]]) -> int:
    return len(_first_batch_intervals(intervals))


def _judge_fields(sample: EvalSample) -> list[str]:
    if sample.metric_group in {"M2", "M3"}:
        return ["answer_facts_ok", "semantic_trace_ok"]
    if sample.metric_group == "M6":
        return ["answer_facts_ok", "recovery_semantics_ok"]
    return []


def _judge_system_prompt() -> str:
    return (
        "你是 GitAgent benchmark judge。label.trace 是语义 gold，普通只读的等价 evidence path 可以接受。"
        "M3 的 serial/parallel、timing 和 overlap 由 deterministic grader 判定；Tool/Agent 样本统一属于同一并发指标组。semantic_trace_ok 只判断任务、路由、证据和汇总语义是否正确。"
        "若提供 serial/parallel 两个候选，两侧答案语义都必须正确。只输出符合 response_schema 的 JSON。"
    )


def _aggregate_performance(
    results: Sequence[DeterministicResult],
) -> dict[str, Any]:
    valid = [result for result in results if result.valid]
    fields = (
        "speedup_p50",
        "speedup_p95",
        "latency_reduction_p50",
        "parallel_overlap_rate",
    )
    output: dict[str, Any] = {
        **_counts(results),
        "scope": "unified_serial_parallel_selected_tasks",
        "configured_samples": len(results),
        "official_task_completion_rate": None,
    }
    for field in fields:
        values = [
            float(result.deterministic[field])
            for result in valid
            if result.deterministic.get(field) is not None
        ]
        label = "mean_" + field if field.startswith(("speedup", "latency")) else field
        output[label] = _mean(values)
        if values:
            output[field + "_min"] = min(values)
            output[field + "_max"] = max(values)
            output[field + "_valid_samples"] = len(values)
    output["serial_overlap_violation_rate"] = _mean(
        [
            result.deterministic["serial_overlap_violation_rate"]
            for result in valid
            if result.deterministic.get("serial_overlap_violation_rate") is not None
        ]
    )
    output["pending_judge"] = any(result.judge_required for result in valid)
    return output


def _counts(results: Sequence[DeterministicResult]) -> dict[str, int]:
    return {
        "valid": sum(result.valid for result in results),
        "invalid": sum(not result.valid for result in results),
        "failed": sum(result.valid and result.status == "failed" for result in results),
        "pending_judge": sum(
            result.valid and bool(result.judge_required) for result in results
        ),
    }


def _rate(results: Sequence[DeterministicResult], field: str) -> float | None:
    values = [result.deterministic.get(field) for result in results]
    booleans = [value for value in values if isinstance(value, bool)]
    return sum(booleans) / len(booleans) if booleans else None


def _sample_rate(
    by_key: Mapping[str, DeterministicResult], key: str, field: str
) -> float | None:
    result = by_key.get(key)
    if (
        result is None
        or not result.valid
        or not isinstance(result.deterministic.get(field), bool)
    ):
        return None
    return float(result.deterministic[field])


def _data(event: Mapping[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return dict(value) if isinstance(value, Mapping) else {}


def _details(event: Mapping[str, Any]) -> dict[str, Any]:
    value = _data(event).get("details")
    return dict(value) if isinstance(value, Mapping) else {}


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    return datetime.fromisoformat(value).timestamp()


def _is_read_like(tool: str) -> bool:
    return not tool.startswith("github.") or tool in {
        "github.list_issues",
        "github.get_issue",
        "github.get_issue_comments",
        "github.list_milestones",
        "github.list_pull_requests",
        "github.get_pr",
        "github.get_pr_comments",
        "github.get_pr_reviews",
        "github.get_workflow_runs",
        "github.get_job_logs",
    }


def _mean(values: Iterable[float]) -> float | None:
    items = [float(value) for value in values]
    return sum(items) / len(items) if items else None


def _boolean_mean(values: Iterable[bool]) -> float | None:
    items = [bool(value) for value in values]
    return sum(items) / len(items) if items else None


def _divide(left: float | None, right: float | None) -> float | None:
    return left / right if left is not None and right else None


def _judge_value_ok(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value) and all(item is True for item in value)
    return value is True


def _validate_judge_row(result: DeterministicResult, row: Mapping[str, Any]) -> None:
    required = {"judge_id", "reason", *result.judge_required}
    if set(row) != required:
        missing = sorted(required - set(row))
        extra = sorted(set(row) - required)
        raise ValueError(
            f"judge output schema mismatch for {result.sample_key}; "
            f"missing={missing}, extra={extra}"
        )
    if not isinstance(row.get("reason"), str):
        raise TypeError(f"judge reason must be a string: {result.sample_key}")
    for field in result.judge_required:
        value = row.get(field)
        if field == "answer_reference_item_results":
            expected = int(result.deterministic.get("answer_reference_count") or 0)
            if (
                not isinstance(value, list)
                or any(not isinstance(item, bool) for item in value)
                or (expected and len(value) != expected)
            ):
                raise TypeError(
                    f"judge field {field} has invalid value: {result.sample_key}"
                )
        elif not isinstance(value, bool):
            raise TypeError(f"judge field {field} must be boolean: {result.sample_key}")
