"""In-process action executors (M1c).

Each action runs directly against the lifespan-scoped stores — the same
objects the API routers use — so the local agent adds no DuckDB/Tantivy
handles and makes no HTTP hop. Executors return *transcript text*, not
structured payloads: the 2.6B's only consumer is the next prompt, and the
wording matters more than the shape.

Availability (all fail-open into an honest transcript line):

* code-index actions need the code-index module (``CodeIndexState`` on
  ``app.state``) and a resolvable repo — the request's ``repo`` slug, or the
  sole indexed repo when the registry has exactly one;
* state actions (``artifact_body`` / ``contract_detail``) need the SDD state
  store, scoped to ``(repo, stream)`` exactly like the ``/state`` routers;
* ``telemetry`` needs the lifespan ``TelemetryQuerier``; ``get_skill_for``
  needs the ``ComposeOrchestrator``.

Heavy imports (code index → tree-sitter, state router → LangGraph, compose
models) are deferred to the executing method so ``import agentalloy.
local_agent.executors`` stays cheap and safe on a service without the
``[code-index]`` extra.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from agentalloy.local_agent.protocol import PHASES, Action

logger = logging.getLogger(__name__)

#: The transcript line for an empty but healthy result. The answer prompt
#: treats "No results" as the signal to say so plainly — keep the wording.
NO_RESULTS = "No results."


class ExecutorError(Exception):
    """A step could not execute (missing dependency, unresolvable repo).

    The loop records the message in the transcript and continues — a step
    failure never fails the request.
    """


def _fmt_loc(file_path: str | None, line: int | None) -> str:
    if not file_path:
        return "?"
    return f"{file_path}:{line}" if line else file_path


def _fmt_search_results(results: list[Any]) -> str:
    lines = [f"{len(results)} result(s):"]
    for i, r in enumerate(results, start=1):
        head = r.qualified_name or r.heading or r.symbol or "?"
        loc = _fmt_loc(getattr(r, "file_path", None), getattr(r, "start_line", None))
        lines.append(
            f"{i}. {head} — {loc}" + (f" [{r.source}]" if getattr(r, "source", None) else "")
        )
        snippet = (getattr(r, "snippet", None) or "").strip()
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


def _fmt_artifact(row: dict[str, Any]) -> str:
    lines = [f"Artifact: {row.get('name')} (phase {row.get('phase')}, contract {row.get('slug')})"]
    content = row.get("content")
    if content:
        lines.append(str(content))
    return "\n".join(lines)


def _fmt_tags(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in cast(list[Any], value)) or None
    return str(value) or None


def _fmt_contract(row: dict[str, Any]) -> str:
    lines = [f"Contract: {row.get('contract_id')} [{row.get('status')}]"]
    work_item = row.get("work_item")
    if work_item:
        lines.append(f"work item: {work_item}")
    tags = _fmt_tags(row.get("domain_tags"))
    if tags:
        lines.append(f"tags: {tags}")
    body = row.get("body")
    if body:
        lines.append(str(body))
    return "\n".join(lines)


class Executors:
    """Executes one action against the lifespan stores and returns transcript text.

    ``repo_root`` is the request's repo root (path) — used to scope the state
    store and as the telemetry ``repo`` slug. ``repo_slug`` resolution for
    code-index actions happens per-step (:meth:`_require_code_index`) because
    the registry may hold several repos.
    """

    def __init__(
        self,
        *,
        code_index_state: Any | None = None,
        state_store: Any | None = None,
        telemetry_querier: Any | None = None,
        compose_orchestrator: Any | None = None,
        repo_root: Path,
    ) -> None:
        self._code_index_state = code_index_state
        self._state_store = state_store
        self._telemetry_querier = telemetry_querier
        self._compose_orchestrator = compose_orchestrator
        self._repo_root = repo_root

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def execute(
        self,
        action: Action,
        args: dict[str, Any],
        *,
        repo_slug: str | None = None,
    ) -> str:
        """Run ``action`` with validated ``args``; return the transcript text.

        Raises :class:`ExecutorError` when the dependency for this action is
        missing (the loop records it and continues).
        """
        if action is Action.NONE:
            raise ValueError("Executors.execute: 'none' has nothing to execute")
        # Each bound _exec_* method is (args, *, repo_slug) -> str; the
        # ellipsis covers the keyword-only repo_slug.
        handler: Callable[..., Awaitable[str]]
        if action is Action.CODE_SEARCH:
            handler = self._exec_code_search
        elif action is Action.SYMBOLS:
            handler = self._exec_symbols
        elif action is Action.KNOWLEDGE_WHY:
            handler = self._exec_knowledge_why
        elif action is Action.KNOWLEDGE_RELATED:
            handler = self._exec_knowledge_related
        elif action is Action.KNOWLEDGE_ENTITIES:
            handler = self._exec_knowledge_entities
        elif action is Action.ARTIFACT_BODY:
            handler = self._exec_artifact_body
        elif action is Action.CONTRACT_DETAIL:
            handler = self._exec_contract_detail
        elif action is Action.TELEMETRY:
            handler = self._exec_telemetry
        elif action is Action.GET_SKILL_FOR:
            handler = self._exec_get_skill_for
        else:  # pragma: no cover — exhaustive over Action
            raise ValueError(f"Executors.execute: unknown action {action!r}")
        return await handler(args, repo_slug=repo_slug)

    # ------------------------------------------------------------------
    # Dependency guards
    # ------------------------------------------------------------------

    def _require_code_index(self, repo_slug: str | None) -> tuple[Any, Any]:
        """(CodeIndexState, IndexedRepo) or ExecutorError with the reason."""
        state = self._code_index_state
        if state is None:
            raise ExecutorError(
                "the code index is not available on this service "
                "(the [code-index] module is not enabled)"
            )
        slug = repo_slug
        if slug is None:
            slugs = sorted({r.slug for r in state.jobs.list_repos()})
            if not slugs:
                raise ExecutorError(
                    "no repo is indexed yet; index one via `agentalloy code index <path>`"
                )
            if len(slugs) > 1:
                raise ExecutorError(
                    f"several repos are indexed ({', '.join(slugs)}); the request must name one via 'repo'"
                )
            slug = slugs[0]
        indexed = state.jobs.get_repo(slug)
        if indexed is None:
            raise ExecutorError(f"repo {slug!r} is not indexed; index it first")
        return state, indexed

    def _require_state_store(self) -> Any:
        if self._state_store is None:
            raise ExecutorError("the SDD state store is not available on this service")
        return self._state_store

    def _scoped_store(self) -> Any:
        """The state store view for this request's (repo, stream) — same as /state."""
        from agentalloy.api.state_router import scoped_state_store  # noqa: PLC0415

        return scoped_state_store(self._require_state_store(), self._repo_root)

    # ------------------------------------------------------------------
    # Code-index actions
    # ------------------------------------------------------------------

    async def _exec_code_search(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        from agentalloy.code_index.retrieval.hybrid import semantic_search  # noqa: PLC0415

        state, indexed = self._require_code_index(repo_slug)
        k = args.get("k") or 10
        results = await semantic_search(
            state,
            indexed.slug,
            args["query"],
            k=k,
            repo_path=indexed.repo_path,
            indexed_head=indexed.head_sha,
        )
        if not results:
            return NO_RESULTS
        return _fmt_search_results(results)

    async def _exec_knowledge_related(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        from agentalloy.code_index.retrieval.hybrid import related_decisions  # noqa: PLC0415

        state, indexed = self._require_code_index(repo_slug)
        k = args.get("k") or 8
        results = await related_decisions(
            state,
            indexed.slug,
            args["query"],
            k=k,
            repo_path=indexed.repo_path,
            indexed_head=indexed.head_sha,
        )
        if not results:
            return NO_RESULTS
        return _fmt_search_results(results)

    async def _exec_symbols(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        from agentalloy.code_index.api.deps import with_handles  # noqa: PLC0415

        state, indexed = self._require_code_index(repo_slug)
        fqn = args["query"]
        sym = await with_handles(
            state, indexed.slug, lambda h: h.graph.symbol(fqn), repo_path=indexed.repo_path
        )
        if sym is None:
            return NO_RESULTS
        lines = [f"Symbol: {sym.qualified_name} ({sym.kind})"]
        loc = _fmt_loc(sym.file_path, sym.start_line)
        if loc != "?":
            lines.append(loc)
        if sym.docstring:
            lines.append(f'"""{sym.docstring.strip()}"""')
        return "\n".join(lines)

    async def _exec_knowledge_why(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        from agentalloy.code_index.api.deps import with_handles  # noqa: PLC0415

        state, indexed = self._require_code_index(repo_slug)
        fqn = args["query"]
        decisions = await with_handles(
            state,
            indexed.slug,
            lambda h: h.graph.governing_decisions(fqn),
            repo_path=indexed.repo_path,
        )
        if not decisions:
            return NO_RESULTS
        lines = [f"Decisions governing {fqn}:"]
        for i, d in enumerate(decisions, start=1):
            lines.append(f"{i}. {d.heading} — {_fmt_loc(d.file_path, d.start_line)}")
            if d.snippet:
                lines.append(f"   {d.snippet.strip()}")
        return "\n".join(lines)

    async def _exec_knowledge_entities(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        from agentalloy.code_index.api.deps import with_handles  # noqa: PLC0415

        state, indexed = self._require_code_index(repo_slug)
        query = args["query"]
        kind = args.get("kind")

        def _run(h: Any) -> list[Any]:
            edges = h.graph.typed_edges_for_fqn(query)
            if not edges:
                candidates = h.graph.symbols_by_name(query)
                if candidates:
                    edges = h.graph.typed_edges_for_fqn(candidates[0][0])
            if kind:
                edges = [e for e in edges if e.kind == kind]
            return edges

        edges = await with_handles(state, indexed.slug, _run, repo_path=indexed.repo_path)
        if not edges:
            return NO_RESULTS
        lines = [f"Entity edges touching {query}:"]
        for e in edges:
            lines.append(
                f"- {e.src} --{e.kind}--> {e.dst}"
                + (f"  ({_fmt_loc(e.file_path, None)})" if e.file_path else "")
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # State actions
    # ------------------------------------------------------------------

    async def _exec_artifact_body(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        store = self._scoped_store()
        slug = args["slug"]
        name = args["query"]
        phase = args.get("phase")
        if phase is not None:
            row = await asyncio.to_thread(store.get_artifact, phase, slug, name, status="active")
            return _fmt_artifact(row) if row else NO_RESULTS
        # Phase unknown: scan the lifecycle in order and return the first
        # active match — the model is not expected to guess the phase.
        for p in PHASES:
            row = await asyncio.to_thread(store.get_artifact, p, slug, name, status="active")
            if row:
                return _fmt_artifact(row)
        return NO_RESULTS

    async def _exec_contract_detail(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        store = self._scoped_store()
        row = await asyncio.to_thread(store.get_contract, args["slug"])
        if row is None:
            return NO_RESULTS
        return _fmt_contract(cast(dict[str, Any], row))

    # ------------------------------------------------------------------
    # Telemetry + compose
    # ------------------------------------------------------------------

    async def _exec_telemetry(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        if self._telemetry_querier is None:
            raise ExecutorError("telemetry is not available on this service")
        k = args.get("k") or 10
        phase = args.get("phase")
        repo = None
        if self._repo_root != Path.cwd():
            # Attribute by the same worktree-collapsing slug the /state and
            # telemetry paths stamp (only when the request pinned a root).
            from agentalloy.api.state_router import _repo_key_for  # noqa: PLC0415

            repo = _repo_key_for(str(self._repo_root))
        response = await self._telemetry_querier.query(
            phase=phase, status=None, since=None, until=None, repo=repo, limit=k, offset=0
        )
        if not response.traces:
            return NO_RESULTS
        lines = [f"{len(response.traces)} recent composition trace(s):"]
        for t in response.traces:
            prompt = (t.task_prompt or "").strip().replace("\n", " ")
            if len(prompt) > 80:
                prompt = prompt[:77] + "..."
            extra = f" (+{t.tokens_returned} tokens)" if t.tokens_returned else ""
            lines.append(f"- [{t.phase}] {t.status}: {prompt}{extra}")
        return "\n".join(lines)

    async def _exec_get_skill_for(self, args: dict[str, Any], *, repo_slug: str | None) -> str:
        if self._compose_orchestrator is None:
            raise ExecutorError("the compose orchestrator is not available on this service")
        from agentalloy.api.compose_models import ComposeRequest  # noqa: PLC0415

        phase = args.get("phase") or "build"
        if phase not in PHASES:
            raise ExecutorError(f"unknown phase {phase!r}")
        request = ComposeRequest(
            task=args["task"],
            phase=cast(Any, phase),
            requesting_agent="local-agent",
        )
        result = await self._compose_orchestrator.compose(
            request, repo=self._repo_key(), record_trace=True
        )
        output = getattr(result, "output", "")
        if output:
            return output
        return NO_RESULTS

    def _repo_key(self) -> str | None:
        """Telemetry/compose repo attribution — None when no root is pinned."""
        if self._repo_root == Path.cwd():
            return None
        from agentalloy.api.state_router import _repo_key_for  # noqa: PLC0415

        return _repo_key_for(str(self._repo_root))
