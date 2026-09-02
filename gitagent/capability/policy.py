"""Default-deny capability discovery, invocation, approval, and Bash policy."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, ClassVar

from gitagent.domain.errors import ApprovalRequired, ValidationError
from gitagent.harness.constraints import ApprovalStore

from .models import AccessLevel, Capability, CapabilityKind, InvocationContext


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

    _GIT_READ_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "grep", "ls-files"})
    _SHELL_OPERATORS = frozenset({";", "&&", "||", "|", ">", ">>", "<", "<<", "&", "(", ")"})
    _VERSION_ARGUMENTS: ClassVar[dict[str, frozenset[str]]] = {
        "pytest": frozenset({"--version"}),
        "ruff": frozenset({"-V", "--version"}),
        "mypy": frozenset({"-V", "--version"}),
        "pyright": frozenset({"--version"}),
        "python": frozenset({"-V", "--version"}),
        "python3": frozenset({"-V", "--version"}),
        "npm": frozenset({"-v", "--version"}),
        "pnpm": frozenset({"-v", "--version"}),
        "yarn": frozenset({"-v", "--version"}),
        "cargo": frozenset({"-V", "--version"}),
        "go": frozenset({"version"}),
    }
    _PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn"})
    _PACKAGE_VALIDATION_SCRIPTS = frozenset(
        {"test", "lint", "typecheck", "check", "build"}
    )
    _PACKAGE_DANGEROUS_SUBCOMMANDS = frozenset(
        {"install", "add", "remove", "publish", "exec", "dlx"}
    )
    _DANGEROUS_SCRIPT_WORDS = frozenset({"deploy", "release", "publish"})

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
        if profile != "coding":
            return Authorization(PermissionDecision.DENY, "no Bash profile is configured", profile)
        if self.is_real_verification(command, profile) or self._is_observation(tokens):
            return Authorization(PermissionDecision.ALLOW, bash_profile=profile)

        executable = Path(tokens[0]).name
        subcommand = tokens[1].casefold() if len(tokens) >= 2 else ""
        if (
            executable in self._PACKAGE_MANAGERS
            and subcommand in self._PACKAGE_DANGEROUS_SUBCOMMANDS
        ):
            return Authorization(
                PermissionDecision.DENY,
                f"{executable} {subcommand} can modify dependencies or publish content",
                profile,
            )
        if executable == "cargo" and subcommand in {"publish", "install"}:
            return Authorization(
                PermissionDecision.DENY,
                f"cargo {subcommand} can modify external state",
                profile,
            )
        if executable == "go" and subcommand in {"get", "install"}:
            return Authorization(
                PermissionDecision.DENY,
                f"go {subcommand} can modify dependencies or install binaries",
                profile,
            )
        return Authorization(
            PermissionDecision.ASK,
            "command is not an approved observation or verification command",
            profile,
        )

    def is_real_verification(self, command: str, profile: str | None) -> bool:
        """Classify commands that validate executable code rather than merely observe it."""
        if profile != "coding":
            return False
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return False
        if not tokens or any(token in self._SHELL_OPERATORS or "`" in token for token in tokens):
            return False
        executable = Path(tokens[0]).name
        if tokens[0] != executable:
            return False
        if executable in {"pytest", "mypy", "pyright"}:
            return len(tokens) == 1 or not self._has_version_or_help(tokens[1:])
        if executable == "ruff":
            return (
                len(tokens) >= 2
                and tokens[1].casefold() == "check"
                and not self._has_version_or_help(tokens[2:])
            )
        if executable in {"python", "python3"}:
            return (
                len(tokens) >= 3
                and tokens[1] == "-m"
                and tokens[2] in {"compileall", "py_compile", "pytest"}
                and not self._has_version_or_help(tokens[3:])
            )
        if executable in self._PACKAGE_MANAGERS:
            script = self._package_script(tokens)
            return (
                script is not None
                and self._is_validation_script(script)
                and not self._has_version_or_help(tokens[2:])
            )
        if executable == "cargo":
            return (
                len(tokens) >= 2
                and tokens[1].casefold() in {"test", "check", "clippy", "build"}
                and not self._has_version_or_help(tokens[2:])
            )
        if executable == "go":
            return (
                len(tokens) >= 2
                and tokens[1].casefold() in {"test", "vet", "build"}
                and not self._has_version_or_help(tokens[2:])
            )
        return False

    def _is_observation(self, tokens: list[str]) -> bool:
        executable = Path(tokens[0]).name
        if tokens[0] != executable:
            return False
        if executable == "git":
            return (
                len(tokens) >= 2
                and tokens[1] in self._GIT_READ_SUBCOMMANDS
                and not any(
                    token == "--ext-diff"
                    or token == "--textconv"
                    or token == "--output"
                    or token.startswith("--output=")
                    for token in tokens[2:]
                )
            )
        return (
            len(tokens) == 2
            and tokens[1] in self._VERSION_ARGUMENTS.get(executable, ())
        )

    def _package_script(self, tokens: list[str]) -> str | None:
        if len(tokens) < 2:
            return None
        subcommand = tokens[1].casefold()
        if subcommand == "run":
            return tokens[2].casefold() if len(tokens) >= 3 else None
        return subcommand

    def _is_validation_script(self, script: str) -> bool:
        words = {word for word in re.split(r"[:._-]+", script) if word}
        return bool(words & self._PACKAGE_VALIDATION_SCRIPTS) and not bool(
            words & self._DANGEROUS_SCRIPT_WORDS
        )

    @staticmethod
    def _has_version_or_help(arguments: list[str]) -> bool:
        return any(
            argument in {"-h", "--help", "-V", "--version"}
            for argument in arguments
        )


class PermissionPolicy:
    _AGENT_KEYS = frozenset({"discover", "invoke", "bash_profile"})
    _INVOKE_KEYS = frozenset({"allow", "ask", "deny"})
    _BASH_PROFILES = frozenset({"coding"})

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
                coding_workspace_mutation = self._is_coding_workspace_mutation(
                    agent_id, capability
                )
                if (
                    in_allow
                    and capability.access != AccessLevel.READ
                    and capability.id != "native.bash"
                    and not coding_workspace_mutation
                ):
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
                coding_workspace_mutation = self._is_coding_workspace_mutation(
                    agent_id, capability
                )
                if (
                    capability.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
                    and capability.id != "native.bash"
                    and capability.id not in buckets["ask"]
                    and not (coding_workspace_mutation and capability.id in buckets["allow"])
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

        coding_workspace_mutation = self._is_coding_workspace_mutation(
            context.agent_id, capability
        )
        if coding_workspace_mutation:
            if not context.workspace_root:
                return Authorization(
                    PermissionDecision.DENY,
                    "Coding workspace mutations require an active isolated worktree",
                    profile,
                )
            if not self._matches(capability.id, invocation.get("allow") or []):
                return Authorization(
                    PermissionDecision.DENY,
                    "Coding workspace mutation is outside the agent invoke policy",
                    profile,
                )
            return Authorization(PermissionDecision.ALLOW, bash_profile=profile)

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
            if (
                context.agent_id == "coding"
                and context.workspace_root
                and bash_decision.decision == PermissionDecision.ASK
            ):
                return Authorization(PermissionDecision.DENY, bash_decision.reason, profile)
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

    def is_bash_verification(self, command: str, context: InvocationContext) -> bool:
        config = self._agents.get(context.agent_id)
        if self._config_error(context.agent_id, config):
            return False
        profile = str(config.get("bash_profile") or "") or None
        return self.bash.is_real_verification(command, profile)

    @staticmethod
    def _is_coding_workspace_mutation(agent_id: str, capability: Capability) -> bool:
        properties = (
            capability.input_schema.get("properties", {})
            if isinstance(capability.input_schema, dict)
            else {}
        )
        return (
            agent_id == "coding"
            and capability.kind == CapabilityKind.NATIVE_TOOL
            and capability.access in {AccessLevel.WRITE, AccessLevel.DESTRUCTIVE}
            and isinstance(properties, dict)
            and "path" in properties
        )

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
