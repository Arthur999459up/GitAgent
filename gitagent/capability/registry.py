"""Process-local capability catalog and binding index."""

from __future__ import annotations

from gitagent.domain.errors import ValidationError

from .models import Capability, CapabilityRegistration
from .schema import validate_schema_definition


class CapabilityRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, CapabilityRegistration] = {}

    def register(self, registration: CapabilityRegistration) -> None:
        capability = registration.capability
        if not capability.id.strip() or "." not in capability.id:
            raise ValidationError("capability id must use <source_id>.<capability_name>")
        if capability.id in self._registrations:
            raise ValidationError(f"duplicate capability: {capability.id}")
        if capability.source_id != capability.id.rsplit(".", 1)[0] and not capability.id.startswith(
            capability.source_id + "."
        ):
            raise ValidationError(f"capability {capability.id} is outside source {capability.source_id}")
        if not capability.description.strip():
            raise ValidationError(f"capability {capability.id} description cannot be empty")
        if registration.binding.capability_id != capability.id:
            raise ValidationError(f"binding does not match capability {capability.id}")
        if capability.input_schema is not None:
            validate_schema_definition(capability.input_schema, f"{capability.id} input schema")
            if capability.input_schema.get("type") != "object":
                raise ValidationError(f"capability {capability.id} input schema must describe an object")
        if capability.output_schema is not None:
            validate_schema_definition(capability.output_schema, f"{capability.id} output schema")
        self._registrations[capability.id] = registration

    def unregister(self, capability_id: str) -> None:
        self._registrations.pop(capability_id, None)

    def get(self, capability_id: str) -> Capability | None:
        registration = self._registrations.get(capability_id)
        return registration.capability if registration is not None else None

    def resolve(self, capability_id: str) -> CapabilityRegistration | None:
        return self._registrations.get(capability_id)

    def list(self) -> tuple[Capability, ...]:
        return tuple(item.capability for item in self._registrations.values())

    def replace_source(self, source_id: str, registrations: list[CapabilityRegistration]) -> None:
        replacement_ids = {item.capability.id for item in registrations}
        if len(replacement_ids) != len(registrations):
            raise ValidationError(f"source {source_id} contains duplicate capabilities")
        if any(item.capability.source_id != source_id for item in registrations):
            raise ValidationError(f"source replacement contains a capability outside {source_id}")
        retained = {
            capability_id: item
            for capability_id, item in self._registrations.items()
            if item.capability.source_id != source_id
        }
        previous = self._registrations
        self._registrations = retained
        try:
            for item in registrations:
                self.register(item)
        except Exception:
            self._registrations = previous
            raise

