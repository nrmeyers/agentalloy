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

import subprocess
from pathlib import Path
from typing import get_args
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from agentalloy.api.compose_models import (
    DEFAULT_K_BY_PHASE,
    DEFAULT_MAX_TOKENS_BY_PHASE,
    ComposeRequest,
    Phase,
    _phase_k,  # pyright: ignore[reportPrivateUsage]
    _phase_max_tokens,  # pyright: ignore[reportPrivateUsage]
    compose_request_from_contract,
)
from agentalloy.api.proxy_apply import _tier2_k  # pyright: ignore[reportPrivateUsage]
from agentalloy.api.retrieve_models import RetrieveQueryRequest
from agentalloy.contracts import Contract, ContractScope
from agentalloy.install import state as install_state
from agentalloy.install.subcommands import write_env
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval import lm_assist
from agentalloy.retrieval.domain import (
    _contract_tag_filter_enabled,  # pyright: ignore[reportPrivateUsage]
    _deepen_band,  # pyright: ignore[reportPrivateUsage]
    _pool_categories,  # pyright: ignore[reportPrivateUsage]
    _soft_tag_filter,  # pyright: ignore[reportPrivateUsage]
    skill_granular_select,
)
from agentalloy.signals import classifier

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_RERANK_PORT = 47952
_DEAD_RERANK_PORT = 60001  # old default; pointed at an unrelated local service

# The hardware presets write-env renders the user .env from (the single source
# of truth). Named by hardware target only — see write_env.VALID_PRESETS.
_HW_PRESETS = ("cpu", "nvidia", "radeon", "apple-silicon")

# Expected Stage B posture per preset: ALL presets enable the compose re-ranker
# as of v4.0.2 — CPU was measured viable when the rerank server runs with
# --parallel 1 -c 2048 (start_rerank_server.rerank_launch_args). Stage B fails
# open to the deterministic path on any preset, so users on slower hardware
# than the measurement still get a safe degradation, not a break.
_LM_ASSIST_BY_PRESET = {
    "cpu": "arbitrate",
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
        # v4.0.2: 2000ms budget across all arbitrate presets — gives cold-start
        # (~1.2s GPU prompt-eval) + warmup-fallback recovery + CPU-headroom
        # margin. Was 1500ms in v4.0.0/4.0.1 — bumped after measuring the
        # budget was 70% unused on warm GPU and CPU needed more cushion.
        assert defaults.get("LM_ASSIST_TIMEOUT_MS") == "2000", (
            f"{preset}: arbitrate preset should set LM_ASSIST_TIMEOUT_MS=2000"
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


# ---------------------------------------------------------------------------
# TODO #8 — LOG_LEVEL fix (§A): the app-side helper applies LOG_LEVEL to the
# agentalloy.* loggers, so the systemd/launchd generators must NOT re-add a
# `--log-level` flag (that would re-couple observability to the ExecStart string
# and rescue only systemd). These guards lock that decision in place.
# ---------------------------------------------------------------------------


def test_systemd_unit_has_no_log_level_flag(tmp_path: Path) -> None:
    from agentalloy.install.subcommands.enable_service import _render_systemd_unit

    unit = _render_systemd_unit("/usr/bin/uv", tmp_path, 47950, tmp_path / "env")
    assert "--log-level" not in unit, (
        "systemd unit re-added a --log-level flag; LOG_LEVEL must flow via the "
        "EnvironmentFile and the app-side configure_logging() helper instead"
    )


def test_launchd_plist_has_no_log_level_flag(tmp_path: Path) -> None:
    from agentalloy.install.subcommands.enable_service import _render_launchd_plist

    plist = _render_launchd_plist("/usr/bin/uv", tmp_path, 47950, {"LOG_LEVEL": "INFO"})
    assert "--log-level" not in plist, (
        "launchd plist re-added a --log-level flag; LOG_LEVEL must flow via the "
        "inlined env and the app-side configure_logging() helper instead"
    )


def test_container_env_log_level_lowercased(monkeypatch, tmp_path: Path) -> None:
    # uvicorn requires lowercase level names; presets emit uppercase LOG_LEVEL.
    # The container env must lowercase a host LOG_LEVEL=DEBUG before passing it on.
    from agentalloy.install.subcommands import container_runtime

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    with patch.object(container_runtime.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        container_runtime._run_container("podman", "", projects_root=tmp_path)

    cmds = [c.args[0] for c in mock_run.call_args_list]
    run_cmd = next(c for c in cmds if "run" in c)
    joined = " ".join(run_cmd)
    assert "LOG_LEVEL=debug" in joined, f"container env LOG_LEVEL not lowercased: {joined}"
    assert "LOG_LEVEL=DEBUG" not in joined


@pytest.mark.parametrize("preset", _HW_PRESETS)
def test_preset_log_level_present(preset: str) -> None:
    # The EnvironmentFile must always supply LOG_LEVEL so the level fix has a value
    # to apply (absence would silently fall back to the code default).
    defaults = write_env._load_preset(preset)
    assert "LOG_LEVEL" in defaults, (
        f"{preset}: missing LOG_LEVEL — the rendered EnvironmentFile would omit it"
    )


# ---------------------------------------------------------------------------
# #13 (§E retrieval engine) — knob-consistency guards + new-mechanism coverage.
# Appended below #8's LOG_LEVEL blocks; does not disturb them. These lock the
# raised build/ship k, the lockstep max_tokens, the per-phase / Tier-2 env
# resolvers, and the dormant-by-default posture of the new retrieval knobs so the
# code defaults can't silently drift the way the reranker port / LM_ASSIST did.
# ---------------------------------------------------------------------------

_ALL_PHASES = get_args(Phase)


def _frag(
    frag_id: str,
    skill_id: str,
    *,
    domain_tags: list[str] | None = None,
    ftype: str = "execution",
) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=frag_id,
        fragment_type=ftype,
        sequence=1,
        content="",
        skill_id=skill_id,
        version_id=f"{skill_id}-v1",
        skill_class="domain",
        category="engineering",
        domain_tags=domain_tags or [],
    )


def test_k_and_max_tokens_tables_cover_full_phase_and_agree_on_keys() -> None:
    # The module comment promises both tables key every Phase value and are
    # indexed directly — enforce it so a new phase can't be added to one only.
    phases = set(_ALL_PHASES)
    assert set(DEFAULT_K_BY_PHASE) == phases
    assert set(DEFAULT_MAX_TOKENS_BY_PHASE) == phases


def test_default_k_by_phase_values() -> None:
    # #13 raised build/ship 2→4; sdd-fast stays tight; long-form phases unchanged.
    assert DEFAULT_K_BY_PHASE["build"] == 4
    assert DEFAULT_K_BY_PHASE["ship"] == 4
    assert DEFAULT_K_BY_PHASE["sdd-fast"] == 2
    for phase in ("qa", "spec", "design", "intake"):
        assert DEFAULT_K_BY_PHASE[phase] == 4


def test_max_tokens_lockstep_with_k() -> None:
    # E2 / Risk #5: raising k must raise the output budget in lockstep. Every
    # k==4 phase carries 4096; the k==2 sdd-fast pass stays at 2048.
    assert DEFAULT_MAX_TOKENS_BY_PHASE["build"] == 4096
    assert DEFAULT_MAX_TOKENS_BY_PHASE["ship"] == 4096
    assert DEFAULT_MAX_TOKENS_BY_PHASE["sdd-fast"] == 2048
    for phase in _ALL_PHASES:
        expected = 4096 if DEFAULT_K_BY_PHASE[phase] == 4 else 2048
        assert DEFAULT_MAX_TOKENS_BY_PHASE[phase] == expected, phase


def test_compose_request_resolved_k_per_phase() -> None:
    assert ComposeRequest(task="t", phase="build").resolved_k() == 4
    assert ComposeRequest(task="t", phase="ship").resolved_k() == 4
    assert ComposeRequest(task="t", phase="sdd-fast").resolved_k() == 2
    assert ComposeRequest(task="t", phase="qa").resolved_k() == 4
    assert ComposeRequest(task="t", phase="design").resolved_k() == 4
    # explicit k still overrides the phase default.
    assert ComposeRequest(task="t", phase="build", k=8).resolved_k() == 8


def test_retrieve_request_resolved_k_tracks_phase_default() -> None:
    # retrieve_models.resolved_k now resolves via the same _phase_k knob.
    assert RetrieveQueryRequest(task="t", phase="build").resolved_k() == 4
    assert RetrieveQueryRequest(task="t", phase="sdd-fast").resolved_k() == 2
    assert RetrieveQueryRequest(task="t", phase="design", k=3).resolved_k() == 3


def test_phase_k_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_K_BUILD", "6")
    assert _phase_k("build") == 6
    assert ComposeRequest(task="t", phase="build").resolved_k() == 6
    # malformed → table default; clamp to [1, 50].
    monkeypatch.setenv("AGENTALLOY_K_BUILD", "x")
    assert _phase_k("build") == 4
    monkeypatch.setenv("AGENTALLOY_K_BUILD", "99")
    assert _phase_k("build") == 50
    monkeypatch.setenv("AGENTALLOY_K_BUILD", "0")
    assert _phase_k("build") == 1
    # hyphenated phase reads the underscored env name.
    monkeypatch.setenv("AGENTALLOY_K_SDD_FAST", "5")
    assert _phase_k("sdd-fast") == 5


def test_phase_max_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_MAX_TOKENS_BUILD", "8192")
    assert _phase_max_tokens("build") == 8192
    # floor 256; malformed → table default.
    monkeypatch.setenv("AGENTALLOY_MAX_TOKENS_BUILD", "10")
    assert _phase_max_tokens("build") == 256
    monkeypatch.setenv("AGENTALLOY_MAX_TOKENS_BUILD", "nope")
    assert _phase_max_tokens("build") == 4096


def test_tier2_k_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTALLOY_TIER2_K", raising=False)
    assert _tier2_k() is None  # unset → phase default downstream
    monkeypatch.setenv("AGENTALLOY_TIER2_K", "3")
    assert _tier2_k() == 3
    monkeypatch.setenv("AGENTALLOY_TIER2_K", "99")
    assert _tier2_k() == 50  # clamped
    monkeypatch.setenv("AGENTALLOY_TIER2_K", "bad")
    assert _tier2_k() is None  # malformed → phase default


def test_compose_request_from_contract_threads_k() -> None:
    contract = Contract(
        path=Path("/x/.agentalloy/contracts/c.md"),
        phase="build",
        task_slug="do-the-thing",
        domain_tags=["react"],
        scope=ContractScope(touches=[], avoids=[]),
        success_criteria=[],
        related_contracts=[],
        created_at=None,
        body="build a react component",
    )
    assert compose_request_from_contract(contract, legs="domain", k=3).k == 3
    # omitted → None → phase default resolved server-side (build=4 post-E1).
    assert compose_request_from_contract(contract, legs="domain").k is None
    assert compose_request_from_contract(contract, legs="domain").resolved_k() == 4


def test_pool_categories_dormant_by_default_and_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # E6 ships dormant: unset → None (phase-agnostic, today's behavior).
    monkeypatch.delenv("AGENTALLOY_PHASE_GATE", raising=False)
    assert _pool_categories() is None
    monkeypatch.setenv("AGENTALLOY_PHASE_GATE", "on")
    cats = _pool_categories()
    assert cats is not None
    # The reserved benchmark category (#14) must never be in the allowlist.
    assert "benchmark" not in cats
    assert "engineering" in cats
    monkeypatch.setenv("AGENTALLOY_PHASE_GATE", "off")
    assert _pool_categories() is None


def test_deepen_band_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    # E4 ships inert: unset → 0.0 (legacy breadth-first).
    monkeypatch.delenv("AGENTALLOY_DEEPEN_BAND", raising=False)
    assert _deepen_band() == 0.0
    monkeypatch.setenv("AGENTALLOY_DEEPEN_BAND", "0.85")
    assert _deepen_band() == 0.85
    monkeypatch.setenv("AGENTALLOY_DEEPEN_BAND", "2.0")
    assert _deepen_band() == 1.0  # clamped to [0, 1]
    monkeypatch.setenv("AGENTALLOY_DEEPEN_BAND", "nope")
    assert _deepen_band() == 0.0


def test_contract_tag_filter_enabled_default_on_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # E5 ships on; AGENTALLOY_CONTRACT_TAG_FILTER=off is the kill-switch.
    monkeypatch.delenv("AGENTALLOY_CONTRACT_TAG_FILTER", raising=False)
    assert _contract_tag_filter_enabled() is True
    monkeypatch.setenv("AGENTALLOY_CONTRACT_TAG_FILTER", "off")
    assert _contract_tag_filter_enabled() is False
    monkeypatch.setenv("AGENTALLOY_CONTRACT_TAG_FILTER", "on")
    assert _contract_tag_filter_enabled() is True


def test_soft_tag_filter_intersect_and_empty_fallback() -> None:
    ranked = [
        _frag("r1", "react-hooks", domain_tags=["react", "ui"]),
        _frag("s1", "snowflake-warehouse", domain_tags=["snowflake"]),
    ]
    # Intersect: snowflake dropped when the contract only carries react.
    kept = _soft_tag_filter(ranked, ["react"])
    assert [f.fragment_id for f in kept] == ["r1"]
    # Empty intersection → full-pool fallback (process-vocab safety valve).
    assert _soft_tag_filter(ranked, ["nonexistent"]) == ranked
    # No contract tags → unchanged.
    assert _soft_tag_filter(ranked, None) == ranked


def test_deepen_gate_band_zero_is_byte_for_byte_legacy() -> None:
    # Same fixture the gate would fire on at band>0 (sibling B is far). At
    # band=0.0 the gated path must equal the no-gate-args (legacy) call exactly.
    ranked = [
        _frag("A1", "skill-a"),
        _frag("A2", "skill-a"),
        _frag("A3", "skill-a"),
        _frag("B1", "skill-b"),
    ]
    scores = {"A1": 1.0, "A2": 0.96, "A3": 0.92, "B1": 0.40}
    legacy, legacy_ranked = skill_granular_select(ranked, 2)
    gated, gated_ranked = skill_granular_select(ranked, 2, scores_by_id=scores, deepen_band=0.0)
    assert [f.fragment_id for f in gated] == [f.fragment_id for f in legacy]
    assert gated_ranked == legacy_ranked
    # And legacy preserves breadth (A + B) on this fixture.
    assert {f.skill_id for f in legacy} == {"skill-a", "skill-b"}


def test_deepen_gate_deepens_top_when_sibling_far() -> None:
    ranked = [
        _frag("A1", "skill-a"),
        _frag("A2", "skill-a"),
        _frag("A3", "skill-a"),
        _frag("B1", "skill-b"),
    ]
    scores = {"A1": 1.0, "A2": 0.96, "A3": 0.92, "B1": 0.40}  # B far below 0.85*1.0
    selected, _ranked = skill_granular_select(ranked, 2, scores_by_id=scores, deepen_band=0.85)
    # Spare slot deepens the top skill instead of admitting the far sibling.
    assert {f.skill_id for f in selected} == {"skill-a"}
    assert [f.fragment_id for f in selected] == ["A1", "A2"]


def test_deepen_gate_keeps_breadth_when_sibling_near() -> None:
    ranked = [
        _frag("A1", "skill-a"),
        _frag("A2", "skill-a"),
        _frag("B1", "skill-b"),
    ]
    scores = {"A1": 1.0, "A2": 0.96, "B1": 0.90}  # B within 0.85*1.0 band → near
    selected, _ranked = skill_granular_select(ranked, 2, scores_by_id=scores, deepen_band=0.85)
    assert {f.skill_id for f in selected} == {"skill-a", "skill-b"}


def test_deepen_gate_fills_k_when_all_far() -> None:
    # Top skill shallow (1 frag) and every sibling below band → Stage 4 still
    # fills k from the far siblings (no under-fill).
    ranked = [
        _frag("A1", "skill-a"),
        _frag("B1", "skill-b"),
        _frag("C1", "skill-c"),
    ]
    scores = {"A1": 1.0, "B1": 0.30, "C1": 0.20}
    selected, _ranked = skill_granular_select(ranked, 3, scores_by_id=scores, deepen_band=0.85)
    assert len(selected) == 3
    assert {f.skill_id for f in selected} == {"skill-a", "skill-b", "skill-c"}


# ---------------------------------------------------------------------------
# #9 (§C/§D Stage B viability) — Stage B fan-out / slot / doc-cap drift guards.
# The reranker --parallel slot count, the client fan-out cap, and the GPU presets
# must agree, and keep_threshold must stay gated-off (absent from every preset)
# until the deferred P(yes) measurement sets it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", _HW_PRESETS)
def test_preset_lm_assist_max_candidates_is_8(preset: str) -> None:
    # All arbitrate presets pin LM_ASSIST_MAX_CANDIDATES=8 (client-side fan-out).
    # The reranker --parallel value is now hardware-conditional via
    # start_rerank_server.rerank_launch_args (2 on GPU, 1 on CPU) — they do NOT
    # need to match: on CPU the 8 client requests serialize at the server (1
    # slot) and that's intentional (avoids OpenMP thread contention). On GPU
    # the 8 client requests use 2 slots × 4-deep queueing.
    defaults = write_env._load_preset(preset)
    if _LM_ASSIST_BY_PRESET[preset] != "arbitrate":
        return
    assert defaults.get("LM_ASSIST_MAX_CANDIDATES") == "8", (
        f"{preset}: arbitrate preset must set LM_ASSIST_MAX_CANDIDATES=8"
    )


@pytest.mark.parametrize("preset", _HW_PRESETS)
def test_preset_phase_gate_absent_by_default(preset: str) -> None:
    # The E6 pool gate (retrieval/domain.py) is a mechanism for hiding genuinely
    # benchmark-only packs from prod retrieval — it MUST NOT be used to hide
    # real product domain skills (fastapi/snowflake/temporal/data-engineering/vue
    # ARE real domain skills used by developers today). No preset sets
    # AGENTALLOY_PHASE_GATE; the gate stays dormant at its code default ("off")
    # until a future PR introduces actually-benchmark-only packs.
    defaults = write_env._load_preset(preset)
    assert "AGENTALLOY_PHASE_GATE" not in defaults, (
        f"{preset}: do not pin AGENTALLOY_PHASE_GATE in presets — the gate is "
        "the mechanism, but using it to hide real product packs is not the fix"
    )


@pytest.mark.parametrize("preset", _HW_PRESETS)
def test_preset_lm_assist_doc_cap_chars(preset: str) -> None:
    # GPU presets pin the runtime doc cap to the locked 2400-char value.
    defaults = write_env._load_preset(preset)
    if _LM_ASSIST_BY_PRESET[preset] != "arbitrate":
        return
    assert defaults.get("LM_ASSIST_DOC_CAP_CHARS") == "2400", (
        f"{preset}: arbitrate preset must set LM_ASSIST_DOC_CAP_CHARS=2400"
    )


@pytest.mark.parametrize("preset", _HW_PRESETS)
def test_preset_keep_threshold_absent(preset: str) -> None:
    # D6: keep_threshold ships gated-off (inert code default). NO preset may carry
    # LM_ASSIST_KEEP_THRESHOLD until a measured P(yes) value is set later — a preset
    # value here would silently arm the filter ahead of the decision gate.
    defaults = write_env._load_preset(preset)
    assert "LM_ASSIST_KEEP_THRESHOLD" not in defaults, (
        f"{preset}: LM_ASSIST_KEEP_THRESHOLD must stay ABSENT (measure-then-set, D6)"
    )


def test_rerank_launch_args_per_target() -> None:
    # Hardware-conditional rerank slot config (start_rerank_server.rerank_launch_args).
    # GPU and CPU have opposite optima: GPU benefits from multi-slot prefill
    # concurrency, CPU suffers from OpenMP thread contention with multiple slots.
    # Concrete values are pinned here so a drift breaks the build with a clear
    # error rather than silently flipping back to the old --parallel 8 config.
    from agentalloy.install.subcommands.start_rerank_server import rerank_launch_args

    # GPU: 2 slots × 2048 tok each (Pareto sweet spot — ~94% of --parallel 8
    # throughput at 50% KV memory; measured Jun 2026).
    assert rerank_launch_args("nvidia") == (2, 4096)
    assert rerank_launch_args("radeon") == (2, 4096)
    assert rerank_launch_args("apple-silicon") == (2, 4096)
    # CPU: 1 slot, all threads to ONE inference; 8 client requests serialize at
    # the server (intentional — avoids OpenMP contention).
    assert rerank_launch_args("cpu") == (1, 2048)
    # None coerces to "cpu" (matches start_rerank_server's launcher default:
    # `getattr(args, "hardware_target", "cpu") or "cpu"`).
    assert rerank_launch_args(None) == (1, 2048)
    # Truly unknown targets fall back to the GPU shape (safer for most installs).
    assert rerank_launch_args("future-target") == (2, 4096)


def test_scorer_pool_width_equals_max_candidates() -> None:
    # The shared FragmentScorer pool width is keyed to the same knob that caps the
    # candidate fan-out, so the two can never drift (both == --parallel).
    cfg = lm_assist.LMAssistConfig(
        mode=lm_assist.LMAssistMode.ARBITRATE,
        url="http://test",
        timeout_ms=300,
        keep_threshold=0.05,
        model="m",
    )
    scorer = lm_assist.FragmentScorer(cfg)
    try:
        assert scorer._pool._max_workers == lm_assist.max_candidates() == 8
    finally:
        scorer.close()
