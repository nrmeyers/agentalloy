"""Service-mediated corpus ingest — ``POST /corpus/ingest-pack`` (T1).

The endpoint the CLI calls when the service is up, so ``lessons promote`` /
``install-pack`` / ``install-packs`` write the *live* corpus without stopping the
service (native) and reach the in-volume corpus at all (container). It runs the
write from inside the serving process — the only writer that legally holds the
DuckDB store — using the same recipe the web wizard install already ships:
release the read handle, ``install_local_pack``, reload the cache.

Security (AC-7): corpus mutation is guarded by the shared ingest secret
(:mod:`agentalloy.install.ingest_secret`). The proxy ``/proj/{token}`` scheme is
not auth, and a container publishes ``0.0.0.0`` — so the guard is a real secret
compared in constant time, resolved live per request (native reads the file the
CLI minted; container reads the injected env). ``AGENTALLOY_CORPUS_INGEST=0``
disables the route entirely.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from agentalloy.config import get_settings
from agentalloy.install.ingest_secret import resolve_ingest_secret, secret_matches
from agentalloy.install.subcommands.install_pack import (
    _read_pack_manifest,  # pyright: ignore[reportPrivateUsage]
    install_local_pack,
)
from agentalloy.install.subcommands.lessons import (
    _default_embed,  # pyright: ignore[reportPrivateUsage]
    probe_lesson_duplicates,
)
from agentalloy.web.runtime_refresh import refresh_runtime_cache

INGEST_TOKEN_HEADER = "X-AgentAlloy-Ingest-Token"
CORPUS_INGEST_ENV = "AGENTALLOY_CORPUS_INGEST"

router = APIRouter()


class IngestPackRequest(BaseModel):
    """A generated pack pushed as bytes so it crosses the host→container boundary."""

    pack: dict[str, str] = Field(
        ..., description="Pack files as {relative-path: text-content} (pack.yaml + skill YAMLs)."
    )
    allow_duplicates: bool = Field(
        default=False, description="Install even on a hard cross-pack near-duplicate."
    )
    strict: bool = Field(
        default=True,
        description="Promote authoring-lint warnings to errors. The CLI sends false for "
        "install-pack --allow-lint-warnings so the routed install matches the host install.",
    )
    reembed: bool = Field(
        default=True,
        description="Run the post-ingest reembed. install-packs sends false on all but the last pack.",
    )
    allow_unreviewed: bool = Field(
        default=False,
        description="Mirrors install-pack --allow-unreviewed — bypass Gate 1.5 for this pack, "
        "recorded (not silent) in the result contract. The CLI computes this locally; the "
        "service-mediated path must honor the same operator choice as the direct-write path.",
    )


def _disabled() -> bool:
    return os.environ.get(CORPUS_INGEST_ENV, "").strip() == "0"


def _materialize(pack: dict[str, str], dest: Path) -> None:
    """Write the pack files under ``dest``, refusing any path that escapes it."""
    dest_root = dest.resolve()
    for rel, content in pack.items():
        target = (dest_root / rel).resolve()
        if target != dest_root and dest_root not in target.parents:
            raise HTTPException(status_code=400, detail=f"unsafe path in pack: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _fragment_texts(pack_dir: Path) -> list[str]:
    """Candidate fragment prose for the pre-install dedup probe (AC-4).

    Reuses the ingest YAML parser so the texts match what would be ingested.
    """
    from agentalloy.ingest import _load_yaml  # pyright: ignore[reportPrivateUsage]

    manifest, _errors = _read_pack_manifest(pack_dir)
    if manifest is None:
        return []
    skills: list[dict[str, Any]] = manifest.get("skills") or []
    texts: list[str] = []
    for entry in skills:
        try:
            record = _load_yaml(pack_dir / str(entry["file"]))
        except Exception:
            # A malformed YAML isn't our problem to diagnose here — install_local_pack's
            # schema gate reports it precisely. Skip for probing; don't crash the probe.
            continue
        texts.extend(frag.content for frag in record.fragments if frag.content)
    return texts


def _run_ingest(app: Any, body: IngestPackRequest) -> dict[str, Any]:
    """Blocking ingest — run in a thread. Mirrors web/wizard_api.install."""
    settings = get_settings()
    # root is only used as the ingest subprocess cwd; the corpus path comes from
    # settings/env. The corpus dir is guaranteed to exist (the service opened it).
    service_root = Path(settings.duckdb_path).parent

    with tempfile.TemporaryDirectory(prefix="agentalloy-ingest-") as tmp:
        pack_dir = Path(tmp) / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        _materialize(body.pack, pack_dir)

        # --- pre-install dedup probe (AC-4/AC-5): block BEFORE any write ---
        vector_store = getattr(app.state, "vector_store", None)
        if vector_store is not None and not body.allow_duplicates:
            texts = _fragment_texts(pack_dir)
            if texts:
                try:
                    hits = probe_lesson_duplicates(
                        texts,
                        embed=_default_embed(),
                        vector_store=vector_store,
                        hard_similarity=settings.dedup_hard_threshold,
                        soft_similarity=settings.dedup_soft_threshold,
                    )
                except Exception as exc:  # embed down, dim mismatch, corrupt store
                    return {
                        "action": "dedup_probe_failed",
                        "error": (
                            f"could not check the pack for duplicates ({exc}). Not installed. "
                            "Re-send with allow_duplicates to skip the check."
                        ),
                    }
                if hits:
                    dups = sorted({getattr(h, "skill_id", "?") for h in hits})
                    return {
                        "action": "duplicate_refused",
                        "duplicates": dups,
                        "error": (
                            f"pack duplicates existing corpus skill(s) {dups} "
                            f"(cosine >= {settings.dedup_hard_threshold}). Not installed. "
                            "Re-send with allow_duplicates to install anyway."
                        ),
                    }

        # --- install through the service's own writer window ---
        store = getattr(app.state, "store", None)
        release = store.released() if store is not None else nullcontext()
        with release:
            result = install_local_pack(
                pack_dir,
                root=service_root,
                no_restart=True,
                strict=body.strict,
                allow_duplicates=body.allow_duplicates,
                allow_unreviewed=body.allow_unreviewed,
                run_reembed=body.reembed,
            )
        refresh_runtime_cache(app)
        return result


@router.post("/corpus/ingest-pack", response_model=None, summary="Ingest a pushed pack (guarded)")
async def ingest_pack(
    request: Request,
    body: IngestPackRequest,
    x_agentalloy_ingest_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    if _disabled():
        raise HTTPException(status_code=404, detail="corpus ingest is disabled")
    # Resolve live (mint=False — the host is source of truth; the service never mints).
    expected = resolve_ingest_secret(mint=False)
    if not secret_matches(expected, x_agentalloy_ingest_token):
        raise HTTPException(status_code=401, detail="invalid or missing ingest token")
    return await asyncio.to_thread(_run_ingest, request.app, body)
