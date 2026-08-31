"""JSON Schema validation at the Capability and model contract boundaries."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from gitagent.domain.errors import ValidationError


def validate_schema(value: Any, schema: dict[str, Any], *, label: str) -> None:
    """Validate a value and expose provider-neutral domain errors."""

    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    path = _path(label, error.absolute_path)
    raise ValidationError(f"{path}: {error.message}")


def validate_schema_definition(schema: Any, label: str) -> None:
    if not isinstance(schema, dict) or not schema:
        raise ValidationError(f"{label} must be a non-empty schema object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        path = _path(label, exc.absolute_path)
        raise ValidationError(f"{path}: {exc.message}") from exc


def _path(label: str, parts: Any) -> str:
    rendered = label
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered
