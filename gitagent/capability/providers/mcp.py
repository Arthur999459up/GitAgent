"""MCP capability definitions, local/remote client ownership, and dispatch."""

from __future__ import annotations

from dataclasses import dataclass, replace
from inspect import Parameter, signature
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from gitagent.domain.errors import (
    ExternalExecutionError,
    PermissionDenied,
    ResourceNotFoundError,
    ValidationError,
)

from ..errors import (
    ProviderAuthenticationError,
    ProviderConflictError,
    ProviderExecutionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..models import (
    AccessLevel,
    Capability,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRegistration,
    CapabilityStatus,
    InvocationContext,
)


@dataclass(frozen=True)
class MCPServerDefinition:
    id: str
    transport: str
    config: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True)
class MCPToolDefinition:
    id: str
    server_id: str
    remote_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    access: AccessLevel


class MCPProvider:
    id = "mcp"

    def __init__(
        self,
        servers: list[MCPServerDefinition],
        tools: list[MCPToolDefinition],
        *,
        clients: dict[str, Any],
    ) -> None:
        self._servers = {server.id: server for server in servers}
        self._tools = list(tools)
        self._clients = dict(clients)

    def load(self) -> list[CapabilityRegistration]:
        registrations: list[CapabilityRegistration] = []
        for definition in self._tools:
            server = self._servers[definition.server_id]
            client = self._clients.get(definition.server_id)
            available = server.enabled and client is not None and bool(getattr(client, "available", True))
            source_id = definition.id.rsplit(".", 1)[0]
            registrations.append(
                CapabilityRegistration(
                    Capability(
                        definition.id,
                        CapabilityKind.MCP_TOOL,
                        definition.description,
                        source_id,
                        CapabilityStatus.AVAILABLE if available else CapabilityStatus.UNAVAILABLE,
                        definition.access,
                        definition.input_schema,
                        definition.output_schema,
                    ),
                    CapabilityBinding(definition.id, self.id, definition),
                )
            )
        return registrations

    def invoke(
        self,
        binding: CapabilityBinding,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> Any:
        definition = binding.target
        if not isinstance(definition, MCPToolDefinition):
            raise TypeError("MCP binding target is invalid")
        client = self._clients.get(definition.server_id)
        if client is None:
            raise ProviderUnavailableError(f"MCP server is not connected: {definition.server_id}")
        call_arguments = dict(arguments)
        server = self._servers[definition.server_id]
        if server.config.get("inject_repository"):
            if not context.repository:
                raise ValidationError("repository context is required")
            call_arguments["repository"] = context.repository
        try:
            if hasattr(client, "call_tool"):
                return client.call_tool(definition.remote_name, call_arguments)
            return getattr(client, definition.remote_name)(**call_arguments)
        except Exception as exc:  # noqa: BLE001 - Provider boundary normalizes expected client failures
            self._raise_normalized_transport(exc, mutation=definition.access != AccessLevel.READ)

    def reconnect(self, binding: CapabilityBinding) -> None:
        definition = binding.target
        if not isinstance(definition, MCPToolDefinition):
            raise TypeError("MCP binding target is invalid")
        client = self._clients.get(definition.server_id)
        if client is not None and hasattr(client, "reconnect"):
            try:
                client.reconnect()
            except Exception as exc:  # noqa: BLE001 - Provider boundary normalizes transport failures
                self._raise_normalized_transport(exc, mutation=False)

    def refresh(self) -> None:
        refreshed: list[MCPToolDefinition] = []
        for server_id in self._servers:
            server_tools = [item for item in self._tools if item.server_id == server_id]
            client = self._clients.get(server_id)
            if client is None or not hasattr(client, "list_tools"):
                refreshed.extend(server_tools)
                continue
            try:
                listed_tools = client.list_tools()
            except Exception as exc:  # noqa: BLE001 - Provider boundary normalizes transport failures
                self._raise_normalized_transport(exc, mutation=False)
            discovered = {str(item.get("name")): item for item in listed_tools}
            for definition in server_tools:
                remote = discovered.get(definition.remote_name)
                if remote is None:
                    continue
                refreshed.append(
                    replace(
                        definition,
                        description=str(remote.get("description") or definition.description),
                        input_schema=dict(remote.get("inputSchema") or definition.input_schema),
                        output_schema=(
                            dict(remote["outputSchema"])
                            if isinstance(remote.get("outputSchema"), dict)
                            else None
                        ),
                    )
                )
        self._tools = refreshed

    @staticmethod
    def _raise_normalized_transport(exc: Exception, *, mutation: bool) -> None:
        status = getattr(exc, "status_code", None)
        retry_after = getattr(exc, "retry_after", None)
        request_sent = bool(getattr(exc, "request_sent", mutation))
        if status == 401:
            raise ProviderAuthenticationError(str(exc)) from exc
        if status == 404:
            raise ResourceNotFoundError(str(exc)) from exc
        if status == 409:
            raise ProviderConflictError(str(exc)) from exc
        if status == 429:
            raise ProviderRateLimitError(str(exc), retry_after=retry_after) from exc
        if status == 408 or isinstance(exc, TimeoutError) or bool(getattr(exc, "timed_out", False)):
            raise ProviderTimeoutError(str(exc), request_sent=request_sent) from exc
        if bool(getattr(exc, "transport_unavailable", False)):
            if mutation and request_sent:
                raise ProviderTimeoutError(str(exc), request_sent=True) from exc
            raise ProviderUnavailableError(str(exc)) from exc
        if isinstance(exc, ConnectionError) or status in {500, 502, 503, 504}:
            if mutation and request_sent:
                raise ProviderTimeoutError(str(exc), request_sent=True) from exc
            raise ProviderUnavailableError(str(exc)) from exc
        if isinstance(
            exc,
            ResourceNotFoundError | ValidationError | PermissionDenied | ExternalExecutionError | ValueError | TypeError,
        ):
            raise exc
        raise ProviderExecutionError(str(exc)) from exc


_READ_IDS = frozenset(
    {
        "repository.get_default_branch",
        "repository.get_file_status",
        "repository.get_repo_tree",
        "repository.search_code",
        "repository.read_file",
        "repository.read_files",
        "repository.find_symbol",
        "repository.find_references",
        "repository.get_pr_diff",
        "repository.get_changed_files",
        "repository.get_file_history",
        "github.get_issue",
        "github.list_issues",
        "github.get_issue_comments",
        "github.list_milestones",
        "github.get_pr",
        "github.list_pull_requests",
        "github.get_pr_comments",
        "github.get_pr_reviews",
        "github.get_workflow_runs",
        "github.get_job_logs",
    }
)
_WRITE_IDS = frozenset(
    {
        "github.post_comment",
        "github.create_issue",
        "github.update_issue",
        "github.set_issue_lock",
        "github.update_pr",
        "github.create_branch",
        "github.push",
        "github.create_draft_pr",
        "github.post_review",
    }
)
_DESTRUCTIVE_IDS = frozenset(
    {"github.commit", "github.commit_to_default_branch", "github.merge"}
)

_DESCRIPTIONS = {
    "repository.get_default_branch": "Fetch the repository default branch and current commit identifier.",
    "repository.get_file_status": "Check which targeted repository paths exist at a ref.",
    "repository.get_repo_tree": "List a bounded remote repository tree.",
    "repository.search_code": "Search remote repository file content.",
    "repository.read_file": "Read a bounded line range from one remote file.",
    "repository.read_files": "Read multiple targeted remote file ranges.",
    "repository.find_symbol": "Find symbol definitions in repository source.",
    "repository.find_references": "Find textual references to one symbol.",
    "repository.get_pr_diff": "Fetch a bounded pull-request diff.",
    "repository.get_changed_files": "Fetch changed paths for a pull request.",
    "repository.get_file_history": "Fetch bounded history for one repository file.",
    "github.get_issue": "Fetch one GitHub issue.",
    "github.list_issues": "List and filter bounded GitHub issue metadata.",
    "github.get_issue_comments": "Fetch bounded issue comments.",
    "github.list_milestones": "List GitHub milestones.",
    "github.get_pr": "Fetch one pull request.",
    "github.list_pull_requests": "List and filter pull requests.",
    "github.get_pr_comments": "Fetch pull-request comments.",
    "github.get_pr_reviews": "Fetch pull-request reviews.",
    "github.get_workflow_runs": "Fetch workflow-run metadata.",
    "github.get_job_logs": "Fetch a bounded workflow job log.",
    "github.post_comment": "Post an issue or pull-request comment.",
    "github.create_issue": "Create a GitHub issue.",
    "github.update_issue": "Update GitHub issue fields.",
    "github.set_issue_lock": "Lock or unlock an issue discussion.",
    "github.update_pr": "Update pull-request state.",
    "github.create_branch": "Create a GitHub branch.",
    "github.commit": "Commit exact file additions, modifications, and deletions.",
    "github.commit_to_default_branch": "Commit exact changes to the default branch.",
    "github.push": "Confirm a prepared GitHub branch is published.",
    "github.create_draft_pr": "Create a draft pull request.",
    "github.post_review": "Publish a pull-request review.",
    "github.merge": "Merge a pull request at an exact reviewed head.",
}


def github_tool_definitions(client: Any, *, server_id: str = "github") -> list[MCPToolDefinition]:
    definitions: list[MCPToolDefinition] = []
    for capability_id in sorted(_READ_IDS | _WRITE_IDS | _DESTRUCTIVE_IDS):
        remote_name = capability_id.split(".", 1)[1]
        handler = getattr(client, remote_name)
        access = (
            AccessLevel.READ
            if capability_id in _READ_IDS
            else AccessLevel.WRITE
            if capability_id in _WRITE_IDS
            else AccessLevel.DESTRUCTIVE
        )
        definitions.append(
            MCPToolDefinition(
                capability_id,
                server_id,
                remote_name,
                _DESCRIPTIONS[capability_id],
                callable_schema(handler, exclude=frozenset({"repository"})),
                _annotation_schema(get_type_hints(handler).get("return", Any)),
                access,
            )
        )
    return definitions


def context7_tool_definitions(*, server_id: str = "context7") -> list[MCPToolDefinition]:
    return [
        MCPToolDefinition(
            "context7.resolve-library-id",
            server_id,
            "resolve-library-id",
            "Resolve a library name to a Context7-compatible library identifier.",
            _object_schema(
                {
                    "libraryName": {"type": "string", "minLength": 1},
                    "query": {"type": "string", "minLength": 1},
                },
                ["libraryName", "query"],
            ),
            None,
            AccessLevel.READ,
        ),
        MCPToolDefinition(
            "context7.query-docs",
            server_id,
            "query-docs",
            "Query current library documentation through Context7.",
            _object_schema(
                {
                    "libraryId": {"type": "string", "minLength": 1},
                    "query": {"type": "string", "minLength": 1},
                },
                ["libraryId", "query"],
            ),
            None,
            AccessLevel.READ,
        ),
    ]


def callable_schema(handler: Any, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    hints = get_type_hints(handler)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature(handler).parameters.items():
        if (
            name == "self"
            or name in exclude
            or parameter.kind == Parameter.KEYWORD_ONLY
            and name.startswith("max_")
        ):
            continue
        properties[name] = _annotation_schema(hints.get(name, Any))
        if parameter.default is Parameter.empty:
            required.append(name)
    return _object_schema(properties, required)


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation is Any:
        return {"type": ["null", "object", "array", "string", "integer", "number", "boolean"]}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {Union, UnionType}:
        types: list[str] = []
        for item in args:
            schema_type = _annotation_schema(item)["type"]
            types.extend(schema_type if isinstance(schema_type, list) else [schema_type])
        return {"type": list(dict.fromkeys(types))}
    if origin is list:
        return {"type": "array", "items": _annotation_schema(args[0] if args else Any)}
    if origin is dict:
        return {"type": "object", "additionalProperties": _annotation_schema(args[1] if len(args) > 1 else Any)}
    mapping = {str: "string", int: "integer", float: "number", bool: "boolean", type(None): "null"}
    if annotation in mapping:
        return {"type": mapping[annotation]}
    return {"type": ["null", "object", "array", "string", "integer", "number", "boolean"]}
