"""Web UI configuration endpoints — GET/PUT ``/api/config``, POST ``/api/config/reload``.

``Settings`` reads the process environment only; the user-scoped ``.env`` at
``${XDG_CONFIG_HOME}/agentalloy/.env`` is sourced into the service's env by the
serve wrapper. So the split is: PUT persists edits to the ``.env`` file
(targeted KEY=VALUE upsert, preserving unknown lines), and reload pushes the
file back into ``os.environ`` so per-request ``get_settings()`` callers see the
new values. Connections opened at lifespan (stores, embed client) keep their
old config until a real restart — reload is deliberately soft.

Mutating endpoints require the ``X-AgentAlloy-CSRF: 1`` header. The service is
localhost-only with no CORS middleware, so a custom header forces a preflight
that no foreign origin can pass — cheap CSRF insurance for a no-auth UI.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from agentalloy.config import AuthoringConfig, configure_logging, get_settings

router = APIRouter()

_MASK = "***"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Editable field → env var written to the user-scoped .env. Bare names mirror
# Settings' pydantic env mapping; AUTHORING_* mirrors AuthoringConfig's prefix.
_EDITABLE_ENV: dict[str, str] = {
    "upstream_url": "UPSTREAM_URL",
    "upstream_model": "UPSTREAM_MODEL",
    "upstream_api_key": "UPSTREAM_API_KEY",
    "anthropic_upstream_url": "ANTHROPIC_UPSTREAM_URL",
    "runtime_embed_base_url": "RUNTIME_EMBED_BASE_URL",
    "runtime_embedding_model": "RUNTIME_EMBEDDING_MODEL",
    "embedding_provider": "EMBEDDING_PROVIDER",
    "log_level": "LOG_LEVEL",
    "dedup_hard_threshold": "DEDUP_HARD_THRESHOLD",
    "dedup_soft_threshold": "DEDUP_SOFT_THRESHOLD",
    "bounce_budget": "BOUNCE_BUDGET",
    "sdd_fast_require_approval": "SDD_FAST_REQUIRE_APPROVAL",
    "profile_root": "PROFILE_ROOT",
    "forced_profile": "FORCED_PROFILE",
    "code_indexer_url": "CODE_INDEXER_URL",
    "compose_enabled": "COMPOSE_ENABLED",
    "code_index_enabled": "CODE_INDEX_ENABLED",
    "code_index_watch": "CODE_INDEX_WATCH",
    "authoring_model": "AUTHORING_MODEL",
    "authoring_critic_model": "AUTHORING_CRITIC_MODEL",
    "authoring_lm_base_url": "AUTHORING_LM_BASE_URL",
}
_FLOAT_FIELDS = ("dedup_hard_threshold", "dedup_soft_threshold")
_INT_FIELDS = ("bounce_budget",)
_BOOL_FIELDS = (
    "sdd_fast_require_approval",
    "compose_enabled",
    "code_index_enabled",
    "code_index_watch",
)
# Optional strings: null (or "") removes the var from .env.
_NULLABLE_FIELDS = ("forced_profile", "upstream_url", "upstream_model", "upstream_api_key")


class ConfigUpdateResult(BaseModel):
    status: str
    message: str
    env_file_path: str


class ReloadResult(BaseModel):
    status: str
    message: str


def _env_file_path() -> Any:
    from agentalloy.install import state as install_state

    return install_state.env_path()


def _require_csrf(header_value: str | None) -> None:
    if header_value != "1":
        raise HTTPException(
            status_code=403,
            detail="Missing X-AgentAlloy-CSRF: 1 header (browser cross-origin guard).",
        )


def _bad(field: str, detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": "invalid_field", "detail": detail})


def _coerce(field: str, value: Any) -> str | None:
    """Validate one incoming field; return the .env string form (None = unset)."""
    if value is None:
        if field in _NULLABLE_FIELDS:
            return None
        raise _bad(field, f"{field} cannot be null")
    if field in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise _bad(field, f"{field} must be a boolean")
        return "1" if value else "0"
    if field in _FLOAT_FIELDS:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise _bad(field, f"{field} must be a number")
        f = float(value)
        if not 0.0 <= f <= 1.0:
            raise _bad(field, f"{field} must be between 0.0 and 1.0")
        return repr(f)
    if field in _INT_FIELDS:
        if not isinstance(value, int) or isinstance(value, bool):
            raise _bad(field, f"{field} must be an integer")
        if not 1 <= value <= 10:
            raise _bad(field, f"{field} must be between 1 and 10")
        return str(value)
    if not isinstance(value, str):
        raise _bad(field, f"{field} must be a string")
    if field == "log_level":
        if value.upper() not in _LOG_LEVELS:
            raise _bad(field, f"log_level must be one of: {', '.join(_LOG_LEVELS)}")
        return value.upper()
    if field in _NULLABLE_FIELDS and value == "":
        return None
    return value


def _upsert_env_file(updates: dict[str, str | None]) -> str:
    """Apply KEY=VALUE upserts (None deletes) to the user-scoped .env, atomically.

    Unknown lines and comments are preserved verbatim — this is a targeted edit,
    not a regeneration, so it composes with ``write-env``'s sentinel-guarded
    full rewrites and any hand edits.
    """
    from agentalloy.install import state as install_state

    env_path = _env_file_path()
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        lines = ["# Written by the agentalloy web UI (/api/config)"]

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else None
        if key is not None and not stripped.startswith("#") and key in remaining:
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
            # None → drop the line (unset)
        else:
            out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(out) + "\n"
    install_state._atomic_write(env_path, content, mode=0o600)  # pyright: ignore[reportPrivateUsage]
    return str(env_path)


@router.get("/api/config", summary="Current configuration (secrets masked)")
async def get_config() -> dict[str, Any]:
    s = get_settings()
    a = AuthoringConfig()
    return {
        "upstream_url": s.upstream_url or None,
        "upstream_model": s.upstream_model or None,
        "upstream_api_key": _MASK if s.upstream_api_key else None,
        "anthropic_upstream_url": s.anthropic_upstream_url,
        "runtime_embed_base_url": s.runtime_embed_base_url,
        "runtime_embedding_model": s.runtime_embedding_model,
        "embedding_provider": s.embedding_provider,
        "log_level": s.log_level,
        "dedup_hard_threshold": s.dedup_hard_threshold,
        "dedup_soft_threshold": s.dedup_soft_threshold,
        "bounce_budget": s.bounce_budget,
        "sdd_fast_require_approval": s.sdd_fast_require_approval,
        "profile_root": s.profile_root,
        "forced_profile": s.forced_profile,
        "code_indexer_url": s.code_indexer_url or None,
        "compose_enabled": s.compose_enabled,
        "code_index_enabled": s.code_index_enabled,
        "code_index_watch": s.code_index_watch,
        "code_index_data_dir": s.code_index_data_dir,
        "authoring_model": a.model,
        "authoring_critic_model": a.critic_model,
        "authoring_lm_base_url": a.lm_base_url,
        "duckdb_path": s.duckdb_path,
        "fragments_lance_path": s.fragments_lance_path,
        "telemetry_db_path": s.telemetry_db_path,
        "env_file_path": str(_env_file_path()),
    }


@router.put("/api/config", response_model=ConfigUpdateResult, summary="Persist config edits")
async def put_config(
    body: dict[str, Any],
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> ConfigUpdateResult:
    _require_csrf(x_agentalloy_csrf)
    unknown = sorted(set(body) - set(_EDITABLE_ENV))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_field",
                "detail": f"Unknown or read-only field(s): {', '.join(unknown)}",
            },
        )

    updates: dict[str, str | None] = {}
    for field, value in body.items():
        if field == "upstream_api_key" and value == _MASK:
            continue  # the UI echoed the mask back — unchanged
        updates[_EDITABLE_ENV[field]] = _coerce(field, value)

    # Cross-field guard on the merged view (current settings + this edit).
    s = get_settings()
    merged_hard = float(updates.get("DEDUP_HARD_THRESHOLD") or s.dedup_hard_threshold)
    merged_soft = float(updates.get("DEDUP_SOFT_THRESHOLD") or s.dedup_soft_threshold)
    if merged_hard < merged_soft:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_field",
                "detail": "dedup_hard_threshold must be >= dedup_soft_threshold",
            },
        )

    if not updates:
        return ConfigUpdateResult(
            status="ok", message="No changes.", env_file_path=str(_env_file_path())
        )
    env_file = _upsert_env_file(updates)
    return ConfigUpdateResult(
        status="ok",
        message="Config updated. POST /api/config/reload applies it without a restart.",
        env_file_path=env_file,
    )


@router.post("/api/config/reload", response_model=ReloadResult, summary="Soft-reload config")
async def reload_config(
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
) -> ReloadResult:
    _require_csrf(x_agentalloy_csrf)
    env_path = _env_file_path()
    if not env_path.exists():
        raise HTTPException(
            status_code=500,
            detail={"error": "reload_failed", "detail": f"No .env at {env_path}"},
        )
    try:
        from agentalloy.install import state as install_state

        values = install_state.parse_env_file(env_path)
        os.environ.update(values)
        configure_logging()
    except Exception as exc:  # noqa: BLE001 — surfaced as a structured 500
        raise HTTPException(
            status_code=500,
            detail={"error": "reload_failed", "detail": f"Failed to reload config: {exc}"},
        ) from exc
    return ReloadResult(
        status="ok",
        message=(
            "Configuration reloaded. Per-request settings pick this up now; "
            "store/embed connections keep their old config until a restart."
        ),
    )
