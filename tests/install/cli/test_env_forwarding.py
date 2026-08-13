"""Audit-enforcement and filter tests for install.env_forwarding.

The classification tests are the guard the spec demands (AC 11): a new
``Settings`` field cannot ship without a forwarding decision.
"""

from pathlib import Path

from agentalloy.config import Settings
from agentalloy.install.env_forwarding import (
    HOST_TOPOLOGY_KEYS,
    INTENT_KEYS,
    MODULE_TOGGLES,
    URL_CLASS_UPSTREAM_KEYS,
    forwarded_env,
    loopback_upstream_warnings,
)

# ---------------------------------------------------------------------------
# Classification audit (spec AC 11)
# ---------------------------------------------------------------------------


def test_settings_keys_all_classified():
    """Every Settings field's env key must carry a forwarding decision."""
    unclassified = [
        name.upper()
        for name in Settings.model_fields
        if name.upper() not in INTENT_KEYS | HOST_TOPOLOGY_KEYS
    ]
    assert not unclassified, (
        f"Unclassified Settings env key(s): {unclassified}. Add each to "
        f"INTENT_KEYS (forwarded into the container) or HOST_TOPOLOGY_KEYS "
        f"(never forwarded) in src/agentalloy/install/env_forwarding.py, with "
        f"a rationale comment."
    )


def test_no_key_in_both_sets():
    assert not INTENT_KEYS & HOST_TOPOLOGY_KEYS


def test_url_class_and_toggles_are_intent():
    """Keys the renderer/upgrade/doctor act on must actually forward."""
    assert URL_CLASS_UPSTREAM_KEYS <= INTENT_KEYS
    assert set(MODULE_TOGGLES) <= INTENT_KEYS


def test_stage_b_posture_keys_are_intent():
    """v6.6.0 posture knobs must reach container deploys: the keep-threshold
    gap meant a host .env override silently never forwarded (found while the
    validated Stage B config had to be hand-injected via podman -e)."""
    assert {
        "LM_ASSIST_KEEP_THRESHOLD",
        "AGENTALLOY_PROCESS_DEMOTION",
        "AGENTALLOY_PROCESS_DEMOTION_WINDOW",
    } <= INTENT_KEYS


# ---------------------------------------------------------------------------
# forwarded_env filtering
# ---------------------------------------------------------------------------


def _write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(content)
    return p


def test_forwarded_env_filters_to_intent(tmp_path: Path):
    env = _write_env(
        tmp_path,
        "\n".join(
            [
                "# comment",
                "",
                "CODE_INDEX_ENABLED=1",
                "COMPOSE_ENABLED=0",
                'LOG_LEVEL="debug"',
                "DUCKDB_PATH=/host/evil.duck",  # host-topology: dropped
                "CODE_INDEX_DATA_DIR=/host/idx",  # host-topology: dropped
                "SOME_RANDOM_KEY=zzz",  # unknown: dropped
                "MALFORMED LINE WITHOUT EQUALS",
            ]
        ),
    )
    assert forwarded_env(env) == {
        "CODE_INDEX_ENABLED": "1",
        "COMPOSE_ENABLED": "0",
        "LOG_LEVEL": "debug",
    }


def test_forwarded_env_missing_file(tmp_path: Path):
    assert forwarded_env(tmp_path / "nope.env") == {}


def test_forwarded_env_assist_group(tmp_path: Path):
    env = _write_env(
        tmp_path,
        "LM_ASSIST=arbitrate\n"
        "SIGNAL_INTENT_BACKEND=rerank\n"
        "SIGNAL_INTENT_RERANK_URL=http://localhost:47952\n"
        "LM_ASSIST_TIMEOUT_MS=2000\n",
    )
    fwd = forwarded_env(env)
    assert fwd == {
        "LM_ASSIST": "arbitrate",
        "SIGNAL_INTENT_BACKEND": "rerank",
        "SIGNAL_INTENT_RERANK_URL": "http://localhost:47952",
        "LM_ASSIST_TIMEOUT_MS": "2000",
    }
    # In-container loopback is CORRECT for rerank URLs — no warning.
    assert loopback_upstream_warnings(fwd) == []


# ---------------------------------------------------------------------------
# Loopback upstream warning (design decision: warn, never rewrite)
# ---------------------------------------------------------------------------


def test_loopback_upstream_warns():
    warnings = loopback_upstream_warnings({"UPSTREAM_URL": "http://localhost:11434/v1"})
    assert len(warnings) == 1
    assert "UPSTREAM_URL" in warnings[0]
    assert "host.containers.internal" in warnings[0]


def test_loopback_127_warns():
    warnings = loopback_upstream_warnings({"ANTHROPIC_UPSTREAM_URL": "http://127.0.0.1:8080"})
    assert len(warnings) == 1
    assert "ANTHROPIC_UPSTREAM_URL" in warnings[0]


def test_non_loopback_upstream_no_warning():
    assert loopback_upstream_warnings({"UPSTREAM_URL": "https://api.openai.com/v1"}) == []
    assert (
        loopback_upstream_warnings({"UPSTREAM_URL": "http://host.containers.internal:11434/v1"})
        == []
    )


def test_localhost_named_host_does_not_false_positive():
    """Substring matches must not trip the warning (localhost.example.com)."""
    assert loopback_upstream_warnings({"UPSTREAM_URL": "https://localhost.example.com/v1"}) == []
