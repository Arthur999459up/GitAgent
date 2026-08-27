"""Default-deny capability discovery, invocation, approval, and Bash policy."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from gitagent.domain.errors import ApprovalRequired, ValidationError
from gitagent.harness.constraints import ApprovalStore

from .models import AccessLevel, Capability, InvocationContext


class PermissionDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


@dataclass(frozen=True)
class Authorization:
    decision: PermissionDecision
    reason: str = ""
    bash_profile: str | None = None


class BashCommandPolicy:
    """Parse one command and classify it without executing shell syntax."""

    _CODING_COMMANDS = frozenset(
        {
            "pytest",
            "ruff",
            "mypy",
            "pyright",
            "git",
            "python",
            "python3",
            "npm",
            "pnpm",
            "yarn",
            "cargo",
            "go",
        }
    )
    _STATIC_COMMANDS = frozenset({"ruff", "mypy", "pyright", "python", "python3"})
    _GIT_READ_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "grep", "ls-files"})
    _SHELL_OPERATORS = frozenset({";", "&&", "||", "|", ">", ">>", "<", "<<", "&", "(", ")"})

    def decide(self, command: str, profile: str | None) -> Authorization:
        if not command.strip():
            return Authorization(PermissionDecision.DENY, "bash command cannot be empty", profile)
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError as exc:
            return Authorization(PermissionDecision.DENY, f"invalid shell syntax: {exc}", profile)
        if not tokens:
            return Authorization(PermissionDecision.DENY, "bash command cannot be empty", profile)
        if any(token in self._SHELL_OPERATORS or "`" in token for token in tokens):
            return Authorization(
                PermissionDecision.APPROVAL_REQUIRED,
                "shell chaining, redirect, pipe, and subshell syntax require approval",
                profile,
            )
        executable = Path(tokens[0]).name
        if profile == "static_only":
            return self._static_decision(executable, tokens, profile)
        if profile != "coding":
            return Authorization(PermissionDecision.DENY, "no Bash profile is configured", profile)
        if executable not in self._CODING_COMMANDS:
            return Authorization(PermissionDecision.APPROVAL_REQUIRED, "command is outside the coding allowlist", profile)
        if executable == "git" and (len(tokens) < 2 or tokens[1] not in self._GIT_READ_SUBCOMMANDS):
            return Authorization(PermissionDecision.APPROVAL_REQUIRED, "Git mutation requires approval", profile)
        if executable in {"python", "python3"} and len(tokens) >= 2 and tokens[1] not in {"-m", "-V", "--version"}:
            return Authorization(PermissionDecision.APPROVAL_REQUIRED, "arbitrary Python execution requires approval", profile)
        return Authorization(PermissionDecision.ALLOW, bash_profile=profile)

    def _static_decision(self, executable: str, tokens: list[str], profile: str) -> Authorization:
        if executable not in self._STATIC_COMMANDS:
            return Authorization(PermissionDecision.DENY, "static_only permits only static analyzers", profile)
        if executable in {"python", "python3"}:
            allowed = len(tokens) >= 3 and tokens[1:3] == ["-m", "py_compile"]
            if not allowed:
                return Authorization(PermissionDecision.DENY, "static_only Python is limited to -m py_compile", profile)
        if executable == "ruff" and (len(tokens) < 2 or tokens[1] != "check"):
            return Authorization(PermissionDecision.DENY, "static_only ruff is limited to ruff check", profile)
        return Authorization(PermissionDecision.ALLOW, bash_profile=profile)


class PermissionPolicy:
    def __init__(
        self,
        agents: dict[str, dict[str, Any]],
        *,
        approvals: ApprovalStore | None = None,
        bash: BashCommandPolicy | None = None,
    ) -> None:
        self._agents = agents
        self.approvals = approvals or ApprovalStore()
        self.bash = bash or BashCommandPolicy()

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        approvals: ApprovalStore | None = None,
        bash: BashCommandPolicy | None = None,
    ) -> PermissionPolicy:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
        if not isinstance(value, dict) or not isinstance(value.get("agents"), dict):
            raise ValidationError("capabilities policy must contain an agents mapping")
        defaults = value.get("defaults") or {}
        if defaults.get("discover", "deny") != "deny" or defaults.get("invoke", "deny") != "deny":
            raise ValidationError("capabilities policy defaults must be deny")
        return cls(dict(value["agents"]), approvals=approvals, bash=bash)

    def can_discover(self, capability: Capability, context: InvocationContext) -> bool:
        if context.effective_capabilities is not None and capability.id not in context.effective_capabilities:
            return False
        config = self._agents.get(context.agent_id) or {}
        return self._matches(capability.id, config.get("discover") or [])

    def authorize(
        self,
        capability: Capability,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Authorization:
        if context.effective_capabilities is not None and capability.id not in context.effective_capabilities:
            return Authorization(
                PermissionDecision.DENY,
                "capability is outside inherited effective permissions",
            )
        config = self._agents.get(context.agent_id)
        if not isinstance(config, dict):
            return Authorization(PermissionDecision.DENY, "agent has no capability policy")
        invocation = config.get("invoke") or {}
        if not isinstance(invocation, dict):
            return Authorization(PermissionDecision.DENY, "agent invoke policy is invalid")
        if context.read_only and capability.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}:
            return Authorization(PermissionDecision.DENY, "read_only context forbids mutations")
        if self._matches(capability.id, invocation.get("approved_only") or []):
            try:
                self.approvals.authorize(
                    approval_id=context.approval_id,
                    session_id=context.session_id,
                    capability_id=capability.id,
                    arguments=arguments,
                )
            except ApprovalRequired as exc:
                return Authorization(PermissionDecision.DENY, str(exc))
            return Authorization(PermissionDecision.ALLOW)
        if self._matches(capability.id, invocation.get("approval_required") or []):
            return Authorization(PermissionDecision.APPROVAL_REQUIRED, "exact user approval is required")
        if not self._matches(capability.id, invocation.get("allow") or []):
            return Authorization(PermissionDecision.DENY, "capability is outside the agent invoke policy")
        profile = str(invocation.get("bash_profile") or "") or None
        if capability.id == "native.bash":
            bash_decision = self.bash.decide(str(arguments.get("command") or ""), profile)
            if bash_decision.decision != PermissionDecision.APPROVAL_REQUIRED or not context.approval_id:
                return bash_decision
            try:
                self.approvals.authorize(
                    approval_id=context.approval_id,
                    session_id=context.session_id,
                    capability_id=capability.id,
                    arguments=arguments,
                )
            except ApprovalRequired as exc:
                return Authorization(PermissionDecision.DENY, str(exc), profile)
            return Authorization(PermissionDecision.ALLOW, bash_profile=profile)
        if capability.id == "native.agent" and (context.agent_id != "coding" or context.delegation_depth >= 1):
            return Authorization(PermissionDecision.DENY, "sub-agent delegation is limited to one level")
        return Authorization(PermissionDecision.ALLOW, bash_profile=profile)

    @staticmethod
    def _matches(capability_id: str, patterns: list[Any]) -> bool:
        return any(isinstance(pattern, str) and fnmatchcase(capability_id, pattern) for pattern in patterns)
