"""MCP Client：调用 MCP Server 的统一入口。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .server import MCPServer


class MCPClient:
    def __init__(self, server: MCPServer) -> None:
        self.server = server

    def call(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if arguments is not None and kwargs:
            raise TypeError("pass either arguments or keyword arguments, not both")
        payload = dict(arguments or kwargs)
        return self.server.call_tool(name, payload)

    def list_tools(self) -> tuple[dict[str, Any], ...]:
        return self.server.list_tools()

    def llm_tools(self, names: Iterable[str]) -> list[dict[str, Any]]:
        """Convert exactly the selected MCP tools to native function-calling definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": self._llm_name(tool["name"]),
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in self._selected_tools(names)
        ]

    def resolve_llm_tool_name(self, name: str, allowed_names: Iterable[str]) -> str:
        """Resolve the provider-safe function identifier back to the registered MCP Tool ID."""
        allowed = set(allowed_names)
        for tool_name in allowed:
            if self._llm_name(tool_name) == name:
                return tool_name
        return name

    def _selected_tools(self, names: Iterable[str] | None) -> list[dict[str, Any]]:
        selected = set(names) if names is not None else None
        return [tool for tool in self.list_tools() if selected is None or tool["name"] in selected]

    @staticmethod
    def _llm_name(name: str) -> str:
        return "mcp__" + name.replace(".", "__")
