"""Deterministic, side-effect-free checks for final CodingWorkspace contents."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Mapping

import yaml

from gitagent.domain.models import VerificationCheck
from gitagent.harness.file_access import safe_repository_path


def deterministic_code_checks(files: Mapping[str, str]) -> list[VerificationCheck]:
    """Return bounded syntax/configuration and text hygiene checks."""

    parse_errors: list[str] = []
    hygiene_findings: list[str] = []
    checked = sorted(safe_repository_path(path) for path in files)
    for raw_path, content in files.items():
        path = safe_repository_path(raw_path)
        try:
            if path.endswith(".py"):
                ast.parse(content, filename=path)
            elif path.endswith(".json"):
                json.loads(content)
            elif path.endswith(".toml"):
                tomllib.loads(content)
            elif path.endswith((".yaml", ".yml")):
                yaml.safe_load(content)
        except (SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
            parse_errors.append(f"{path}: {exc}")
        if re.search(r"^(?:<{7}|={7}|>{7})", content, re.MULTILINE):
            parse_errors.append(f"{path}: unresolved merge-conflict marker")
        for number, line in enumerate(content.splitlines(), 1):
            if line.rstrip() != line:
                hygiene_findings.append(f"{path}:{number}: trailing whitespace")
            if "\t" in line and path.endswith(".py"):
                hygiene_findings.append(f"{path}:{number}: tab indentation in Python")
            if len(line) > 160:
                hygiene_findings.append(f"{path}:{number}: line exceeds 160 characters")

    return [
        VerificationCheck(
            name="deterministic_syntax_and_configuration",
            status="FAIL" if parse_errors else "PASS",
            details="; ".join(parse_errors)
            or "Changed files parsed successfully and contain no conflict markers.",
            files=checked,
        ),
        VerificationCheck(
            name="deterministic_text_hygiene",
            status="WARN" if hygiene_findings else "PASS",
            details="; ".join(hygiene_findings) or "No bounded text-hygiene findings.",
            files=checked,
        ),
    ]
