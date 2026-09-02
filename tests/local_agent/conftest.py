"""Local-agent suite hygiene — hermetic LM-endpoint env + clean process caches.

The loop under test reads config and the endpoint-down latch through module
caches, so every test starts from a pristine state; tests that exercise the
latch or env overrides reset/monkeypatch explicitly.
"""

from __future__ import annotations

import pytest

from agentalloy.local_agent import client as _la_client
from agentalloy.local_agent import config as _la_config


@pytest.fixture(autouse=True)
def _hermetic_local_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_AGENT", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_URL", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_MODEL", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_MAX_STEPS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_TEMPERATURE", raising=False)
    reset_local_agent_process_state()


def reset_local_agent_process_state() -> None:
    _la_config.reset_local_agent_cache()
    _la_client.reset_client_caches()
