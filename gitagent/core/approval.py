"""Exact-scope, one-time human approval for mutation plans."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, ClassVar

from .errors import ApprovalRequired, ValidationError
from .models import PlannedToolCall


def _canonical(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(f"{tool}\0{_canonical(arguments)}".encode()).hexdigest()


@dataclass
class ApprovalRequest:
    approval_id: str
    session_id: str
    repository: str
    summary: str
    calls: list[PlannedToolCall]
    proposal_revision: int
    proposal_hash: str
    created_at: str
    decision: str | None = None
    decided_at: str | None = None
    _remaining: list[str] = field(default_factory=list, repr=False)


class ApprovalStore:
    """Approval is valid only for exact calls, in order, and for one Session."""

    _DECISIONS: ClassVar[frozenset[str]] = frozenset({"Approve", "Reject"})

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = Lock()

    def create(
        self,
        *,
        session_id: str,
        repository: str,
        summary: str,
        calls: list[PlannedToolCall],
        proposal_revision: int,
        proposal_content: str,
    ) -> ApprovalRequest:
        if not calls:
            raise ValidationError("an approval request must describe at least one mutation")
        request = ApprovalRequest(
            approval_id=f"approval-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            repository=repository,
            summary=summary,
            calls=calls,
            proposal_revision=proposal_revision,
            proposal_hash=self._proposal_hash(proposal_revision, proposal_content, calls),
            created_at=datetime.now(timezone.utc).isoformat(),
            _remaining=[_fingerprint(call.tool, call.arguments) for call in calls],
        )
        with self._lock:
            self._requests[request.approval_id] = request
        return request

    def decide(self, approval_id: str, decision: str) -> ApprovalRequest:
        if decision not in self._DECISIONS:
            raise ValidationError("decision must be exactly Approve or Reject")
        with self._lock:
            request = self._get(approval_id)
            if request.decision is not None:
                raise ApprovalRequired("approval has already been decided")
            request.decision = decision
            request.decided_at = datetime.now(timezone.utc).isoformat()
            return request

    def supersede(self, approval_id: str) -> ApprovalRequest:
        with self._lock:
            request = self._get(approval_id)
            if request.decision is not None:
                raise ApprovalRequired("only a pending approval can be superseded")
            request.decision = "Superseded"
            request.decided_at = datetime.now(timezone.utc).isoformat()
            return request

    def invalidate_all(self) -> int:
        invalidated = 0
        decided_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for request in self._requests.values():
                if request.decision == "Invalidated" and not request._remaining:
                    continue
                request.decision = "Invalidated"
                request.decided_at = decided_at
                request._remaining.clear()
                invalidated += 1
        return invalidated

    def authorize(
        self,
        *,
        approval_id: str | None,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> None:
        if not approval_id:
            raise ApprovalRequired("GitHub mutation requires explicit approval")
        with self._lock:
            request = self._get(approval_id)
            if request.session_id != session_id:
                raise ApprovalRequired("approval belongs to a different Session")
            if request.decision != "Approve":
                raise ApprovalRequired("only an explicit Approve decision authorizes mutation")
            actual = _fingerprint(tool, arguments)
            if not request._remaining or request._remaining[0] != actual:
                raise ApprovalRequired("approval scope or mutation order does not match the actual operation")
            request._remaining.pop(0)

    def get(self, approval_id: str) -> ApprovalRequest:
        with self._lock:
            return self._get(approval_id)

    def complete(self, approval_id: str) -> bool:
        with self._lock:
            request = self._get(approval_id)
            return request.decision == "Approve" and not request._remaining

    def _get(self, approval_id: str) -> ApprovalRequest:
        try:
            return self._requests[approval_id]
        except KeyError as exc:
            raise ApprovalRequired("unknown approval") from exc

    @staticmethod
    def _proposal_hash(revision: int, content: str, calls: list[PlannedToolCall]) -> str:
        payload = {
            "revision": revision,
            "content": content,
            "calls": [{"tool": call.tool, "arguments": call.arguments} for call in calls],
        }
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()
