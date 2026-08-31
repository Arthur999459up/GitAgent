"""Application composition for the fixed Capability Layer MVP."""

from __future__ import annotations

import sysconfig
from pathlib import Path
from typing import Any

from gitagent.capability import CapabilityLayer, PermissionPolicy
from gitagent.capability.providers import (
    MCPProvider,
    MCPServerDefinition,
    NativeProvider,
    RAGProvider,
    SkillDefinition,
    SkillProvider,
    context7_tool_definitions,
    github_tool_definitions,
)
from gitagent.infra.mcp import Context7Client


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
    policy = PermissionPolicy.from_file(resource_root / "capabilities.yaml")
    layer = CapabilityLayer(policy=policy, trace=trace)
    native = NativeProvider(
        workspace_root or Path.cwd(),
        memory_roots=memory_roots,
        blocked_paths=blocked_paths,
        secret_values=secret_values,
    )
    context7 = Context7Client(api_key=context7_api_key)
    layer.add_provider(native)
    layer.add_provider(
        MCPProvider(
            [
                MCPServerDefinition(
                    "github", "local_adapter", {"inject_repository": True}
                ),
                MCPServerDefinition(
                    "context7", "streamable_http", {"endpoint": context7.endpoint}
                ),
            ],
            [*github_tool_definitions(github), *context7_tool_definitions()],
            clients={"github": github, "context7": context7},
        )
    )
    skills_root = resource_root / "skills"
    layer.add_provider(
        SkillProvider(
            [
                SkillDefinition(
                    "skill.code-review",
                    "Load the fixed, read-only code review workflow context.",
                    "skill",
                    "code-review/SKILL.md",
                ),
                SkillDefinition(
                    "skill.debug",
                    "Load the fixed causal debugging workflow context.",
                    "skill",
                    "debug/SKILL.md",
                ),
            ],
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
