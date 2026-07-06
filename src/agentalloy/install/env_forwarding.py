"""Audited env-key forwarding for container deploys.

The host ``~/.config/agentalloy/.env`` is the single source of truth for user
intent on every deploy path. The native path reads it directly (sourced into
the service's process env); the container path forwards a subset as ``-e``
flags at ``podman run`` time. This module owns the classification that decides
which subset: every key the ``.env`` can carry is either *intent* (forwarded —
pure behavior, means the same thing inside the container) or *host-topology*
(never forwarded — the container bakes its own value and a host value would
point at paths or services that don't exist in its namespace).

Classification is enforced for ``Settings`` fields by
``tests/test_env_forwarding.py``, which enumerates ``Settings.model_fields``
and fails on any env key absent from both sets — a new config key cannot ship
without a forwarding decision.

Forwarded values land in the container's create spec, so anything here is
visible in ``podman inspect`` — including ``UPSTREAM_API_KEY``. That is
readable only by the same user who already owns the 0600 ``.env`` and the
rootless runtime socket; no privilege boundary is crossed.

Design doc: docs/design/container-module-env-propagation/approach.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from agentalloy.install import state

# Keys forwarded into the container when present in the host .env.
INTENT_KEYS: frozenset[str] = frozenset(
    {
        # Module toggles — the reason this module exists (see MODULE_TOGGLES).
        "COMPOSE_ENABLED",
        "CODE_INDEX_ENABLED",
        # Behavioral toggle, meaningful in-container (watchdog reindex).
        "CODE_INDEX_WATCH",
        # Operator intent; forwarded value wins over the renderer's old
        # process-env fallback.
        "LOG_LEVEL",
        # Retrieval tuning — pure behavior, no host paths.
        "DEDUP_HARD_THRESHOLD",
        "DEDUP_SOFT_THRESHOLD",
        "BOUNCE_BUDGET",
        # Workflow policy toggle.
        "SDD_FAST_REQUIRE_APPROVAL",
        # Proxy upstream config — same unreachable-on-container bug class as
        # the module toggles. Forwarded verbatim; a loopback host resolves
        # inside the container, so the renderer warns (URL_CLASS_UPSTREAM_KEYS).
        "UPSTREAM_URL",
        "UPSTREAM_MODEL",
        "UPSTREAM_API_KEY",
        "ANTHROPIC_UPSTREAM_URL",
        "RESPONSES_UPSTREAM_URL",
        # Release-check opt-out — pure behavior toggle (not a Settings field;
        # read via os.environ by install/release_check.py).
        "AGENTALLOY_RELEASE_CHECK",
        # Assist-stack group (not Settings fields; read via os.environ by the
        # retrieval/signal layers). Forwarded as one coupled group: the preset
        # rerank URLs are localhost:47952 and the container's baked reranker
        # listens on in-container localhost:47952, so a forwarded preset URL
        # and the baked reality are identical; the behavioral keys are
        # hardware intent and the container runs on the machine the preset
        # was chosen for.
        "LM_ASSIST",
        "LM_ASSIST_DOC_CAP_CHARS",
        "LM_ASSIST_MAX_CANDIDATES",
        "LM_ASSIST_TIMEOUT_MS",
        "LM_ASSIST_MODEL",
        "LM_ASSIST_RERANK_URL",
        "SIGNAL_INTENT_BACKEND",
        "SIGNAL_INTENT_RERANK_URL",
        "SIGNAL_INTENT_RERANK_MODEL",
    }
)

# Keys NEVER forwarded — the container bakes its own value.
HOST_TOPOLOGY_KEYS: frozenset[str] = frozenset(
    {
        # Volume-resident data paths, baked to /app/data/* by the renderer.
        "DUCKDB_PATH",
        "FRAGMENTS_LANCE_PATH",
        "TELEMETRY_DB_PATH",
        # The container runs its own baked llama-server + GGUF; switching the
        # embed model is a re-embed, not a config change (EmbeddingDimMismatch).
        "RUNTIME_EMBED_BASE_URL",
        "RUNTIME_EMBEDDING_MODEL",
        "EMBEDDING_PROVIDER",
        # Host-home-relative path.
        "PROFILE_ROOT",
        # Test-only override, never a deploy knob.
        "FORCED_PROFILE",
        # Per-repo index data stays on the agentalloy-data volume.
        "CODE_INDEX_DATA_DIR",
        # Renderer-owned: the run command computes these itself (pack list,
        # cadence-state dir, host-side mount resolution). Listed so the .env
        # inventory is complete in one place.
        "AGENTALLOY_PACKS",
        "AGENTALLOY_RUNTIME_STATE_DIR",
        "AGENTALLOY_PROJECTS_ROOT",
    }
)

# Forwarded URL keys that normally point at a service on the HOST. A loopback
# host in these resolves inside the container and won't reach it — the
# renderer emits a one-line warning naming host.containers.internal. The
# rerank URLs are deliberately excluded: in-container loopback is correct for
# them (the baked reranker listens there).
URL_CLASS_UPSTREAM_KEYS: frozenset[str] = frozenset(
    {
        "UPSTREAM_URL",
        "ANTHROPIC_UPSTREAM_URL",
        "RESPONSES_UPSTREAM_URL",
    }
)


class ModuleToggle(NamedTuple):
    """Display metadata for a module env toggle (upgrade notice, doctor).

    ``default_enabled`` mirrors the Settings default: the upgrade notice only
    fires for default-off modules a stale ``.env`` predates — an absent toggle
    for a default-on module means the module is already running, so there is
    nothing to announce. ``health_key`` is the module's name in ``/health``'s
    ``modules`` block (doctor drift check).
    """

    module: str
    enable_hint: str
    default_enabled: bool
    health_key: str


# The module toggles `upgrade` diffs against the user .env to announce
# newly-shipped modules, and `doctor` compares against /health "modules".
MODULE_TOGGLES: dict[str, ModuleToggle] = {
    "COMPOSE_ENABLED": ModuleToggle(
        module="instruction injector",
        enable_hint=(
            "add COMPOSE_ENABLED=1 to ~/.config/agentalloy/.env, then `agentalloy upgrade`"
        ),
        default_enabled=True,
        health_key="compose",
    ),
    "CODE_INDEX_ENABLED": ModuleToggle(
        module="codebase indexer",
        enable_hint=(
            "add CODE_INDEX_ENABLED=1 to ~/.config/agentalloy/.env, then `agentalloy upgrade`"
        ),
        default_enabled=False,
        health_key="code_index",
    ),
}


def forwarded_env(env_path: Path | None = None) -> dict[str, str]:
    """Intent keys from the host ``.env``, ready to merge into the run command.

    Reads the user-scoped ``.env`` (``state.env_path()`` unless overridden),
    and returns only the keys classified as intent. Missing file → ``{}`` —
    the container runs on baked defaults, identical to a host with no config.
    Unknown keys are dropped: forwarding is allowlist-only by design.
    """
    values = state.parse_env_file(env_path)
    return {k: v for k, v in values.items() if k in INTENT_KEYS}


def loopback_upstream_warnings(forwarded: dict[str, str]) -> list[str]:
    """One warning line per forwarded upstream key carrying a loopback host.

    A ``localhost``/``127.0.0.1`` upstream resolves *inside* the container.
    Warn — never rewrite: ``host.containers.internal`` only reaches host
    services bound beyond loopback, so a silent rewrite can break setups a
    warning cannot.
    """
    warnings: list[str] = []
    for key in sorted(URL_CLASS_UPSTREAM_KEYS & forwarded.keys()):
        value = forwarded[key]
        host = value.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
        if host in ("localhost", "127.0.0.1", "[::1]", "::1"):
            warnings.append(
                f"{key}={value} points at a loopback address, which resolves inside "
                f"the container — use host.containers.internal or a LAN address to "
                f"reach a service on the host."
            )
    return warnings
