"""Load and validate the fixed capability catalog from one YAML document."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gitagent.domain.errors import ValidationError

from .models import AccessLevel


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """Human-maintained metadata and static binding for one fixed capability."""

    id: str
    provider_id: str
    source_id: str
    description: str
    access: AccessLevel
    enabled: bool
    handler_name: str | None = None
    server_id: str | None = None
    remote_name: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class MCPServerDefinition:
    """Static MCP server configuration from the capability catalog."""

    id: str
    transport: str
    enabled: bool
    config: dict[str, Any]


class CapabilityCatalog:
    """Validated, process-local view of ``capabilities.yaml``."""

    def __init__(
        self,
        agents: dict[str, dict[str, Any]],
        capabilities: tuple[CapabilityDefinition, ...],
        mcp_servers: tuple[MCPServerDefinition, ...],
    ) -> None:
        self._agents = deepcopy(agents)
        self._capabilities = capabilities
        self._mcp_servers = mcp_servers
        self._servers = {server.id: server for server in mcp_servers}

    @classmethod
    def from_file(cls, path: str | Path) -> CapabilityCatalog:
        catalog_path = Path(path)
        try:
            value = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValidationError(
                f"capability catalog could not be loaded: {catalog_path}: {exc}"
            ) from exc
        root = _mapping(value, "capability catalog")
        _keys(
            root,
            required={"defaults", "agents", "mcp_servers", "capabilities"},
            label="capability catalog",
        )

        defaults = _mapping(root["defaults"], "capability catalog defaults")
        if defaults != {"discover": "deny", "invoke": "deny"}:
            raise ValidationError("capability catalog defaults must deny discover and invoke")
        agents = _mapping(root["agents"], "capability catalog agents")
        if not agents or not all(
            isinstance(agent_id, str)
            and agent_id.strip()
            and isinstance(config, dict)
            for agent_id, config in agents.items()
        ):
            raise ValidationError("capability catalog agents must be non-empty mappings")

        raw_servers = _list(root["mcp_servers"], "capability catalog mcp_servers")
        servers = tuple(_server(item, index) for index, item in enumerate(raw_servers))
        if len({server.id for server in servers}) != len(servers):
            raise ValidationError("capability catalog contains duplicate MCP servers")

        raw_capabilities = _list(
            root["capabilities"], "capability catalog capabilities"
        )
        server_ids = {server.id for server in servers}
        capabilities = tuple(
            _capability(item, index, server_ids)
            for index, item in enumerate(raw_capabilities)
        )
        if len({item.id for item in capabilities}) != len(capabilities):
            raise ValidationError("capability catalog contains duplicate capabilities")
        return cls(dict(agents), capabilities, servers)

    @property
    def agents(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._agents)

    @property
    def capabilities(self) -> tuple[CapabilityDefinition, ...]:
        return self._capabilities

    @property
    def mcp_servers(self) -> tuple[MCPServerDefinition, ...]:
        return self._mcp_servers

    def for_provider(self, provider_id: str) -> tuple[CapabilityDefinition, ...]:
        return tuple(
            item for item in self._capabilities if item.provider_id == provider_id
        )

    def mcp_server(self, server_id: str) -> MCPServerDefinition:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise ValidationError(f"unknown MCP server: {server_id}") from exc


def _server(value: Any, index: int) -> MCPServerDefinition:
    label = f"mcp_servers[{index}]"
    item = _mapping(value, label)
    _keys(item, required={"id", "transport", "enabled", "config"}, label=label)
    server_id = _string(item["id"], f"{label}.id")
    transport = _string(item["transport"], f"{label}.transport")
    enabled = _bool(item["enabled"], f"{label}.enabled")
    config = _mapping(item["config"], f"{label}.config")
    if transport == "local_adapter":
        _keys(config, required={"inject_repository"}, label=f"{label}.config")
        _bool(config["inject_repository"], f"{label}.config.inject_repository")
    elif transport == "streamable_http":
        _keys(config, required={"endpoint"}, label=f"{label}.config")
        _string(config["endpoint"], f"{label}.config.endpoint")
    else:
        raise ValidationError(f"{label}.transport is unsupported: {transport}")
    return MCPServerDefinition(server_id, transport, enabled, deepcopy(config))


def _capability(
    value: Any, index: int, server_ids: set[str]
) -> CapabilityDefinition:
    label = f"capabilities[{index}]"
    item = _mapping(value, label)
    _keys(
        item,
        required={"id", "provider", "source", "description", "access", "enabled"},
        optional={"handler", "server", "remote_name", "path"},
        label=label,
    )
    capability_id = _string(item["id"], f"{label}.id")
    provider_id = _string(item["provider"], f"{label}.provider")
    source_id = _string(item["source"], f"{label}.source")
    description = _string(item["description"], f"{label}.description")
    enabled = _bool(item["enabled"], f"{label}.enabled")
    if "." not in capability_id or not capability_id.startswith(source_id + "."):
        raise ValidationError(f"capability {capability_id} is outside source {source_id}")
    if provider_id not in {"native", "mcp", "skill"}:
        raise ValidationError(
            f"capability {capability_id} has unsupported provider {provider_id}"
        )
    try:
        access = AccessLevel(item["access"])
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"capability {capability_id} has invalid access") from exc

    handler_name = _optional_string(item.get("handler"), f"{label}.handler")
    server_id = _optional_string(item.get("server"), f"{label}.server")
    remote_name = _optional_string(item.get("remote_name"), f"{label}.remote_name")
    path = _optional_string(item.get("path"), f"{label}.path")
    if provider_id == "native":
        if handler_name is None or any((server_id, remote_name, path)):
            raise ValidationError(
                f"Native capability {capability_id} requires only a handler binding"
            )
    elif provider_id == "mcp":
        if (
            server_id is None
            or remote_name is None
            or server_id not in server_ids
            or any((handler_name, path))
        ):
            raise ValidationError(
                f"MCP capability {capability_id} requires server and remote_name"
            )
    elif path is None or any((handler_name, server_id, remote_name)):
        raise ValidationError(f"Skill capability {capability_id} requires only path")

    return CapabilityDefinition(
        id=capability_id,
        provider_id=provider_id,
        source_id=source_id,
        description=description,
        access=access,
        enabled=enabled,
        handler_name=handler_name,
        server_id=server_id,
        remote_name=remote_name,
        path=path,
    )


def _keys(
    value: dict[str, Any],
    *,
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - (optional or set())
    if missing:
        raise ValidationError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ValidationError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a mapping")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be a list")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be boolean")
    return value
