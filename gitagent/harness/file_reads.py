"""Shared file-read coverage tracking for single and batched repository reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gitagent.domain.errors import ValidationError
from gitagent.harness.file_access import parse_file_read_requests, safe_repository_path


class FileReadOutputValidationError(ValidationError):
    """A file-read Provider result violates the ledger's output contract."""


@dataclass(frozen=True)
class FileReadRequest:
    path: str
    start_line: int = 1
    limit: int = 200
    explicit_start: bool = False

    @classmethod
    def parse(cls, value: dict[str, Any]) -> FileReadRequest:
        unknown = set(value) - {"path", "start_line", "limit"}
        if unknown:
            raise ValidationError(f"file read request contains unknown field: {min(unknown)}")
        path = safe_repository_path(str(value.get("path") or ""))
        start_line = _positive_int(value.get("start_line", 1), "start_line")
        limit = min(_positive_int(value.get("limit", 200), "limit"), 400)
        return cls(path, start_line, limit, "start_line" in value)

    def to_arguments(self) -> dict[str, Any]:
        return {"path": self.path, "start_line": self.start_line, "limit": self.limit}


@dataclass
class FileCoverage:
    repository: str
    path: str
    ref: str | None
    covered_ranges: list[tuple[int, int]] = field(default_factory=list)
    eof_line: int | None = None

    @property
    def ranges(self) -> list[list[int]]:
        ordered = sorted((start, end) for start, end in self.covered_ranges if end >= start)
        merged: list[list[int]] = []
        for start, end in ordered:
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return merged

    def add(self, result: dict[str, Any]) -> None:
        start = int(result.get("start_line", 1))
        end = int(result.get("end_line", start - 1))
        if end >= start:
            self.covered_ranges.append((start, end))
        if result.get("truncated") is False:
            self.eof_line = end

    def next_request(self, request: FileReadRequest) -> FileReadRequest | None:
        if request.explicit_start:
            requested_end = request.start_line + request.limit - 1
            gap = self._first_gap(request.start_line, requested_end)
            if gap is None:
                return None
            gap_start, gap_end = gap
            return FileReadRequest(request.path, gap_start, gap_end - gap_start + 1, True)

        gap_start = self._first_uncovered_from(1)
        if self.eof_line is not None and gap_start > self.eof_line:
            return None
        gap = self._first_gap(gap_start, gap_start + request.limit - 1)
        if gap is None:
            return None
        gap_start, gap_end = gap
        return FileReadRequest(request.path, gap_start, gap_end - gap_start + 1, True)

    def summary(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "path": self.path,
            "ref": self.ref,
            "ranges": self.ranges,
            "eof": self.eof_line is not None,
            "eof_line": self.eof_line,
        }

    def to_plain(self) -> dict[str, Any]:
        return self.summary()

    @classmethod
    def from_plain(cls, value: dict[str, Any]) -> FileCoverage:
        coverage = cls(
            repository=str(value.get("repository") or ""),
            path=str(value.get("path") or ""),
            ref=str(value["ref"]) if value.get("ref") is not None else None,
            covered_ranges=[
                (int(item[0]), int(item[1]))
                for item in value.get("ranges", [])
                if isinstance(item, list) and len(item) == 2
            ],
            eof_line=int(value["eof_line"]) if value.get("eof_line") is not None else None,
        )
        return coverage

    def _first_uncovered_from(self, start: int) -> int:
        candidate = start
        for range_start, range_end in self.ranges:
            if range_end < candidate:
                continue
            if range_start > candidate:
                return candidate
            candidate = range_end + 1
        return candidate

    def _first_gap(self, start: int, end: int) -> tuple[int, int] | None:
        if self.eof_line is not None:
            end = min(end, self.eof_line)
        if end < start:
            return None
        candidate = start
        for range_start, range_end in self.ranges:
            if range_end < candidate:
                continue
            if range_start > end:
                break
            if range_start > candidate:
                return candidate, min(end, range_start - 1)
            candidate = max(candidate, range_end + 1)
            if candidate > end:
                return None
        return (candidate, end) if candidate <= end else None


@dataclass(frozen=True)
class PreparedFileRead:
    capability_id: str
    repository: str
    ref: str | None
    requested_arguments: dict[str, Any]
    actual_arguments: dict[str, Any] | None
    requested: tuple[FileReadRequest, ...]
    actual: tuple[FileReadRequest, ...]
    covered_indexes: tuple[int, ...]

    def to_plain(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "repository": self.repository,
            "ref": self.ref,
            "requested_arguments": self.requested_arguments,
            "actual_arguments": self.actual_arguments,
            "requested": [
                {
                    "path": item.path,
                    "start_line": item.start_line,
                    "limit": item.limit,
                    "explicit_start": item.explicit_start,
                }
                for item in self.requested
            ],
            "actual": [
                {
                    "path": item.path,
                    "start_line": item.start_line,
                    "limit": item.limit,
                    "explicit_start": item.explicit_start,
                }
                for item in self.actual
            ],
            "covered_indexes": list(self.covered_indexes),
        }

    @classmethod
    def from_plain(cls, value: dict[str, Any]) -> PreparedFileRead:
        def request(item: dict[str, Any]) -> FileReadRequest:
            return FileReadRequest(
                safe_repository_path(str(item.get("path") or "")),
                _positive_int(item.get("start_line"), "start_line"),
                _positive_int(item.get("limit"), "limit"),
                bool(item.get("explicit_start")),
            )

        requested = tuple(request(dict(item)) for item in value.get("requested", []))
        actual = tuple(request(dict(item)) for item in value.get("actual", []))
        return cls(
            capability_id=str(value.get("capability_id") or ""),
            repository=str(value.get("repository") or ""),
            ref=str(value["ref"]) if value.get("ref") is not None else None,
            requested_arguments=dict(value.get("requested_arguments") or {}),
            actual_arguments=(
                dict(value["actual_arguments"])
                if isinstance(value.get("actual_arguments"), dict)
                else None
            ),
            requested=requested,
            actual=actual,
            covered_indexes=tuple(int(item) for item in value.get("covered_indexes", [])),
        )


class FileReadLedger:
    """Persist shared line coverage and prevent overlapping repository reads."""

    def __init__(self) -> None:
        self._files: dict[tuple[str, str, str | None], FileCoverage] = {}

    def prepare(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        repository: str,
    ) -> PreparedFileRead | None:
        if capability_id not in {"repository.read_file", "repository.read_files"}:
            return None
        ref = str(arguments["ref"]) if arguments.get("ref") is not None else None
        if capability_id == "repository.read_file":
            requests = (
                FileReadRequest.parse(
                    {key: arguments[key] for key in ("path", "start_line", "limit") if key in arguments}
                ),
            )
        else:
            raw_requests = arguments.get("requests")
            if not isinstance(raw_requests, list) or not raw_requests:
                raise ValidationError("read_files requires one or more requests")
            parsed_requests = parse_file_read_requests(raw_requests)
            requests = tuple(
                FileReadRequest(
                    parsed["path"],
                    parsed["start_line"],
                    parsed["limit"],
                    "start_line" in raw,
                )
                for raw, parsed in zip(raw_requests, parsed_requests, strict=True)
            )

        actual: list[FileReadRequest] = []
        covered: list[int] = []
        for index, request in enumerate(requests):
            coverage = self._files.get((repository, request.path, ref))
            planned = coverage.next_request(request) if coverage is not None else request
            if planned is None:
                covered.append(index)
            else:
                actual.append(planned)

        actual_arguments: dict[str, Any] | None
        if not actual:
            actual_arguments = None
        elif capability_id == "repository.read_file":
            actual_arguments = {
                **{
                    key: value
                    for key, value in arguments.items()
                    if key not in {"path", "start_line", "limit", "ref"}
                },
                **actual[0].to_arguments(),
                **({"ref": ref} if ref is not None else {}),
            }
        else:
            actual_arguments = {
                **{
                    key: value
                    for key, value in arguments.items()
                    if key not in {"requests", "ref"}
                },
                "requests": [request.to_arguments() for request in actual],
                **({"ref": ref} if ref is not None else {}),
            }
        return PreparedFileRead(
            capability_id,
            repository,
            ref,
            dict(arguments),
            actual_arguments,
            requests,
            tuple(actual),
            tuple(covered),
        )

    def complete(
        self,
        prepared: PreparedFileRead,
        result: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        repository = prepared.repository
        ref = prepared.ref
        fresh = self._validated_fresh_results(prepared, result)
        for request, item in zip(prepared.actual, fresh, strict=True):
            coverage = self._coverage(repository, request.path, ref)
            coverage.add(item)

        fresh_by_path = {str(item.get("path") or ""): item for item in fresh}
        returned: list[dict[str, Any]] = []
        observation_files: list[dict[str, Any]] = []
        for request in prepared.requested:
            coverage = self._coverage(repository, request.path, ref)
            item = fresh_by_path.get(request.path)
            if item is None:
                item = {"already_read": True, "coverage": coverage.summary()}
            else:
                item = dict(item)
            observation_files.append(item)
            returned.append(item)

        if prepared.capability_id == "repository.read_file":
            return returned[0], observation_files[0]
        return {"files": returned}, {"files": observation_files}

    @staticmethod
    def _validated_fresh_results(
        prepared: PreparedFileRead,
        result: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not prepared.actual:
            return []
        if not isinstance(result, dict):
            raise FileReadOutputValidationError(
                "file read Capability returned a non-object result"
            )
        if prepared.capability_id == "repository.read_file":
            fresh = [result]
        else:
            files = result.get("files")
            if not isinstance(files, list):
                raise FileReadOutputValidationError(
                    "read_files Capability result has no files array"
                )
            if any(not isinstance(item, dict) for item in files):
                raise FileReadOutputValidationError(
                    "read_files Capability returned a non-object file"
                )
            fresh = files
        if len(fresh) != len(prepared.actual):
            raise FileReadOutputValidationError(
                "file read Capability result count does not match its request"
            )
        for request, item in zip(prepared.actual, fresh, strict=True):
            if str(item.get("path") or "") != request.path:
                raise FileReadOutputValidationError(
                    "file read Capability result path does not match its request"
                )
            start = item.get("start_line")
            end = item.get("end_line")
            content = item.get("content")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or start < 1
                or not isinstance(end, int)
                or isinstance(end, bool)
                or end < 0
                or not isinstance(content, str)
                or bool(content)
                and end < start
                or not isinstance(item.get("truncated"), bool)
            ):
                raise FileReadOutputValidationError(
                    "file read Capability returned invalid range metadata"
                )
        return fresh

    def summaries(self) -> list[dict[str, Any]]:
        return [self._files[key].summary() for key in sorted(self._files, key=lambda item: tuple(str(x) for x in item))]

    def to_plain(self) -> list[dict[str, Any]]:
        return [
            self._files[key].to_plain() for key in sorted(self._files, key=lambda item: tuple(str(x) for x in item))
        ]

    def clear(self) -> None:
        self._files.clear()

    @classmethod
    def from_plain(cls, value: Any) -> FileReadLedger:
        ledger = cls()
        if not isinstance(value, list):
            return ledger
        for raw in value:
            if not isinstance(raw, dict):
                continue
            coverage = FileCoverage.from_plain(raw)
            if coverage.repository and coverage.path:
                ledger._files[(coverage.repository, coverage.path, coverage.ref)] = coverage
        return ledger

    def _coverage(self, repository: str, path: str, ref: str | None) -> FileCoverage:
        key = (repository, path, ref)
        if key not in self._files:
            self._files[key] = FileCoverage(repository, path, ref)
        return self._files[key]


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


__all__ = [
    "FileReadLedger",
    "FileReadOutputValidationError",
    "PreparedFileRead",
]
