"""Tests for qwen-code proxy wiring — #504.

`agentalloy add qwen-code` writes a provider entry (`modelProviders.openai[]`,
id ``"agentalloy-proxy"``) plus a top-level ``model`` block. Qwen resolves the
*active* provider by matching ``model.name`` against a provider ``id`` — if
``model.name`` is left as the upstream model name (e.g. ``"qwen3.6-35B-XL"`)
and the user already has a real provider entry with that same id, qwen
resolves the colliding real entry and silently discards ``model.baseUrl``
(the proxy URL), routing every request straight to the real upstream instead.

The fix: ``model.name`` must be set to the proxy's own provider id
(``"agentalloy-proxy"``) so the id qwen resolves IS the proxy entry, and can
never collide with a provider the user configured under the real model name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.install.subcommands import wire_harness


def _read_repo_settings(root: Path) -> dict[str, object]:
    data = json.loads((root / ".qwen" / "settings.json").read_text())
    assert isinstance(data, dict)
    return data


def _resolve_active_base_url(settings: dict[str, object]) -> str | None:
    """Mimic qwen-code's own resolution: `model.name` is looked up as a
    provider `id` in `modelProviders.openai[]`; that provider's `baseUrl` wins
    over `model.baseUrl` on an id match (the exact behavior #504 exploited)."""
    model = settings.get("model")
    assert isinstance(model, dict)
    name = model.get("name")
    providers = settings.get("modelProviders")
    if isinstance(providers, dict):
        openai_providers = providers.get("openai")
        if isinstance(openai_providers, list):
            for entry in openai_providers:
                if isinstance(entry, dict) and entry.get("id") == name:
                    base_url = entry.get("baseUrl")
                    assert isinstance(base_url, str)
                    return base_url
    base_url = model.get("baseUrl")
    return base_url if isinstance(base_url, str) else None


class TestQwenCodeModelNameCollision:
    def test_model_name_is_the_proxy_provider_id_not_the_upstream_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        root = tmp_path / "repo"
        root.mkdir()

        wire_harness._wire_proxy_qwen_code(47950, root, scope="repo")  # pyright: ignore[reportPrivateUsage]

        settings = _read_repo_settings(root)
        model = settings["model"]
        assert isinstance(model, dict)
        assert model["name"] == "agentalloy-proxy"

    def test_resolves_to_proxy_even_when_colliding_provider_id_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduces #504: the user's global ~/.qwen/settings.json already has
        a real provider entry whose id equals the upstream model name that
        `add` would otherwise have written into `model.name`. After wiring,
        qwen's own id-resolution (mimicked by _resolve_active_base_url) must
        land on the PROXY's baseUrl, not the colliding real one."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        qwen_home = home / ".qwen"
        qwen_home.mkdir()

        colliding_model_id = "qwen3.6-35B-XL"
        real_upstream_url = "http://100.115.181.90:60002/v1"
        (qwen_home / "settings.json").write_text(
            json.dumps(
                {
                    "model": {"name": colliding_model_id, "baseUrl": real_upstream_url},
                    "modelProviders": {
                        "openai": [
                            {
                                "id": colliding_model_id,
                                "name": colliding_model_id,
                                "baseUrl": real_upstream_url,
                                "envKey": "OPENAI_API_KEY",
                            }
                        ]
                    },
                }
            )
        )

        root = tmp_path / "repo"
        root.mkdir()
        wire_harness._wire_proxy_qwen_code(47950, root, scope="repo")  # pyright: ignore[reportPrivateUsage]

        settings = _read_repo_settings(root)
        # The colliding real provider entry must survive untouched (existing
        # providers are never overwritten -- only a new proxy entry is added).
        providers = settings["modelProviders"]
        assert isinstance(providers, dict)
        openai_providers = providers["openai"]
        assert isinstance(openai_providers, list)
        ids = {e["id"] for e in openai_providers if isinstance(e, dict)}
        assert colliding_model_id in ids
        assert "agentalloy-proxy" in ids

        # But the id qwen actually resolves (model.name) must be the proxy's
        # own id -- so id-resolution lands on the proxy entry, not the real one.
        resolved_base_url = _resolve_active_base_url(settings)
        assert resolved_base_url is not None
        assert "/proj/" in resolved_base_url
        assert resolved_base_url != real_upstream_url

    def test_no_prior_model_block_still_gets_proxy_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        root = tmp_path / "repo"
        root.mkdir()

        wire_harness._wire_proxy_qwen_code(47950, root, scope="repo")  # pyright: ignore[reportPrivateUsage]

        settings = _read_repo_settings(root)
        model = settings["model"]
        assert isinstance(model, dict)
        assert model["name"] == "agentalloy-proxy"
        assert "/proj/" in model["baseUrl"]
