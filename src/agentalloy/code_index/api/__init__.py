"""Code-index HTTP surface (``/code/*``)."""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["build_code_index_router"]


def build_code_index_router() -> APIRouter:
    """Assemble the ``/code`` router from the module's sub-routers.

    Imports stay inside the function: the routers pull in the ingest pipeline
    → facade → vendored tree-sitter engine, so an install without the
    ``[code-index]`` extra raises ImportError here and ``create_app`` reports
    the module ``unavailable`` instead of crashing.
    """
    from agentalloy.code_index.api.index_router import router as index_router
    from agentalloy.code_index.api.repos_router import router as repos_router

    root = APIRouter(prefix="/code", tags=["code-index"])
    root.include_router(index_router)
    root.include_router(repos_router)
    return root
