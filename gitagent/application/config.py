"""Load GitAgent runtime settings from one JSON file."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

_CONFIGURABLE_AGENTS = frozenset(
    {
        "main",
        "repository",
        "coding",
        "issues",
        "pull_requests",
        "static_verifier",
    }
)


@dataclass(slots=True)
class RuntimeConfig:
    """Validated runtime configuration loaded directly from ``config.json``."""

    model: str
    api_key: str
    base_url: str | None
    github_token: str
    github_api_url: str
    temperature: float
    max_output_tokens: int
    llm_timeout: float
    github_timeout: float
    state_path: Path
    event_path: Path
    memory_path: Path
    event_retention_days: int
    context_window_tokens: dict[str, int]
    memory_automation: bool
    context7_api_key: str
    source_path: Path = field(init=False, repr=False)

    @classmethod
    def from_file(cls, path: str | Path = "config.json") -> RuntimeConfig:
        config_path = Path(path).expanduser().resolve()
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"配置文件不存在: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"配置文件不是有效 JSON: {config_path}:{exc.lineno}:{exc.colno}"
            ) from exc
        if not isinstance(value, dict):
            raise TypeError("config.json 顶层必须是对象")

        expected = {item.name for item in fields(cls) if item.init}
        actual = set(value)
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing:
            raise ValueError("config.json 缺少字段: " + ", ".join(missing))
        if unknown:
            raise ValueError("config.json 包含未知字段: " + ", ".join(unknown))

        data = dict(value)
        for name in ("state_path", "event_path", "memory_path"):
            data[name] = _resolve_path(data[name], name=name, base=config_path.parent)
        config = cls(**data)
        config.source_path = config_path
        config.validate()
        return config

    def context_window_for(self, agent: str) -> int:
        return self.context_window_tokens.get(
            agent, self.context_window_tokens["default"]
        )

    @property
    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.api_key, self.github_token, self.context7_api_key)
            if value
        )

    def validate(self) -> None:
        _nonempty_string(self.model, "model")
        _string(self.api_key, "api_key")
        if self.base_url is not None:
            _nonempty_string(self.base_url, "base_url")
        _string(self.github_token, "github_token")
        _nonempty_string(self.github_api_url, "github_api_url")
        _number(self.temperature, "temperature")
        _positive_integer(self.max_output_tokens, "max_output_tokens")
        _positive_number(self.llm_timeout, "llm_timeout")
        _positive_number(self.github_timeout, "github_timeout")
        _absolute_path(self.state_path, "state_path")
        _absolute_path(self.event_path, "event_path")
        _absolute_path(self.memory_path, "memory_path")
        _nonnegative_integer(self.event_retention_days, "event_retention_days")
        if not isinstance(self.context_window_tokens, dict):
            raise TypeError("context_window_tokens 必须是对象")
        if "default" not in self.context_window_tokens:
            raise ValueError("context_window_tokens 必须包含 default")
        unknown_agents = (
            set(self.context_window_tokens) - _CONFIGURABLE_AGENTS - {"default"}
        )
        if unknown_agents:
            raise ValueError(
                "context_window_tokens 包含未知 Agent: "
                + ", ".join(sorted(unknown_agents))
            )
        for agent, size in self.context_window_tokens.items():
            _positive_integer(size, f"context_window_tokens.{agent}")
        if not isinstance(self.memory_automation, bool):
            raise TypeError("memory_automation 必须为布尔值")
        _string(self.context7_api_key, "context7_api_key")


def _resolve_path(value: Any, *, name: str, base: Path) -> Path:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是路径字符串")
    if not value.strip():
        raise ValueError(f"{name} 不能为空")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _string(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是字符串")


def _nonempty_string(value: Any, name: str) -> None:
    _string(value, name)
    if not value.strip():
        raise ValueError(f"{name} 不能为空")


def _number(value: Any, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise TypeError(f"{name} 必须是有限数值")


def _positive_number(value: Any, name: str) -> None:
    _number(value, name)
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")


def _positive_integer(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")


def _nonnegative_integer(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if value < 0:
        raise ValueError(f"{name} 不能小于 0")


def _absolute_path(value: Any, name: str) -> None:
    if not isinstance(value, Path) or not value.is_absolute():
        raise TypeError(f"{name} 必须是绝对路径")
