"""Prompt templates: all LLM-facing text, editable without touching code."""

from __future__ import annotations

from .library import (
    PromptError,
    PromptLibrary,
    configure,
    get_prompt_library,
)

__all__ = ["PromptError", "PromptLibrary", "configure", "get_prompt_library"]
