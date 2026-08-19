"""命令行与模型/GitHub API 的环境配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Capture this before dotenv loading. Repository-controlled .env files must never
# select the persistent state location.
_STARTUP_STATE_PATH = os.environ.get("GITAGENT_STATE_PATH")
# Same pre-dotenv stance for the prompt directory: a repository-controlled .env
# must never redirect which prompts are loaded (mirrors prompts/library.py).
_STARTUP_PROMPTS_DIR = os.environ.get("GITAGENT_PROMPTS_DIR")


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
    max_tokens: int = 4096
    request_timeout: float = 30.0
    state_path: str = str(Path.home() / ".gitagent" / "state.db")
    prompts_dir: str | None = None
    context_window_tokens: int = 32768
    context_safety_tokens: int = 2048

    @property
    def effective_input_budget(self) -> int:
        return self.context_window_tokens - self.max_tokens - self.context_safety_tokens - 512

    def validate(self) -> None:
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool) or self.max_tokens < 1:
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

    @classmethod
    def from_env(cls) -> CLIConfig:
        _load_dotenv()
        config = cls(
            model=os.getenv("GITAGENT_MODEL") or "gpt-5.4-mini",
            api_key=(
                os.getenv("GITAGENT_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
            ),
            base_url=os.getenv("GITAGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
            provider=os.getenv("GITAGENT_PROVIDER") or "openai",
            github_token=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "",
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            temperature=float(os.getenv("GITAGENT_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("GITAGENT_MAX_TOKENS", "4096")),
            request_timeout=float(os.getenv("GITAGENT_REQUEST_TIMEOUT", "30")),
            state_path=_STARTUP_STATE_PATH or str(Path.home() / ".gitagent" / "state.db"),
            prompts_dir=_STARTUP_PROMPTS_DIR,
            context_window_tokens=int(os.getenv("GITAGENT_CONTEXT_WINDOW_TOKENS", "32768")),
            context_safety_tokens=int(os.getenv("GITAGENT_CONTEXT_SAFETY_TOKENS", "2048")),
        )
        config.validate()
        return config
