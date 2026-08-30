"""Application composition for the fixed Capability Layer MVP."""

from __future__ import annotations

import json
import os
import sysconfig
from pathlib import Path
from typing import Any

from gitagent.capability import CapabilityLayer, InvocationContext, PermissionPolicy
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
from gitagent.harness.context import assistant_tool_call, tool_result_message
from gitagent.infra.mcp import Context7Client
from gitagent.model import Reasoner


def build_capability_layer(
    github: Any,
    *,
    trace: Any | None = None,
    reasoner: Reasoner | None = None,
    workspace_root: str | Path | None = None,
    memory_roots: dict[str, Path] | None = None,
) -> CapabilityLayer:
    resource_root = _resource_root()
    policy = PermissionPolicy.from_file(resource_root / "capabilities.yaml")
    layer = CapabilityLayer(policy=policy, trace=trace)
    native = NativeProvider(
        workspace_root or Path.cwd(),
        memory_roots=memory_roots,
    )
    context7 = Context7Client(api_key=os.getenv("CONTEXT7_API_KEY", ""))
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

    native.permission_resolver = lambda context: frozenset(
        capability.id for capability in layer.discover(context)
    )
    if reasoner is not None:
        native.subagent_runner = _subagent_runner(layer, reasoner)
    return layer


def _subagent_runner(layer: CapabilityLayer, reasoner: Reasoner) -> Any:
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["finish"]},
            "message": {"type": "string"},
        },
        "required": ["kind", "message"],
        "additionalProperties": False,
    }

    def run(
        task: str, context: InvocationContext, effective: frozenset[str]
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a restricted coding sub-agent. Work only on the assigned task, use only the supplied "
                    "capabilities, do not request GitHub mutation, and finish with a concise evidence-based summary. "
                    "READ actions may execute directly when allowed by runtime policy. "
                    "WRITE and DESTRUCTIVE actions require explicit user approval enforced by the runtime. "
                    "After approval, the same agent executes the exact approved capability call. "
                    "Never claim a mutation succeeded before observing a successful capability result."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"task": task, "repository": context.repository},
                    ensure_ascii=False,
                ),
            },
        ]
        discovered = [item for item in layer.discover(context) if item.id in effective]
        available_tools = [
            {
                "type": "function",
                "function": {
                    "name": _function_name(item.id),
                    "description": item.description,
                    "parameters": item.input_schema,
                },
            }
            for item in discovered
            if item.input_schema is not None
        ]
        for step in range(1, 21):
            value = reasoner.complete_structured_messages(
                messages=messages,
                schema=schema,
                tool_name="finish_subagent",
                tools=available_tools,
            )
            response_message = getattr(value, "assistant_message", None)
            if not isinstance(response_message, dict):
                response_message = assistant_tool_call(
                    f"call-subagent-{step}", "finish_subagent", value
                )
            messages.append(response_message)
            if value.get("kind") != "capability":
                call_id = str((response_message.get("tool_calls") or [{}])[0].get("id") or "")
                if call_id:
                    messages.append(tool_result_message(call_id, {"status": "finished"}))
                return {"summary": str(value.get("message") or ""), "steps": step}
            supplied_id = str(value.get("capability_id") or "")
            capability_id = next(
                (
                    item.id
                    for item in discovered
                    if _function_name(item.id) == supplied_id
                ),
                supplied_id,
            )
            arguments = dict(value.get("arguments") or {})
            result = layer.invoke(capability_id, arguments, context)
            call_id = str((response_message.get("tool_calls") or [{}])[0].get("id") or "")
            if result.status == "success":
                messages.append(tool_result_message(call_id, result.content))
            else:
                messages.append(
                    tool_result_message(
                        call_id,
                        {
                        "capability_id": capability_id,
                        "error": result.error.type.value
                        if result.error is not None
                        else result.status,
                        "message": result.error.message
                        if result.error is not None
                        else "",
                        "details": result.error.details
                        if result.error is not None
                        else None,
                        "attempts": result.attempts,
                        },
                    )
                )
        return {
            "summary": "Sub-agent reached its 20-step limit.",
            "steps": 20,
        }

    return run


def _function_name(capability_id: str) -> str:
    return "capability__" + capability_id.replace(".", "__").replace("-", "_")


def _resource_root() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "capabilities.yaml").is_file():
        return source_root
    return Path(sysconfig.get_path("data")) / "share" / "gitagent"
