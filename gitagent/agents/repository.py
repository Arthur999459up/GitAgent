"""Repository domain agent for bounded search, analysis, history, and modification."""

from __future__ import annotations

import json
import re
from typing import Any

from gitagent.agent_loop import AgentAction, AgentActionKind, rejection_feedback
from gitagent.domain.errors import LLMProviderError, ValidationError, WorkflowError
from gitagent.domain.models import (
    AgentSpec,
    ChangeRequest,
    DomainAction,
    RepositoryOperation,
    RepositoryResult,
    Route,
    to_plain,
)
from gitagent.harness.context import (
    capability_attempted,
    capability_failure_observed,
)
from gitagent.harness.context.state import AgentContext
from gitagent.harness.execution import AgentHarness
from gitagent.harness.validation.static import StaticVerifier
from gitagent.model import Reasoner
from gitagent.prompts import get_prompt_library

from .coding import (
    CodingAgent,
    prepare_verified_candidate,
    record_candidate_capability_error,
)
from .decide import AGENT_ACTION_SCHEMA, parse_action
from .guidance import guidance_section

_PROMPTS = get_prompt_library()
_OPERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": [operation.value for operation in RepositoryOperation]},
    },
    "required": ["operation"],
}
_SEARCH_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
            "minItems": 1,
            "maxItems": 3,
        },
        "path_terms": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
            "maxItems": 4,
        },
        "symbols": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
            "maxItems": 2,
        },
    },
    "required": ["queries", "path_terms", "symbols"],
    "additionalProperties": False,
}

_MAX_SEARCH_STEPS = 12
_MAX_RESULT_PATHS = 12
_READ_CONTEXT_LINES = 81
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.:<>-]*")
_EXPLICIT_PATH = re.compile(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+")
_QUERY_STOPWORDS = frozenset(
    {
        "all",
        "and",
        "code",
        "document",
        "documents",
        "file",
        "files",
        "find",
        "for",
        "in",
        "related",
        "repository",
        "search",
        "the",
        "where",
    }
)

REPOSITORY_SPEC = AgentSpec(
    name="repository",
    role="Explore, search, explain, analyze, plan, inspect history, and coordinate repository modifications.",
    system_prompt=_PROMPTS.text("system.repository"),
    output_schema=("action", "operation", "answer", "files", "symbols", "reasoning"),
    routes=frozenset({Route.REPOSITORY}),
    required_context=("repository",),
    routing_examples=(
        "解释认证模块的调用链",
        "format_name 在哪里实现？",
        "分析修改配置加载器的影响范围",
        "这个文件最近为什么改过？",
        "为配置加载器增加超时参数",
    ),
)


class RepositoryAgent:
    """Own repository-scoped read workflows and direct repository modifications."""

    def __init__(
        self,
        harness: AgentHarness,
        coding: CodingAgent,
        verifier: StaticVerifier,
        reasoner: Reasoner | None = None,
    ) -> None:
        self.harness = harness
        self.coding = coding
        self.verifier = verifier
        self.reasoner = reasoner
        harness.register(REPOSITORY_SPEC)

    def operation_for(self, request: str) -> RepositoryOperation:
        text = request.strip()
        if not text:
            raise ValidationError("repository request cannot be empty")
        if self.reasoner is None:
            return self._fallback_operation(text)
        raw = self.reasoner.complete_structured_messages(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify one repository request into exactly one operation. "
                        "EXPLORE browses structure; SEARCH locates code; EXPLAIN explains behavior/call chains; "
                        "IMPACT_ANALYZE evaluates change impact; PLAN proposes an implementation plan without writing; "
                        "HISTORY inspects file history; MODIFY requests an arbitrary direct repository code change. "
                        "Issue-scoped and Pull-Request-scoped work must already have been routed elsewhere."
                    ),
                },
                {"role": "user", "content": text},
            ],
            schema=_OPERATION_SCHEMA,
            tool_name="select_repository_operation",
        )
        try:
            return RepositoryOperation(str(raw.get("operation") or ""))
        except ValueError as exc:
            raise ValidationError("Repository Agent selected an unknown operation") from exc

    def prepare(self, context: AgentContext, operation: RepositoryOperation) -> None:
        context.operation = operation.value
        if operation == RepositoryOperation.MODIFY:
            if context.change_request is None:
                context.change_request = ChangeRequest(repository=context.repository, description=context.goal)
            return
        context.max_steps = min(context.max_steps, _MAX_SEARCH_STEPS)

    def decide(self, context: AgentContext) -> AgentAction:
        try:
            operation = RepositoryOperation(context.operation)
        except ValueError as exc:
            raise WorkflowError("Repository Agent requires a valid operation") from exc
        if self.reasoner is not None and capability_failure_observed(context):
            return self._llm_decide_after_failure(context, operation)
        if operation == RepositoryOperation.MODIFY:
            self.prepare(context, operation)
            return self._decide_modify(context)
        return self._decide_read(context, operation)

    def build_result(self, context: AgentContext) -> RepositoryResult:
        try:
            operation = RepositoryOperation(context.operation)
        except ValueError as exc:
            raise WorkflowError("Repository Agent result requires a valid operation") from exc
        if operation == RepositoryOperation.MODIFY:
            return self._build_modify_result(context)
        return self._build_read_result(context, operation)

    def _llm_decide_after_failure(
        self,
        context: AgentContext,
        operation: RepositoryOperation,
    ) -> AgentAction:
        if self.reasoner is None:
            raise WorkflowError("capability failure recovery requires a reasoner")
        tools = self.harness.llm_tools(context)
        value = context.reason_structured(
            self.reasoner,
            schema=AGENT_ACTION_SCHEMA,
            tool_name="decide_action",
            tools=tools,
        )
        context.record_model_response(value, tool_name="decide_action")
        if value.get("kind") == "capability":
            value["capability_id"] = self.harness.resolve_llm_name(
                str(value.get("capability_id", "")),
                context,
            )
        return parse_action(value, requires_candidate=False)

    def _decide_read(self, context: AgentContext, operation: RepositoryOperation) -> AgentAction:
        if operation == RepositoryOperation.EXPLORE:
            arguments = {"depth": 4, "max_entries": 300}
            if not capability_attempted(context, "repository.get_repo_tree", arguments=arguments):
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id="repository.get_repo_tree",
                    arguments=arguments,
                    summary="读取有界仓库结构",
                )
            return AgentAction(AgentActionKind.FINISH, summary="仓库结构证据已收集")

        explicit_history_path = self._explicit_path(context.goal) if operation == RepositoryOperation.HISTORY else ""
        if explicit_history_path:
            context.repository_history_path = explicit_history_path
            arguments = {"path": explicit_history_path, "limit": 20}
            if not capability_attempted(context, "repository.get_file_history", arguments=arguments):
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id="repository.get_file_history",
                    arguments=arguments,
                    summary=f"读取 {explicit_history_path} 的文件历史",
                )
            return AgentAction(AgentActionKind.FINISH, summary="文件历史证据已收集")

        plan = self._search_plan(context, operation)
        for query in plan["queries"]:
            arguments = {"query": query, "max_results": 30}
            if not capability_attempted(context, "repository.search_code", arguments=arguments):
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id="repository.search_code",
                    arguments=arguments,
                    summary=f"检索仓库：{query}",
                )

        searches = self._capability_calls(context, "repository.search_code")
        no_search_hits = not any((call["data"] or {}).get("results") for call in searches)
        incomplete_search = any(not bool((call["data"] or {}).get("complete", False)) for call in searches)
        needs_tree = bool(plan["path_terms"]) or (no_search_hits and incomplete_search)
        tree_arguments: dict[str, Any] = {"depth": 8, "max_entries": 500}
        search_refs = list(
            dict.fromkeys(
                str((call["data"] or {}).get("ref"))
                for call in searches
                if (call["data"] or {}).get("ref")
            )
        )
        if len(search_refs) == 1:
            tree_arguments["ref"] = search_refs[0]
        if needs_tree and not capability_attempted(
            context,
            "repository.get_repo_tree",
            arguments=tree_arguments,
        ):
            return AgentAction(
                AgentActionKind.CAPABILITY,
                capability_id="repository.get_repo_tree",
                arguments=tree_arguments,
                summary="检查文件路径并补充不完整搜索",
            )

        for symbol in plan["symbols"]:
            arguments = {"symbol": symbol, "max_results": 30}
            if not capability_attempted(context, "repository.find_symbol", arguments=arguments):
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id="repository.find_symbol",
                    arguments=arguments,
                    summary=f"定位符号定义：{symbol}",
                )

        evidence, paths, _ = self._repository_evidence(context)
        if operation == RepositoryOperation.HISTORY:
            context.repository_history_path = self._choose_history_path(context, paths)
            history_arguments = {"path": context.repository_history_path, "limit": 20}
            if context.repository_history_path and not capability_attempted(
                context,
                "repository.get_file_history",
                arguments=history_arguments,
            ):
                return AgentAction(
                    AgentActionKind.CAPABILITY,
                    capability_id="repository.get_file_history",
                    arguments=history_arguments,
                    summary=f"读取 {context.repository_history_path} 的文件历史",
                )
            return AgentAction(AgentActionKind.FINISH, summary="文件历史检索已完成")

        read_arguments: dict[str, Any] = {"requests": self._read_requests(paths, evidence)}
        refs = list(evidence["coverage"]["refs"])
        if len(refs) == 1:
            read_arguments["ref"] = refs[0]
        if paths and not capability_attempted(context, "repository.read_files", arguments=read_arguments):
            return AgentAction(
                AgentActionKind.CAPABILITY,
                capability_id="repository.read_files",
                arguments=read_arguments,
                summary="读取命中位置附近的源码上下文",
            )
        return AgentAction(AgentActionKind.FINISH, summary="有界仓库检索已完成")

    def _decide_modify(self, context: AgentContext) -> AgentAction:
        if context.change_request is None:
            raise WorkflowError("repository modification requires a change request")
        applied = self._last_capability_data(context, "github.commit_to_default_branch")
        if applied is not None:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="仓库变更已提交",
                message=(
                    f"已直接提交到默认分支 `{applied.get('branch', context.change_request.base_branch)}`，"
                    f"Commit `{applied.get('commit', '')}`。"
                ),
            )
        feedback = rejection_feedback(context)
        if feedback is not None and not feedback:
            return AgentAction(
                AgentActionKind.FINISH,
                summary="已放弃",
                message="已按你的要求放弃，未执行任何仓库写入。",
            )
        if context.code_candidate is None:
            prepared = prepare_verified_candidate(
                self.coding,
                self.verifier,
                context.change_request,
                session_id=context.session_id,
                guidance=context.guidance,
            )
            if record_candidate_capability_error(context, prepared):
                return AgentAction(
                    AgentActionKind.CONTINUE,
                    summary="Coding capability 失败，重新评估下一步",
                )
            if prepared.candidate is None:
                return AgentAction(
                    AgentActionKind.FINISH,
                    summary="模型未生成文件内容",
                    message=prepared.message,
                )
            candidate = prepared.candidate
            report = prepared.verification
            context.code_candidate = candidate
            context.verification = report
            context.observations.append(
                {
                    "kind": "agent",
                    "payload": {
                        "agent": "coding",
                        "summary": candidate.summary,
                        "added_files": list(candidate.added_files),
                        "modified_files": list(candidate.modified_files),
                        "deleted_files": list(candidate.deleted_files),
                        "verification_passed": bool(report and report.passed),
                    },
                }
            )
            if report is None or not report.passed:
                raise WorkflowError("静态验证失败；拒绝生成默认分支写入提案")
        return AgentAction(
            AgentActionKind.APPLY_REPOSITORY_CHANGE,
            summary="将已验证的多文件变更直接提交到默认分支",
        )

    def _build_modify_result(self, context: AgentContext) -> RepositoryResult:
        summary = context.change_request.description if context.change_request is not None else "代码变更流程已完成。"
        answer = context.final_message or summary
        files = list(context.code_candidate.changed_files) if context.code_candidate is not None else []
        return RepositoryResult(
            action=DomainAction.ANSWER,
            operation=RepositoryOperation.MODIFY,
            answer=answer,
            files=files,
            candidate=context.code_candidate,
            verification=context.verification,
            reasoning=(
                "Direct repository modification was lowered through CodingAgent, static verification, "
                "exact user approval, and execution by this Repository Agent context."
            ),
        )

    def _build_read_result(
        self,
        context: AgentContext,
        operation: RepositoryOperation,
    ) -> RepositoryResult:
        if operation == RepositoryOperation.EXPLORE:
            tree = self._last_capability_data(context, "repository.get_repo_tree") or {}
            entries = [str(path) for path in list(tree.get("entries") or [])[:120]]
            evidence = {
                "tree": entries,
                "coverage": {
                    "complete": not bool(tree.get("truncated")),
                    "ref": tree.get("ref"),
                },
            }
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                context.final_message or self._grounded_text(context, evidence),
                files=entries[:40],
                reasoning="Bounded repository tree evidence was inspected.",
            )

        evidence, paths, symbols = self._repository_evidence(context)
        if operation == RepositoryOperation.HISTORY:
            history = self._last_capability_data(context, "repository.get_file_history") or {}
            path = str(history.get("path") or context.repository_history_path)
            if not path:
                return RepositoryResult(
                    DomainAction.CLARIFY,
                    operation,
                    "需要明确要查看历史的文件路径。",
                    files=paths,
                    question="请指定要查看提交历史的文件路径。",
                )
            commits = list(history.get("commits") or [])
            history_evidence = {**evidence, "history": {"path": path, "commits": commits}}
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                context.final_message or self._grounded_text(context, history_evidence),
                files=[path],
                history=commits,
                reasoning="Bounded search selected one file before its history was inspected.",
            )

        if operation == RepositoryOperation.SEARCH:
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                context.final_message or self._grounded_text(context, evidence),
                files=paths,
                symbols=symbols,
                reasoning="A bounded multi-query search loop and targeted line-window reads were used.",
            )
        if operation == RepositoryOperation.EXPLAIN:
            interpretation = self.coding.explain(
                context.repository,
                context.goal,
                evidence,
                session_id=context.session_id,
                guidance=context.guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                context.final_message
                or self._grounded_text(context, {**evidence, "interpretation": to_plain(interpretation)}),
                files=paths,
                symbols=list(dict.fromkeys(symbols + interpretation.key_symbols)),
                interpretation=interpretation,
                reasoning="Repository evidence was interpreted by CodingAgent without mutation authority.",
            )
        if operation == RepositoryOperation.IMPACT_ANALYZE:
            interpretation = self.coding.explain(
                context.repository,
                context.goal,
                evidence,
                session_id=context.session_id,
                guidance=context.guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                context.final_message
                or self._grounded_text(context, {**evidence, "impact": to_plain(interpretation)}),
                files=paths,
                symbols=list(dict.fromkeys(symbols + interpretation.key_symbols)),
                interpretation=interpretation,
                reasoning="Expanded searches, symbol definitions, occurrences, and targeted reads were combined.",
            )
        if operation == RepositoryOperation.PLAN:
            plan = self.coding.plan(
                context.repository,
                context.goal,
                evidence,
                session_id=context.session_id,
                guidance=context.guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                context.final_message or self._grounded_text(context, {**evidence, "plan": to_plain(plan)}),
                files=list(dict.fromkeys(paths + plan.files)),
                symbols=symbols,
                plan=plan,
                reasoning="CodingAgent produced a non-mutating plan from bounded multi-query repository evidence.",
            )
        raise WorkflowError(f"unsupported repository operation: {operation.value}")

    def _search_plan(self, context: AgentContext, operation: RepositoryOperation) -> dict[str, Any]:
        if context.repository_search_plan is not None:
            return context.repository_search_plan
        fallback = self._fallback_search_plan(context.goal)
        if self.reasoner is None:
            context.repository_search_plan = fallback
            return fallback
        try:
            raw = context.complete_structured(
                self.reasoner,
                prompt=_PROMPTS.render(
                    "agents.repository_search_plan",
                    operation=operation.value,
                    request=context.goal,
                ),
                schema=_SEARCH_PLAN_SCHEMA,
                tool_name="plan_repository_search",
            )
            plan = self._normalize_search_plan(raw, fallback, context.goal)
        except (LLMProviderError, ValidationError):
            plan = fallback
        context.repository_search_plan = plan
        return plan

    def _repository_evidence(self, context: AgentContext) -> tuple[dict[str, Any], list[str], list[str]]:
        plan = context.repository_search_plan or self._fallback_search_plan(context.goal)
        searches = [
            dict(call["data"] or {})
            for call in self._capability_calls(context, "repository.search_code")
        ]
        symbol_searches = [
            dict(call["data"] or {})
            for call in self._capability_calls(context, "repository.find_symbol")
        ]
        tree = self._last_capability_data(context, "repository.get_repo_tree") or {}
        path_matches = self._path_matches(list(tree.get("entries") or []), list(plan["path_terms"]))

        ordered_hits: list[dict[str, Any]] = []
        for result in searches:
            ordered_hits.extend(dict(item) for item in result.get("results", []) if isinstance(item, dict))
        for result in symbol_searches:
            ordered_hits.extend(dict(item) for item in result.get("results", []) if isinstance(item, dict))

        paths = list(
            dict.fromkeys(
                [str(item.get("path")) for item in ordered_hits if item.get("path")]
                + path_matches
            )
        )[:_MAX_RESULT_PATHS]
        reads = []
        for call in self._capability_calls(context, "repository.read_files"):
            reads.extend(list((call["data"] or {}).get("files") or []))
        symbols = [
            str(result.get("symbol"))
            for result in symbol_searches
            if result.get("symbol") and result.get("results")
        ]
        search_complete = all(
            bool(result.get("complete", False)) and not bool(result.get("truncated"))
            for result in searches
        )
        tree_complete = not bool(tree.get("truncated")) if tree else not bool(plan["path_terms"])
        refs = list(
            dict.fromkeys(
                str(item.get("ref"))
                for item in [*searches, *symbol_searches, tree]
                if item.get("ref")
            )
        )
        ref_consistent = len(refs) <= 1
        evidence = {
            "search_plan": plan,
            "searches": searches,
            "path_matches": path_matches,
            "symbols": symbol_searches,
            "files": reads,
            "coverage": {
                "complete": search_complete and tree_complete and ref_consistent,
                "search_complete": search_complete,
                "tree_complete": tree_complete,
                "ref_consistent": ref_consistent,
                "backends": list(dict.fromkeys(str(item.get("backend")) for item in searches if item.get("backend"))),
                "refs": refs,
            },
        }
        if symbols:
            symbol_set = {symbol.casefold() for symbol in symbols}
            evidence["references"] = [
                item
                for result in searches
                if str(result.get("query") or "").casefold() in symbol_set
                for item in result.get("results", [])
            ]
        return evidence, paths, list(dict.fromkeys(symbols))

    def _grounded_text(self, context: AgentContext, evidence: dict[str, Any]) -> str:
        if self.reasoner is None:
            return json.dumps(evidence, ensure_ascii=False, default=str)[:12000]
        return context.complete_text(
            self.reasoner,
            prompt=_PROMPTS.render(
                "agents.repository",
                repository=context.repository,
                question=context.goal,
                evidence=json.dumps(evidence, ensure_ascii=False, default=str),
                guidance=guidance_section(context.guidance),
            ),
        )

    def _choose_history_path(self, context: AgentContext, candidates: list[str]) -> str:
        if len(candidates) == 1:
            return candidates[0]
        if self.reasoner is not None and candidates:
            selected = context.complete_text(
                self.reasoner,
                prompt=(
                    "Choose exactly one file path from the candidates. Return only that path.\n"
                    + json.dumps(
                        {"request": context.goal, "candidates": candidates},
                        ensure_ascii=False,
                    )
                ),
            ).strip()
            if selected in candidates:
                return selected
        return ""

    @staticmethod
    def _read_requests(paths: list[str], evidence: dict[str, Any]) -> list[dict[str, Any]]:
        first_lines: dict[str, int] = {}
        for group in (evidence.get("searches", []), evidence.get("symbols", [])):
            for result in group:
                for item in result.get("results", []):
                    path = str(item.get("path") or "")
                    line = item.get("line")
                    if path and isinstance(line, int) and line > 0:
                        first_lines.setdefault(path, line)
        requests = []
        for path in paths[:_MAX_RESULT_PATHS]:
            line = first_lines.get(path)
            if line is None:
                requests.append({"path": path, "start_line": 1, "limit": 120})
                continue
            requests.append(
                {
                    "path": path,
                    "start_line": max(1, line - (_READ_CONTEXT_LINES // 2)),
                    "limit": _READ_CONTEXT_LINES,
                }
            )
        return requests

    @staticmethod
    def _capability_calls(context: AgentContext, capability_id: str) -> list[dict[str, Any]]:
        calls = []
        for observation in context.observations:
            payload = observation.get("payload") or {}
            if (
                observation.get("kind") != "capability"
                or payload.get("capability_id") != capability_id
            ):
                continue
            calls.append(
                {
                    "arguments": dict(payload.get("arguments") or {}),
                    "data": payload.get("data") or {},
                }
            )
        return calls

    @classmethod
    def _last_capability_data(cls, context: AgentContext, capability_id: str) -> dict[str, Any] | None:
        calls = cls._capability_calls(context, capability_id)
        return dict(calls[-1]["data"]) if calls else None

    @staticmethod
    def _path_matches(entries: list[Any], terms: list[str]) -> list[str]:
        needles = [term.casefold() for term in terms if term.strip()]
        return [
            path
            for path in dict.fromkeys(str(entry) for entry in entries)
            if any(needle in path.casefold() for needle in needles)
        ][:_MAX_RESULT_PATHS]

    @classmethod
    def _fallback_search_plan(cls, request: str) -> dict[str, Any]:
        explicit_paths = list(dict.fromkeys(cls._explicit_paths(request)))[:4]
        tokens = [
            token
            for token in _IDENTIFIER.findall(request)
            if token.casefold() not in _QUERY_STOPWORDS and len(token) > 1
        ]
        queries = list(dict.fromkeys(tokens))[:3]
        if not queries:
            queries = [request.strip()[:80]]
        symbols = [token for token in queries if cls._identifier(token)][:2] if cls._has_symbol_intent(request) else []
        path_terms = list(dict.fromkeys(explicit_paths + queries))[:4]
        return {"queries": queries, "path_terms": path_terms, "symbols": symbols}

    @classmethod
    def _normalize_search_plan(cls, raw: Any, fallback: dict[str, Any], request: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return fallback
        queries = cls._bounded_strings(raw.get("queries"), limit=3, maximum=80)
        path_terms = cls._bounded_strings(raw.get("path_terms"), limit=4, maximum=80)
        symbols = [
            item
            for item in cls._bounded_strings(raw.get("symbols"), limit=2, maximum=120)
            if cls._identifier(item) and item.casefold() in request.casefold()
        ] if cls._has_symbol_intent(request) else []
        if not queries:
            queries = list(fallback["queries"])
        for symbol in reversed(symbols):
            if symbol.casefold() not in {query.casefold() for query in queries}:
                queries.insert(0, symbol)
        return {"queries": queries[:3], "path_terms": path_terms, "symbols": symbols}

    @staticmethod
    def _has_symbol_intent(request: str) -> bool:
        return bool(
            re.search(
                r"(?:函数|类|符号|定义|实现|function|class|symbol|defined|implemented)",
                request,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _bounded_strings(value: Any, *, limit: int, maximum: int) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        seen = set()
        for item in value:
            text = str(item).strip()[:maximum]
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _identifier(value: str) -> str:
        cleaned = value.strip().strip("`'\"")
        return cleaned if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$.:<>-]*", cleaned) else ""

    @staticmethod
    def _explicit_paths(request: str) -> list[str]:
        return _EXPLICIT_PATH.findall(request)

    @classmethod
    def _explicit_path(cls, request: str) -> str:
        paths = cls._explicit_paths(request)
        return paths[0] if paths else ""

    @staticmethod
    def _fallback_operation(request: str) -> RepositoryOperation:
        text = request.casefold()
        if any(
            word in text
            for word in (
                "修改",
                "修复",
                "实现",
                "增加",
                "新增",
                "添加",
                "创建文件",
                "删除",
                "移除",
                "重构",
                "change",
                "fix",
                "implement",
                "add",
                "create file",
                "delete",
                "remove",
            )
        ):
            return RepositoryOperation.MODIFY
        if any(word in text for word in ("历史", "提交", "history", "commit")):
            return RepositoryOperation.HISTORY
        if any(word in text for word in ("计划", "方案", "plan")):
            return RepositoryOperation.PLAN
        if any(word in text for word in ("影响", "impact", "引用", "reference")):
            return RepositoryOperation.IMPACT_ANALYZE
        if any(word in text for word in ("解释", "为什么", "调用链", "explain", "behavior")):
            return RepositoryOperation.EXPLAIN
        if any(word in text for word in ("结构", "目录", "浏览", "tree", "explore")):
            return RepositoryOperation.EXPLORE
        return RepositoryOperation.SEARCH
