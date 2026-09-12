"""Config tests — verify env-driven loading."""

from agentalloy.config import Config


def test_config_defaults() -> None:
    """Default config loads without env vars."""
    config = Config.from_env()
    assert config.service_port == 48950
    assert config.model == "lfm2.5-2.6b"
    assert config.max_steps == 6


def test_config_env_override(monkeypatch: object) -> None:
    """Environment variables override defaults."""
    from typing import Any

    mp: Any = monkeypatch
    mp.setenv("AGENTALLOY_SERVICE_PORT", "47950")
    mp.setenv("AGENTALLOY_MODEL", "test-model")
    config = Config.from_env()
    assert config.service_port == 47950
    assert config.model == "test-model"
