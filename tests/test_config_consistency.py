"""Config-consistency guards — the cheapest tests that catch cross-file drift.

Two drift bugs motivate this file, both of the same shape: a value that looked
fine in isolation disagreed with the *same* value somewhere else, so thousands
of unit tests stayed green while the files pointed different directions.

1. The reranker endpoint (``lm_assist`` default, ``classifier`` default, and
   every shipped preset) once pointed at three different ports — the dead
   ``60001`` leaked through because no preset set it.
2. ``LM_ASSIST`` (Stage B compose re-ranker) lived only in a parallel, *non-
   functional* set of root ``.env.*`` reference files; the **preset YAMLs that
   ``write-env`` actually renders from never set it**, so every generated ``.env``
   — GPU included — silently fell back to the code default ``off``.

The fix for (2) made ``src/agentalloy/install/presets/*.yaml`` the single source
of truth and deleted the root mirrors. These tests assert directly against that
source: the files ``write-env`` ships, not a mirror that can drift from it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands import write_env
from agentalloy.retrieval import lm_assist
from agentalloy.signals import classifier

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_RERANK_PORT = 47952
_DEAD_RERANK_PORT = 60001  # old default; pointed at an unrelated local service

# The hardware presets write-env renders the user .env from (the single source
# of truth). Named by hardware target only — see write_env.VALID_PRESETS.
_HW_PRESETS = ("cpu", "nvidia", "radeon", "apple-silicon")

# Expected Stage B posture per preset: GPU presets enable the compose re-ranker
# (budget headroom to score real fragments), cpu leaves it off.
_LM_ASSIST_BY_PRESET = {
    "cpu": "off",
    "nvidia": "arbitrate",
    "radeon": "arbitrate",
    "apple-silicon": "arbitrate",
}

# The .env.example template documents every knob; it's not a per-hardware source
# of truth (its LM_ASSIST is a template default), but it must still never carry
# the dead reranker port and must agree on the canonical port in reranker mode.
_ENV_TEMPLATE = ".env.example"


def _port(url: str) -> int | None:
    return urlparse(url).port


def test_hw_presets_match_write_env() -> None:
    # Guard against a rename/move silently skipping the parametrized cases below
    # and reporting all-green coverage of nothing.
    assert set(_HW_PRESETS) == set(write_env.VALID_PRESETS)
    assert set(_LM_ASSIST_BY_PRESET) == set(_HW_PRESETS)


def test_code_reranker_defaults_agree() -> None:
    # The reranker endpoint is configured in two modules; they must not drift.
    assert lm_assist._DEFAULT_URL == classifier._DEFAULT_RERANK_URL
    assert lm_assist._DEFAULT_MODEL == classifier._DEFAULT_RERANK_MODEL


def test_code_reranker_default_is_canonical_port() -> None:
    for url in (
        lm_assist._DEFAULT_URL,
        classifier._DEFAULT_RERANK_URL,
        install_state.DEFAULT_RERANK_URL,
    ):
        assert _port(url) == _CANONICAL_RERANK_PORT, url
        assert str(_DEAD_RERANK_PORT) not in url


@pytest.mark.parametrize("preset", _HW_PRESETS)
def test_preset_reranker_url_matches_code_default(preset: str) -> None:
    defaults = write_env._load_preset(preset)

    # The dead port must never reappear in any value of any preset.
    for key, val in defaults.items():
        assert str(_DEAD_RERANK_PORT) not in str(val), (
            f"{preset}:{key} resurrects the dead :{_DEAD_RERANK_PORT} reranker port"
        )

    backend = str(defaults.get("SIGNAL_INTENT_BACKEND", "reranker")).strip().lower()
    url = defaults.get("SIGNAL_INTENT_RERANK_URL")

    if backend == "cosine":
        # cosine presets legitimately ship no reranker URL (embedder-based floor).
        return

    # reranker-mode presets MUST set the URL — the original bug was that NO preset
    # set it, so the code default (then 60001) leaked through unnoticed.
    assert url is not None, f"{preset} is reranker-mode but sets no SIGNAL_INTENT_RERANK_URL"
    assert _port(str(url)) == _CANONICAL_RERANK_PORT, (
        f"{preset}: {url} is not the canonical reranker port"
    )


@pytest.mark.parametrize("preset", _HW_PRESETS)
def test_preset_lm_assist_posture(preset: str) -> None:
    # The whole point: LM_ASSIST must be present (absence = the bug) and match the
    # hardware posture — GPU presets arbitrate, cpu off.
    defaults = write_env._load_preset(preset)
    expected = _LM_ASSIST_BY_PRESET[preset]
    actual = defaults.get("LM_ASSIST")
    assert actual == expected, (
        f"{preset}: LM_ASSIST is {actual!r}, expected {expected!r} "
        f"(missing → silently falls back to the code default 'off')"
    )
    if expected == "arbitrate":
        # GPU presets raise the 600ms code default for headroom.
        assert defaults.get("LM_ASSIST_TIMEOUT_MS") == "1500", (
            f"{preset}: arbitrate preset should set LM_ASSIST_TIMEOUT_MS=1500"
        )


def test_env_template_reranker_url_is_canonical() -> None:
    path = _REPO_ROOT / _ENV_TEMPLATE
    assert path.exists(), f"{_ENV_TEMPLATE} is the documented template and must exist"
    env = install_state.parse_env_file(path)

    for key, val in env.items():
        assert str(_DEAD_RERANK_PORT) not in val, (
            f"{_ENV_TEMPLATE}:{key} resurrects the dead :{_DEAD_RERANK_PORT} reranker port"
        )

    backend = env.get("SIGNAL_INTENT_BACKEND", "reranker").strip().lower()
    if backend == "cosine":
        return
    url = env.get("SIGNAL_INTENT_RERANK_URL")
    assert url is not None, f"{_ENV_TEMPLATE} is reranker-mode but sets no SIGNAL_INTENT_RERANK_URL"
    assert _port(url) == _CANONICAL_RERANK_PORT, (
        f"{_ENV_TEMPLATE}: {url} is not the canonical reranker port"
    )
