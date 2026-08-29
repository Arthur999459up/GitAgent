"""命令行与模型/GitHub API 的环境配置。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

# Capture this before dotenv loading. Repository-controlled .env files must never
# select the persistent state location.
_STARTUP_STATE_PATH = os.environ.get("GITAGENT_STATE_PATH")
_STARTUP_EVENT_PATH = os.environ.get("GITAGENT_EVENT_PATH")
_STARTUP_MEMORY_PATH = os.environ.get("GITAGENT_MEMORY_PATH")
_STARTUP_EVENT_RETENTION_DAYS = os.environ.get("GITAGENT_EVENT_RETENTION_DAYS")
# Same pre-dotenv stance for the prompt directory: a repository-controlled .env
# must never redirect which prompts are loaded (mirrors prompts/library.py).
_STARTUP_PROMPTS_DIR = os.environ.get("GITAGENT_PROMPTS_DIR")

_DEFAULT_REQUEST_TIMEOUT = 30.0
# Code-generation responses are both larger and slower than GitHub API calls.
# Keeping the old 30-second default here makes an otherwise healthy provider
# fail before it can return a candidate patch.
_DEFAULT_LLM_TIMEOUT = 300.0
_DEFAULT_MAX_TOKENS = 16_384
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_ROOT = _PROJECT_ROOT.parent / "database"
_DEFAULT_STATE_PATH = _DEFAULT_DATA_ROOT / "state.db"
_DEFAULT_EVENT_PATH = _DEFAULT_DATA_ROOT / "sessions"
_DEFAULT_MEMORY_PATH = _DEFAULT_DATA_ROOT / "memory"


def _load_dotenv() -> None:
    """加载当前目录或父目录中的 .env；未安装依赖时保持可用。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)


@dataclass
class CLIConfig:
    model: str = "gpt-5.4-mini"
    api_key: str = ""
    base_url: str | None = None
    provider: str = "openai"
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    temperature: float = 0.0
    max_tokens: int = _DEFAULT_MAX_TOKENS
    request_timeout: float = _DEFAULT_REQUEST_TIMEOUT
    llm_timeout: float | None = None
    github_timeout: float | None = None
    state_path: str = str(_DEFAULT_STATE_PATH)
    event_path: str = str(_DEFAULT_EVENT_PATH)
    memory_path: str = str(_DEFAULT_MEMORY_PATH)
    event_retention_days: int = 30
    prompts_dir: str | None = None
    context_window_tokens: int = 32768
    context_safety_tokens: int = 2048
    auto_learning: bool = True

    @property
    def effective_input_budget(self) -> int:
        return (
            self.context_window_tokens
            - self.max_tokens
            - self.context_safety_tokens
            - 512
        )

    @property
    def effective_llm_timeout(self) -> float:
        return self.request_timeout if self.llm_timeout is None else self.llm_timeout

    @property
    def effective_github_timeout(self) -> float:
        return (
            self.request_timeout if self.github_timeout is None else self.github_timeout
        )

    def validate(self) -> None:
        for name, value in (
            ("GITAGENT_REQUEST_TIMEOUT", self.request_timeout),
            ("GITAGENT_LLM_TIMEOUT", self.effective_llm_timeout),
            ("GITAGENT_GITHUB_TIMEOUT", self.effective_github_timeout),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} 必须为正数")
        if (
            not isinstance(self.max_tokens, int)
            or isinstance(self.max_tokens, bool)
            or self.max_tokens < 1
        ):
            raise ValueError("GITAGENT_MAX_TOKENS 必须为正整数")
        if (
            not isinstance(self.context_window_tokens, int)
            or isinstance(self.context_window_tokens, bool)
            or self.context_window_tokens < 1
            or not isinstance(self.context_safety_tokens, int)
            or isinstance(self.context_safety_tokens, bool)
            or self.context_safety_tokens < 0
        ):
            raise ValueError("Context window 必须为正数，safety budget 不能为负数")
        if self.effective_input_budget < 4096:
            raise ValueError(
                "Context 输入预算过小；需要 GITAGENT_CONTEXT_WINDOW_TOKENS - GITAGENT_MAX_TOKENS "
                "- GITAGENT_CONTEXT_SAFETY_TOKENS - 512 至少为 4096"
            )
        if self.prompts_dir is not None and not Path(self.prompts_dir).is_dir():
            raise ValueError(f"GITAGENT_PROMPTS_DIR 不是有效目录: {self.prompts_dir}")
        for name, value in (
            ("GITAGENT_STATE_PATH", self.state_path),
            ("GITAGENT_EVENT_PATH", self.event_path),
            ("GITAGENT_MEMORY_PATH", self.memory_path),
        ):
            if not Path(value).expanduser().is_absolute():
                raise ValueError(f"{name} 必须是绝对路径")
        if (
            not isinstance(self.event_retention_days, int)
            or isinstance(self.event_retention_days, bool)
            or self.event_retention_days < 0
        ):
            raise ValueError("GITAGENT_EVENT_RETENTION_DAYS 必须是非负整数")
        if not isinstance(self.auto_learning, bool):
            raise TypeError("GITAGENT_AUTO_LEARNING 必须为布尔值")

    @classmethod
    def from_env(cls) -> CLIConfig:
        _load_dotenv()
        legacy_timeout_is_set = "GITAGENT_REQUEST_TIMEOUT" in os.environ
        legacy_timeout = float(
            os.getenv("GITAGENT_REQUEST_TIMEOUT", str(_DEFAULT_REQUEST_TIMEOUT))
        )
        if "GITAGENT_LLM_TIMEOUT" in os.environ:
            llm_timeout = float(os.environ["GITAGENT_LLM_TIMEOUT"])
        elif legacy_timeout_is_set:
            # Preserve the old single-timeout setting for existing deployments
            # that explicitly configured it.
            llm_timeout = None
        else:
            llm_timeout = _DEFAULT_LLM_TIMEOUT
        state_path = _STARTUP_STATE_PATH or str(_DEFAULT_STATE_PATH)
        data_root = Path(state_path).expanduser().parent
        config = cls(
            model=os.getenv("GITAGENT_MODEL") or "gpt-5.4-mini",
            api_key=(
                os.getenv("GITAGENT_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or os.getenv("DEEPSEEK_API_KEY")
                or ""
            ),
            base_url=os.getenv("GITAGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
            provider=os.getenv("GITAGENT_PROVIDER") or "openai",
            github_token=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "",
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip(
                "/"
            ),
            temperature=float(os.getenv("GITAGENT_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("GITAGENT_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS))),
            request_timeout=legacy_timeout,
            llm_timeout=llm_timeout,
            github_timeout=(
                float(os.environ["GITAGENT_GITHUB_TIMEOUT"])
                if "GITAGENT_GITHUB_TIMEOUT" in os.environ
                else None
            ),
            state_path=state_path,
            event_path=_STARTUP_EVENT_PATH or str(data_root / "sessions"),
            memory_path=_STARTUP_MEMORY_PATH or str(data_root / "memory"),
            event_retention_days=int(
                _STARTUP_EVENT_RETENTION_DAYS or "30"
            ),
            prompts_dir=_STARTUP_PROMPTS_DIR,
            context_window_tokens=int(
                os.getenv("GITAGENT_CONTEXT_WINDOW_TOKENS", "32768")
            ),
            context_safety_tokens=int(
                os.getenv("GITAGENT_CONTEXT_SAFETY_TOKENS", "2048")
            ),
            auto_learning=_environment_boolean("GITAGENT_AUTO_LEARNING", default=True),
        )
        config.validate()
        return config


def _environment_boolean(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false、yes/no、on/off 或 1/0")
