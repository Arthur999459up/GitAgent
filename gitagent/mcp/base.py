"""MCP 基础公共工具。"""

from __future__ import annotations

from pathlib import PurePosixPath

from ..core.errors import ValidationError


def safe_repository_path(path: str) -> str:
    """校验远程仓库内路径，拒绝空路径和目录穿越。"""
    normalized = str(PurePosixPath(path.strip().lstrip("/")))
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in normalized:
        raise ValidationError(f"invalid repository path: {path!r}")
    return normalized


__all__ = ["safe_repository_path"]
