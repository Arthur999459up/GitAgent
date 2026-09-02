"""Trusted, fixed-directory Skill capability provider."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from gitagent.domain.errors import PermissionDenied, ValidationError

from ..catalog import CapabilityDefinition
from ..errors import CapabilityInternalError
from ..models import (
    Capability,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRegistration,
    CapabilityStatus,
    InvocationContext,
)


class SkillProvider:
    id = "skill"

    def __init__(
        self,
        definitions: Iterable[CapabilityDefinition],
        *,
        trusted_root: str | Path,
    ) -> None:
        self.trusted_root = Path(trusted_root).resolve()
        self._definitions = tuple(definitions)
        if any(definition.provider_id != self.id for definition in self._definitions):
            raise ValidationError("skill provider received a foreign capability")

    def load(self) -> list[CapabilityRegistration]:
        registrations = []
        for definition in self._definitions:
            path = self._path(definition)
            status = (
                CapabilityStatus.AVAILABLE
                if definition.enabled and path.is_file()
                else CapabilityStatus.DISABLED
                if not definition.enabled
                else CapabilityStatus.UNAVAILABLE
            )
            registrations.append(
                CapabilityRegistration(
                    Capability(
                        definition.id,
                        CapabilityKind.SKILL,
                        definition.description,
                        definition.source_id,
                        status,
                        definition.access,
                    ),
                    CapabilityBinding(definition.id, self.id, definition),
                )
            )
        return registrations

    def invoke(
        self,
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> str:
        del arguments, context
        definition = binding.target
        if not isinstance(definition, CapabilityDefinition):
            raise CapabilityInternalError("Skill binding target is invalid")
        return self._path(definition).read_text(encoding="utf-8")

    def _path(self, definition: CapabilityDefinition) -> Path:
        if definition.path is None:
            raise CapabilityInternalError("Skill binding has no configured path")
        path = (self.trusted_root / definition.path).resolve(strict=False)
        try:
            path.relative_to(self.trusted_root)
        except ValueError as exc:
            raise PermissionDenied("Skill path escapes the trusted root") from exc
        if path.name != "SKILL.md":
            raise ValidationError("Skill definitions must target a SKILL.md file")
        return path
