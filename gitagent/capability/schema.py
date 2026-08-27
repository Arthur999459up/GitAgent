"""Small JSON Schema subset used by native and MCP capability contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gitagent.domain.errors import ValidationError


def validate_schema(value: Any, schema: Mapping[str, Any], *, label: str) -> None:
    _validate(value, schema, path=label)


def validate_schema_definition(schema: Any, label: str) -> None:
    if not isinstance(schema, dict) or not schema:
        raise ValidationError(f"{label} must be a non-empty schema object")
    schema_type = schema.get("type")
    types = [schema_type] if isinstance(schema_type, str) else schema_type
    if not isinstance(types, list) or not types:
        raise ValidationError(f"{label} must declare type")
    allowed = {"null", "object", "array", "string", "integer", "number", "boolean"}
    unknown = [item for item in types if item not in allowed]
    if unknown:
        raise ValidationError(f"{label} uses unsupported type: {unknown[0]!r}")


def _validate(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    expected = schema.get("type")
    if expected is not None:
        allowed = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(value, item) for item in allowed):
            raise ValidationError(f"{path} must be of type {' or '.join(str(item) for item in allowed)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path} must be one of {schema['enum']!r}")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValidationError(f"{path} is shorter than {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValidationError(f"{path} is longer than {maximum} characters")
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValidationError(f"{path} must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValidationError(f"{path} must be <= {maximum}")
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValidationError(f"{path} must contain at least {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValidationError(f"{path} must contain at most {maximum} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item, item_schema, path=f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValidationError(f"{path} is missing required field: {key}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = properties.get(key) if isinstance(properties, Mapping) else None
            if isinstance(child, Mapping):
                _validate(item, child, path=f"{path}.{key}")
            elif additional is False:
                raise ValidationError(f"{path} contains unknown field: {key}")
            elif isinstance(additional, Mapping):
                _validate(item, additional, path=f"{path}.{key}")


def _matches_type(value: Any, expected: Any) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    raise ValidationError(f"unsupported schema type: {expected!r}")

