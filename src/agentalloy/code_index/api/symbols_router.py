"""Symbol-relation endpoints (``/code/symbols/*``).

``fqn`` is the path (``:path`` converter so dotted — and any future
slash-bearing — names round-trip); ``repo`` is a required query param. The
``/callers`` and ``/callees`` routes are registered BEFORE the bare symbol
route: starlette matches in order and the greedy ``{fqn:path}`` would
otherwise swallow the literal suffix segments.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from agentalloy.code_index.api.deps import require_indexed_repo, with_handles
from agentalloy.code_index.api.models import CallSiteView, SymbolView
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state

router = APIRouter()


@router.get(
    "/symbols/{fqn:path}/callers",
    response_model=list[CallSiteView],
    summary="Symbols that call fqn (depth > 1 walks transitively)",
)
async def symbol_callers(
    fqn: str,
    repo: str = Query(description="Indexed repo slug"),
    depth: int = Query(default=1, ge=1, le=10),
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[CallSiteView]:
    require_indexed_repo(state, repo)
    sites = await with_handles(
        state,
        repo,
        lambda h: (
            h.graph.callers(fqn) if depth == 1 else h.graph.transitive_callers(fqn, max_depth=depth)
        ),
    )
    return [CallSiteView.from_call_site(s) for s in sites]


@router.get(
    "/symbols/{fqn:path}/callees",
    response_model=list[CallSiteView],
    summary="Symbols fqn calls",
)
async def symbol_callees(
    fqn: str,
    repo: str = Query(description="Indexed repo slug"),
    state: CodeIndexState = Depends(get_code_index_state),
) -> list[CallSiteView]:
    require_indexed_repo(state, repo)
    sites = await with_handles(state, repo, lambda h: h.graph.callees(fqn))
    return [CallSiteView.from_call_site(s) for s in sites]


@router.get(
    "/symbols/{fqn:path}",
    response_model=SymbolView,
    summary="One symbol's full graph row",
)
async def symbol_detail(
    fqn: str,
    repo: str = Query(description="Indexed repo slug"),
    state: CodeIndexState = Depends(get_code_index_state),
) -> SymbolView:
    require_indexed_repo(state, repo)
    sym = await with_handles(state, repo, lambda h: h.graph.symbol(fqn))
    if sym is None:
        raise HTTPException(status_code=404, detail=f"no such symbol in {repo!r}: {fqn}")
    return SymbolView.from_symbol(sym)
