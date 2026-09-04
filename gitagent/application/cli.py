"""通过 GitHub 身份选择仓库的 GitAgent 交互式 CLI。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from prompt_toolkit import prompt as terminal_prompt
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gitagent.capability.rag import (
    KnowledgeBaseManager,
    KnowledgeBaseStatus,
    RAGUnavailableError,
)
from gitagent.domain.errors import GitAgentError, ValidationError
from gitagent.domain.models import (
    DraftResult,
    IssueAgentResult,
    MutationRejectedResult,
    PullRequestAgentResult,
    RepositoryResult,
    VerificationReport,
    to_plain,
)
from gitagent.harness.context.state import AgentContext
from gitagent.infra.persistence import SessionRecord

from .bootstrap import LiveApplication, build_live_application
from .config import RuntimeConfig
from .projection import MAX_VISIBLE_ITEMS, visible_items
from .service import ServiceResult
from .terminal_ui import TerminalUI

console = Console()
ui = TerminalUI(console)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitagent",
        description="安全、可审计的个人 GitHub 仓库维护助手。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="运行时 JSON 配置文件（默认：当前目录的 config.json）",
    )
    parser.add_argument("-v", "--version", action="version", version="gitagent 0.1.0")
    commands = parser.add_subparsers(dest="command")
    rag = commands.add_parser("rag", help="管理内置的只读 Markdown 知识库")
    rag_actions = rag.add_subparsers(dest="rag_action", required=True)
    register = rag_actions.add_parser(
        "register", help="注册并索引 database/knowledge_base/<kb-id>"
    )
    register.add_argument("knowledge_base_id", help="知识库 ID，例如 engineering")
    register.add_argument(
        "-d", "--description", required=True, help="供 Agent 选择知识库时使用的说明"
    )
    sync = rag_actions.add_parser("sync", help="增量同步知识库")
    sync.add_argument("knowledge_base_id")
    rag_actions.add_parser("list", help="列出已注册知识库")
    status = rag_actions.add_parser("status", help="查看知识库与运行状态")
    status.add_argument("knowledge_base_id")
    remove = rag_actions.add_parser("remove", help="删除注册信息和派生索引")
    remove.add_argument("knowledge_base_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "rag":
        try:
            return _run_rag_management(args)
        except (GitAgentError, RAGUnavailableError, ValueError, OSError) as exc:
            ui.text(str(exc), title="Error · RAG 管理失败", kind="error")
            return 1
    try:
        config = RuntimeConfig.from_file(args.config)
        application = build_live_application(config)
        application.trace.subscribe(ui.trace)
        if not _select_startup_session(application):
            application.close()
            return 0
    except (GitAgentError, OSError, TypeError, ValueError) as exc:
        ui.text(str(exc), title="Error · 启动失败", kind="error")
        _show_configuration_hint()
        return 1
    except (EOFError, KeyboardInterrupt):
        console.print("\n已取消启动。")
        return 0

    try:
        return _repl(application)
    finally:
        application.close()


def _run_rag_management(args: argparse.Namespace) -> int:
    manager = KnowledgeBaseManager()
    if args.rag_action == "register":
        knowledge_base = manager.register_knowledge_base(
            args.knowledge_base_id,
            args.description,
        )
        ui.json(knowledge_base.to_dict(), title="RAG · Registered")
        return 0
    if args.rag_action == "sync":
        result = manager.sync(args.knowledge_base_id)
        ui.json(result.to_dict(), title="RAG · Synced")
        return 0
    if args.rag_action == "list":
        table = Table(title="RAG Knowledge Bases")
        table.add_column("ID", style="cyan")
        table.add_column("Status")
        table.add_column("Documents", justify="right")
        table.add_column("Source")
        for knowledge_base in manager.list():
            table.add_row(
                knowledge_base.id,
                knowledge_base.status.value,
                str(len(knowledge_base.documents)),
                knowledge_base.source_directory,
            )
        console.print(table)
        return 0
    if args.rag_action == "status":
        knowledge_base = manager.get(args.knowledge_base_id)
        if knowledge_base.status != KnowledgeBaseStatus.ERROR:
            knowledge_base = manager.freshness_check(args.knowledge_base_id)
        effective_status, reason = manager.capability_status(knowledge_base)
        value = knowledge_base.to_dict()
        value["status"] = effective_status.value
        if reason:
            value["unavailable_reason"] = reason
        ui.json(value, title="RAG · Status")
        return 0
    if args.rag_action == "remove":
        removed = manager.remove(args.knowledge_base_id)
        ui.json(
            {
                "knowledge_base": removed.id,
                "removed": True,
                "source_directory_preserved": removed.source_directory,
            },
            title="RAG · Removed",
        )
        return 0
    raise ValidationError(f"unknown RAG action: {args.rag_action}")


def _select_startup_session(application: LiveApplication) -> bool:
    """让已认证账号显式恢复、创建或删除 Session。"""
    with console.status("[cyan]正在验证 GitHub Token…"):
        account = application.github.get_authenticated_user()
    try:
        authenticated_user_id = _github_numeric_id(account["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GitAgentError("GitHub 未返回稳定的账号数字 ID") from exc

    login = str(account.get("login") or "unknown")
    while True:
        sessions = application.list_account_sessions(authenticated_user_id)
        _show_sessions(sessions, active_session_id=None, title=f"@{login} Sessions")
        choice = terminal_prompt(
            "操作：[编号] 恢复  [n] 新建  [d 编号] 删除  [q] 退出\n> "
        ).strip()
        normalized = choice.casefold()
        if normalized in {"q", "quit", "exit", "/quit", "/exit"}:
            return False
        if normalized in {"n", "new", "/new"}:
            return _select_repository(application, account=account)

        delete_reference = _delete_reference(choice)
        if delete_reference is not None:
            try:
                target = _session_from_number(sessions, delete_reference)
                application.delete_account_session(
                    authenticated_user_id, target.session_id
                )
                console.print(f"已删除 Session [cyan]{target.session_id}[/cyan]。")
            except (GitAgentError, ValueError) as exc:
                console.print(f"[red]Session 删除失败：{exc}[/red]")
            continue

        try:
            target = _session_from_number(sessions, choice)
            resumed = application.resume_session(
                authenticated_user_id, target.session_id
            )
        except (GitAgentError, ValueError) as exc:
            console.print(f"[yellow]无法恢复 Session：{exc}[/yellow]")
            continue
        console.print(
            f"已恢复 [bold cyan]{resumed.repository_full_name}[/bold cyan] · "
            f"[cyan]{resumed.title}[/cyan]"
        )
        _show_session_safety_note()
        return True


def _select_repository(
    application: LiveApplication, *, account: dict[str, Any] | None = None
) -> bool:
    """读取 Token 可访问仓库，并为所选仓库创建一个全新 Session。"""
    previous_repository = application.repository
    with console.status("[cyan]正在读取可访问仓库…"):
        if account is None:
            account = application.github.get_authenticated_user()
        repositories = application.github.list_repositories()
    if not repositories:
        raise GitAgentError("当前 GitHub Token 没有可访问的仓库")

    login = account.get("login") or "unknown"
    candidates = repositories
    while True:
        visible = candidates[:20]
        _render_repository_choices(visible, login=login, total=len(candidates))
        choice = terminal_prompt(
            "选择仓库（序号或名称，输入关键词筛选，quit 退出）> "
        ).strip()
        if choice.casefold() in {"quit", "exit", "/quit", "/exit"}:
            return False
        if choice.isdigit() and 1 <= int(choice) <= len(visible):
            selected = visible[int(choice) - 1]
            break

        exact = next(
            (
                repository
                for repository in repositories
                if repository["full_name"].casefold() == choice.casefold()
            ),
            None,
        )
        if exact:
            selected = exact
            break
        candidates = [
            repository
            for repository in repositories
            if choice.casefold() in repository["full_name"].casefold()
        ]
        if not candidates:
            console.print(f"[yellow]未找到匹配 {choice!r} 的可访问仓库。[/yellow]")
            candidates = repositories

    try:
        authenticated_user_id = _github_numeric_id(account["id"])
        repository_id = _github_numeric_id(selected["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GitAgentError("GitHub 未返回稳定的账号或仓库数字 ID") from exc
    repository = str(selected["full_name"])
    application.create_session(
        authenticated_user_id=authenticated_user_id,
        repository_id=repository_id,
        repository_full_name=repository,
    )
    console.print(f"已为 [bold cyan]{repository}[/bold cyan] 创建新 Session")
    if previous_repository:
        _show_session_safety_note()
    return True


def _github_numeric_id(value: Any) -> int:
    """Normalize a GitHub ID without accepting booleans or lossy numeric coercion."""
    if isinstance(value, bool):
        raise TypeError("GitHub ID must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        result = int(value)
    else:
        raise TypeError("GitHub ID must be a positive integer")
    if result <= 0:
        raise ValueError("GitHub ID must be a positive integer")
    return result


def _render_repository_choices(
    repositories: list[dict[str, Any]], *, login: str, total: int
) -> None:
    table = Table(title=f"@{login} 可访问的仓库（当前筛选 {total} 个，最多显示 20 个）")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("仓库")
    table.add_column("可见性")
    table.add_column("状态")
    for index, repository in enumerate(repositories, 1):
        visibility = "private" if repository.get("private") else "public"
        state = "archived" if repository.get("archived") else "active"
        table.add_row(str(index), str(repository["full_name"]), visibility, state)
    console.print(table)


def _repl(application: LiveApplication) -> int:
    console.print(
        Panel.fit(
            f"[bold]{application.repository}[/bold]  ·  {application.config.model}\n"
            f"[dim]session {application.session_id}[/dim]\n"
            "[dim]自然语言提问 · /help 命令 · /context 上下文 · /latency 延迟 · /trace 轨迹 · quit 退出[/dim]",
            title="[bold cyan]GitAgent[/bold cyan] [dim]v0.1.0[/dim]",
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )
    )
    history = InMemoryHistory()
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        try:
            # Memory stop hooks finish on worker threads. StdoutProxy lets their
            # status lines render above the active input and then redraws the
            # prompt, instead of splicing output into the user's text.
            with patch_stdout(raw=True):
                request = terminal_prompt(
                    "You › ",
                    history=history,
                    multiline=True,
                    key_bindings=bindings,
                    prompt_continuation="...  ",
                ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见！")
            return 0
        if not request:
            continue
        if request.casefold() in {"quit", "exit", "/quit", "/exit"}:
            return 0
        if request.startswith("/"):
            try:
                _run_command(application, request)
            except KeyboardInterrupt:
                ui.text("当前命令已中断。", title="Interrupted", kind="approval")
            except Exception as exc:  # noqa: BLE001 - facade 已负责 fail closed，REPL 需继续服务
                ui.text(str(exc), title="Error · 命令失败", kind="error")
            continue
        try:
            application.handle(
                request,
                renderer=lambda result: _render_application_output(application, result),
            )
        except KeyboardInterrupt:
            ui.text("当前请求已中断。", title="Interrupted", kind="approval")
        except Exception as exc:  # noqa: BLE001 - REPL 必须隔离单轮错误并继续服务
            ui.text(str(exc), title="Error · 请求失败", kind="error")


def _run_command(application: LiveApplication, request: str) -> None:
    command, _, argument = request.partition(" ")
    argument = argument.strip()
    if command == "/help":
        _show_help()
    elif command == "/repo":
        if argument:
            console.print(
                "[yellow]/repo 不接受仓库名，请从 Token 可访问仓库列表中选择。[/yellow]"
            )
            return
        try:
            _select_repository(application)
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]仓库读取失败：{exc}[/red]")
    elif command == "/tokens":
        prompt_tokens = int(getattr(application.llm, "total_prompt_tokens", 0))
        completion_tokens = int(getattr(application.llm, "total_completion_tokens", 0))
        text = f"Tokens：{prompt_tokens} prompt + {completion_tokens} completion = {prompt_tokens + completion_tokens} total"
        cost = getattr(application.llm, "estimated_cost", None)
        if cost is not None:
            text += f"（约 ${cost:.4f}）"
        console.print(text)
    elif command == "/context":
        if argument:
            console.print("[yellow]用法：/context[/yellow]")
            return
        ui.context_usage(
            application.context_usage(), session_id=application.session_id or ""
        )
    elif command == "/latency":
        if argument:
            console.print("[yellow]用法：/latency[/yellow]")
            return
        ui.turn_latencies(
            application.turn_latencies(), session_id=application.session_id or ""
        )
    elif command == "/audit":
        events = application.service.harness.audit.events(
            argument or application.session_id
        )
        ui.json(to_plain(events), title="Audit Log")
    elif command == "/trace":
        ui.trace_history(application.trace.events(argument or application.session_id))
    elif command == "/debug":
        _show_debug_history(application, argument)
    elif command == "/sessions":
        if argument:
            console.print("[yellow]用法：/sessions[/yellow]")
            return
        try:
            _show_sessions(
                application.list_sessions(), active_session_id=application.session_id
            )
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Session 读取失败：{exc}[/red]")
    elif command == "/new":
        if argument:
            console.print("[yellow]用法：/new[/yellow]")
            return
        try:
            application.new_session()
            console.print(f"已创建并进入 Session [cyan]{application.session_id}[/cyan]")
            _show_session_safety_note()
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Session 创建失败：{exc}[/red]")
    elif command == "/switch":
        if not argument:
            console.print("[yellow]用法：/switch <编号>[/yellow]")
            return
        previous_session_id = application.session_id
        try:
            target = _session_from_number(application.list_sessions(), argument)
            application.switch_session(target.session_id)
            if application.session_id == previous_session_id:
                console.print(
                    f"Session [cyan]{application.session_id}[/cyan] 已是当前 Session；未更改状态。"
                )
            else:
                console.print(f"已切换到 Session [cyan]{application.session_id}[/cyan]")
                _show_session_safety_note()
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Session 切换失败：{exc}[/red]")
    elif command == "/reset":
        if argument:
            console.print("[yellow]用法：/reset[/yellow]")
            return
        try:
            application.reset_session()
            ui.text(
                "已建立新的 Context 边界；历史 Turn 和 Memory 仍保留，旧 Workflow 与审批已失效。",
                title="Session Reset",
                kind="approval",
            )
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Session reset 失败：{exc}[/red]")
    elif command == "/delete":
        if not argument:
            console.print("[yellow]用法：/delete <编号>[/yellow]")
            return
        try:
            target = _session_from_number(application.list_sessions(), argument)
            deleting_active_session = target.session_id == application.session_id
            application.delete_session(target.session_id)
            console.print(
                f"已删除 Session [cyan]{target.session_id}[/cyan]；"
                f"当前 Session：[cyan]{application.session_id}[/cyan]"
            )
            if deleting_active_session:
                _show_session_safety_note()
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Session 删除失败：{exc}[/red]")
    elif command == "/compact":
        if argument:
            console.print("[yellow]用法：/compact[/yellow]")
            return
        try:
            result = application.compact()
            if not result.changed:
                console.print("[dim]当前没有可压缩的旧 Context。[/dim]")
                return
            ui.compaction(
                automatic=False,
                agent="main",
                level=result.level,
                before_tokens=result.before_tokens,
                after_tokens=result.after_tokens,
                context_window_tokens=result.context_window_tokens,
            )
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Context 压缩失败：{exc}[/red]")
    else:
        console.print(f"[yellow]未知命令：{command}（使用 /help 查看命令）[/yellow]")


def _render_application_output(application: LiveApplication, output: Any) -> None:
    """Render facade callbacks with the established typed-result renderer."""
    if isinstance(output, ServiceResult):
        _render_result(application, output)
    elif isinstance(output, str):
        ui.text(output, title="GitAgent", kind="info")
    else:
        _render_output(application, output)


def _render_result(application: LiveApplication, result: ServiceResult) -> None:
    if result.agent is None and isinstance(result.output, str):
        ui.markdown(result.output, title="GitAgent", kind="info")
        return
    _render_output(application, result.output)


def _render_output(application: LiveApplication, output: Any) -> None:
    if output is None:
        return
    if isinstance(output, str):
        ui.markdown(output, title="GitAgent", kind="info")
        return
    if isinstance(output, DraftResult):
        content = output.body
        if output.note:
            content += f"\n\n---\n{output.note}"
        ui.markdown(content, title=output.title, kind="agent")
        return
    if isinstance(output, MutationRejectedResult):
        ui.markdown(
            f"**操作：** {output.summary}\n\n**结果：** 未执行\n\n**失败原因：** {output.reason}",
            title="操作未执行",
            kind="agent",
        )
        return
    if isinstance(output, AgentContext):
        _render_context(application, output)
        return
    if isinstance(output, IssueAgentResult):
        visible, truncated = visible_items(output.issues)
        operation = output.operation.value if output.operation else ""
        content = output.answer
        if operation in {"LIST", "SUMMARIZE"} and visible:
            items = "\n".join(
                f"- **#{issue.number}** {issue.title}  `{issue.state}`"
                + (
                    f"  {', '.join(f'`{label}`' for label in issue.labels)}"
                    if issue.labels
                    else ""
                )
                for issue in visible
            )
            content = f"{content}\n\n{items}" if content else items
        if truncated:
            content += f"\n\n_仅显示前 {MAX_VISIBLE_ITEMS} 项。_"
        title = (
            f"Issue #{output.issue_number}"
            if output.issue_number
            else f"Issues · {len(output.issues)}"
        )
        ui.markdown(content, title=title, kind="agent")
        return
    if isinstance(output, PullRequestAgentResult):
        visible, truncated = visible_items(output.pull_requests)
        operation = output.operation.value if output.operation else ""
        content = output.answer
        if operation in {"LIST", "SUMMARIZE"} and visible:
            items = "\n".join(
                f"- **#{pull_request.number}** {pull_request.title}  `{pull_request.state}`  "
                f"`{pull_request.head or '?'} → {pull_request.base or '?'}`"
                + ("  `draft`" if pull_request.draft else "")
                for pull_request in visible
            )
            content = f"{content}\n\n{items}" if content else items
        if truncated:
            content += f"\n\n_仅显示前 {MAX_VISIBLE_ITEMS} 项。_"
        if output.changed_files:
            content += "\n\n**变更文件**  " + ", ".join(
                f"`{path}`" for path in output.changed_files
            )
        title = (
            f"PR #{output.pr_number}"
            if output.pr_number
            else f"Pull Requests · {len(output.pull_requests)}"
        )
        ui.markdown(content, title=title, kind="agent")
        return
    if isinstance(output, RepositoryResult):
        content = output.answer
        if output.files:
            content += "\n\n---\n**相关文件：** " + ", ".join(
                f"`{path}`" for path in output.files
            )
        if output.symbols:
            content += "\n\n**相关符号：** " + ", ".join(
                f"`{symbol}`" for symbol in output.symbols
            )
        ui.markdown(
            content, title=f"Repository · {output.operation.value}", kind="agent"
        )
        return
    raise ValidationError(f"不支持渲染结果类型：{type(output).__name__}")


def _render_context(application: LiveApplication, context: AgentContext) -> None:
    if context.error:
        ui.text(context.error, title="执行失败", kind="error")
        return
    if context.waiting_question:
        ui.text(context.waiting_question, title="需要补充信息", kind="router")
        return
    if context.pending is not None:
        _render_proposal(application, context)
        return
    if context.result is not None:
        _render_output(application, context.result)
        return
    ui.text(context.final_message or "执行已结束。", title="GitAgent", kind="info")


def _render_proposal(application: LiveApplication, context: AgentContext) -> None:
    calls = context.pending.calls
    if context.issue_reply is not None and context.issue_reply.draft and any(
        call.capability_id == "github.post_comment" for call in calls
    ):
        issue_number = next(
            (
                call.arguments.get("issue_number")
                for call in calls
                if call.capability_id == "github.post_comment"
            ),
            context.entity_id,
        )
        ui.markdown(
            context.issue_reply.draft,
            title=f"Issue #{issue_number} 回复草稿 · 待发布审批",
            kind="agent",
        )
    elif any(
        call.capability_id
        in {"github.create_draft_pr", "github.commit_to_default_branch"}
        for call in calls
    ):
        _render_code_change_proposal(application, context)
    else:
        details = []
        for call in context.pending.calls:
            arguments = json.dumps(
                call.arguments, ensure_ascii=False, indent=2, sort_keys=True
            )
            details.append(f"### `{call.capability_id}`\n```json\n{arguments}\n```")
        payload = "\n\n".join(details) or "（内部操作，无外部写入参数）"
        ui.markdown(
            f"{context.pending.summary}\n\n{payload}",
            title="变更提案 · 待批准",
            kind="agent",
        )
    _render_approval(application, context.pending.approval_id)


def _render_code_change_proposal(
    application: LiveApplication, context: AgentContext
) -> None:
    candidate = context.code_candidate
    if candidate is None:
        ui.text(context.pending.summary, title="代码变更 · 待批准", kind="agent")
        return
    files = ", ".join(f"`{path}`" for path in candidate.changed_files)
    content = (
        f"{candidate.summary}\n\n"
        f"**根因：** {candidate.root_cause}\n\n"
        f"**新增文件：** {', '.join(f'`{path}`' for path in candidate.added_files) or '无'}\n\n"
        f"**修改文件：** {', '.join(f'`{path}`' for path in candidate.modified_files) or '无'}\n\n"
        f"**删除文件：** {', '.join(f'`{path}`' for path in candidate.deleted_files) or '无'}\n\n"
        f"**全部变更：** {files}\n\n"
        f"### Diff\n```diff\n{candidate.patch}\n```"
    )
    direct_call = next(
        (
            call
            for call in context.pending.calls
            if call.capability_id == "github.commit_to_default_branch"
        ),
        None,
    )
    if direct_call is not None:
        content += (
            f"\n\n### 目标\n直接提交到默认分支 `{context.change_request.base_branch if context.change_request else ''}`"
            f"\n\n### Commit message\n{direct_call.arguments.get('message', '')}"
        )
    pr_call = next(
        (
            call
            for call in context.pending.calls
            if call.capability_id == "github.create_draft_pr"
        ),
        None,
    )
    if pr_call is not None:
        content += (
            f"\n\n### Draft PR 标题\n{pr_call.arguments.get('title', '')}\n\n"
            f"### Draft PR 正文\n{pr_call.arguments.get('body', '')}"
        )
    ui.markdown(content, title="代码变更 · 待批准", kind="agent")
    if context.verification is not None:
        _render_verification(context.verification)


def _render_verification(report: VerificationReport) -> None:
    checks = "\n".join(
        f"- **{check.name}**: {check.status} — {check.details}"
        for check in report.checks
    )
    skipped = "\n".join(f"- {item}" for item in report.skipped)
    content = checks or "无检查结果"
    if skipped:
        content += f"\n\n### Skipped\n{skipped}"
    ui.markdown(
        content,
        title=f"Verification · {'PASS' if report.passed else 'FAIL'}",
        kind="verification_pass" if report.passed else "verification_fail",
        subtitle=f"attempt={report.attempts}",
    )


def _render_approval(application: LiveApplication, approval_id: str | None) -> None:
    if not approval_id:
        return
    approval = application.service.harness.approvals.get(approval_id)
    if approval.decision is None:
        ui.markdown(
            "**批准**  回复 `可以` / `就这么改` / `同意`\n\n"
            "**修改**  直接说修改要求，例如 `README 不要动，其他执行`\n\n"
            "**拒绝**  回复 `算了` / `不要执行`",
            title="需要你的确认",
            kind="approval",
        )


def _show_sessions(
    sessions: Sequence[SessionRecord],
    *,
    active_session_id: str | None,
    title: str = "Sessions",
) -> None:
    if not sessions:
        console.print("[dim]当前没有 Session。[/dim]")
        return
    table = Table(title=title)
    table.add_column("#", justify="right", style="cyan")
    show_current = active_session_id is not None
    if show_current:
        table.add_column("当前", justify="center")
    table.add_column("标题")
    table.add_column("仓库")
    table.add_column("创建时间")
    table.add_column("更新时间")
    table.add_column("Session ID", style="dim")
    for index, session in enumerate(sessions, 1):
        session_id = session.session_id
        row = [
            str(index),
            session.title,
            session.repository_full_name,
            session.created_at,
            session.updated_at,
            session_id,
        ]
        if show_current:
            row.insert(1, "●" if session_id == active_session_id else "")
        table.add_row(*row)
    console.print(table)


def _session_from_number(
    sessions: Sequence[SessionRecord], number: str
) -> SessionRecord:
    value = number.strip()
    if not value:
        raise ValidationError("请提供 Session 编号")
    if not value.isascii() or not value.isdecimal():
        raise ValidationError("Session 必须使用 /sessions 显示的数字编号")
    index = int(value)
    if 1 <= index <= len(sessions):
        return sessions[index - 1]
    if sessions:
        raise ValidationError(f"Session 编号必须在 1–{len(sessions)} 之间")
    raise ValidationError("当前没有可选的 Session")


def _delete_reference(choice: str) -> str | None:
    command, separator, reference = choice.strip().partition(" ")
    if command.casefold() not in {"d", "delete", "/delete"}:
        return None
    if not separator or not reference.strip():
        return ""
    return reference.strip()


def _show_session_safety_note() -> None:
    console.print(
        "[dim]后续请求只载入当前 Session 的历史，以及当前账号/仓库作用域的 Memory；"
        "运行时审批 ID 不跨 Service 复用。[/dim]"
    )


def _show_debug_history(application: LiveApplication, argument: str) -> None:
    parts = argument.split()
    if len(parts) > 2:
        console.print(
            "[yellow]用法：/debug [agent] 或 /debug <session_id> [agent][/yellow]"
        )
        return
    session_id = application.session_id
    if session_id is None:
        raise ValidationError("select a repository before reading Debug History")
    agent: str | None = None
    if len(parts) == 1:
        if parts[0].startswith("session-"):
            session_id = parts[0]
        else:
            agent = parts[0]
    elif len(parts) == 2:
        session_id, agent = parts
        if not session_id.startswith("session-"):
            console.print(
                "[yellow]用法：/debug [agent] 或 /debug <session_id> [agent][/yellow]"
            )
            return
    events = application.trace.debug_events(session_id, agent)
    ui.debug_history(events, session_id=session_id, agent=agent)


def _show_help() -> None:
    console.print(
        Panel(
            "[bold]会话命令[/bold]\n"
            "  /help                 显示帮助\n"
            "  /repo                 重新读取列表并选择仓库\n"
            "  /sessions             列出当前仓库的 Sessions\n"
            "  /new                  为当前仓库创建并进入新 Session\n"
            "  /switch <编号>        切换 Session\n"
            "  /reset                保留 Turn 并建立新 Context 边界\n"
            "  /delete <编号>        删除 Session\n"
            "  /compact              压缩旧 Turn 的 Context 投影\n"
            "  /tokens               查看模型 token 与估算费用\n"
            "  /context              查看各 Agent 上下文占用\n"
            "  /latency              查看当前 Session 每轮端到端耗时\n"
            "  /audit [session_id]   查看当前或指定 Session 审计记录\n"
            "  /trace [session_id]   回放当前或指定 Session 实时调用轨迹\n"
            "  /debug [agent]        查看当前 Session 的 Agent Debug History\n"
            "  /debug <session_id> [agent]  查看指定 Session/Agent 的 Debug History\n"
            "  quit                  退出\n\n"
            "[bold]输入[/bold]\n"
            "  Enter                 提交请求\n"
            "  Esc+Enter             插入换行",
            title="GitAgent Help",
            border_style="dim",
        )
    )


def _show_configuration_hint() -> None:
    console.print("\n请检查 config.json；也可通过 --config 指定其他 JSON 配置文件。")
