"""Read-only repository question answering."""

from __future__ import annotations

import json

from ..core.models import AgentGuidance, AgentSpec, RepoQAResult, Route
from ..prompts import get_prompt_library
from ..reasoning import Reasoner
from ..runtime import AgentContext, AgentHarness
from .guidance import guidance_section

_PROMPTS = get_prompt_library()

REPO_TOOLS = frozenset(
    {
        "repository.get_repo_tree",
        "repository.search_code",
        "repository.read_file",
        "repository.read_files",
        "repository.find_symbol",
        "repository.find_references",
    }
)

REPO_QA_SPEC = AgentSpec(
    name="repo_qa",
    role="Answer repository questions from bounded remote evidence.",
    system_prompt=_PROMPTS.text("system.repo_qa"),
    allowed_tools=REPO_TOOLS,
    output_schema=("answer", "files", "symbols", "reasoning"),
    capabilities=frozenset({Route.REPO_QA}),
    required_context=("repository",),
    routing_examples=(
        "解释认证模块的调用链",
        "format_name 在哪里实现？",
    ),
)


class RepoQAAgent:
    def __init__(self, harness: AgentHarness, reasoner: Reasoner | None = None) -> None:
        self.harness = harness
        self.reasoner = reasoner
        harness.register(REPO_QA_SPEC)

    def answer(
        self,
        repository: str,
        question: str,
        *,
        session_id: str,
        guidance: AgentGuidance | None = None,
    ) -> RepoQAResult:
        return self.harness.run(
            "repo_qa",
            session_id=session_id,
            operation=lambda context: self._answer(context, repository, question, guidance),
            repository=repository,
            goal=question,
            guidance=guidance,
        )

    def _answer(
        self,
        context: AgentContext,
        repository: str,
        question: str,
        guidance: AgentGuidance | None,
    ) -> RepoQAResult:
        tree = context.tool("repository.get_repo_tree", repository=repository, depth=3)
        query = self._search_term(question)
        search = context.tool("repository.search_code", repository=repository, query=query, max_results=20)
        hits = search["results"]
        paths = list(dict.fromkeys(hit["path"] for hit in hits))[:4]
        reads = (
            context.tool("repository.read_files", repository=repository, paths=paths, limit_per_file=220)["files"]
            if paths
            else []
        )
        symbols: list[str] = []
        evidence = {"tree": tree["entries"][:80], "search": hits, "files": reads, "symbols": symbols}
        if self.reasoner:
            answer = self.reasoner.complete_text(
                system=context.system_prompt,
                prompt=_PROMPTS.render(
                    "agents.repo_qa",
                    repository=repository,
                    question=question,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                    guidance=guidance_section(guidance),
                ),
            )
            return RepoQAResult(
                answer=answer,
                files=paths,
                symbols=symbols,
                reasoning="The answer is grounded in bounded tree, search, symbol, and targeted file evidence.",
            )

        if hits:
            excerpts = "; ".join(f"{hit['path']}:{hit['line']} {hit['snippet'].strip()}" for hit in hits[:5])
            answer = f"Repository evidence matching {query!r}: {excerpts}"
            reasoning = (
                "The answer is limited to tree → code search → targeted file reads; no full repository was fetched."
            )
        else:
            answer = f"No repository content matching {query!r} was found in the bounded search."
            reasoning = f"The visible tree contains {len(tree['entries'])} entries; broader conclusions need a more specific query."
        return RepoQAResult(answer=answer, files=paths, symbols=symbols, reasoning=reasoning)

    def _search_term(self, question: str) -> str:
        if self.reasoner is None:
            return question.strip()[:120]
        query = self.reasoner.complete_text(
            system="Return one concise repository search term for this question. Return only the term.",
            prompt=question,
        ).strip()
        return query[:120] or question.strip()[:120]
