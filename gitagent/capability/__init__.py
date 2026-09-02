"""The only Agent-visible capability API."""

from .catalog import CapabilityCatalog, CapabilityDefinition, MCPServerDefinition
from .errors import CapabilityError, CapabilityErrorType, CapabilityInternalError
from .layer import CapabilityLayer, FailureGuard
from .models import (
    AccessLevel,
    Capability,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRegistration,
    CapabilityResult,
    CapabilityStatus,
    InvocationContext,
)
from .policy import BashCommandPolicy, PermissionDecision, PermissionPolicy
from .registry import CapabilityRegistry
from .schema import validate_schema
from .trace import CapabilityTrace, CapabilityTraceEvent

__all__ = [
    "AccessLevel",
    "BashCommandPolicy",
    "Capability",
    "CapabilityCatalog",
    "CapabilityDefinition",
    "CapabilityBinding",
    "CapabilityError",
    "CapabilityErrorType",
    "CapabilityInternalError",
    "CapabilityKind",
    "CapabilityLayer",
    "CapabilityRegistration",
    "CapabilityRegistry",
    "CapabilityResult",
    "CapabilityStatus",
    "CapabilityTrace",
    "CapabilityTraceEvent",
    "FailureGuard",
    "InvocationContext",
    "MCPServerDefinition",
    "PermissionDecision",
    "PermissionPolicy",
    "validate_schema",
]
