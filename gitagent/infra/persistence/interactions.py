"""Retention and lifecycle of high-fidelity Domain interaction evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from gitagent.domain.errors import StateError, ValidationError
from gitagent.domain.learning import DomainInteractionRecord
from gitagent.domain.models import SessionScope

from ._validation import (
    optional_string,
    optional_timestamp,
    positive_integer,
    string,
    timestamp,
    utc_now,
    validate_interaction_id,
    validate_scope_keys,
    validate_session_id,
)
from .store import StateStore

DOMAIN_INTERACTION_LIMIT = 80
_REFLECTION_STATUSES = {"pending", "reflected", "skipped", "reflection_failed"}


class DomainEvidenceStore:
    """Persist raw learning evidence independently from Sessions and Knowledge."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def save(
        self,
        scope: SessionScope,
        *,
        interaction_id: str,
        repository_full_name: str,
        origin_turn_seq: int,
        completed_turn_seq: int,
        agent: str,
        entity_type: str | None,
        entity_id: str | None,
        goal: str,
        evidence: Mapping[str, Any],
    ) -> DomainInteractionRecord:
        validate_scope_keys(scope.account_key, scope.repository_key)
        validate_session_id(scope.session_id)
        validate_interaction_id(interaction_id)
        origin = positive_integer(origin_turn_seq, "origin Turn sequence")
        completed = positive_integer(completed_turn_seq, "completed Turn sequence")
        if completed < origin:
            raise ValidationError("completed Turn sequence cannot precede origin Turn sequence")
        safe_repository = self._text(repository_full_name, "repository_full_name", maximum=240)
        safe_agent = self._text(agent, "Domain agent", maximum=80)
        safe_goal = self._text(goal, "Domain goal", maximum=2_000)
        safe_entity_type = self._optional_text(entity_type or "", "entity type", maximum=80)
        safe_entity_id = self._optional_text(entity_id or "", "entity ID", maximum=160)
        safe_evidence = self.store.json(dict(evidence), max_bytes=768 * 1024)
        now = utc_now()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM domain_interactions WHERE interaction_id=?", (interaction_id,)
            ).fetchone()
            if existing is not None:
                return _interaction(existing)
            connection.execute(
                """
                INSERT INTO domain_interactions(
                    interaction_id,account_key,repository_key,repository_full_name,session_id,
                    origin_turn_seq,completed_turn_seq,agent,entity_type,entity_id,goal,evidence,
                    reflection_status,reflection_error,created_at,reflected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'',?,NULL)
                """,
                (
                    interaction_id,
                    scope.account_key,
                    scope.repository_key,
                    safe_repository,
                    scope.session_id,
                    origin,
                    completed,
                    safe_agent,
                    safe_entity_type or None,
                    safe_entity_id or None,
                    safe_goal,
                    safe_evidence,
                    "pending",
                    now,
                ),
            )
            stale = connection.execute(
                """
                SELECT interaction_id FROM domain_interactions
                WHERE account_key=? AND repository_key=? AND interaction_id<>?
                ORDER BY created_at DESC, interaction_id DESC
                LIMIT -1 OFFSET ?
                """,
                (
                    scope.account_key,
                    scope.repository_key,
                    interaction_id,
                    DOMAIN_INTERACTION_LIMIT - 1,
                ),
            ).fetchall()
            for row in stale:
                connection.execute(
                    "DELETE FROM domain_interactions WHERE interaction_id=?", (row["interaction_id"],)
                )
            row = connection.execute(
                "SELECT * FROM domain_interactions WHERE interaction_id=?", (interaction_id,)
            ).fetchone()
            if row is None:
                raise StateError("new Domain interaction was unexpectedly evicted")
            return _interaction(row)

    def get(
        self,
        account_key: str,
        repository_key: str,
        interaction_id: str,
    ) -> DomainInteractionRecord | None:
        validate_scope_keys(account_key, repository_key)
        validate_interaction_id(interaction_id)
        connection = self.store.read()
        try:
            row = connection.execute(
                """
                SELECT * FROM domain_interactions
                WHERE account_key=? AND repository_key=? AND interaction_id=?
                """,
                (account_key, repository_key, interaction_id),
            ).fetchone()
            return _interaction(row) if row is not None else None
        finally:
            connection.close()

    def mark_reflected(
        self,
        record: DomainInteractionRecord,
        *,
        status: str,
        error: str = "",
    ) -> DomainInteractionRecord:
        if status not in _REFLECTION_STATUSES - {"pending"}:
            raise ValidationError("invalid terminal reflection status")
        safe_error = self.store.text(
            string(error, "reflection error", maximum=500).strip(),
            max_characters=500,
        )
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM domain_interactions
                WHERE interaction_id=? AND account_key=? AND repository_key=?
                """,
                (record.interaction_id, record.account_key, record.repository_key),
            ).fetchone()
            if row is None:
                raise StateError("Domain interaction not found")
            reflected_at = utc_now()
            connection.execute(
                """
                UPDATE domain_interactions SET reflection_status=?,reflection_error=?,reflected_at=?
                WHERE interaction_id=?
                """,
                (status, safe_error, reflected_at, record.interaction_id),
            )
            return _interaction(
                connection.execute(
                    "SELECT * FROM domain_interactions WHERE interaction_id=?", (record.interaction_id,)
                ).fetchone()
            )

    def _text(self, value: Any, label: str, *, maximum: int) -> str:
        text = string(value, label, maximum=maximum, allow_empty=False).strip()
        safe = self.store.text(text, max_characters=maximum, reject_secrets=True)
        if not safe:
            raise ValidationError(f"{label} cannot be empty")
        return safe

    def _optional_text(self, value: Any, label: str, *, maximum: int) -> str:
        text = string(value, label, maximum=maximum).strip()
        return self.store.text(text, max_characters=maximum, reject_secrets=True)


def _interaction(row: Any) -> DomainInteractionRecord:
    try:
        interaction_id = validate_interaction_id(row["interaction_id"])
        account_key, repository_key = validate_scope_keys(row["account_key"], row["repository_key"])
        repository_full_name = string(
            row["repository_full_name"], "repository_full_name", maximum=240, allow_empty=False
        )
        session_id = validate_session_id(row["session_id"])
        origin_turn_seq = positive_integer(row["origin_turn_seq"], "origin Turn sequence")
        completed_turn_seq = positive_integer(row["completed_turn_seq"], "completed Turn sequence")
        if completed_turn_seq < origin_turn_seq:
            raise ValidationError("stored Domain interaction Turn range is invalid")
        agent = string(row["agent"], "Domain agent", maximum=80, allow_empty=False)
        entity_type = optional_string(row["entity_type"], "entity type", maximum=80)
        entity_id = optional_string(row["entity_id"], "entity ID", maximum=160)
        goal = string(row["goal"], "Domain goal", maximum=2_000, allow_empty=False)
        evidence = json.loads(string(row["evidence"], "Domain evidence", allow_empty=False))
        if not isinstance(evidence, dict):
            raise ValidationError("stored Domain evidence must be an object")
        reflection_status = string(row["reflection_status"], "reflection status", allow_empty=False)
        if reflection_status not in _REFLECTION_STATUSES:
            raise ValidationError("stored reflection status is invalid")
        reflection_error = string(row["reflection_error"], "reflection error", maximum=500)
        created_at = timestamp(row["created_at"], "created_at")
        reflected_at = optional_timestamp(row["reflected_at"], "reflected_at")
        if (reflection_status == "pending") != (reflected_at is None):
            raise ValidationError("stored reflection completion metadata is inconsistent")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise StateError("stored Domain interaction is invalid") from exc
    return DomainInteractionRecord(
        interaction_id,
        account_key,
        repository_key,
        repository_full_name,
        session_id,
        origin_turn_seq,
        completed_turn_seq,
        agent,
        entity_type,
        entity_id,
        goal,
        evidence,
        reflection_status,
        reflection_error,
        created_at,
        reflected_at,
    )


__all__ = ["DOMAIN_INTERACTION_LIMIT", "DomainEvidenceStore"]
