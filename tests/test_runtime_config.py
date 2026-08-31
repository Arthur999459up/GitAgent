from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from gitagent.application.config import RuntimeConfig
from gitagent.capability.providers import NativeProvider
from gitagent.domain.errors import PermissionDenied


def _config(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "model": "test-model",
        "api_key": "model-secret",
        "base_url": None,
        "github_token": "github-secret",
        "github_api_url": "https://api.github.com",
        "temperature": 0.0,
        "max_output_tokens": 4096,
        "llm_timeout": 300.0,
        "github_timeout": 30.0,
        "state_path": "data/state.db",
        "event_path": "data/events",
        "memory_path": "data/memory",
        "event_retention_days": 30,
        "context_window_tokens": {
            "default": 128_000,
            "coding": 200_000,
            "issues": 64_000,
        },
        "memory_automation": True,
        "context7_api_key": "context7-secret",
    }
    value.update(overrides)
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_runtime_config_loads_directly_from_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write(path, _config())

    config = RuntimeConfig.from_file(path)

    assert config.model == "test-model"
    assert config.context7_api_key == "context7-secret"
    assert config.state_path == tmp_path / "data/state.db"
    assert config.event_path == tmp_path / "data/events"
    assert config.memory_path == tmp_path / "data/memory"
    assert config.context_window_for("coding") == 200_000
    assert config.context_window_for("issues") == 64_000
    assert config.context_window_for("repository") == 128_000


def test_environment_cannot_override_json_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    _write(path, _config(model="json-model", api_key="json-key"))
    monkeypatch.setenv("GITAGENT_MODEL", "environment-model")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")

    config = RuntimeConfig.from_file(path)

    assert config.model == "json-model"
    assert config.api_key == "json-key"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("model"), "缺少字段: model"),
        (
            lambda value: value.update({"legacy_setting": True}),
            "未知字段: legacy_setting",
        ),
    ],
)
def test_runtime_config_rejects_missing_and_obsolete_fields(
    tmp_path: Path, mutation: Callable[[dict[str, object]], object], message: str
) -> None:
    value = _config()
    mutation(value)
    path = tmp_path / "config.json"
    _write(path, value)

    with pytest.raises(ValueError, match=message):
        RuntimeConfig.from_file(path)


def test_runtime_config_requires_default_context_window(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write(path, _config(context_window_tokens={"coding": 200_000}))

    with pytest.raises(ValueError, match="必须包含 default"):
        RuntimeConfig.from_file(path)


def test_native_capabilities_cannot_expose_runtime_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write(path, _config())
    config = RuntimeConfig.from_file(path)
    provider = NativeProvider(
        tmp_path,
        blocked_paths=(config.source_path,),
        secret_values=config.secret_values,
    )

    with pytest.raises(PermissionDenied, match="runtime configuration"):
        provider._safe_path("config.json", must_exist=True)
    assert provider._glob({"pattern": "*.json"}, None)["matches"] == []
    assert provider._redact("model-secret github-secret context7-secret") == (
        "[REDACTED] [REDACTED] [REDACTED]"
    )
