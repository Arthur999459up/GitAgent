"""Minimal Streamable HTTP MCP transport with no capability metadata."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class MCPTransportError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
        transport_unavailable: bool = False,
        request_sent: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.timed_out = timed_out
        self.transport_unavailable = transport_unavailable
        self.request_sent = request_sent


class StreamableHTTPTransport:
    def __init__(self, endpoint: str, *, api_key: str = "", timeout: float = 30.0) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.available = bool(endpoint)
        self._session_id: str | None = None
        self._next_request_id = 1

    def reconnect(self) -> None:
        self._session_id = None

    def list_tools(self) -> list[dict[str, Any]]:
        self._initialize()
        result = self._rpc("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            message = _content_text(result.get("content")) or f"MCP tool failed: {name}"
            raise MCPTransportError(message)
        if "structuredContent" in result:
            return result["structuredContent"]
        content = result.get("content") or []
        if len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
            text = str(content[0].get("text") or "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return content

    def _initialize(self) -> None:
        if self._session_id is not None:
            return
        result, session_id = self._send(
            {
                "jsonrpc": "2.0",
                "id": self._request_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "GitAgent", "version": "0.1.0"},
                },
            }
        )
        if not isinstance(result.get("result"), dict):
            raise MCPTransportError("MCP initialize returned no result")
        self._session_id = session_id or "stateless"
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response, _ = self._send(
            {"jsonrpc": "2.0", "id": self._request_id(), "method": method, "params": params}
        )
        if isinstance(response.get("error"), dict):
            error = response["error"]
            raise MCPTransportError(str(error.get("message") or f"MCP {method} failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPTransportError(f"MCP {method} returned an invalid result")
        return result

    def _send(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "GitAgent/0.1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self._session_id and self._session_id != "stateless":
            headers["Mcp-Session-Id"] = self._session_id
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read().decode("utf-8", errors="replace")
                session_id = response.headers.get("Mcp-Session-Id")
                content_type = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            retry_header = exc.headers.get("Retry-After") if exc.headers is not None else None
            retry_after = float(retry_header) if retry_header and retry_header.isdecimal() else None
            details = exc.read().decode("utf-8", errors="replace")[:1000]
            raise MCPTransportError(
                f"MCP HTTP request failed ({exc.code}): {details}",
                status_code=exc.code,
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            raise MCPTransportError(
                f"MCP transport failed: {reason}",
                timed_out=timed_out,
                transport_unavailable=not timed_out,
            ) from exc
        if not data.strip():
            return {}, session_id
        if content_type == "text/event-stream" or data.lstrip().startswith("event:"):
            messages = [line[5:].strip() for line in data.splitlines() if line.startswith("data:")]
            if not messages:
                raise MCPTransportError("MCP event stream contained no data")
            data = messages[-1]
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MCPTransportError("MCP transport returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise MCPTransportError("MCP transport returned a non-object response")
        return value, session_id

    def _request_id(self) -> int:
        value = self._next_request_id
        self._next_request_id += 1
        return value


class Context7Client(StreamableHTTPTransport):
    def __init__(self, *, api_key: str = "", timeout: float = 30.0) -> None:
        super().__init__("https://mcp.context7.com/mcp", api_key=api_key, timeout=timeout)


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ).strip()
