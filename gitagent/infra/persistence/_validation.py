"""Shared validation for scoped persistence records."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from gitagent.domain.errors import ValidationError

from .sessions import normalize_api_url

_ACCOUNT_KEY = re.compile(r"^(?P<api>.+)#user:(?P<id>[1-9][0-9]*)$")
_REPOSITORY_KEY = re.compile(r"^(?P<api>.+)#repo:(?P<id>[1-9][0-9]*)$")
_SESSION_ID = re.compile(r"^session-[0-9a-f]{32}$")
_KNOWLEDGE_ID = re.compile(r"^knowledge-[0-9a-f]{16}$")
_INTERACTION_ID = re.compile(r"^interaction-[0-9a-f]{32}$")


def validate_scope_keys(account_key: Any, repository_key: Any) -> tuple[str, str]:
    account, account_api = validate_account_key(account_key)
    repository, repository_api = validate_repository_key(repository_key)
    if account_api != repository_api:
        raise ValidationError("account_key and repository_key must use the same normalized API URL")
    return account, repository


def validate_account_key(value: Any) -> tuple[str, str]:
    key = string(value, "account_key", maximum=500, allow_empty=False)
    match = _ACCOUNT_KEY.fullmatch(key)
    if match is None or normalize_api_url(match.group("api")) != match.group("api"):
        raise ValidationError("account_key has an invalid canonical shape")
    return key, match.group("api")


def validate_repository_key(value: Any) -> tuple[str, str]:
    key = string(value, "repository_key", maximum=500, allow_empty=False)
    match = _REPOSITORY_KEY.fullmatch(key)
    if match is None or normalize_api_url(match.group("api")) != match.group("api"):
        raise ValidationError("repository_key has an invalid canonical shape")
    return key, match.group("api")


def validate_session_id(value: Any) -> str:
    session_id = string(value, "session_id", maximum=80, allow_empty=False)
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValidationError("session_id has an invalid shape")
    return session_id


def validate_knowledge_id(value: Any) -> str:
    knowledge_id = string(value, "knowledge_id", maximum=80, allow_empty=False)
    if _KNOWLEDGE_ID.fullmatch(knowledge_id) is None:
        raise ValidationError("knowledge_id has an invalid shape")
    return knowledge_id


def validate_interaction_id(value: Any) -> str:
    interaction_id = string(value, "interaction_id", maximum=80, allow_empty=False)
    if _INTERACTION_ID.fullmatch(interaction_id) is None:
        raise ValidationError("interaction_id has an invalid shape")
    return interaction_id


def string(
    value: Any,
    label: str,
    *,
    maximum: int | None = None,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValidationError(f"{label} cannot be empty")
    if maximum is not None and len(value) > maximum:
        raise ValidationError(f"{label} must be at most {maximum} Unicode characters")
    return value


def optional_string(value: Any, label: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return string(value, label, maximum=maximum) or None


def positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def timestamp(value: Any, label: str) -> str:
    serialized = string(value, label, allow_empty=False)
    try:
        parsed = datetime.fromisoformat(serialized)
    except ValueError as exc:
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    return serialized


def optional_timestamp(value: Any, label: str) -> str | None:
    return None if value is None else timestamp(value, label)


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
