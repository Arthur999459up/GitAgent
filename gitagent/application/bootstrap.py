"""Assemble the live, Session-aware application facade."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gitagent.domain.errors import StateError, ValidationError
from gitagent.domain.models import SessionScope
from gitagent.harness.context import CompactResult, ContextBuilder
from gitagent.infra.github import GitHubClient
from gitagent.infra.observability import TraceBus
from gitagent.infra.persistence import (
    MemoryRecord,
    SessionManager,
    SessionRecord,
    StateStore,
    build_account_key,
    build_repository_key,
    merge_working_state,
)
from gitagent.model import ChatClient, LiteLLMChatClient, LLMReasoner, OpenAIChatClient
from gitagent.prompts import get_prompt_library

from .capabilities import build_capability_layer
from .config import CLIConfig
from .projection import project_output, project_service_result
from .service import GitAgentService

Renderer = Callable[[Any], None]

@dataclass
class LiveApplication:
    config: CLIConfig
    github: GitHubClient
    llm: ChatClient
    reasoner: LLMReasoner
    trace: TraceBus
    service: GitAgentService
    store: StateStore
    sessions: SessionManager
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
        account_key = build_account_key(self.config.github_api_url, authenticated_user_id)
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

    def list_account_sessions(self, authenticated_user_id: int) -> tuple[SessionRecord, ...]:
        account_key = build_account_key(self.config.github_api_url, authenticated_user_id)
        return self.sessions.list_account_sessions(account_key)

    def resume_session(self, authenticated_user_id: int, session_id: str) -> SessionRecord:
        account_key = build_account_key(self.config.github_api_url, authenticated_user_id)
        target = self.sessions.get_account_session(account_key, session_id)
        if target is None:
            raise StateError("Session not found")
        staged = self._prepare_service(target.scope)
        self.repository = target.repository_full_name
        self.scope = target.scope
        self._swap_service(staged)
        return target

    def delete_account_session(self, authenticated_user_id: int, session_id: str) -> SessionRecord:
        if self.scope is not None:
            raise StateError("account Session deletion is only available before entering a Session")
        account_key = build_account_key(self.config.github_api_url, authenticated_user_id)
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
        turn = self.sessions.start_turn(scope, text)
        dispatch_started = False
        rendered = False
        try:
            routing_context = self.context_builder.build(
                scope,
                repository,
                text,
                prompt_renderer=lambda context: self.service.main_agent.render_input_context(
                    text, repository, context
                ),
            )
            try:
                result = self.service.handle(
                    text, repository=repository, routing_context=routing_context, session_scope=scope
                )
            except BaseException:
                dispatch_started = self.service.dispatch_started
                raise
            dispatch_started = self.service.dispatch_started
            projection = project_service_result(result, turn_seq=turn.seq, text_sanitizer=self.store.text)
            current = self._session().working_state
            next_state = merge_working_state(current, projection=projection)
            if result.agent is None and not result.decision.clarify:
                next_state["open_question"] = current["open_question"]
            if renderer is not None:
                renderer(result)
                rendered = True
            self.sessions.complete_turn(
                scope, turn.seq, assistant_text=projection.assistant_text, history_text=projection.history_text,
                route_summary=projection.route_summary, entity_manifests=projection.entity_manifests,
                working_state=next_state,
            )
            return result
        except KeyboardInterrupt as exc:
            self._fail_turn(scope, turn.seq, exc)
            rebuild_error = self._rebuild_current_service() if dispatch_started else None
            if rebuild_error is not None:
                raise StateError(
                    "本轮已中断；运行时审批已失效，而且新的 Service 无法建立。"
                    "Session 中保存的 agent context 仍在，请重新选择 Session 或仓库恢复执行环境"
                ) from exc
            raise
        except Exception as exc:
            self._fail_turn(scope, turn.seq, exc)
            rebuild_error = self._rebuild_current_service() if dispatch_started else None
            if rendered:
                cleanup_note = "；新的 Service 也未能建立" if rebuild_error is not None else ""
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
        return self._run_context_command(lambda: self.service.revise_proposal(instruction), renderer=renderer)

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
        target = self.sessions.get_session(scope.account_key, scope.repository_key, session_id)
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
        target = self.sessions.get_session(scope.account_key, scope.repository_key, session_id)
        if target is None:
            raise StateError("Session not found")
        if session_id != scope.session_id:
            return self.sessions.delete_session(target.scope)

        remaining = [item for item in self.list_sessions() if item.session_id != session_id]
        replacement_id = remaining[0].session_id if remaining else _new_session_id()
        replacement_scope = SessionScope(scope.account_key, scope.repository_key, replacement_id)
        staged = self._prepare_service(replacement_scope)
        if remaining:
            replacement = remaining[0]
            self.sessions.delete_session(target.scope)
        else:
            replacement = self.sessions.replace_session(target.scope, replacement_id)
        self.scope = replacement_scope
        self._swap_service(staged)
        return replacement

    def compact(self) -> CompactResult:
        return self.context_builder.compact(self._require_scope())

    def remember(self, scope: str, kind: str, content: str) -> tuple[MemoryRecord, bool]:
        current = self._require_scope()
        return self.sessions.remember(
            current.account_key,
            current.repository_key,
            scope=scope,
            kind=kind,
            content=content,
        )

    def list_memories(self, *, scope: str | None = None) -> tuple[MemoryRecord, ...]:
        current = self._require_scope()
        return self.sessions.list_memories(current.account_key, current.repository_key, scope)

    def forget(self, memory_id: str) -> MemoryRecord:
        current = self._require_scope()
        forgotten = self.sessions.forget(current.account_key, current.repository_key, memory_id)
        if forgotten is None:
            raise StateError("Memory not found")
        return forgotten

    def _run_context_command(self, operation: Callable[[], Any], *, renderer: Renderer | None) -> Any:
        self._require_scope()
        try:
            output = operation()
            project_output(output, text_sanitizer=self.store.text)
            if renderer is not None:
                renderer(output)
            return output
        except BaseException as exc:
            rebuild_error = self._rebuild_current_service()
            cleanup_note = "；新的 Service 未能建立" if rebuild_error is not None else ""
            raise StateError(
                f"Session agent 操作失败{cleanup_note}；运行时审批已失效，请核对外部状态后再继续"
            ) from exc

    def _prepare_service(self, scope: SessionScope | None) -> GitAgentService:
        if self._service_factory is not None:
            return self._service_factory(scope)
        return GitAgentService(
            build_capability_layer(self.github, trace=self.trace, reasoner=self.reasoner),
            main_reasoner=self.reasoner,
            agent_reasoner=self.reasoner,
            session_manager=self.sessions,
            trace=self.trace,
            session_scope=scope,
            input_budget_tokens=self.config.effective_input_budget,
        )

    def _swap_service(self, service: GitAgentService) -> None:
        previous = self.service
        if previous is not service:
            previous.invalidate()
        self.service = service

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
        session = self.sessions.get_session(scope.account_key, scope.repository_key, scope.session_id)
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
        except Exception:  # noqa: BLE001 - preserve the original failure; startup recovery owns the row
            # The original started row remains and startup recovery will interrupt it.
            return


def build_live_application(config: CLIConfig) -> LiveApplication:
    config.validate()
    if not config.api_key:
        raise ValidationError("未找到模型 API Key；请设置 OPENAI_API_KEY 或 GITAGENT_API_KEY")
    if config.provider not in {"openai", "litellm"}:
        raise ValidationError("GITAGENT_PROVIDER 必须是 openai 或 litellm")

    library = get_prompt_library()
    library.validate()
    if config.prompts_dir is not None and Path(config.prompts_dir).resolve() != library.root:
        raise ValidationError(
            f"GITAGENT_PROMPTS_DIR 指向 {config.prompts_dir!r}，但提示词库加载自 {library.root}"
        )

    client_class = LiteLLMChatClient if config.provider == "litellm" else OpenAIChatClient
    llm = client_class(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout=config.effective_llm_timeout,
    )
    reasoner = LLMReasoner(llm)
    trace = TraceBus()
    github = GitHubClient(
        token=config.github_token,
        api_url=config.github_api_url,
        timeout=config.effective_github_timeout,
    )
    store = StateStore(config.state_path, secret_values=(config.github_token, config.api_key))
    sessions = SessionManager(store)
    context_builder = ContextBuilder(
        sessions,
        context_window_tokens=config.context_window_tokens,
        max_output_tokens=config.max_tokens,
        safety_tokens=config.context_safety_tokens,
    )
    stateless_service = GitAgentService(
        build_capability_layer(github, trace=trace, reasoner=reasoner),
        main_reasoner=reasoner,
        agent_reasoner=reasoner,
        session_manager=sessions,
        trace=trace,
        input_budget_tokens=config.effective_input_budget,
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
        context_builder=context_builder,
    )


def _new_session_id() -> str:
    return f"session-{uuid.uuid4().hex}"
