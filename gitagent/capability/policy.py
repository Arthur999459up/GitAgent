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
    ASK = "ASK"
    DENY = "DENY"


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
                PermissionDecision.ASK,
                "shell chaining, redirect, pipe, and subshell syntax require approval",
                profile,
            )
        executable = Path(tokens[0]).name
        if profile == "static_only":
            return self._static_decision(executable, tokens, profile)
        if profile != "coding":
            return Authorization(PermissionDecision.DENY, "no Bash profile is configured", profile)
        if executable not in self._CODING_COMMANDS:
            return Authorization(PermissionDecision.ASK, "command is outside the coding allowlist", profile)
        if executable == "git" and (len(tokens) < 2 or tokens[1] not in self._GIT_READ_SUBCOMMANDS):
            return Authorization(PermissionDecision.ASK, "Git mutation requires approval", profile)
        if executable in {"python", "python3"}:
            safe_python = len(tokens) == 2 and tokens[1] in {"-V", "--version"}
            safe_python = safe_python or (len(tokens) >= 3 and tokens[1:3] == ["-m", "py_compile"])
            if not safe_python:
                return Authorization(PermissionDecision.ASK, "arbitrary Python execution requires approval", profile)
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
    _AGENT_KEYS = frozenset({"discover", "invoke", "bash_profile"})
    _INVOKE_KEYS = frozenset({"allow", "ask", "deny"})
    _BASH_PROFILES = frozenset({"coding", "static_only"})

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
        policy = cls(dict(value["agents"]), approvals=approvals, bash=bash)
        policy.validate_structure()
        return policy

    def validate_structure(self) -> None:
        """Reject unsupported or ambiguous policy syntax before capability execution starts."""
        for agent_id, config in self._agents.items():
            error = self._config_error(agent_id, config)
            if error:
                raise ValidationError(error)

    def validate_capabilities(self, capabilities: tuple[Capability, ...]) -> None:
        """Validate policy buckets against registered capability access levels at startup."""
        self.validate_structure()
        for agent_id, config in self._agents.items():
            invocation = config["invoke"]
            buckets = {
                name: {
                    capability.id
                    for capability in capabilities
                    if self._matches(capability.id, invocation.get(name) or [])
                }
                for name in self._INVOKE_KEYS
            }
            for left, right in (("allow", "ask"), ("allow", "deny"), ("ask", "deny")):
                overlap = buckets[left] & buckets[right]
                if overlap:
                    raise ValidationError(
                        f"agent {agent_id} capability policy overlaps {left}/{right}: {min(overlap)}"
                    )
            for capability in capabilities:
                in_allow = capability.id in buckets["allow"]
                in_ask = capability.id in buckets["ask"]
                if in_allow and capability.access != AccessLevel.READ and capability.id != "native.bash":
                    raise ValidationError(
                        f"agent {agent_id} must place {capability.id} in invoke.ask"
                    )
                if in_ask and capability.access == AccessLevel.READ:
                    raise ValidationError(
                        f"agent {agent_id} must place READ capability {capability.id} in invoke.allow"
                    )
                if in_ask and capability.id == "native.bash":
                    raise ValidationError(
                        f"agent {agent_id} must place native.bash in invoke.allow for command-level policy"
                    )

            discover = config.get("discover") or []
            for capability in capabilities:
                if not self._matches(capability.id, discover):
                    continue
                if capability.access == AccessLevel.READ and capability.id not in buckets["allow"]:
                    raise ValidationError(
                        f"agent {agent_id} discovers READ capability {capability.id} without invoke.allow"
                    )
                if capability.id == "native.bash" and capability.id not in buckets["allow"]:
                    raise ValidationError(
                        f"agent {agent_id} discovers native.bash without command-level invoke.allow"
                    )
                if (
                    capability.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
                    and capability.id != "native.bash"
                    and capability.id not in buckets["ask"]
                ):
                    raise ValidationError(
                        f"agent {agent_id} discovers mutation {capability.id} without invoke.ask"
                    )

    def can_discover(self, capability: Capability, context: InvocationContext) -> bool:
        config = self._agents.get(context.agent_id)
        if self._config_error(context.agent_id, config):
            return False
        return self._matches(capability.id, config.get("discover") or [])

    def authorize(
        self,
        capability: Capability,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Authorization:
        config = self._agents.get(context.agent_id)
        config_error = self._config_error(context.agent_id, config)
        if config_error:
            return Authorization(PermissionDecision.DENY, config_error)
        invocation = config["invoke"]
        profile = str(config.get("bash_profile") or "") or None

        if self._matches(capability.id, invocation.get("deny") or []):
            return Authorization(PermissionDecision.DENY, "capability is denied by the agent invoke policy")

        if self._matches(capability.id, invocation.get("ask") or []):
            if capability.access == AccessLevel.READ:
                return Authorization(PermissionDecision.DENY, "READ capabilities must not require approval")
            if not context.approval_id:
                return Authorization(PermissionDecision.ASK, "explicit user approval is required", profile)
            try:
                self.approvals.authorize(
                    approval_id=context.approval_id,
                    session_id=context.session_id,
                    capability_id=capability.id,
                    arguments=arguments,
                )
            except ApprovalRequired as exc:
                return Authorization(PermissionDecision.DENY, str(exc))
            return Authorization(PermissionDecision.ALLOW, bash_profile=profile)

        if not self._matches(capability.id, invocation.get("allow") or []):
            return Authorization(PermissionDecision.DENY, "capability is outside the agent invoke policy")

        if capability.id == "native.bash":
            bash_decision = self.bash.decide(str(arguments.get("command") or ""), profile)
            if bash_decision.decision != PermissionDecision.ASK or not context.approval_id:
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
        if capability.access != AccessLevel.READ:
            return Authorization(
                PermissionDecision.DENY,
                "WRITE and DESTRUCTIVE capabilities must use the agent invoke.ask policy",
            )
        return Authorization(PermissionDecision.ALLOW, bash_profile=profile)

    @classmethod
    def _config_error(cls, agent_id: str, config: Any) -> str:
        if not isinstance(config, dict):
            return "agent has no capability policy"
        unknown_agent_keys = set(config) - cls._AGENT_KEYS
        if unknown_agent_keys:
            return f"agent {agent_id} capability policy has unknown keys: {', '.join(sorted(unknown_agent_keys))}"
        discover = config.get("discover")
        if not cls._is_pattern_list(discover):
            return f"agent {agent_id} discover policy must be a list of capability patterns"
        invocation = config.get("invoke")
        if not isinstance(invocation, dict):
            return f"agent {agent_id} invoke policy is invalid"
        unknown_invoke_keys = set(invocation) - cls._INVOKE_KEYS
        if unknown_invoke_keys:
            return f"agent {agent_id} invoke policy has unknown keys: {', '.join(sorted(unknown_invoke_keys))}"
        for key in cls._INVOKE_KEYS:
            if key in invocation and not cls._is_pattern_list(invocation[key]):
                return f"agent {agent_id} invoke.{key} must be a list of capability patterns"
        profile = config.get("bash_profile")
        if profile is not None and profile not in cls._BASH_PROFILES:
            return f"agent {agent_id} has an invalid Bash profile"
        return ""

    @staticmethod
    def _is_pattern_list(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) and item for item in value)

    @staticmethod
    def _matches(capability_id: str, patterns: list[Any]) -> bool:
        return any(isinstance(pattern, str) and fnmatchcase(capability_id, pattern) for pattern in patterns)
