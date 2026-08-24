"""Repository domain agent for exploration, explanation, planning, history, and direct modifications."""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.errors import ValidationError, WorkflowError
from ..core.models import (
    AgentGuidance,
    AgentSpec,
    ChangeRequest,
    DomainAction,
    RepositoryOperation,
    RepositoryResult,
    Route,
    to_plain,
)
from ..prompts import get_prompt_library
from ..reasoning import Reasoner
from ..runtime import AgentAction, AgentContext, AgentHarness
from .code_change_controller import CodeChangeController
from .coding import CodingAgent
from .guidance import guidance_section

_PROMPTS = get_prompt_library()
_OPERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": [operation.value for operation in RepositoryOperation]},
    },
    "required": ["operation"],
}

REPOSITORY_TOOLS = frozenset(
    {
        "repository.get_repo_tree",
        "repository.search_code",
        "repository.read_file",
        "repository.read_files",
        "repository.find_symbol",
        "repository.find_references",
        "repository.get_file_history",
    }
)

REPOSITORY_SPEC = AgentSpec(
    name="repository",
    role="Explore, explain, analyze, plan, inspect history, and coordinate direct repository modifications.",
    system_prompt=_PROMPTS.text("system.repository"),
    allowed_tools=REPOSITORY_TOOLS,
    output_schema=("action", "operation", "answer", "files", "symbols", "reasoning"),
    capabilities=frozenset({Route.REPOSITORY}),
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
    """Own repository-scoped work; MODIFY delegates only its internal workflow mechanics."""

    def __init__(
        self,
        harness: AgentHarness,
        coding: CodingAgent,
        controller: CodeChangeController,
        reasoner: Reasoner | None = None,
    ) -> None:
        self.harness = harness
        self.coding = coding
        self.controller = controller
        self.reasoner = reasoner
        harness.register(REPOSITORY_SPEC)

    def operation_for(self, request: str) -> RepositoryOperation:
        text = request.strip()
        if not text:
            raise ValidationError("repository request cannot be empty")
        if self.reasoner is None:
            return self._fallback_operation(text)
        raw = self.reasoner.complete_structured(
            system=(
                "Classify one repository request into exactly one operation. "
                "EXPLORE browses structure; SEARCH locates code; EXPLAIN explains behavior/call chains; "
                "IMPACT_ANALYZE evaluates change impact; PLAN proposes an implementation plan without writing; "
                "HISTORY inspects file history; MODIFY requests an arbitrary direct repository code change. "
                "Issue-scoped and Pull-Request-scoped work must already have been routed elsewhere."
            ),
            prompt=text,
            schema=_OPERATION_SCHEMA,
            tool_name="select_repository_operation",
        )
        try:
            return RepositoryOperation(str(raw.get("operation") or ""))
        except ValueError as exc:
            raise ValidationError("Repository Agent selected an unknown operation") from exc

    def answer(
        self,
        repository: str,
        request: str,
        *,
        session_id: str,
        operation: RepositoryOperation | None = None,
        guidance: AgentGuidance | None = None,
    ) -> RepositoryResult:
        selected = operation or self.operation_for(request)
        if selected == RepositoryOperation.MODIFY:
            raise WorkflowError("MODIFY must run through the Repository AgentLoop context")
        return self.harness.run(
            "repository",
            session_id=session_id,
            operation=lambda context: self._answer(context, request, selected, guidance),
            repository=repository,
            goal=request,
            guidance=guidance,
        )

    def prepare_modify(self, context: AgentContext) -> None:
        context.operation = RepositoryOperation.MODIFY.value
        if context.change_request is None:
            context.change_request = ChangeRequest(repository=context.repository, description=context.goal)

    def decide(self, context: AgentContext) -> AgentAction:
        if context.operation != RepositoryOperation.MODIFY.value:
            raise WorkflowError("Repository AgentLoop is reserved for MODIFY")
        self.prepare_modify(context)
        return self.controller.decide(context)

    def build_result(self, context: AgentContext) -> RepositoryResult:
        if context.operation != RepositoryOperation.MODIFY.value:
            raise WorkflowError("Repository AgentLoop result requires MODIFY")
        internal = self.controller.build_result(context)
        answer = context.final_message or str(internal.get("summary") or "代码变更流程已完成。")
        files = list(context.code_candidate.changed_files) if context.code_candidate is not None else []
        return RepositoryResult(
            action=DomainAction.ANSWER,
            operation=RepositoryOperation.MODIFY,
            answer=answer,
            files=files,
            candidate=context.code_candidate,
            verification=context.verification,
            reasoning="Direct repository modification was lowered through CodingAgent, static verification, approval, and the GitHub mutator.",
        )

    def _answer(
        self,
        context: AgentContext,
        request: str,
        operation: RepositoryOperation,
        guidance: AgentGuidance | None,
    ) -> RepositoryResult:
        context.operation = operation.value
        if operation == RepositoryOperation.EXPLORE:
            tree = context.tool("repository.get_repo_tree", repository=context.repository, depth=4)
            entries = [str(path) for path in list(tree.get("entries") or [])[:120]]
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                self._grounded_text(context, request, {"tree": entries}, guidance),
                files=entries[:40],
                reasoning="Bounded repository tree evidence was inspected.",
            )

        evidence, paths, symbols = self._targeted_evidence(context, request)
        if operation == RepositoryOperation.SEARCH:
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                self._grounded_text(context, request, evidence, guidance),
                files=paths,
                symbols=symbols,
                reasoning="Bounded code search and targeted file reads were used.",
            )
        if operation == RepositoryOperation.EXPLAIN:
            interpretation = self.coding.explain(
                context.repository,
                request,
                evidence,
                session_id=context.session_id,
                guidance=guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                self._grounded_text(context, request, {**evidence, "interpretation": to_plain(interpretation)}, guidance),
                files=paths,
                symbols=list(dict.fromkeys(symbols + interpretation.key_symbols)),
                interpretation=interpretation,
                reasoning="Repository evidence was interpreted by CodingAgent without mutation authority.",
            )
        if operation == RepositoryOperation.IMPACT_ANALYZE:
            symbol = self._identifier(self._search_term(request))
            if symbol:
                refs = context.tool(
                    "repository.find_references",
                    repository=context.repository,
                    symbol=symbol,
                    max_results=50,
                )
                evidence["references"] = refs.get("results") or []
                symbols.append(symbol)
                paths = list(
                    dict.fromkeys(paths + [str(item.get("path")) for item in evidence["references"] if item.get("path")])
                )[:20]
            interpretation = self.coding.explain(
                context.repository,
                request,
                evidence,
                session_id=context.session_id,
                guidance=guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                self._grounded_text(context, request, {**evidence, "impact": to_plain(interpretation)}, guidance),
                files=paths,
                symbols=list(dict.fromkeys(symbols + interpretation.key_symbols)),
                interpretation=interpretation,
                reasoning="Search, targeted reads, references when available, and CodingAgent interpretation were combined.",
            )
        if operation == RepositoryOperation.PLAN:
            plan = self.coding.plan(
                context.repository,
                request,
                evidence,
                session_id=context.session_id,
                guidance=guidance,
            )
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                self._grounded_text(context, request, {**evidence, "plan": to_plain(plan)}, guidance),
                files=list(dict.fromkeys(paths + plan.files)),
                symbols=symbols,
                plan=plan,
                reasoning="CodingAgent produced a non-mutating plan from bounded repository evidence.",
            )
        if operation == RepositoryOperation.HISTORY:
            path = self._history_path(request, paths)
            if not path:
                return RepositoryResult(
                    DomainAction.CLARIFY,
                    operation,
                    "需要明确要查看历史的文件路径。",
                    files=paths,
                    question="请指定要查看提交历史的文件路径。",
                )
            history = context.tool("repository.get_file_history", repository=context.repository, path=path, limit=20)
            commits = list(history.get("commits") or [])
            return RepositoryResult(
                DomainAction.ANSWER,
                operation,
                self._grounded_text(context, request, {"path": path, "commits": commits}, guidance),
                files=[path],
                history=commits,
                reasoning="Only bounded history for the requested file was inspected.",
            )
        raise WorkflowError(f"unsupported repository operation: {operation.value}")

    def _targeted_evidence(self, context: AgentContext, request: str) -> tuple[dict[str, Any], list[str], list[str]]:
        query = self._search_term(request)
        search = context.tool(
            "repository.search_code",
            repository=context.repository,
            query=query,
            max_results=30,
        )
        hits = list(search.get("results") or [])
        paths = list(dict.fromkeys(str(hit.get("path")) for hit in hits if hit.get("path")))[:8]
        reads = (
            context.tool(
                "repository.read_files",
                repository=context.repository,
                requests=[{"path": path, "limit": 220} for path in paths],
            ).get("files", [])
            if paths
            else []
        )
        symbol = self._identifier(query)
        symbols: list[str] = []
        symbol_hits: list[dict[str, Any]] = []
        if symbol:
            found = context.tool(
                "repository.find_symbol",
                repository=context.repository,
                symbol=symbol,
                max_results=20,
            )
            symbol_hits = list(found.get("results") or [])
            if symbol_hits:
                symbols.append(symbol)
        return {"query": query, "search": hits, "files": reads, "symbols": symbol_hits}, paths, symbols

    def _grounded_text(
        self,
        context: AgentContext,
        request: str,
        evidence: dict[str, Any],
        guidance: AgentGuidance | None,
    ) -> str:
        if self.reasoner is None:
            return json.dumps(evidence, ensure_ascii=False, default=str)[:8000]
        return self.reasoner.complete_text(
            system=context.system_prompt,
            prompt=_PROMPTS.render(
                "agents.repository",
                repository=context.repository,
                question=request,
                evidence=json.dumps(evidence, ensure_ascii=False, default=str),
                guidance=guidance_section(guidance),
            ),
        )

    def _search_term(self, request: str) -> str:
        if self.reasoner is None:
            return request.strip()[:120]
        query = self.reasoner.complete_text(
            system="Return one concise repository search term for this request. Return only the term.",
            prompt=request,
        ).strip()
        return query[:120] or request.strip()[:120]

    def _history_path(self, request: str, candidates: list[str]) -> str:
        explicit = re.findall(r"(?:[\w.-]+/)+[\w.-]+", request)
        if explicit:
            return explicit[0]
        if len(candidates) == 1:
            return candidates[0]
        if self.reasoner is not None and candidates:
            selected = self.reasoner.complete_text(
                system="Choose exactly one file path from the provided candidates that best matches the request. Return only the path.",
                prompt=json.dumps({"request": request, "candidates": candidates}, ensure_ascii=False),
            ).strip()
            if selected in candidates:
                return selected
        return ""

    @staticmethod
    def _identifier(value: str) -> str:
        cleaned = value.strip().strip("`'\"")
        return cleaned if re.fullmatch(r"[A-Za-z_$][\w$.:<>-]*", cleaned) else ""

    @staticmethod
    def _fallback_operation(request: str) -> RepositoryOperation:
        text = request.casefold()
        if any(word in text for word in ("修改", "修复", "实现", "增加", "删除", "重构", "change", "fix", "implement")):
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
