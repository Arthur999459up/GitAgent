"""统一的 Tool 定义、注册与 JSON-schema 子集校验。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import Parameter, signature
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from gitagent.domain.errors import ToolExecutionError, ValidationError
from gitagent.domain.models import AccessLevel

Handler = Callable[..., Any]
JSONSchema = dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    """Agent 可调用 Tool 的唯一完整定义。"""

    name: str
    description: str
    input_schema: JSONSchema
    output_schema: JSONSchema
    access: AccessLevel
    handler: Handler


def tool_spec(name: str, access: AccessLevel, description: str, handler: Handler) -> ToolSpec:
    """从 handler 的类型签名生成 ToolSpec，避免另一份参数定义漂移。"""

    hints = get_type_hints(handler)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter_name, parameter in signature(handler).parameters.items():
        if parameter_name == "self":
            continue
        properties[parameter_name] = _annotation_schema(hints.get(parameter_name, Any))
        if parameter.default is Parameter.empty:
            required.append(parameter_name)
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        output_schema=_annotation_schema(hints.get("return", Any)),
        access=access,
        handler=handler,
    )


class ToolRegistry:
    """Tool 元数据与 handler 的唯一事实来源。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        if not tool.name or not tool.name.strip():
            raise ValidationError("tool name cannot be empty")
        if tool.name in self._tools:
            raise ValidationError(f"duplicate MCP tool: {tool.name}")
        if not tool.description.strip():
            raise ValidationError(f"tool {tool.name} description cannot be empty")
        _validate_schema_definition(tool.input_schema, f"{tool.name} input schema")
        _validate_schema_definition(tool.output_schema, f"{tool.name} output schema")
        if tool.input_schema.get("type") != "object":
            raise ValidationError(f"tool {tool.name} input schema must describe an object")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolExecutionError(f"unknown MCP tool: {name}") from exc

    @property
    def tools(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())


def validate_schema(value: Any, schema: Mapping[str, Any], *, label: str) -> None:
    """校验项目 Tool schema 使用到的 JSON Schema 子集。"""

    _validate(value, schema, path=label)


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
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ValidationError(f"{path} is missing required field: {key}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = properties.get(key) if isinstance(properties, Mapping) else None
            if isinstance(child, Mapping):
                _validate(item, child, path=f"{path}.{key}")
                continue
            if additional is False:
                raise ValidationError(f"{path} contains unknown field: {key}")
            if isinstance(additional, Mapping):
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
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    raise ValidationError(f"unsupported schema type: {expected!r}")


def _validate_schema_definition(schema: Any, label: str) -> None:
    if not isinstance(schema, dict) or not schema:
        raise ValidationError(f"{label} must be a non-empty schema object")
    schema_type = schema.get("type")
    types = [schema_type] if isinstance(schema_type, str) else schema_type
    if not isinstance(types, list) or not types:
        raise ValidationError(f"{label} must declare type")
    allowed_types = {"null", "object", "array", "string", "integer", "number", "boolean"}
    unknown = [item for item in types if item not in allowed_types]
    if unknown:
        raise ValidationError(f"{label} uses unsupported type: {unknown[0]!r}")


def _annotation_schema(annotation: Any) -> JSONSchema:
    if annotation is Any:
        return {"type": ["null", "object", "array", "string", "integer", "number", "boolean"]}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {Union, UnionType}:
        types: list[str] = []
        for arg in args:
            schema_type = _annotation_schema(arg)["type"]
            types.extend(schema_type if isinstance(schema_type, list) else [schema_type])
        return {"type": list(dict.fromkeys(types))}
    if origin is list:
        return {"type": "array", "items": _annotation_schema(args[0] if args else Any)}
    if origin is dict:
        return {"type": "object", "additionalProperties": _annotation_schema(args[1] if len(args) > 1 else Any)}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is type(None):
        return {"type": "null"}
    return {"type": ["null", "object", "array", "string", "integer", "number", "boolean"]}
