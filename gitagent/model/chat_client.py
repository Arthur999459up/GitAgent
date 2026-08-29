"""项目内置的模型客户端。

默认客户端调用 OpenAI-compatible Chat Completions API；可选客户端通过
LiteLLM 接入其他提供商。该模块只负责传输、响应归一化和 token 统计，
不会接触仓库或绕过 Agent Harness。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from gitagent.domain.errors import (
    LLMProviderError,
    StructuredOutputError,
    ValidationError,
)


@dataclass(frozen=True)
class ToolCall:
    """归一化后的模型工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    """不同模型后端共享的响应格式。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_content: str | None = None

    @property
    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.reasoning_content is not None:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class ChatClient(Protocol):
    """应用层依赖的最小模型客户端协议。"""

    model: str
    total_prompt_tokens: int
    total_completion_tokens: int

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_token: Any | None = None,
    ) -> ChatResponse: ...


class OpenAIChatClient:
    """OpenAI-compatible Chat Completions 客户端。"""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 16_384,
        timeout: float = 30.0,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValidationError("模型 API Key 不能为空")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - 安装依赖损坏时的保护
                raise ValidationError("缺少 openai 依赖，请重新执行 pip install -e .") from exc
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=2)
        self.model = model
        self.base_url = base_url
        self.client = client
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def estimated_cost(self) -> None:
        """价格随提供商变化，客户端不提供可能误导的费用估算。"""
        return None

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_token: Any | None = None,
    ) -> ChatResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": _outbound_messages(
                messages,
                preserve_reasoning_content=_requires_reasoning_content(
                    self.model, self.base_url
                ),
            ),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            params["tools"] = tools
        try:
            raw = self.client.chat.completions.create(**params)
        except Exception as exc:
            raise LLMProviderError(_provider_failure_message(exc, timeout=self.timeout)) from exc
        if not getattr(raw, "choices", None):
            raise StructuredOutputError("模型响应不包含 choices")

        message = raw.choices[0].message
        content = _content_text(getattr(message, "content", ""))
        reasoning_content = _reasoning_content(message)
        calls = _tool_calls(getattr(message, "tool_calls", None))
        prompt_tokens, completion_tokens = _usage_tokens(getattr(raw, "usage", None))
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        if on_token and content:
            on_token(content)
        return ChatResponse(
            content,
            calls,
            prompt_tokens,
            completion_tokens,
            reasoning_content,
        )


class LiteLLMChatClient:
    """可选的 LiteLLM 客户端，用于非 OpenAI-compatible 提供商。"""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 16_384,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValidationError("模型 API Key 不能为空")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def estimated_cost(self) -> None:
        return None

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_token: Any | None = None,
    ) -> ChatResponse:
        try:
            import litellm
        except ImportError as exc:
            raise ValidationError('LiteLLM 后端未安装，请执行 pip install -e ".[litellm]"') from exc

        params: dict[str, Any] = {
            "model": self.model,
            "messages": _outbound_messages(
                messages,
                preserve_reasoning_content=_requires_reasoning_content(
                    self.model, self.base_url
                ),
            ),
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "drop_params": True,
        }
        if self.base_url:
            params["api_base"] = self.base_url
        if tools:
            params["tools"] = tools
        try:
            raw = litellm.completion(**params)
        except Exception as exc:
            raise LLMProviderError(_provider_failure_message(exc, timeout=self.timeout)) from exc
        if not getattr(raw, "choices", None):
            raise StructuredOutputError("模型响应不包含 choices")

        message = raw.choices[0].message
        content = _content_text(getattr(message, "content", ""))
        reasoning_content = _reasoning_content(message)
        calls = _tool_calls(getattr(message, "tool_calls", None))
        prompt_tokens, completion_tokens = _usage_tokens(getattr(raw, "usage", None))
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        if on_token and content:
            on_token(content)
        return ChatResponse(
            content,
            calls,
            prompt_tokens,
            completion_tokens,
            reasoning_content,
        )


def _requires_reasoning_content(model: str, base_url: str | None) -> bool:
    """Return whether the provider expects DeepSeek thinking metadata on tool turns."""

    model_name = str(model or "").casefold()
    endpoint = str(base_url or "").casefold()
    return "deepseek" in model_name or "deepseek" in endpoint


def _outbound_messages(
    messages: list[dict[str, Any]], *, preserve_reasoning_content: bool
) -> list[dict[str, Any]]:
    """Adapt provider metadata without mutating the durable canonical thread."""

    outbound: list[dict[str, Any]] = []
    for message in messages:
        projected = dict(message)
        if projected.get("role") == "assistant":
            if preserve_reasoning_content:
                if projected.get("tool_calls") and "reasoning_content" not in projected:
                    projected["reasoning_content"] = ""
            else:
                projected.pop("reasoning_content", None)
        outbound.append(projected)
    return outbound


def _reasoning_content(message: Any) -> str | None:
    value = getattr(message, "reasoning_content", None)
    if value is None and isinstance(message, dict):
        value = message.get("reasoning_content")
    if value is None:
        model_dump = getattr(message, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                value = dumped.get("reasoning_content")
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text", "")))
            elif getattr(item, "type", None) in {"text", "output_text"}:
                parts.append(str(getattr(item, "text", "")))
        return "".join(parts)
    return str(content)


def _provider_failure_message(exc: BaseException, *, timeout: float) -> str:
    current: BaseException | None = exc
    seen: set[int] = set()
    status_code: int | None = None
    detail = ""
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if "timeout" in type(current).__name__.casefold():
            return f"模型提供方请求超时（单次读取超时 {timeout:g} 秒）"
        if status_code is None:
            raw_status = getattr(current, "status_code", None)
            if not isinstance(raw_status, int):
                response = getattr(current, "response", None)
                raw_status = getattr(response, "status_code", None)
            if isinstance(raw_status, int):
                status_code = raw_status
        if not detail:
            detail = _provider_error_detail(current)
        current = current.__cause__ or current.__context__
    suffix: list[str] = []
    if status_code is not None:
        suffix.append(f"HTTP {status_code}")
    if detail:
        suffix.append(detail)
    return "模型提供方请求失败" + (f"（{'；'.join(suffix)}）" if suffix else "")


def _provider_error_detail(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        message = error.get("message") if isinstance(error, dict) else None
        error_type = error.get("type") if isinstance(error, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        parts = [str(item).strip() for item in (error_type, code, message) if item]
        if parts:
            return _bounded_provider_detail(": ".join(parts))
    return ""


def _bounded_provider_detail(value: str) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= 400 else compact[:397] + "..."


def _tool_calls(raw_calls: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for raw in raw_calls or []:
        function = getattr(raw, "function", None)
        raw_arguments = getattr(function, "arguments", "{}")
        try:
            arguments = json.loads(raw_arguments or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise StructuredOutputError("模型工具调用参数不是合法 JSON") from exc
        if not isinstance(arguments, dict):
            raise StructuredOutputError("模型工具调用参数必须是 JSON object")
        calls.append(
            ToolCall(
                id=str(getattr(raw, "id", "")),
                name=str(getattr(function, "name", "")),
                arguments=arguments,
            )
        )
    return calls


def _usage_tokens(usage: Any) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)
