"""Code-index HTTP surface (``/code/*``)."""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["build_code_index_router"]


def build_code_index_router() -> APIRouter:
    """Assemble the ``/code`` router from the module's sub-routers.

    Scaffolding stage: the module's routers land in follow-up PRs; until the
    engine and storage layers exist this raises ImportError so ``create_app``
    reports the module ``unavailable`` instead of mounting a dead prefix.
    """
    raise ImportError(
        "the code-index module is not functional yet (scaffolding only); "
        "routers arrive with the engine + storage layers"
    )
