"""Config (M1a) — env parsing, clamping, and the module cache."""

from __future__ import annotations

import pytest

from agentalloy.local_agent.config import (
    LocalAgentConfig,
    LocalAgentMode,
    get_config,
    load_config,
)


class TestLoadConfig:
    def test_defaults(self) -> None:
        cfg = load_config()
        assert cfg.mode is LocalAgentMode.OFF
        assert cfg.enabled is False
        assert cfg.url == "http://127.0.0.1:50001"
        assert cfg.model == "minicpm5-2b"
        assert cfg.timeout_ms == 30000
        assert cfg.max_steps == 2
        assert cfg.max_tokens == 2048
        assert cfg.temperature == 0.0
        assert cfg.result_cap_chars == 2400

    def test_on_mode_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT", "on")
        assert load_config().enabled is True

    def test_unknown_mode_falls_back_to_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT", "maybe")
        assert load_config().mode is LocalAgentMode.OFF

    def test_max_steps_clamped_to_hard_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "10")
        assert load_config().max_steps == 3

    def test_max_steps_below_one_clamped_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "0")
        assert load_config().max_steps == 1

    def test_invalid_ints_fall_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "two")
        monkeypatch.setenv("LOCAL_AGENT_TIMEOUT_MS", "oops")
        cfg = load_config()
        assert cfg.max_steps == 2
        assert cfg.timeout_ms == 30000

    def test_nonpositive_numerics_fall_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_TIMEOUT_MS", "-1")
        monkeypatch.setenv("LOCAL_AGENT_MAX_TOKENS", "0")
        cfg = load_config()
        assert cfg.timeout_ms == 30000
        assert cfg.max_tokens == 2048

    def test_url_trailing_slash_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_URL", "http://10.0.0.9:1234/")
        assert load_config().url == "http://10.0.0.9:1234"

    def test_model_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_MODEL", "custom-model")
        assert load_config().model == "custom-model"


class TestConfigCache:
    def test_get_config_caches_until_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_MODEL", "first")
        first = get_config()
        monkeypatch.setenv("LOCAL_AGENT_MODEL", "second")
        cached = get_config()
        assert cached is first
        assert cached.model == "first"
        assert get_config() is first

    def test_reset_local_agent_cache_reflects_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentalloy.local_agent.config import reset_local_agent_cache

        reset_local_agent_cache()
        monkeypatch.setenv("LOCAL_AGENT_MODEL", "second")
        reset_local_agent_cache()
        assert get_config().model == "second"


def test_config_dataclass_defaults() -> None:
    cfg = LocalAgentConfig(
        mode=LocalAgentMode.ON,
        url="http://x:1",
        model="m",
        timeout_ms=30000,
        max_steps=2,
        max_tokens=2048,
        result_cap_chars=2400,
    )
    assert cfg.temperature == 0.0  # fixed in code, never an env knob
    assert cfg.enabled is True
    assert (
        LocalAgentConfig(
            mode=LocalAgentMode.OFF,
            url="http://x:1",
            model="m",
            timeout_ms=30000,
            max_steps=2,
            max_tokens=2048,
            result_cap_chars=2400,
        ).enabled
        is False
    )
