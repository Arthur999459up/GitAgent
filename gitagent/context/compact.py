"""Deterministic rolling summaries for bounded Session history projections."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..state import TurnRecord

SUMMARY_TOKEN_LIMIT = 1500
SUMMARY_RECORD_CHARACTER_LIMIT = 400
SUMMARY_TAIL_UNITS = 6

TokenCounter = Callable[[str], int]

_TURN_LINE = re.compile(r"^\[turn:(\d+)](?:\s|$)")
_OMITTED_LINE = re.compile(r"^\[older turns omitted through:(\d+)]$")


def estimate_tokens(value: str) -> int:
    """Estimate tokens using the project's fixed ceil(UTF-8 byte length / 3) rule."""

    return (len(value.encode("utf-8")) + 2) // 3


@dataclass(frozen=True)
class CompactionPlan:
    """One as-yet-unsummarised continuous sequence range."""

    from_seq: int | None
    through_seq: int
    turns: tuple[TurnRecord, ...] = ()

    @property
    def has_range(self) -> bool:
        return self.from_seq is not None and self.through_seq >= self.from_seq


@dataclass(frozen=True)
class SummaryBuildResult:
    summary: str
    through_seq: int
    covered_from_seq: int | None
    covered_to_seq: int | None
    before_tokens: int
    after_tokens: int

    @property
    def changed(self) -> bool:
        return self.covered_from_seq is not None


@dataclass(frozen=True)
class CompactResult:
    """Public result of manual or automatic persisted compaction."""

    changed: bool
    before_tokens: int
    after_tokens: int
    covered_from_seq: int | None
    covered_to_seq: int | None
    summary_through_seq: int
    summary: str = ""


class DeterministicCompactor:
    """Create and merge structured summary records without invoking a model."""

    def __init__(
        self,
        *,
        token_counter: TokenCounter = estimate_tokens,
        max_summary_tokens: int = SUMMARY_TOKEN_LIMIT,
    ) -> None:
        if max_summary_tokens < 1:
            raise ValueError("max_summary_tokens must be positive")
        self.token_counter = token_counter
        self.max_summary_tokens = max_summary_tokens

    def plan(
        self,
        turns: Sequence[TurnRecord],
        *,
        context_boundary_seq: int,
        summary_through_seq: int,
        tail_units: int = SUMMARY_TAIL_UNITS,
    ) -> CompactionPlan:
        return select_compaction_range(
            turns,
            context_boundary_seq=context_boundary_seq,
            summary_through_seq=summary_through_seq,
            tail_units=tail_units,
        )

    def build(
        self,
        existing_summary: str,
        plan: CompactionPlan,
        *,
        current_summary_through_seq: int,
    ) -> SummaryBuildResult:
        before_tokens = self.token_counter(existing_summary)
        if not plan.has_range:
            return SummaryBuildResult(
                summary=existing_summary,
                through_seq=current_summary_through_seq,
                covered_from_seq=None,
                covered_to_seq=None,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
            )

        records = tuple(render_summary_record(turn) for turn in plan.turns if is_history_unit(turn))
        records = tuple(record for record in records if record)
        merged = merge_summary_records(
            existing_summary,
            records,
            token_counter=self.token_counter,
            max_tokens=self.max_summary_tokens,
        )
        return SummaryBuildResult(
            summary=merged,
            through_seq=plan.through_seq,
            covered_from_seq=plan.from_seq,
            covered_to_seq=plan.through_seq,
            before_tokens=before_tokens,
            after_tokens=self.token_counter(merged),
        )


def select_compaction_range(
    turns: Sequence[TurnRecord],
    *,
    context_boundary_seq: int,
    summary_through_seq: int,
    tail_units: int = SUMMARY_TAIL_UNITS,
) -> CompactionPlan:
    """Select old rows while leaving the newest ``tail_units`` history units intact."""

    if tail_units < 0:
        raise ValueError("tail_units cannot be negative")
    floor = max(context_boundary_seq, summary_through_seq)
    ordered_rows = sorted(
        (turn for turn in turns if turn.seq > floor),
        key=lambda turn: turn.seq,
    )
    history = [turn for turn in ordered_rows if is_history_unit(turn)]
    if len(history) <= tail_units:
        return CompactionPlan(None, floor)

    tail_start_seq = history[-tail_units].seq if tail_units else None
    if tail_start_seq is None:
        # With an empty tail every available row can be covered.
        range_end = max((turn.seq for turn in ordered_rows), default=floor)
    else:
        range_end = tail_start_seq - 1
    range_start = floor + 1
    if range_end < range_start:
        return CompactionPlan(None, floor)

    selected = tuple(turn for turn in ordered_rows if range_start <= turn.seq <= range_end and is_history_unit(turn))
    return CompactionPlan(range_start, range_end, selected)


def render_summary_record(turn: TurnRecord) -> str:
    """Render one safe summary line from allowlisted fields.

    Deliberately do not use ``asdict`` or generic object serialisation here: doing so
    would read ``user_text`` and other fields that summaries are forbidden to inspect.
    """

    return _render_summary_record(
        seq=turn.seq,
        status=turn.status,
        history_text=turn.history_text,
        route_summary=turn.route_summary,
        entity_manifests=turn.entity_manifests,
    )


def _render_summary_record(
    *,
    seq: int,
    status: str,
    history_text: str,
    route_summary: Sequence[Mapping[str, Any]],
    entity_manifests: Sequence[Mapping[str, Any]],
) -> str:
    """Render already-allowlisted fields without inspecting a full Turn object."""

    status = status.strip().casefold() or "unknown"
    route_entries = tuple(route_summary)
    routes = _unique_text(
        str(entry.get("route", "")).strip() for entry in route_entries if str(entry.get("route", "")).strip()
    )
    route_status = "+".join(routes)
    if route_status:
        route_status = f"{route_status}/{status}"
    else:
        route_status = status

    goals = _unique_text(
        _single_line(entry.get("session_goal", ""), 1000)
        for entry in route_entries
        if _single_line(entry.get("session_goal", ""), 1000)
    )
    references: list[str] = []
    for entry in route_entries:
        for reference in entry.get("resolved_references", ()):
            reference_type = _single_line(reference.get("type", ""), 40)
            identifier = _safe_identifier(reference.get("id"))
            if reference_type and identifier:
                references.append(f"{reference_type}:{identifier}")
    for manifest in entity_manifests:
        entity_type = _single_line(manifest.get("entity_type", ""), 40)
        for item in manifest.get("items", ()):
            identifier = _safe_identifier(item.get("entity_id"))
            if entity_type and identifier:
                references.append(f"{entity_type}:{identifier}")

    fields = [f"[turn:{seq}] {route_status}"]
    if goals:
        fields.append(f"goal={'; '.join(goals)}")
    deduplicated_references = _unique_text(references)
    if deduplicated_references:
        fields.append(f"refs={','.join(deduplicated_references)}")
    result = _single_line(history_text, 220)
    if result:
        fields.append(f"result={result}")
    return _limit_characters(" | ".join(fields), SUMMARY_RECORD_CHARACTER_LIMIT)


def merge_summary_records(
    existing_summary: str,
    new_records: Iterable[str],
    *,
    token_counter: TokenCounter = estimate_tokens,
    max_tokens: int = SUMMARY_TOKEN_LIMIT,
) -> str:
    """Merge by sequence, sort, deduplicate, then evict oldest records to fit."""

    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    records: dict[int, str] = {}
    omitted_through = 0
    for line in str(existing_summary or "").splitlines():
        line = line.strip()
        turn_match = _TURN_LINE.match(line)
        omitted_match = _OMITTED_LINE.match(line)
        if turn_match:
            records[int(turn_match.group(1))] = _limit_characters(line, SUMMARY_RECORD_CHARACTER_LIMIT)
        elif omitted_match:
            omitted_through = max(omitted_through, int(omitted_match.group(1)))
    for record in new_records:
        line = str(record).strip().replace("\r", " ").replace("\n", " ")
        match = _TURN_LINE.match(line)
        if match:
            records[int(match.group(1))] = _limit_characters(line, SUMMARY_RECORD_CHARACTER_LIMIT)

    ordered = sorted(records.items())

    def render() -> str:
        lines: list[str] = []
        if omitted_through:
            lines.append(f"[older turns omitted through:{omitted_through}]")
        lines.extend(line for _, line in ordered)
        return "\n".join(lines)

    summary = render()
    while ordered and token_counter(summary) > max_tokens:
        removed_seq, _ = ordered.pop(0)
        omitted_through = max(omitted_through, removed_seq)
        summary = render()
    if token_counter(summary) > max_tokens:
        # This is only reachable with an unusually tiny injected test counter limit.
        return ""
    return summary


def is_history_unit(turn: TurnRecord) -> bool:
    return turn.turn_kind == "conversation" and turn.status in {"completed", "failed"}


def _single_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return _limit_characters(text, limit)


def _limit_characters(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return f"{value[: limit - 1]}…"


def _safe_identifier(value: Any, *, allow_non_numeric: bool = False) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if text.isdecimal():
        return str(int(text))
    if allow_non_numeric and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", text):
        return text
    return ""


def _unique_text(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
