"""Trusted, fixed-directory Skill capability provider."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gitagent.domain.errors import PermissionDenied, ValidationError

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
class SkillDefinition:
    id: str
    description: str
    source_id: str
    path: str
    enabled: bool = True


class SkillProvider:
    id = "skill"

    def __init__(self, definitions: list[SkillDefinition], *, trusted_root: str | Path) -> None:
        self.trusted_root = Path(trusted_root).resolve()
        self._definitions = definitions

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
                        AccessLevel.READ,
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
        if not isinstance(definition, SkillDefinition):
            raise TypeError("Skill binding target is invalid")
        return self._path(definition).read_text(encoding="utf-8")

    def _path(self, definition: SkillDefinition) -> Path:
        path = (self.trusted_root / definition.path).resolve(strict=False)
        try:
            path.relative_to(self.trusted_root)
        except ValueError as exc:
            raise PermissionDenied("Skill path escapes the trusted root") from exc
        if path.name != "SKILL.md":
            raise ValidationError("Skill definitions must target a SKILL.md file")
        return path

