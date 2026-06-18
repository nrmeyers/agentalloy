"""Unit tests for the ``write-env`` subcommand.

Maps to test-plan.md § Preset templating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.subcommands.write_env import (
    _SENTINEL,  # pyright: ignore[reportPrivateUsage]
    DEFAULT_PORT,
    VALID_PRESETS,
    _load_preset,  # pyright: ignore[reportPrivateUsage]
    _parse_overrides,  # pyright: ignore[reportPrivateUsage]
    write_env,
)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


# ---------------------------------------------------------------------------
# Preset loading
# ---------------------------------------------------------------------------


class TestPresetLoading:
    def test_all_presets_load(self) -> None:
        for name in VALID_PRESETS:
            defaults = _load_preset(name)
            assert isinstance(defaults, dict)
            assert len(defaults) > 0

    def test_preset_has_expected_keys(self) -> None:
        # Schema v2: presets no longer carry DUCKDB_PATH / LADYBUG_DB_PATH —
        # those are computed at runtime from the user data dir, not pinned
        # to a project-relative path.
        defaults = _load_preset("cpu")
        assert "RUNTIME_EMBED_BASE_URL" in defaults
        assert "RUNTIME_EMBEDDING_MODEL" in defaults
        assert "DUCKDB_PATH" not in defaults
        assert "LADYBUG_DB_PATH" not in defaults

    def test_unknown_preset_exits(self) -> None:
        with pytest.raises(SystemExit):
            _load_preset("nonexistent")


# ---------------------------------------------------------------------------
# Port recording (port is stored for wire-harness, not templated into URLs)
# ---------------------------------------------------------------------------


class TestPortRecording:
    def test_default_port_8000(self, repo_root: Path) -> None:
        result = write_env("cpu", root=repo_root)
        assert result["port"] == DEFAULT_PORT

    def test_custom_port(self, repo_root: Path) -> None:
        result = write_env("cpu", port=9090, root=repo_root)
        assert result["port"] == 9090

    def test_preset_urls_are_fixed(self, repo_root: Path) -> None:
        """Preset URLs bind the embed llama-server to its fixed port (47951)."""
        result = write_env("cpu", root=repo_root)
        assert result["values_written"]["RUNTIME_EMBED_BASE_URL"] == "http://localhost:47951"
        assert "LM_STUDIO_BASE_URL" not in result["values_written"]
        assert "AUTHORING_EMBED_BASE_URL" not in result["values_written"]

    def test_preset_carries_reranker_vars(self, repo_root: Path) -> None:
        """Presets wire the signal-intent reranker to the second llama-server (47952)."""
        result = write_env("cpu", root=repo_root)
        values = result["values_written"]
        assert values["SIGNAL_INTENT_BACKEND"] == "reranker"
        assert values["SIGNAL_INTENT_RERANK_URL"] == "http://127.0.0.1:47952"
        assert values["SIGNAL_INTENT_RERANK_MODEL"] == "Qwen3-Reranker-0.6B-Q8_0.gguf"


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_valid_override_applied(self, repo_root: Path) -> None:
        result = write_env(
            "cpu", overrides={"RUNTIME_EMBEDDING_MODEL": "nomic-embed-text-v1.5"}, root=repo_root
        )
        assert result["values_written"]["RUNTIME_EMBEDDING_MODEL"] == "nomic-embed-text-v1.5"

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_overrides(["BOGUS_KEY=value"])

    def test_invalid_format_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_overrides(["no-equals-sign"])


# ---------------------------------------------------------------------------
# .env file handling
# ---------------------------------------------------------------------------


class TestEnvFileHandling:
    def test_creates_env_file(self, repo_root: Path) -> None:
        result = write_env("cpu", root=repo_root)
        env_path = Path(result["env_path"])
        assert env_path.exists()
        content = env_path.read_text()
        assert _SENTINEL in content

    def test_overwrites_own_env(self, repo_root: Path) -> None:
        write_env("cpu", root=repo_root)
        # Second write should succeed (same sentinel)
        result = write_env("cpu", port=9090, root=repo_root)
        content = Path(result["env_path"]).read_text()
        assert "Port: 9090" in content

    def test_refuses_to_overwrite_foreign_env(self, repo_root: Path) -> None:
        # `.env` is now user-scoped under XDG_CONFIG_HOME, not at repo root.
        from agentalloy.install import state as install_state

        env_path = install_state.env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("SOME_USER_KEY=value\n")
        with pytest.raises(SystemExit):
            write_env("cpu", root=repo_root)

    def test_force_overwrites_foreign_env(self, repo_root: Path) -> None:
        from agentalloy.install import state as install_state

        env_path = install_state.env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("SOME_USER_KEY=value\n")
        result = write_env("cpu", force=True, root=repo_root)
        assert Path(result["env_path"]).exists()


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class TestWriteEnvSchema:
    def test_output_has_required_keys(self, repo_root: Path) -> None:
        result = write_env("cpu", root=repo_root)
        assert result["schema_version"] == 1
        assert "env_path" in result
        assert "preset" in result
        assert "port" in result
        assert "values_written" in result
