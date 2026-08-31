"""Agent-visible capability contracts and provider-internal bindings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CapabilityKind(str, Enum):
    NATIVE_TOOL = "native_tool"
    MCP_TOOL = "mcp_tool"
    SKILL = "skill"
    RAG = "rag"


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class AccessLevel(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass(frozen=True)
class Capability:
    id: str
    kind: CapabilityKind
    description: str
    source_id: str
    status: CapabilityStatus
    access: AccessLevel
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class CapabilityBinding:
    capability_id: str
    provider_id: str
    target: Any


@dataclass(frozen=True)
class CapabilityRegistration:
    capability: Capability
    binding: CapabilityBinding


@dataclass(frozen=True)
class InvocationContext:
    run_id: str
    session_id: str
    agent_id: str
    repository: str = ""
    approval_id: str | None = None


@dataclass(frozen=True)
class CapabilityResult:
    capability_id: str
    status: str
    type: str
    content: Any = None
    error: Any = None
    attempts: int = 1
