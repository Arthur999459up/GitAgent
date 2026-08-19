"""MCP Server：通过唯一 ToolRegistry 暴露和执行工具。"""

from __future__ import annotations

from typing import Any

from .registry import ToolRegistry, ToolSpec, validate_schema


class MCPServer:
    """负责 Tool 注册、发现、输入/输出校验与 handler 调用。"""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()

    def register(self, tool: ToolSpec) -> None:
        self.registry.register(tool)

    def get_tool(self, name: str) -> ToolSpec:
        return self.registry.get(name)

    @property
    def tools(self) -> tuple[ToolSpec, ...]:
        return self.registry.tools

    def list_tools(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "access": tool.access.value,
            }
            for tool in self.registry.tools
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.registry.get(name)
        validate_schema(arguments, tool.input_schema, label=f"tool {name} input")
        result = tool.handler(**arguments)
        validate_schema(result, tool.output_schema, label=f"tool {name} output")
        return result
