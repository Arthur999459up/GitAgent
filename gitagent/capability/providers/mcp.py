"""MCP runtime bindings, local/remote client ownership, and dispatch."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from inspect import Parameter, signature
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from gitagent.domain.errors import (
    ExternalExecutionError,
    PermissionDenied,
    ResourceNotFoundError,
    ValidationError,
)

from ..catalog import CapabilityDefinition, MCPServerDefinition
from ..errors import (
    CapabilityInternalError,
    ProviderAuthenticationError,
    ProviderConflictError,
    ProviderExecutionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..models import (
    AccessLevel,
    Capability,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRegistration,
    CapabilityStatus,
    InvocationContext,
)


@dataclass(frozen=True)
class MCPToolBinding:
    definition: CapabilityDefinition
    description: str
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None


class MCPProvider:
    id = "mcp"

    def __init__(
        self,
        servers: Iterable[MCPServerDefinition],
        definitions: Iterable[CapabilityDefinition],
        *,
        clients: dict[str, Any],
    ) -> None:
        self._servers = {server.id: server for server in servers}
        self._clients = dict(clients)
        self._tools = [
            self._build_binding(definition) for definition in definitions
        ]

    def load(self) -> list[CapabilityRegistration]:
        registrations: list[CapabilityRegistration] = []
        for binding in self._tools:
            definition = binding.definition
            if definition.server_id is None:
                raise CapabilityInternalError(
                    f"MCP capability has no server binding: {definition.id}"
                )
            server = self._servers[definition.server_id]
            client = self._clients.get(definition.server_id)
            available = (
                client is not None
                and bool(getattr(client, "available", True))
            )
            registrations.append(
                CapabilityRegistration(
                    Capability(
                        definition.id,
                        CapabilityKind.MCP_TOOL,
                        binding.description,
                        definition.source_id,
                        CapabilityStatus.DISABLED
                        if not definition.enabled or not server.enabled
                        else CapabilityStatus.AVAILABLE
                        if available
                        else CapabilityStatus.UNAVAILABLE,
                        definition.access,
                        binding.input_schema,
                        binding.output_schema,
                    ),
                    CapabilityBinding(definition.id, self.id, binding),
                )
            )
        return registrations

    def invoke(
        self,
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        tool = binding.target
        if not isinstance(tool, MCPToolBinding):
            raise CapabilityInternalError("MCP binding target is invalid")
        definition = tool.definition
        if definition.server_id is None or definition.remote_name is None:
            raise CapabilityInternalError("MCP binding is incomplete")
        client = self._clients.get(definition.server_id)
        if client is None:
            raise ProviderUnavailableError(
                f"MCP server is not connected: {definition.server_id}"
            )
        call_arguments = dict(arguments)
        server = self._servers[definition.server_id]
        if server.config.get("inject_repository"):
            if not context.repository:
                raise ValidationError("repository context is required")
            call_arguments["repository"] = context.repository
        try:
            if hasattr(client, "call_tool"):
                return client.call_tool(definition.remote_name, call_arguments)
            return getattr(client, definition.remote_name)(**call_arguments)
        except Exception as exc:  # noqa: BLE001 - Provider boundary normalizes expected client failures
            self._raise_normalized_transport(
                exc, mutation=definition.access != AccessLevel.READ
            )

    @staticmethod
    def describe_execution(
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        del arguments
        from gitagent.harness.execution import ExecutionProfile

        tool = binding.target
        if not isinstance(tool, MCPToolBinding):
            return ExecutionProfile.unknown(repository=context.repository)
        definition = tool.definition
        if definition.access != AccessLevel.READ:
            scope = context.repository.strip() or "<unscoped>"
            return ExecutionProfile.exclusive(write=(f"repo:{scope}",))
        return ExecutionProfile.concurrent()

    def reconnect(self, binding: CapabilityBinding) -> None:
        tool = binding.target
        if not isinstance(tool, MCPToolBinding):
            raise CapabilityInternalError("MCP binding target is invalid")
        definition = tool.definition
        if definition.server_id is None:
            raise CapabilityInternalError("MCP binding has no server")
        client = self._clients.get(definition.server_id)
        if client is not None and hasattr(client, "reconnect"):
            try:
                client.reconnect()
            except Exception as exc:  # noqa: BLE001 - Provider boundary normalizes transport failures
                self._raise_normalized_transport(exc, mutation=False)

    def refresh(self) -> None:
        refreshed: list[MCPToolBinding] = []
        for server_id in self._servers:
            server_tools = [
                item
                for item in self._tools
                if item.definition.server_id == server_id
            ]
            client = self._clients.get(server_id)
            if client is None or not hasattr(client, "list_tools"):
                refreshed.extend(server_tools)
                continue
            try:
                listed_tools = client.list_tools()
            except Exception as exc:  # noqa: BLE001 - Provider boundary normalizes transport failures
                self._raise_normalized_transport(exc, mutation=False)
            discovered = {str(item.get("name")): item for item in listed_tools}
            for definition in server_tools:
                remote = discovered.get(definition.definition.remote_name)
                if remote is None:
                    continue
                refreshed.append(
                    replace(
                        definition,
                        description=(
                            str(remote["description"])
                            if remote.get("description") is not None
                            else definition.description
                        ),
                        input_schema=(
                            dict(remote["inputSchema"])
                            if isinstance(remote.get("inputSchema"), dict)
                            else None
                        ),
                        output_schema=(
                            dict(remote["outputSchema"])
                            if isinstance(remote.get("outputSchema"), dict)
                            else None
                        ),
                    )
                )
        self._tools = refreshed

    def _build_binding(self, definition: CapabilityDefinition) -> MCPToolBinding:
        if definition.provider_id != self.id:
            raise ValidationError(
                f"MCP provider received a foreign capability: {definition.id}"
            )
        if definition.server_id is None or definition.remote_name is None:
            raise ValidationError(f"MCP capability binding is incomplete: {definition.id}")
        try:
            server = self._servers[definition.server_id]
        except KeyError as exc:
            raise ValidationError(
                f"MCP capability {definition.id} references unknown server "
                f"{definition.server_id}"
            ) from exc
        client = self._clients.get(definition.server_id)
        if server.transport == "local_adapter" and client is not None:
            handler = getattr(client, definition.remote_name, None)
            if not callable(handler):
                raise ValidationError(
                    f"MCP local adapter has no handler for {definition.id}: "
                    f"{definition.remote_name}"
                )
            excluded = (
                frozenset({"repository"})
                if server.config.get("inject_repository")
                else frozenset()
            )
            input_schema = callable_schema(handler, exclude=excluded)
            output_schema = _annotation_schema(
                get_type_hints(handler).get("return", Any)
            )
        else:
            input_schema = {"type": "object"}
            output_schema = None
        return MCPToolBinding(
            definition,
            definition.description,
            input_schema,
            output_schema,
        )

    @staticmethod
    def _raise_normalized_transport(exc: Exception, *, mutation: bool) -> None:
        status = getattr(exc, "status_code", None)
        retry_after = getattr(exc, "retry_after", None)
        request_sent = bool(getattr(exc, "request_sent", mutation))
        if status == 401:
            raise ProviderAuthenticationError(str(exc)) from exc
        if status == 404:
            raise ResourceNotFoundError(str(exc)) from exc
        if status == 409:
            raise ProviderConflictError(str(exc)) from exc
        if status == 429:
            raise ProviderRateLimitError(str(exc), retry_after=retry_after) from exc
        if (
            status == 408
            or isinstance(exc, TimeoutError)
            or bool(getattr(exc, "timed_out", False))
        ):
            raise ProviderTimeoutError(str(exc), request_sent=request_sent) from exc
        if bool(getattr(exc, "transport_unavailable", False)):
            if mutation and request_sent:
                raise ProviderTimeoutError(str(exc), request_sent=True) from exc
            raise ProviderUnavailableError(str(exc)) from exc
        if isinstance(exc, ConnectionError) or status in {500, 502, 503, 504}:
            if mutation and request_sent:
                raise ProviderTimeoutError(str(exc), request_sent=True) from exc
            raise ProviderUnavailableError(str(exc)) from exc
        if isinstance(
            exc,
            ResourceNotFoundError
            | ValidationError
            | PermissionDenied
            | ExternalExecutionError
            | ValueError
            | TypeError,
        ):
            raise exc
        raise ProviderExecutionError(str(exc)) from exc


def callable_schema(
    handler: Any, *, exclude: frozenset[str] = frozenset()
) -> dict[str, Any]:
    hints = get_type_hints(handler)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature(handler).parameters.items():
        if (
            name == "self"
            or name in exclude
            or parameter.kind == Parameter.KEYWORD_ONLY
            and name.startswith("max_")
        ):
            continue
        properties[name] = _annotation_schema(hints.get(name, Any))
        if parameter.default is Parameter.empty:
            required.append(name)
    return _object_schema(properties, required)


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation is Any:
        return {
            "type": [
                "null",
                "object",
                "array",
                "string",
                "integer",
                "number",
                "boolean",
            ]
        }
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {Union, UnionType}:
        types: list[str] = []
        for item in args:
            schema_type = _annotation_schema(item)["type"]
            types.extend(
                schema_type if isinstance(schema_type, list) else [schema_type]
            )
        return {"type": list(dict.fromkeys(types))}
    if origin is list:
        return {"type": "array", "items": _annotation_schema(args[0] if args else Any)}
    if origin is dict:
        return {
            "type": "object",
            "additionalProperties": _annotation_schema(
                args[1] if len(args) > 1 else Any
            ),
        }
    mapping = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        type(None): "null",
    }
    if annotation in mapping:
        return {"type": mapping[annotation]}
    return {
        "type": ["null", "object", "array", "string", "integer", "number", "boolean"]
    }
