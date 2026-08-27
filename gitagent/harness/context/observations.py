"""Threshold-driven projections for one agent's in-process observation history."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

from .budget import (
    EMERGENCY_THRESHOLD,
    LIGHT_THRESHOLD,
    SUMMARY_THRESHOLD,
    estimate_tokens,
)

CAPABILITY_TEXT_PROJECTION_CHARACTERS = 6_000
SUMMARY_TAIL_OBSERVATIONS = 6
EMERGENCY_TAIL_OBSERVATIONS = 2


def render_agent_observations(
    observations: Sequence[dict[str, Any]],
    *,
    file_coverage: Sequence[dict[str, Any]],
    effective_input_budget: int,
    prompt_overhead: int = 0,
) -> str:
    """Render full observations until shared context-pressure thresholds require compression."""

    entries = [_entry(observation) for observation in observations]
    raw = _json(entries)
    if _pressure(raw, prompt_overhead, effective_input_budget) < LIGHT_THRESHOLD:
        return raw

    light_entries = [_light_value(entry) for entry in entries]
    light = _with_projection("light", light_entries, len(entries), file_coverage)
    if _pressure(light, prompt_overhead, effective_input_budget) < SUMMARY_THRESHOLD:
        return light

    summary_entries = _summary_projection(light_entries)
    summary = _with_projection("summary", summary_entries, len(entries), file_coverage)
    if _pressure(summary, prompt_overhead, effective_input_budget) < EMERGENCY_THRESHOLD:
        return summary

    emergency_entries = _emergency_projection(light_entries)
    emergency = _with_projection("emergency", emergency_entries, len(entries), file_coverage)
    available_tokens = max(1, effective_input_budget - prompt_overhead)
    if estimate_tokens(emergency) <= available_tokens:
        return emergency
    return _json(
        [
            {
                "context_projection": {
                    "level": "emergency",
                    "observations": len(entries),
                    "file_coverage": list(file_coverage),
                    "notice": "Non-essential observation details were omitted to fit the input budget.",
                }
            }
        ]
    )


def _entry(observation: dict[str, Any]) -> dict[str, Any]:
    kind = str(observation.get("kind") or "")
    payload = observation.get("payload")
    if kind == "capability" and isinstance(payload, dict):
        entry = {
            "capability_id": payload.get("capability_id", ""),
            "arguments": payload.get("arguments", {}),
            "data": payload.get("data"),
        }
        if payload.get("cached"):
            entry["cached"] = True
        return entry
    return {kind or "observation": payload}


def _light_value(value: Any) -> Any:
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and len(item) > CAPABILITY_TEXT_PROJECTION_CHARACTERS:
                half = CAPABILITY_TEXT_PROJECTION_CHARACTERS // 2
                projected[key] = (
                    item[:half]
                    + f"\n... ({len(item) - CAPABILITY_TEXT_PROJECTION_CHARACTERS} characters projected) ...\n"
                    + item[-half:]
                )
                projected[f"__{key}_projection__"] = {
                    "projected": True,
                    "original_chars": len(item),
                    "retained_chars": CAPABILITY_TEXT_PROJECTION_CHARACTERS,
                }
            else:
                projected[key] = _light_value(item)
        return projected
    if isinstance(value, list):
        return [_light_value(item) for item in value]
    return value


def _summary_projection(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    split = max(0, len(entries) - SUMMARY_TAIL_OBSERVATIONS)
    older = entries[:split]
    tail = list(entries[split:])
    summaries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for entry in older:
        summary = _summary_entry(entry)
        signature = _json(summary)
        if signature in seen:
            summaries[seen[signature]]["repetitions"] = int(summaries[seen[signature]].get("repetitions", 1)) + 1
            continue
        seen[signature] = len(summaries)
        summaries.append(summary)
    return [
        {
            "observation_summary": {
                "older_observations": len(older),
                "entries": summaries,
            }
        },
        *tail,
    ]


def _emergency_projection(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    capabilities = Counter(
        str(entry.get("capability_id") or "") for entry in entries if entry.get("capability_id")
    )
    tail = [_bounded_value(entry, string_limit=1_000, item_limit=5) for entry in entries[-EMERGENCY_TAIL_OBSERVATIONS:]]
    return [
        {
            "observation_summary": {
                "total": len(entries),
                "capability_counts": dict(sorted(capabilities.items())),
            }
        },
        *tail,
    ]


def _summary_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if "capability_id" not in entry:
        return _bounded_value(entry, string_limit=1_000, item_limit=8)
    data = entry.get("data")
    if str(entry.get("capability_id") or "") in {"repository.read_file", "repository.read_files"}:
        data = _file_result_metadata(data)
    else:
        data = _bounded_value(data, string_limit=1_000, item_limit=8)
    return {
        "capability_id": entry.get("capability_id", ""),
        "arguments": _bounded_value(entry.get("arguments", {}), string_limit=500, item_limit=12),
        "data": data,
        **({"cached": True} if entry.get("cached") else {}),
    }


def _file_result_metadata(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("files"), list):
        return {"files": [_file_result_metadata(item) for item in value["files"]]}
    metadata = {
        key: value.get(key)
        for key in ("path", "start_line", "end_line", "truncated", "already_read", "coverage")
        if key in value
    }
    content = value.get("content")
    if isinstance(content, str) and content:
        metadata["content_excerpt"] = _bounded_value(content, string_limit=1_200, item_limit=5)
    return metadata


def _bounded_value(value: Any, *, string_limit: int, item_limit: int, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        half = string_limit // 2
        return value[:half] + f"... <{len(value) - string_limit} chars omitted> ..." + value[-half:]
    if isinstance(value, dict):
        if depth >= 4:
            return f"<{len(value)} keys>"
        items = list(value.items())
        result = {
            str(key): _bounded_value(item, string_limit=string_limit, item_limit=item_limit, depth=depth + 1)
            for key, item in items[:item_limit]
        }
        if len(items) > item_limit:
            result["__omitted__"] = f"{len(items) - item_limit} keys"
        return result
    if isinstance(value, (list, tuple)):
        if depth >= 4:
            return f"<{len(value)} items>"
        result = [
            _bounded_value(item, string_limit=string_limit, item_limit=item_limit, depth=depth + 1)
            for item in value[:item_limit]
        ]
        if len(value) > item_limit:
            result.append(f"<{len(value) - item_limit} items omitted>")
        return result
    return value


def _with_projection(
    level: str,
    entries: Sequence[dict[str, Any]],
    observation_count: int,
    file_coverage: Sequence[dict[str, Any]],
) -> str:
    return _json(
        [
            {
                "context_projection": {
                    "level": level,
                    "observations": observation_count,
                    "file_coverage": list(file_coverage),
                }
            },
            *entries,
        ]
    )


def _pressure(serialised: str, fixed_tokens: int, effective_input_budget: int) -> float:
    if effective_input_budget < 1:
        raise ValueError("effective_input_budget must be positive")
    return (max(0, fixed_tokens) + estimate_tokens(serialised)) / effective_input_budget


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = ["render_agent_observations"]
