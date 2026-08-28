"""Consolidation and bounded retrieval of durable long-term knowledge."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from gitagent.domain.errors import StateError, ValidationError
from gitagent.domain.learning import (
    ConsolidationResult,
    DomainInteractionRecord,
    KnowledgeChange,
    KnowledgeRecord,
    LearningAction,
    LearningProposal,
)
from gitagent.domain.models import SessionScope

from ._validation import (
    normalize,
    positive_integer,
    string,
    timestamp,
    utc_now,
    validate_account_key,
    validate_knowledge_id,
    validate_repository_key,
    validate_scope_keys,
    validate_session_id,
)
from .store import StateStore

PROVENANCE_LIMIT = 12

_KNOWLEDGE_SOURCES = {"explicit_user", "auto_reflection", "user_feedback", "domain_experience"}
_CONFIDENCE = {"direct", "strong", "moderate"}
_SOURCE_RANK = {
    "domain_experience": 1,
    "auto_reflection": 2,
    "user_feedback": 3,
    "explicit_user": 4,
}


class KnowledgeStore:
    """Own consolidation and retrieval without granting any runtime authority."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def remember(
        self,
        account_key: str,
        repository_key: str,
        *,
        scope: str,
        kind: str,
        content: str,
    ) -> tuple[KnowledgeRecord, bool]:
        """Persist an explicit user instruction, deduplicating exact semantics."""

        validate_scope_keys(account_key, repository_key)
        _validate_knowledge_type(scope, kind)
        if scope == "user" and kind != "preference":
            raise ValidationError("explicit User Memory must be a preference")
        safe_content = self._knowledge_text(content, "Knowledge content", maximum=800)
        actual_repository = None if scope == "user" else repository_key
        topic = "collaboration" if scope == "user" else kind
        now = utc_now()
        provenance = ({"source": "explicit_user", "observed_at": now},)
        with self.store.transaction() as connection:
            duplicate = self._exact_match_tx(
                connection,
                account_key=account_key,
                repository_key=actual_repository,
                scope=scope,
                content=safe_content,
                conditions="",
            )
            if duplicate is not None:
                return _knowledge(duplicate), False
            knowledge_id = f"knowledge-{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO knowledge(
                    knowledge_id,account_key,repository_key,scope,kind,topic,content,conditions,
                    source,confidence,provenance,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    knowledge_id,
                    account_key,
                    actual_repository,
                    scope,
                    kind,
                    topic,
                    safe_content,
                    "",
                    "explicit_user",
                    "direct",
                    self.store.json(list(provenance), max_bytes=16 * 1024, reject_secrets=True),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM knowledge WHERE knowledge_id=?", (knowledge_id,)).fetchone()
            return _knowledge(row), True

    def list_knowledge(
        self,
        account_key: str,
        repository_key: str,
        scope: str | None = None,
        *,
        kind: str | None = None,
    ) -> tuple[KnowledgeRecord, ...]:
        validate_scope_keys(account_key, repository_key)
        if scope not in {None, "user", "repository"}:
            raise ValidationError("Knowledge scope must be user or repository")
        if kind is not None and kind not in {"preference", "decision", "constraint", "reference", "experience"}:
            raise ValidationError("unknown Knowledge kind")
        connection = self.store.read()
        try:
            clauses = ["account_key=?", "(scope='user' OR repository_key=?)"]
            parameters: list[Any] = [account_key, repository_key]
            if scope is not None:
                clauses.append("scope=?")
                parameters.append(scope)
            if kind is not None:
                clauses.append("kind=?")
                parameters.append(kind)
            rows = connection.execute(
                f"SELECT * FROM knowledge WHERE {' AND '.join(clauses)} "
                "ORDER BY CASE scope WHEN 'user' THEN 0 ELSE 1 END, updated_at DESC, knowledge_id ASC",
                parameters,
            ).fetchall()
            return tuple(_knowledge(row) for row in rows)
        finally:
            connection.close()

    def relevant(
        self,
        account_key: str,
        repository_key: str,
        query: str,
        *,
        user_limit: int = 8,
        repository_limit: int = 12,
        recent_repository: int = 4,
    ) -> tuple[KnowledgeRecord, ...]:
        """Select a small index-like working set using topics and plain lexical overlap."""

        records = self.list_knowledge(account_key, repository_key)
        query_terms = _terms(query)
        user = [record for record in records if record.scope == "user"]
        repository = [record for record in records if record.scope == "repository"]

        # Stable collaboration preferences are the equivalent of a short always-loaded index.
        selected_user = user[:user_limit]
        scored_repository = [
            (_relevance(record, query_terms), record)
            for record in repository
        ]
        relevant_repository = [
            record
            for score, record in sorted(
                scored_repository,
                key=lambda item: (item[0], item[1].updated_at, item[1].knowledge_id),
                reverse=True,
            )
            if score > 0
        ][:repository_limit]
        if recent_repository > 0:
            selected_ids = {record.knowledge_id for record in relevant_repository}
            recent = [
                record
                for record in repository
                if record.kind != "experience" and record.knowledge_id not in selected_ids
            ]
            relevant_repository.extend(recent[:recent_repository])
            relevant_repository = relevant_repository[:repository_limit]
        if not relevant_repository and not query_terms:
            relevant_repository = repository[:repository_limit]
        return (*selected_user, *relevant_repository)

    def forget(self, account_key: str, repository_key: str, knowledge_id: str) -> KnowledgeRecord | None:
        validate_scope_keys(account_key, repository_key)
        validate_knowledge_id(knowledge_id)
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM knowledge WHERE knowledge_id=? AND account_key=?
                    AND (scope='user' OR repository_key=?)
                """,
                (knowledge_id, account_key, repository_key),
            ).fetchone()
            if row is None:
                return None
            record = _knowledge(row)
            connection.execute("DELETE FROM knowledge WHERE knowledge_id=?", (knowledge_id,))
            return record

    def consolidate(
        self,
        scope: SessionScope,
        proposal: LearningProposal,
        *,
        turn_seq: int,
        interaction: DomainInteractionRecord | None = None,
    ) -> ConsolidationResult:
        """Apply MainAgent's semantic edits after deterministic scope and trust checks."""

        validate_scope_keys(scope.account_key, scope.repository_key)
        validate_session_id(scope.session_id)
        positive_integer(turn_seq, "Reflection Turn sequence")
        added: list[str] = []
        updated: list[str] = []
        removed: list[str] = []
        skipped: list[str] = []
        changes: list[KnowledgeChange] = []
        now = utc_now()
        with self.store.transaction() as connection:
            for index, candidate in enumerate(proposal.candidates):
                label = candidate.target_id or f"candidate-{index + 1}"
                if candidate.action == LearningAction.DISCARD:
                    skipped.append(label)
                    continue
                try:
                    actual_repository = None if candidate.scope == "user" else scope.repository_key
                    _validate_knowledge_type(candidate.scope, candidate.kind)
                    if candidate.evidence_strength not in _CONFIDENCE:
                        raise ValidationError("invalid Knowledge evidence strength")
                    safe_topic = self._knowledge_text(candidate.topic, "Knowledge topic", maximum=80)
                    safe_reason = self._knowledge_text(candidate.reason, "Knowledge reason", maximum=500)
                    if candidate.action == LearningAction.ADD and candidate.target_id:
                        raise ValidationError("new Knowledge cannot name an existing target")
                    if candidate.action in {LearningAction.UPDATE, LearningAction.REMOVE}:
                        validate_knowledge_id(candidate.target_id)
                    if candidate.action == LearningAction.REMOVE and (
                        candidate.content.strip() or candidate.conditions.strip()
                    ):
                        raise ValidationError("removed Knowledge cannot carry replacement content")
                    safe_content = ""
                    safe_conditions = ""
                    if candidate.action in {LearningAction.ADD, LearningAction.UPDATE}:
                        safe_content = self._knowledge_text(
                            candidate.content, "Knowledge content", maximum=800
                        )
                        safe_conditions = self._optional_knowledge_text(
                            candidate.conditions, "Experience conditions", maximum=500
                        )
                        if candidate.kind == "experience" and not safe_conditions:
                            raise ValidationError("Experience requires explicit applicability conditions")
                        if candidate.kind != "experience" and safe_conditions:
                            raise ValidationError("only Experience can define applicability conditions")
                    source = _candidate_source(candidate.correction, candidate.kind, interaction is not None)
                    provenance = {
                        "source": source,
                        "session_id": scope.session_id,
                        "turn_seq": turn_seq,
                        "interaction_id": interaction.interaction_id if interaction is not None else None,
                        "agent": interaction.agent if interaction is not None else "main",
                        "entity_type": interaction.entity_type if interaction is not None else None,
                        "entity_id": interaction.entity_id if interaction is not None else None,
                        "evidence_summary": safe_reason,
                        "observed_at": now,
                    }
                    if candidate.action == LearningAction.ADD:
                        duplicate = self._exact_match_tx(
                            connection,
                            account_key=scope.account_key,
                            repository_key=actual_repository,
                            scope=candidate.scope,
                            content=safe_content,
                            conditions=safe_conditions,
                        )
                        if duplicate is not None:
                            record = _knowledge(duplicate)
                            merged = _merge_provenance(record.provenance, provenance)
                            connection.execute(
                                "UPDATE knowledge SET provenance=?,updated_at=? WHERE knowledge_id=?",
                                (
                                    self.store.json(list(merged), max_bytes=16 * 1024, reject_secrets=True),
                                    now,
                                    record.knowledge_id,
                                ),
                            )
                            updated.append(record.knowledge_id)
                            changed = connection.execute(
                                "SELECT * FROM knowledge WHERE knowledge_id=?", (record.knowledge_id,)
                            ).fetchone()
                            changes.append(KnowledgeChange(LearningAction.UPDATE, _knowledge(changed)))
                            continue
                        knowledge_id = f"knowledge-{uuid.uuid4().hex[:16]}"
                        connection.execute(
                            """
                            INSERT INTO knowledge(
                                knowledge_id,account_key,repository_key,scope,kind,topic,content,conditions,
                                source,confidence,provenance,created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                knowledge_id,
                                scope.account_key,
                                actual_repository,
                                candidate.scope,
                                candidate.kind,
                                safe_topic,
                                safe_content,
                                safe_conditions,
                                source,
                                candidate.evidence_strength,
                                self.store.json([provenance], max_bytes=16 * 1024, reject_secrets=True),
                                now,
                                now,
                            ),
                        )
                        added.append(knowledge_id)
                        created = connection.execute(
                            "SELECT * FROM knowledge WHERE knowledge_id=?", (knowledge_id,)
                        ).fetchone()
                        changes.append(KnowledgeChange(LearningAction.ADD, _knowledge(created)))
                        continue

                    target = self._target_tx(connection, scope, candidate.target_id)
                    if target is None:
                        skipped.append(label)
                        continue
                    record = _knowledge(target)
                    if record.scope != candidate.scope or record.kind != candidate.kind:
                        skipped.append(label)
                        continue
                    if record.source == "explicit_user" and not candidate.correction:
                        skipped.append(label)
                        continue
                    if candidate.action == LearningAction.REMOVE:
                        connection.execute("DELETE FROM knowledge WHERE knowledge_id=?", (record.knowledge_id,))
                        removed.append(record.knowledge_id)
                        changes.append(KnowledgeChange(LearningAction.REMOVE, record))
                        continue
                    merged = _merge_provenance(record.provenance, provenance)
                    effective_source = (
                        source
                        if _SOURCE_RANK[source] >= _SOURCE_RANK[record.source] or candidate.correction
                        else record.source
                    )
                    connection.execute(
                        """
                        UPDATE knowledge SET topic=?,content=?,conditions=?,source=?,confidence=?,
                            provenance=?,updated_at=? WHERE knowledge_id=?
                        """,
                        (
                            safe_topic,
                            safe_content,
                            safe_conditions,
                            effective_source,
                            candidate.evidence_strength,
                            self.store.json(list(merged), max_bytes=16 * 1024, reject_secrets=True),
                            now,
                            record.knowledge_id,
                        ),
                    )
                    updated.append(record.knowledge_id)
                    changed = connection.execute(
                        "SELECT * FROM knowledge WHERE knowledge_id=?", (record.knowledge_id,)
                    ).fetchone()
                    changes.append(KnowledgeChange(LearningAction.UPDATE, _knowledge(changed)))
                except (StateError, ValidationError):
                    skipped.append(label)
        return ConsolidationResult(
            tuple(added),
            tuple(updated),
            tuple(removed),
            tuple(skipped),
            tuple(changes),
        )

    def _knowledge_text(self, value: Any, label: str, *, maximum: int) -> str:
        text = string(value, label, maximum=maximum, allow_empty=False).strip()
        safe = self.store.text(text, max_characters=maximum, reject_secrets=True)
        if not safe:
            raise ValidationError(f"{label} cannot be empty")
        return safe

    def _optional_knowledge_text(self, value: Any, label: str, *, maximum: int) -> str:
        text = string(value, label, maximum=maximum).strip()
        return self.store.text(text, max_characters=maximum, reject_secrets=True)

    @staticmethod
    def _exact_match_tx(
        connection: Any,
        *,
        account_key: str,
        repository_key: str | None,
        scope: str,
        content: str,
        conditions: str,
    ) -> Any | None:
        rows = connection.execute(
            """
            SELECT * FROM knowledge WHERE account_key=? AND scope=?
                AND ((? IS NULL AND repository_key IS NULL) OR repository_key=?)
            ORDER BY updated_at DESC, knowledge_id ASC
            """,
            (account_key, scope, repository_key, repository_key),
        ).fetchall()
        normalized_content = normalize(content)
        normalized_conditions = normalize(conditions)
        return next(
            (
                row
                for row in rows
                if normalize(str(row["content"])) == normalized_content
                and normalize(str(row["conditions"])) == normalized_conditions
            ),
            None,
        )

    @staticmethod
    def _target_tx(connection: Any, scope: SessionScope, knowledge_id: str) -> Any | None:
        validate_knowledge_id(knowledge_id)
        return connection.execute(
            """
            SELECT * FROM knowledge WHERE knowledge_id=? AND account_key=?
                AND (scope='user' OR repository_key=?)
            """,
            (knowledge_id, scope.account_key, scope.repository_key),
        ).fetchone()


def _knowledge(row: Any) -> KnowledgeRecord:
    try:
        knowledge_id = validate_knowledge_id(row["knowledge_id"])
        account_key, account_api = validate_account_key(row["account_key"])
        repository_key = None
        if row["repository_key"] is not None:
            repository_key, repository_api = validate_repository_key(row["repository_key"])
            if account_api != repository_api:
                raise ValidationError("stored Knowledge crosses API scope")
        scope = string(row["scope"], "Knowledge scope", allow_empty=False)
        kind = string(row["kind"], "Knowledge kind", allow_empty=False)
        _validate_knowledge_type(scope, kind)
        if (scope == "user") != (repository_key is None):
            raise ValidationError("stored Knowledge scope is inconsistent")
        topic = string(row["topic"], "Knowledge topic", maximum=80, allow_empty=False)
        content = string(row["content"], "Knowledge content", maximum=800, allow_empty=False)
        conditions = string(row["conditions"], "Knowledge conditions", maximum=500)
        if (kind == "experience") != bool(conditions):
            raise ValidationError("stored Experience conditions are inconsistent")
        source = string(row["source"], "Knowledge source", allow_empty=False)
        confidence = string(row["confidence"], "Knowledge confidence", allow_empty=False)
        if source not in _KNOWLEDGE_SOURCES or confidence not in _CONFIDENCE:
            raise ValidationError("stored Knowledge trust metadata is invalid")
        raw_provenance = json.loads(string(row["provenance"], "Knowledge provenance", allow_empty=False))
        if not isinstance(raw_provenance, list) or not raw_provenance or len(raw_provenance) > PROVENANCE_LIMIT:
            raise ValidationError("stored Knowledge provenance is invalid")
        if any(not isinstance(item, dict) for item in raw_provenance):
            raise ValidationError("stored Knowledge provenance entries must be objects")
        created_at = timestamp(row["created_at"], "created_at")
        updated_at = timestamp(row["updated_at"], "updated_at")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise StateError("stored Knowledge record is invalid") from exc
    return KnowledgeRecord(
        knowledge_id,
        account_key,
        repository_key,
        scope,
        kind,
        topic,
        content,
        conditions,
        source,
        confidence,
        tuple(raw_provenance),
        created_at,
        updated_at,
    )


def _validate_knowledge_type(scope: str, kind: str) -> None:
    allowed = {
        "user": {"preference", "experience"},
        "repository": {"decision", "constraint", "reference", "experience"},
    }
    if scope not in allowed or kind not in allowed[scope]:
        raise ValidationError("invalid Knowledge scope/kind combination")


def _candidate_source(correction: bool, kind: str, has_interaction: bool) -> str:
    if correction:
        return "user_feedback"
    if kind == "experience" and has_interaction:
        return "domain_experience"
    return "auto_reflection"


def _merge_provenance(
    existing: Sequence[dict[str, Any]], incoming: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    entries = [*existing, incoming]
    return tuple(entries[-PROVENANCE_LIMIT:])


def _terms(value: str) -> set[str]:
    text = str(value or "").casefold()
    result = set(re.findall(r"[a-z0-9_]{2,}", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index : index + 2] for index in range(len(run) - 1))
    return result


def _relevance(record: KnowledgeRecord, query_terms: set[str]) -> int:
    if not query_terms:
        return 0
    topic = _terms(record.topic)
    content = _terms(record.content)
    conditions = _terms(record.conditions)
    return 4 * len(query_terms & topic) + 2 * len(query_terms & conditions) + len(query_terms & content)


__all__ = ["KnowledgeStore"]
