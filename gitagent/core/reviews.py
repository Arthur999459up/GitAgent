"""Normalize GitHub Pull Request review states for agent decisions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_REVIEW_EVENT_BY_VALUE = {
    "APPROVE": "APPROVE",
    "APPROVED": "APPROVE",
    "COMMENT": "COMMENT",
    "COMMENTED": "COMMENT",
    "REQUEST_CHANGES": "REQUEST_CHANGES",
    "CHANGES_REQUESTED": "REQUEST_CHANGES",
}
_NON_DECISIVE_REVIEW_STATES = frozenset({"COMMENT", "COMMENTED", "PENDING"})


def canonical_review_event(review: Mapping[str, Any]) -> str:
    """Return the request-style event corresponding to a Review API response."""

    state = str(review.get("state") or "").strip().upper()
    if state:
        return _REVIEW_EVENT_BY_VALUE.get(state, "")
    event = str(review.get("event") or "").strip().upper()
    return _REVIEW_EVENT_BY_VALUE.get(event, "")


def normalize_review(review: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve GitHub's response while adding its canonical review event."""

    normalized = dict(review)
    event = canonical_review_event(normalized)
    if event:
        normalized["event"] = event
    else:
        normalized.pop("event", None)
    return normalized


def effective_review_events(reviews: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Return each reviewer's latest approval or change-request decision.

    GitHub keeps historical reviews. A later decisive review from the same user
    replaces the older one; comments and pending reviews do not replace it, and
    a dismissed review clears it.
    """

    latest: dict[str, tuple[tuple[int, str, int, int], str]] = {}
    for position, review in enumerate(reviews):
        state = str(review.get("state") or review.get("event") or "").strip().upper()
        if state in _NON_DECISIVE_REVIEW_STATES:
            continue
        if state == "DISMISSED":
            event = ""
        else:
            event = canonical_review_event(review)
            if event not in {"APPROVE", "REQUEST_CHANGES"}:
                continue
        key = _reviewer_key(review, position)
        rank = _review_rank(review, position)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, event)
    return tuple(event for _rank, event in latest.values() if event)


def _reviewer_key(review: Mapping[str, Any], position: int) -> str:
    user = review.get("user")
    if isinstance(user, Mapping):
        if user.get("id") is not None:
            return f"user-id:{user['id']}"
        login = str(user.get("login") or "").strip().casefold()
        if login:
            return f"user-login:{login}"
    if review.get("id") is not None:
        return f"review-id:{review['id']}"
    return f"review-position:{position}"


def _review_rank(review: Mapping[str, Any], position: int) -> tuple[int, str, int, int]:
    submitted_at = str(review.get("submitted_at") or "")
    try:
        review_id = int(review.get("id", -1))
    except (TypeError, ValueError):
        review_id = -1
    return (1 if submitted_at else 0, submitted_at, review_id, position)
