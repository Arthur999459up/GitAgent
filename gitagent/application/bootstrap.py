"""Assemble the live, Session-aware application facade."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from gitagent.domain.errors import StateError, ValidationError
from gitagent.domain.models import SessionScope
from gitagent.harness.context import (
    CompactionResult,
    ContextBuilder,
    derive_main_messages,
)
from gitagent.infra.github import GitHubClient
from gitagent.infra.observability import TraceBus
from gitagent.infra.persistence import (
    SessionEventLog,
    SessionEventRecorder,
    SessionManager,
    SessionRecord,
    StateStore,
    build_account_key,
    build_repository_key,
    merge_working_state,
)
from gitagent.memory import (
    AutoDream,
    MemoryExtractionContextBuilder,
    MemoryExtractor,
    MemoryPage,
    MemoryPageStore,
    MemorySearch,
    MemoryStopHooks,
)
from gitagent.model import ChatClient, LLMReasoner, OpenAIChatClient
from gitagent.prompts import get_prompt_library

from .capabilities import build_capability_layer
from .config import RuntimeConfig
from .projection import project_output, project_service_result
from .service import GitAgentService

Renderer = Callable[[Any], None]


@dataclass(frozen=True)
class IndexedMemory:
    """One CLI-visible memory with a short repository-scope index."""

    index: int
    scope: str
    item: MemoryPage


@dataclass
class LiveApplication:
    config: RuntimeConfig
    github: GitHubClient
    llm: ChatClient
    reasoner: LLMReasoner
    trace: TraceBus
    service: GitAgentService
    store: StateStore
    sessions: SessionManager
    memory: MemoryPageStore
    memory_search: MemorySearch
    memory_hooks: MemoryStopHooks
    context_builder: ContextBuilder
    repository: str | None = None
    scope: SessionScope | None = None
    _service_factory: Callable[[SessionScope | None], GitAgentService] | None = field(
        default=None,
        repr=False,
    )

    @property
    def session_id(self) -> str | None:
        return self.scope.session_id if self.scope is not None else None

    def create_session(
        self,
        *,
        authenticated_user_id: int,
        repository_id: int,
        repository_full_name: str,
    ) -> SessionRecord:
        account_key = build_account_key(
            self.config.github_api_url, authenticated_user_id
        )
        repository_key = build_repository_key(self.config.github_api_url, repository_id)
        session_id = _new_session_id()
        target_scope = SessionScope(account_key, repository_key, session_id)
        staged_service = self._prepare_service(target_scope)
        created = self.sessions.create_session(
            account_key,
            repository_key,
            repository_full_name,
            session_id=session_id,
        )
        self.repository = repository_full_name
        self.scope = target_scope
        self._swap_service(staged_service)
        return created

    def list_account_sessions(
        self, authenticated_user_id: int
    ) -> tuple[SessionRecord, ...]:
        account_key = build_account_key(
            self.config.github_api_url, authenticated_user_id
        )
        return self.sessions.list_account_sessions(account_key)

    def resume_session(
        self, authenticated_user_id: int, session_id: str
    ) -> SessionRecord:
        account_key = build_account_key(
            self.config.github_api_url, authenticated_user_id
        )
        target = self.sessions.get_account_session(account_key, session_id)
        if target is None:
            raise StateError("Session not found")
        staged = self._prepare_service(target.scope)
        self.repository = target.repository_full_name
        self.scope = target.scope
        self._swap_service(staged)
        return target

    def delete_account_session(
        self, authenticated_user_id: int, session_id: str
    ) -> SessionRecord:
        if self.scope is not None:
            raise StateError(
                "account Session deletion is only available before entering a Session"
            )
        account_key = build_account_key(
            self.config.github_api_url, authenticated_user_id
        )
        target = self.sessions.get_account_session(account_key, session_id)
        if target is None:
            raise StateError("Session not found")
        return self.sessions.delete_session(target.scope)

    def handle(self, user_input: str, *, renderer: Renderer | None = None) -> Any:
        scope = self._require_scope()
        repository = self._require_repository()
        text = user_input.strip()
        if not text:
            raise ValidationError("request cannot be empty")
        stored_context = self._session().agent_context
        waiting_agent = _waiting_child_agent(
            stored_context,
            main_messages=derive_main_messages(
                self.sessions.event_log.iter_events(scope)
            ),
        )
        turn = self.sessions.start_turn(scope, text, agent=waiting_agent)
        dispatch_started = False
        rendered = False
        try:
            memory_context = self.memory_search.context(
                scope.account_key, scope.repository_key, text
            )
            main_tools = self.service.main_agent.provider_tools(
                session_id=scope.session_id,
                repository=repository,
                goal=text,
            )
            main_messages, main_tools = self.context_builder.build(
                scope,
                repository,
                text,
                system=self.service.main_agent.current_system(
                    repository=repository,
                    memory_context=memory_context,
                ),
                tools=main_tools,
                turn_seq=turn.seq,
                current_user_is_main=waiting_agent is None,
            )
            compaction = self.context_builder.last_compaction
            if compaction is not None and compaction.changed:
                self.trace.emit_auto_compaction(
                    session_id=scope.session_id,
                    agent="main",
                    level=compaction.level,
                    before_tokens=compaction.before_tokens,
                    after_tokens=compaction.after_tokens,
                    context_window_tokens=compaction.context_window_tokens,
                    turn_seq=turn.seq,
                )
            try:
                result = self.service.handle(
                    text,
                    repository=repository,
                    main_messages=main_messages,
                    main_tools=main_tools,
                    session_scope=scope,
                    turn_seq=turn.seq,
                )
            except BaseException:
                dispatch_started = self.service.dispatch_started
                raise
            dispatch_started = self.service.dispatch_started
            projection = project_service_result(
                result, turn_seq=turn.seq, text_sanitizer=self.store.text
            )
            current = self._session().working_state
            next_state = merge_working_state(current, projection=projection)
            if result.agent is None:
                next_state["open_question"] = current["open_question"]
            if renderer is not None:
                renderer(result)
                rendered = True
            self.sessions.complete_turn(
                scope,
                turn.seq,
                assistant_text=projection.assistant_text,
                assistant_agent=(
                    result.output_agent if result.output_agent != "main" else None
                ),
                workflow_summary=projection.workflow_summary,
                route=projection.route,
                entity_manifests=projection.entity_manifests,
                working_state=next_state,
            )
            self.service.memory_after_turn(turn_seq=turn.seq)
            return result
        except KeyboardInterrupt as exc:
            self._fail_turn(scope, turn.seq, exc)
            rebuild_error = (
                self._rebuild_current_service() if dispatch_started else None
            )
            if rebuild_error is not None:
                raise StateError(
                    "本轮已中断；运行时审批已失效，而且新的 Service 无法建立。"
                    "Session 中保存的 agent context 仍在，请重新选择 Session 或仓库恢复执行环境"
                ) from exc
            raise
        except Exception as exc:
            self._fail_turn(scope, turn.seq, exc)
            rebuild_error = (
                self._rebuild_current_service() if dispatch_started else None
            )
            if rendered:
                cleanup_note = (
                    "；新的 Service 也未能建立" if rebuild_error is not None else ""
                )
                raise StateError(
                    "本轮结果已显示但 Turn 未保存；运行时审批已失效"
                    f"{cleanup_note}，请先核对 Session agent context 与外部状态，避免盲目重试"
                ) from exc
            if rebuild_error is not None:
                raise StateError(
                    "本轮失败；运行时审批已失效，而且新的 Service 无法建立。"
                    "Session 中保存的 agent context 仍在，请重新选择 Session 或仓库恢复执行环境"
                ) from exc
            raise

    def approve(self, *, renderer: Renderer | None = None) -> Any:
        return self._run_context_command(self.service.approve, renderer=renderer)

    def reject(self, *, renderer: Renderer | None = None) -> Any:
        return self._run_context_command(self.service.reject, renderer=renderer)

    def revise(self, instruction: str, *, renderer: Renderer | None = None) -> Any:
        if not instruction.strip():
            raise ValidationError("revision instruction cannot be empty")
        return self._run_context_command(
            lambda: self.service.revise_proposal(instruction), renderer=renderer
        )

    def list_sessions(self) -> tuple[SessionRecord, ...]:
        scope = self._require_scope()
        return self.sessions.list_sessions(scope.account_key, scope.repository_key)

    def new_session(self) -> SessionRecord:
        scope = self._require_scope()
        repository = self._require_repository()
        session_id = _new_session_id()
        target_scope = SessionScope(scope.account_key, scope.repository_key, session_id)
        staged = self._prepare_service(target_scope)
        created = self.sessions.create_session(
            scope.account_key,
            scope.repository_key,
            repository,
            session_id=session_id,
        )
        self.scope = target_scope
        self._swap_service(staged)
        return created

    def switch_session(self, session_id: str) -> SessionRecord:
        scope = self._require_scope()
        if session_id == scope.session_id:
            return self._session()
        target = self.sessions.get_session(
            scope.account_key, scope.repository_key, session_id
        )
        if target is None:
            raise StateError("Session not found")
        target_scope = target.scope
        staged = self._prepare_service(target_scope)
        self.scope = target_scope
        self._swap_service(staged)
        return target

    def reset_session(self) -> SessionRecord:
        scope = self._require_scope()
        staged = self._prepare_service(scope)
        reset = self.sessions.reset_session(scope)
        self._swap_service(staged)
        return reset

    def delete_session(self, session_id: str) -> SessionRecord:
        scope = self._require_scope()
        target = self.sessions.get_session(
            scope.account_key, scope.repository_key, session_id
        )
        if target is None:
            raise StateError("Session not found")
        if session_id != scope.session_id:
            return self.sessions.delete_session(target.scope)

        remaining = [
            item for item in self.list_sessions() if item.session_id != session_id
        ]
        replacement_id = remaining[0].session_id if remaining else _new_session_id()
        replacement_scope = SessionScope(
            scope.account_key, scope.repository_key, replacement_id
        )
        staged = self._prepare_service(replacement_scope)
        if remaining:
            replacement = remaining[0]
            self.sessions.delete_session(target.scope)
        else:
            replacement = self.sessions.replace_session(target.scope, replacement_id)
        self.scope = replacement_scope
        self._swap_service(staged)
        return replacement

    def compact(self) -> CompactionResult:
        scope = self._require_scope()
        repository = self._require_repository()
        goal = "/compact"
        memory_context = self.memory_search.context(
            scope.account_key, scope.repository_key, goal
        )
        system = self.service.main_agent.current_system(
            repository=repository,
            memory_context=memory_context,
        )
        tools = self.service.main_agent.provider_tools(
            session_id=scope.session_id,
            repository=repository,
            goal=goal,
        )
        return self.context_builder.compact(scope, system=system, tools=tools)

    def set_memory_automation(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise TypeError("Memory automation state must be a boolean")
        self.config.memory_automation = enabled
        self.memory_hooks.enabled = enabled
        return enabled

    def remember(
        self, content: str, *, scope: str = "private"
    ) -> tuple[IndexedMemory, bool]:
        current = self._require_scope()
        item, created = self.memory.manual_write(
            current.account_key,
            current.repository_key,
            content,
            scope=scope,
        )
        indexed = next(
            indexed
            for indexed in self.indexed_memories(scope=item.scope, include_inactive=True)
            if indexed.item.id == item.id
        )
        return indexed, created

    def indexed_memories(
        self,
        *,
        scope: str | None = None,
        memory_type: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[IndexedMemory, ...]:
        current = self._require_scope()
        pages = self.memory.list_pages(
            current.account_key,
            current.repository_key,
            scope=scope,
            memory_type=memory_type,
            include_inactive=include_inactive,
        )
        return tuple(
            IndexedMemory(index, item.scope, item)
            for index, item in enumerate(pages, start=1)
        )

    def search_memories(self, query: str) -> tuple[Any, ...]:
        current = self._require_scope()
        return self.memory_search.search(
            current.account_key, current.repository_key, query
        )

    def show_memory(self, identifier: str) -> MemoryPage:
        matches = [
            indexed.item
            for indexed in self.indexed_memories(include_inactive=True)
            if identifier in {
                indexed.item.id,
                indexed.item.name,
                indexed.item.relative_path,
            }
        ]
        if len(matches) > 1:
            raise ValidationError("Memory identifier is ambiguous; include its scope")
        if not matches:
            raise StateError("Memory not found")
        return matches[0]

    def forget(self, identifier: str, *, scope: str | None = None) -> MemoryPage:
        current = self._require_scope()
        forgotten = self.memory.forget(
            current.account_key,
            current.repository_key,
            identifier=identifier,
            scope=scope,
        )
        if forgotten is None:
            raise StateError("Memory not found")
        return forgotten

    def dream_memory(self) -> dict[str, tuple[str, ...]] | None:
        self._require_repository()
        return self.service.dream_memory()

    def _run_context_command(
        self, operation: Callable[[], Any], *, renderer: Renderer | None
    ) -> Any:
        self._require_scope()
        try:
            output = operation()
            project_output(output, text_sanitizer=self.store.text)
            if renderer is not None:
                renderer(output)
            return output
        except BaseException as exc:
            rebuild_error = self._rebuild_current_service()
            cleanup_note = (
                "；新的 Service 未能建立" if rebuild_error is not None else ""
            )
            raise StateError(
                f"Session agent 操作失败{cleanup_note}；运行时审批已失效，请核对外部状态后再继续"
            ) from exc

    def _prepare_service(self, scope: SessionScope | None) -> GitAgentService:
        if self._service_factory is not None:
            return self._service_factory(scope)
        return GitAgentService(
            build_capability_layer(
                self.github,
                trace=self.trace,
                context7_api_key=self.config.context7_api_key,
                blocked_paths=(self.config.source_path,),
                secret_values=self.config.secret_values,
                memory_roots=self.memory.roots(scope.account_key, scope.repository_key)
                if scope
                else None,
            ),
            main_reasoner=self.reasoner,
            agent_reasoner=self.reasoner,
            session_manager=self.sessions,
            memory_store=self.memory,
            memory_search=self.memory_search,
            memory_hooks=self.memory_hooks,
            trace=self.trace,
            session_scope=scope,
            context_window_tokens=self.config.context_window_tokens,
            execution=asdict(self.config.execution),
        )

    def _swap_service(self, service: GitAgentService) -> None:
        previous = self.service
        if previous is not service:
            previous.invalidate()
        self.service = service
        if self.scope is not None and self.repository:
            self.memory_hooks.resume_pending(self.scope, self.repository)

    def _rebuild_current_service(self) -> BaseException | None:
        scope = self._require_scope()
        replacement: GitAgentService | None = None
        preparation_error: BaseException | None = None
        try:
            replacement = self._prepare_service(scope)
        except BaseException as exc:  # noqa: BLE001 - revocation must still run
            preparation_error = exc

        if replacement is not None:
            self._swap_service(replacement)
        else:
            self.service.invalidate()
        return preparation_error

    def _session(self) -> SessionRecord:
        scope = self._require_scope()
        session = self.sessions.get_session(
            scope.account_key, scope.repository_key, scope.session_id
        )
        if session is None:
            raise StateError("active Session not found")
        return session

    def _require_scope(self) -> SessionScope:
        if self.scope is None:
            raise StateError("select a repository before using Session state")
        return self.scope

    def _require_repository(self) -> str:
        if not self.repository:
            raise StateError("select a repository before handling a request")
        return self.repository

    def _fail_turn(self, scope: SessionScope, seq: int, error: BaseException) -> None:
        message = f"{type(error).__name__}: {error}"
        try:
            self.sessions.fail_turn(scope, seq, message)
        except Exception:  # noqa: BLE001, S110 - startup recovery owns the row
            # The original started row remains and startup recovery will interrupt it.
            pass


def _waiting_child_agent(
    context: Mapping[str, Any],
    *,
    main_messages: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return the owner of a paused user turn without interpreting its semantics."""

    agent = str(context.get("agent") or "")
    if agent == "main":
        children = context.get("active_children")
        if not isinstance(children, Mapping):
            return None
        ordered_children = []
        if main_messages is not None:
            resolved = {
                str(message.get("tool_call_id") or "")
                for message in main_messages
                if message.get("role") == "tool"
            }
            ordered_children = [
                children[call_id]
                for message in main_messages
                if message.get("role") == "assistant"
                for call in message.get("tool_calls") or []
                for call_id in [str(call.get("id") or "")]
                if call_id not in resolved and call_id in children
            ]
        if not ordered_children:
            ordered_children = list(children.values())
        for child in ordered_children:
            waiting = _waiting_child_agent(child) if isinstance(child, Mapping) else None
            if waiting is not None:
                return waiting
        return None
    if agent not in {"issues", "pull_requests", "repository"}:
        return None
    if bool(context.get("finished")) or context.get("error") is not None:
        return None
    if context.get("pending") is not None or context.get("waiting_for_user") is not None:
        return agent
    if context.get("active_children") or context.get("issue_reply") is not None:
        return agent
    return None


def build_live_application(config: RuntimeConfig) -> LiveApplication:
    config.validate()
    if not config.api_key:
        raise ValidationError("config.json 中的 api_key 不能为空")
    if not config.github_token:
        raise ValidationError("config.json 中的 github_token 不能为空")

    library = get_prompt_library()
    library.validate()

    llm = OpenAIChatClient(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
        context_window_tokens=config.context_window_for("default"),
        timeout=config.llm_timeout,
    )
    reasoner = LLMReasoner(llm)
    github = GitHubClient(
        token=config.github_token,
        api_url=config.github_api_url,
        timeout=config.github_timeout,
    )
    store = StateStore(
        config.state_path,
        secret_values=config.secret_values,
    )
    event_log = SessionEventLog(config.event_path, redactor=store.redact)
    sessions = SessionManager(store, event_log)
    sessions.collect_event_logs(config.event_retention_days)
    trace = TraceBus(
        persistent_sink=SessionEventRecorder(event_log, sessions.scope_for_session)
    )
    memory = MemoryPageStore(
        config.memory_path,
        text_sanitizer=lambda value: store.text(
            value,
            max_characters=8_000,
            reject_secrets=True,
        ),
    )
    memory_search = MemorySearch(memory)
    extractor = MemoryExtractor(
        reasoner,
        memory,
        context_window_tokens=config.context_window_for("default"),
    )
    extraction_contexts = MemoryExtractionContextBuilder(
        sessions,
        memory,
    )
    dream = AutoDream(sessions, memory)
    memory_hooks = MemoryStopHooks(
        sessions,
        extractor,
        extraction_contexts,
        dream,
        trace,
        enabled=config.memory_automation,
    )
    context_builder = ContextBuilder(
        sessions,
        context_window_tokens=config.context_window_for("main"),
    )
    stateless_service = GitAgentService(
        build_capability_layer(
            github,
            trace=trace,
            context7_api_key=config.context7_api_key,
            blocked_paths=(config.source_path,),
            secret_values=config.secret_values,
        ),
        main_reasoner=reasoner,
        agent_reasoner=reasoner,
        session_manager=sessions,
        memory_store=memory,
        memory_search=memory_search,
        memory_hooks=memory_hooks,
        trace=trace,
        context_window_tokens=config.context_window_tokens,
        execution=asdict(config.execution),
    )
    return LiveApplication(
        config=config,
        github=github,
        llm=llm,
        reasoner=reasoner,
        trace=trace,
        service=stateless_service,
        store=store,
        sessions=sessions,
        memory=memory,
        memory_search=memory_search,
        memory_hooks=memory_hooks,
        context_builder=context_builder,
    )


def _new_session_id() -> str:
    return f"session-{uuid.uuid4().hex}"
