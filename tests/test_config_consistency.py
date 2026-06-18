"""Config-consistency guards — the cheapest tests that would have caught the
60001-vs-47952 reranker-port bug.

The reranker endpoint is defined in three places: the ``lm_assist`` code default,
the ``classifier`` code default, and every shipped ``.env`` preset. Each one
looked fine in isolation, so 2200 unit tests stayed green while they pointed at
three different ports. These tests assert the files *agree with each other* and
that the dead ``60001`` port can never silently return. They parse the real
files, not mocks — the bug lived in the drift *between* files.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from agentalloy.install import state as install_state
from agentalloy.retrieval import lm_assist
from agentalloy.signals import classifier

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_RERANK_PORT = 47952
_DEAD_RERANK_PORT = 60001  # old default; pointed at an unrelated local service
_PRESETS = (
    ".env.cpu",
    ".env.nvidia",
    ".env.apple-silicon",
    ".env.strix-point",
    ".env.example",
)


def _port(url: str) -> int | None:
    return urlparse(url).port


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


def test_at_least_one_preset_exists() -> None:
    # Guard against the parametrized test silently skipping every case (e.g. if
    # the presets are renamed/moved) and reporting all-green coverage of nothing.
    assert any((_REPO_ROOT / p).exists() for p in _PRESETS)


@pytest.mark.parametrize("preset", _PRESETS)
def test_preset_reranker_url_matches_code_default(preset: str) -> None:
    path = _REPO_ROOT / preset
    if not path.exists():
        pytest.skip(f"{preset} not present")
    env = install_state.parse_env_file(path)

    # The dead port must never reappear in any value of any preset.
    for key, val in env.items():
        assert str(_DEAD_RERANK_PORT) not in val, (
            f"{preset}:{key} resurrects the dead :{_DEAD_RERANK_PORT} reranker port"
        )

    backend = env.get("SIGNAL_INTENT_BACKEND", "reranker").strip().lower()
    url = env.get("SIGNAL_INTENT_RERANK_URL")

    if backend == "cosine":
        # cosine presets legitimately ship no reranker URL (e.g. strix-point NPU box).
        return

    # reranker-mode presets MUST set the URL — the original bug was that NO preset
    # set it, so the code default (then 60001) leaked through unnoticed.
    assert url is not None, f"{preset} is reranker-mode but sets no SIGNAL_INTENT_RERANK_URL"
    assert _port(url) == _CANONICAL_RERANK_PORT, (
        f"{preset}: {url} is not the canonical reranker port"
    )
