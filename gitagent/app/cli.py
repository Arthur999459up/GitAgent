"""通过 GitHub 身份选择仓库的 GitAgent 交互式 CLI。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from prompt_toolkit import prompt as terminal_prompt
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..context import CompactResult
from ..core.errors import GitAgentError, ValidationError
from ..core.models import (
    CIDiagnosisResult,
    DomainAction,
    DraftResult,
    IssueAgentResult,
    PullRequestAgentResult,
    RepoQAResult,
    VerificationReport,
    to_plain,
)
from ..runtime import AgentContext
from ..state import SessionRecord
from .config import CLIConfig
from .factory import LiveApplication, build_live_application
from .projection import MAX_VISIBLE_ITEMS, visible_items
from .service import ServiceResult
from .ui import TerminalUI

console = Console()
ui = TerminalUI(console)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitagent",
        description="安全、可审计的个人 GitHub 仓库维护助手。",
    )
    parser.add_argument("-m", "--model", help="模型名称，覆盖 GITAGENT_MODEL")
    parser.add_argument("--base-url", help="OpenAI 兼容 API 地址，覆盖 GITAGENT_BASE_URL / OPENAI_BASE_URL")
    parser.add_argument("--api-key", help="模型 API Key；省略时启动后隐藏输入")
    parser.add_argument("--provider", choices=["openai", "litellm"], help="模型后端")
    parser.add_argument("--github-token", help="GitHub Token；省略时启动后隐藏输入")
    parser.add_argument("--github-api-url", help="GitHub API 地址，GitHub Enterprise 可覆盖")
    parser.add_argument("-v", "--version", action="version", version="gitagent 0.1.0")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)
    try:
        if not _collect_credentials(config):
            return 0
        application = build_live_application(config)
        application.trace.subscribe(ui.trace)
        if not _select_repository(application):
            return 0
    except (GitAgentError, ValueError) as exc:
        ui.text(str(exc), title="Error · 启动失败", kind="error")
        _show_configuration_hint()
        return 1
    except (EOFError, KeyboardInterrupt):
        console.print("\n已取消启动。")
        return 0

    return _repl(application)


def _config_from_args(args: argparse.Namespace) -> CLIConfig:
    config = CLIConfig.from_env()
    for argument, attribute in [
        (args.model, "model"),
        (args.base_url, "base_url"),
        (args.api_key, "api_key"),
        (args.provider, "provider"),
        (args.github_token, "github_token"),
        (args.github_api_url, "github_api_url"),
    ]:
        if argument is not None:
            setattr(config, attribute, argument)
    return config


def _collect_credentials(config: CLIConfig) -> bool:
    """缺少凭据时在终端安全读取，不强制用户预先配置环境变量。"""
    if not config.github_token:
        config.github_token = terminal_prompt("GitHub Token > ", is_password=True).strip()
    if not config.github_token:
        ui.text("需要 GitHub Token 才能读取并选择仓库。", title="Credentials", kind="approval")
        return False
    if not config.api_key:
        config.api_key = terminal_prompt("模型 API Key > ", is_password=True).strip()
    if not config.api_key:
        ui.text("需要模型 API Key 才能启动仓库助手。", title="Credentials", kind="approval")
        return False
    return True


def _select_repository(application: LiveApplication) -> bool:
    """读取 Token 可访问仓库，并通过编号或名称筛选进入其中一个仓库。"""
    previous_repository = application.repository
    previous_service = application.service
    with console.status("[cyan]正在验证 GitHub Token 并读取可访问仓库…"):
        account = application.github.get_authenticated_user()
        repositories = application.github.list_repositories()
    if not repositories:
        raise GitAgentError("当前 GitHub Token 没有可访问的仓库")

    login = account.get("login") or "unknown"
    candidates = repositories
    while True:
        visible = candidates[:20]
        _render_repository_choices(visible, login=login, total=len(candidates))
        choice = terminal_prompt("选择仓库（序号或名称，输入关键词筛选，quit 退出）> ").strip()
        if choice.casefold() in {"quit", "exit", "/quit", "/exit"}:
            return False
        if choice.isdigit() and 1 <= int(choice) <= len(visible):
            selected = visible[int(choice) - 1]
            break

        exact = next(
            (repository for repository in repositories if repository["full_name"].casefold() == choice.casefold()),
            None,
        )
        if exact:
            selected = exact
            break
        candidates = [
            repository for repository in repositories if choice.casefold() in repository["full_name"].casefold()
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
    application.activate_scope(
        authenticated_user_id=authenticated_user_id,
        repository_id=repository_id,
        repository_full_name=repository,
    )
    console.print(f"已进入 [bold cyan]{repository}[/bold cyan]")
    if previous_repository and application.service is not previous_service:
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


def _render_repository_choices(repositories: list[dict[str, Any]], *, login: str, total: int) -> None:
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
            "[dim]自然语言提问 · /help 命令 · /trace 轨迹 · /debug Agent History · quit 退出[/dim]",
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
            application.handle(request, renderer=lambda result: _render_application_output(application, result))
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
            console.print("[yellow]/repo 不接受仓库名，请从 Token 可访问仓库列表中选择。[/yellow]")
            return
        try:
            _select_repository(application)
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]仓库读取失败：{exc}[/red]")
    elif command == "/model":
        if argument:
            application.config.model = argument
            application.llm.model = argument
            console.print(f"模型已切换为 [cyan]{argument}[/cyan]")
        else:
            console.print(f"当前模型：[cyan]{application.config.model}[/cyan]")
    elif command == "/tokens":
        prompt_tokens = int(getattr(application.llm, "total_prompt_tokens", 0))
        completion_tokens = int(getattr(application.llm, "total_completion_tokens", 0))
        text = f"Tokens：{prompt_tokens} prompt + {completion_tokens} completion = {prompt_tokens + completion_tokens} total"
        cost = getattr(application.llm, "estimated_cost", None)
        if cost is not None:
            text += f"（约 ${cost:.4f}）"
        console.print(text)
    elif command == "/approve":
        if argument:
            console.print("[yellow]用法：/approve[/yellow]")
            return
        application.approve(renderer=lambda output: _render_application_output(application, output))
    elif command == "/reject":
        if argument:
            console.print("[yellow]用法：/reject[/yellow]")
            return
        application.reject(renderer=lambda output: _render_application_output(application, output))
    elif command == "/edit":
        if not argument:
            console.print("[yellow]用法：/edit <修改要求>[/yellow]")
            return
        application.revise(
            argument,
            renderer=lambda output: _render_application_output(application, output),
        )
    elif command == "/audit":
        events = application.service.harness.audit.events(argument or application.session_id)
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
            _show_sessions(application.list_sessions(), active_session_id=application.session_id)
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Session 读取失败：{exc}[/red]")
    elif command == "/new":
        try:
            application.new_session(title=argument)
            console.print(f"已创建并进入 Session [cyan]{application.session_id}[/cyan]")
            _show_session_safety_note()
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Session 创建失败：{exc}[/red]")
    elif command == "/switch":
        if not argument:
            console.print("[yellow]用法：/switch <session_id>[/yellow]")
            return
        previous_session_id = application.session_id
        try:
            application.switch_session(argument)
            if application.session_id == previous_session_id:
                console.print(f"Session [cyan]{application.session_id}[/cyan] 已是当前 Session；未更改状态。")
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
            console.print("[yellow]用法：/delete <session_id>[/yellow]")
            return
        deleting_active_session = argument == application.session_id
        try:
            application.delete_session(argument)
            console.print(
                f"已删除 Session [cyan]{argument}[/cyan]；当前 Session：[cyan]{application.session_id}[/cyan]"
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
            _show_compaction(application.compact())
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Context 压缩失败：{exc}[/red]")
    elif command == "/remember":
        _remember(application, argument)
    elif command == "/memory":
        _memory(application, argument)
    elif command == "/forget":
        if not argument:
            console.print("[yellow]用法：/forget <memory_id>[/yellow]")
            return
        try:
            application.forget(argument)
            console.print(f"已删除 Memory [cyan]{argument}[/cyan]")
        except (GitAgentError, ValueError) as exc:
            console.print(f"[red]Memory 删除失败：{exc}[/red]")
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
    if result.decision.clarify:
        ui.text(str(result.output or result.decision.message), title="需要补充信息", kind="router")
        return
    if result.agent is None and isinstance(result.output, str):
        ui.text(result.output, title="GitAgent", kind="info")
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
    if isinstance(output, dict) and set(output) == {"diagnosis", "code_change"}:
        _render_output(application, output["diagnosis"])
        _render_output(application, output["code_change"])
        return
    if isinstance(output, AgentContext):
        _render_context(application, output)
        return
    if isinstance(output, IssueAgentResult):
        if output.action == DomainAction.CLARIFY:
            ui.text(output.question, title="需要补充信息 · Issues", kind="router")
            return
        visible, truncated = visible_items(output.issues)
        operation = output.operation.value if output.operation else output.action.value
        content = output.answer
        if operation in {"LIST", "SUMMARIZE"} and visible:
            items = "\n".join(
                f"- **#{issue.number}** {issue.title}  `{issue.state}`"
                + (f"  {', '.join(f'`{label}`' for label in issue.labels)}" if issue.labels else "")
                for issue in visible
            )
            content = f"{content}\n\n{items}" if content else items
        if truncated:
            content += f"\n\n_仅显示前 {MAX_VISIBLE_ITEMS} 项。_"
        title = f"Issue #{output.issue_number}" if output.issue_number else f"Issues · {len(output.issues)}"
        ui.markdown(content, title=title, kind="agent")
        return
    if isinstance(output, PullRequestAgentResult):
        if output.action == DomainAction.CLARIFY:
            ui.text(output.question, title="需要补充信息 · Pull Requests", kind="router")
            return
        visible, truncated = visible_items(output.pull_requests)
        operation = output.operation.value if output.operation else output.action.value
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
            content += "\n\n**变更文件**  " + ", ".join(f"`{path}`" for path in output.changed_files)
        title = f"PR #{output.pr_number}" if output.pr_number else f"Pull Requests · {len(output.pull_requests)}"
        ui.markdown(content, title=title, kind="agent")
        return
    if isinstance(output, RepoQAResult):
        content = output.answer
        if output.files:
            content += "\n\n---\n**相关文件：** " + ", ".join(f"`{path}`" for path in output.files)
        if output.symbols:
            content += "\n\n**相关符号：** " + ", ".join(f"`{symbol}`" for symbol in output.symbols)
        ui.markdown(content, title="Repository", kind="agent")
        return
    if isinstance(output, CIDiagnosisResult):
        files = ", ".join(f"`{path}`" for path in output.suspected_files) or "无"
        content = (
            f"{output.failure_summary}\n\n"
            f"**可能根因：** {output.probable_root_cause}\n\n"
            f"**建议：** {output.suggested_fix}\n\n"
            f"**相关文件：** {files}"
        )
        ui.markdown(
            content,
            title=f"CI · {output.failed_job}",
            kind="ci",
            subtitle=f"confidence={output.confidence:.0%}",
        )
        return
    raise ValidationError(f"不支持渲染结果类型：{type(output).__name__}")


def _render_context(application: LiveApplication, context: AgentContext) -> None:
    if context.question:
        ui.text(context.question or context.goal, title="需要补充信息", kind="router")
        return
    if context.pending is not None:
        _render_proposal(application, context)
        return
    if context.error:
        ui.text(context.error, title="执行失败", kind="error")
        return
    if context.result is not None:
        _render_output(application, context.result)
        return
    ui.text(context.final_message or "执行已结束。", title="GitAgent", kind="info")


def _render_proposal(application: LiveApplication, context: AgentContext) -> None:
    calls = context.pending.calls
    if context.reply_draft is not None and any(call.tool == "github.post_comment" for call in calls):
        issue_number = next(
            (call.arguments.get("issue_number") for call in calls if call.tool == "github.post_comment"),
            context.entity_id,
        )
        ui.markdown(
            context.reply_draft,
            title=f"Issue #{issue_number} 修改报告 · 待发布",
            kind="agent",
        )
    elif any(call.tool == "github.create_draft_pr" for call in calls):
        _render_code_change_proposal(application, context)
    else:
        details = []
        for call in context.pending.calls:
            arguments = json.dumps(call.arguments, ensure_ascii=False, indent=2, sort_keys=True)
            details.append(f"### `{call.tool}`\n```json\n{arguments}\n```")
        payload = "\n\n".join(details) or "（内部操作，无外部写入参数）"
        ui.markdown(
            f"{context.pending.summary}\n\n{payload}",
            title="变更提案 · 待批准",
            kind="agent",
        )
    _render_approval(application, context.pending.approval_id)


def _render_code_change_proposal(application: LiveApplication, context: AgentContext) -> None:
    candidate = context.code_candidate
    if candidate is None:
        ui.text(context.pending.summary, title="代码变更 · 待批准", kind="agent")
        return
    files = ", ".join(f"`{path}`" for path in candidate.changed_files)
    content = (
        f"{candidate.summary}\n\n"
        f"**根因：** {candidate.root_cause}\n\n"
        f"**变更文件：** {files}\n\n"
        f"### Diff\n```diff\n{candidate.patch}\n```"
    )
    pr_call = next(
        (call for call in context.pending.calls if call.tool == "github.create_draft_pr"),
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
    checks = "\n".join(f"- **{check.name}**: {check.status} — {check.details}" for check in report.checks)
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


def _show_sessions(sessions: Sequence[SessionRecord], *, active_session_id: str | None) -> None:
    if not sessions:
        console.print("[dim]当前仓库没有 Session。[/dim]")
        return
    table = Table(title="Sessions")
    table.add_column("当前", justify="center")
    table.add_column("Session ID", style="cyan")
    table.add_column("标题")
    table.add_column("更新时间")
    for session in sessions:
        session_id = session.session_id
        table.add_row(
            "●" if session_id == active_session_id else "",
            session_id,
            session.title,
            session.updated_at,
        )
    console.print(table)


def _show_session_safety_note() -> None:
    console.print("[dim]只有目标 Session 的上下文可用；运行时审批不会跨 Service 恢复。[/dim]")


def _show_compaction(result: CompactResult) -> None:
    if not result.changed:
        console.print("[dim]当前没有可压缩的旧 Context。[/dim]")
        return
    console.print(
        f"已压缩 Turn [cyan]{result.covered_from_seq}–{result.covered_to_seq}[/cyan]；"
        f"摘要估算 token：{result.before_tokens} → {result.after_tokens}"
    )


def _remember(application: LiveApplication, argument: str) -> None:
    scope_token, _, remainder = argument.partition(" ")
    scope_token = scope_token.casefold()
    remainder = remainder.strip()
    if scope_token == "user":
        scope = "user"
        kind = "preference"
        content = remainder
    elif scope_token == "repo":
        kind, _, content = remainder.partition(" ")
        scope = "repository"
        kind = kind.casefold()
        content = content.strip()
        if kind not in {"decision", "constraint", "reference"}:
            content = ""
    else:
        content = ""

    if not content:
        console.print(
            "[yellow]用法：/remember user <长期协作偏好>\n"
            "      /remember repo <decision|constraint|reference> <内容>[/yellow]"
        )
        return
    try:
        record, created = application.remember(scope, kind, content)
        memory_id = record.memory_id
        if created:
            console.print(f"已保存 Memory [cyan]{memory_id}[/cyan]")
        else:
            console.print(f"Memory [cyan]{memory_id}[/cyan] 已存在；未重复保存。")
    except (GitAgentError, ValueError) as exc:
        console.print(f"[red]Memory 保存失败：{exc}[/red]")


def _memory(application: LiveApplication, argument: str) -> None:
    scope_token = argument.casefold()
    if scope_token not in {"", "user", "repo"}:
        console.print("[yellow]用法：/memory [user|repo][/yellow]")
        return
    scope = {"": None, "user": "user", "repo": "repository"}[scope_token]
    try:
        memories = application.list_memories(scope=scope)
    except (GitAgentError, ValueError) as exc:
        console.print(f"[red]Memory 读取失败：{exc}[/red]")
        return
    if not memories:
        console.print("[dim]当前作用域没有 Memory。[/dim]")
        return
    table = Table(title="Memory")
    table.add_column("Memory ID", style="cyan")
    table.add_column("作用域")
    table.add_column("类型")
    table.add_column("内容")
    table.add_column("更新时间")
    for memory in memories:
        table.add_row(
            memory.memory_id,
            memory.scope,
            memory.kind,
            memory.content,
            memory.updated_at,
        )
    console.print(table)


def _show_debug_history(application: LiveApplication, argument: str) -> None:
    parts = argument.split()
    if len(parts) > 2:
        console.print("[yellow]用法：/debug [agent] 或 /debug <session_id> [agent][/yellow]")
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
            console.print("[yellow]用法：/debug [agent] 或 /debug <session_id> [agent][/yellow]")
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
            "  /new [title]          创建并进入新 Session\n"
            "  /switch <session_id>  切换 Session\n"
            "  /reset                保留 Turn 并建立新 Context 边界\n"
            "  /delete <session_id>  删除 Session\n"
            "  /compact              压缩旧 Turn 的 Context 投影\n"
            "  /remember user <偏好>  保存 User Memory\n"
            "  /remember repo <decision|constraint|reference> <内容>\n"
            "                        保存 Repository Memory\n"
            "  /memory [user|repo]   查看 Memory\n"
            "  /forget <memory_id>   删除 Memory\n"
            "  /model [name]         查看或切换模型\n"
            "  /tokens               查看模型 token 与估算费用\n"
            "  /approve              批准当前 Session 的待执行提案\n"
            "  /reject               拒绝当前 Session 的待执行提案\n"
            "  /edit <要求>          修改当前待执行方案\n"
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
    console.print(
        "\n直接运行 gitagent 后可在终端安全输入 GitHub Token 和模型 API Key；"
        "也可以使用环境变量或 .env 免去重复输入。详细配置见 README.md。"
    )
