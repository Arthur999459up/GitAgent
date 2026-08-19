"""Runtime loader for every LLM-facing prompt template.

All prompt wording lives in ``gitagent/prompts/**/*.md`` so operators can tune
it without editing code. ``PromptLibrary`` reads those files once at startup and
renders ``{{placeholder}}`` substitutions. A template with no placeholders is
served verbatim by :meth:`text`; dynamic templates go through :meth:`render`.

The directory is resolved from this package, so the bundle works from a source
tree and a wheel. ``GITAGENT_PROMPTS_DIR`` can redirect it, but that value is
captured here before dotenv loads — a repository-controlled ``.env`` can never
redirect prompt loading (mirrors ``_STARTUP_STATE_PATH`` in ``app/config.py``).

Schemas the validator depends on (``INTENT_SCHEMA`` / ``APPROVAL_SCHEMA``) stay
in code; they are function-calling contracts, not wording.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_RESIDUAL = re.compile(r"\{\{|\}\}")

_DEFAULT_DIR = Path(__file__).resolve().parent
# Captured before dotenv (like GITAGENT_STATE_PATH): a repository-controlled
# .env must never be able to redirect the prompt directory.
_ENV_DIR = os.environ.get("GITAGENT_PROMPTS_DIR")
_library: PromptLibrary | None = None


class PromptError(ValueError):
    """Raised for invalid template access; the message names the key and file."""


def _normalise(text: str) -> str:
    """Tolerate CRLF and a trailing editor newline while keeping byte fidelity."""
    return text.replace("\r\n", "\n").removesuffix("\n")


class PromptLibrary:
    """Loads every prompt template under a root directory once, then renders it."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root).resolve() if root is not None else _DEFAULT_DIR
        if not self._root.is_dir():
            raise PromptError(f"prompts root is not a directory: {self._root}")
        self._templates: dict[str, str] = {}
        self._paths: dict[str, Path] = {}
        self._load()

    @property
    def root(self) -> Path:
        return self._root

    def _load(self) -> None:
        markdown = sorted(
            path
            for path in self._root.rglob("*.md")
            if path.name != "README.md"
        )
        if not markdown:
            raise PromptError(f"prompts root contains no markdown templates: {self._root}")
        for path in markdown:
            key = str(path.relative_to(self._root).with_suffix("")).replace(os.sep, ".")
            if key in self._templates:
                raise PromptError(f"duplicate prompt template key {key!r} ({path})")
            self._templates[key] = _normalise(path.read_text(encoding="utf-8"))
            self._paths[key] = path

    def _template(self, key: str) -> str:
        try:
            return self._templates[key]
        except KeyError as exc:
            raise PromptError(
                f"unknown prompt template {key!r}; known keys: {', '.join(sorted(self._templates))}"
            ) from exc

    def _path(self, key: str) -> Path:
        return self._paths[key]

    def _has_residual(self, key: str) -> bool:
        """True when the template still has unmatched braces after valid placeholders."""
        template = self._template(key)
        return bool(_RESIDUAL.search(_PLACEHOLDER.sub("", template)))

    def keys(self) -> frozenset[str]:
        return frozenset(self._templates)

    def placeholders(self, key: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_PLACEHOLDER.findall(self._template(key))))

    def text(self, key: str) -> str:
        template = self._template(key)
        if _RESIDUAL.search(template):
            raise PromptError(
                f"prompt {key!r} ({self._path(key)}) contains '{{{{'/'}}}}'; use render()"
            )
        return template

    def render(self, key: str, **values: object) -> str:
        template = self._template(key)
        if self._has_residual(key):
            raise PromptError(f"prompt {key!r} ({self._path(key)}) has unmatched '{{{{' or '}}}}'")
        placeholders = tuple(dict.fromkeys(_PLACEHOLDER.findall(template)))
        missing = [name for name in placeholders if name not in values]
        if missing:
            raise PromptError(f"render({key!r}) is missing values: {', '.join(missing)}")
        extra = sorted(set(values) - set(placeholders))
        if extra:
            raise PromptError(f"render({key!r}) got unexpected values: {', '.join(extra)}")
        for name in placeholders:
            if values[name] is None:
                raise PromptError(f"render({key!r}) value for {name!r} must not be None")
        # Only the original template is scanned; substituted values are never
        # re-scanned, so a value may legitimately contain braces (JSON bodies).
        return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template)

    def validate(self) -> None:
        """Fail fast on malformed prompt templates."""
        for key in sorted(self._templates):
            if self._has_residual(key):
                raise PromptError(f"prompt {key!r} ({self._path(key)}) has unmatched '{{{{' or '}}}}'")


def get_prompt_library() -> PromptLibrary:
    """Return the process-global library, built from the package or the env override."""
    global _library
    if _library is None:
        _library = PromptLibrary(Path(_ENV_DIR) if _ENV_DIR else _DEFAULT_DIR)
    return _library


def configure(root: str | Path | None = None) -> PromptLibrary:
    """Point the process-global library at another directory (tests/overrides)."""
    global _library, _ENV_DIR
    if root is not None:
        _ENV_DIR = str(Path(root))
    _library = PromptLibrary(Path(_ENV_DIR) if _ENV_DIR else _DEFAULT_DIR)
    return _library
