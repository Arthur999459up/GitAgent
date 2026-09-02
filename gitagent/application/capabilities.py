"""Application composition for the fixed Capability Layer MVP."""

from __future__ import annotations

import sysconfig
from pathlib import Path
from typing import Any

from gitagent.capability import CapabilityCatalog, CapabilityLayer, PermissionPolicy
from gitagent.capability.providers import (
    MCPProvider,
    NativeProvider,
    RAGProvider,
    SkillProvider,
)
from gitagent.infra.mcp import StreamableHTTPTransport


def build_capability_layer(
    github: Any,
    *,
    trace: Any | None = None,
    context7_api_key: str,
    blocked_paths: tuple[Path, ...],
    secret_values: tuple[str, ...],
    workspace_root: str | Path | None = None,
    memory_roots: dict[str, Path] | None = None,
) -> CapabilityLayer:
    resource_root = _resource_root()
    catalog = CapabilityCatalog.from_file(resource_root / "capabilities.yaml")
    policy = PermissionPolicy(catalog.agents)
    policy.validate_structure()
    layer = CapabilityLayer(policy=policy, trace=trace)
    native = NativeProvider(
        workspace_root or Path.cwd(),
        definitions=catalog.for_provider("native"),
        memory_roots=memory_roots,
        blocked_paths=blocked_paths,
        secret_values=secret_values,
    )
    mcp_clients: dict[str, Any] = {}
    for server in catalog.mcp_servers:
        if server.transport == "local_adapter":
            mcp_clients[server.id] = github
        elif server.transport == "streamable_http":
            mcp_clients[server.id] = StreamableHTTPTransport(
                str(server.config["endpoint"]), api_key=context7_api_key
            )
    layer.add_provider(native)
    layer.add_provider(
        MCPProvider(
            catalog.mcp_servers,
            catalog.for_provider("mcp"),
            clients=mcp_clients,
        )
    )
    skills_root = resource_root / "skills"
    layer.add_provider(
        SkillProvider(
            catalog.for_provider("skill"),
            trusted_root=skills_root,
        )
    )
    layer.add_provider(RAGProvider())
    layer.load()

    return layer


def _resource_root() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "capabilities.yaml").is_file():
        return source_root
    return Path(sysconfig.get_path("data")) / "share" / "gitagent"
