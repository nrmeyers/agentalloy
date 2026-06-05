# ruff: noqa: I001 -- testing private module members intentionally
"""Tests for install state schema migration (v3 → v4)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


from agentalloy.install import state as install_state

# Local aliases for private helpers
_empty_state = install_state._empty_state  # pyright: ignore[reportPrivateUsage]


class TestSchemaVersion:
    """Test CURRENT_SCHEMA_VERSION is 5."""

    def test_schema_version_is_5(self):
        assert install_state.CURRENT_SCHEMA_VERSION == 5


class TestFreshStateV4:
    """Test _empty_state() returns schema v4 with runtime fields."""

    def test_fresh_state_has_runtime_fields(self):
        st = _empty_state()
        assert st["schema_version"] == 5
        # Runtime fields present
        assert "runtime_binary" in st
        assert "image_tag" in st
        assert "container_name" in st
        assert "data_volume" in st
        # Compose fields removed
        assert "compose_file" not in st
        assert "compose_binary" not in st
        assert "compose_binary_path" not in st
        # v5 bootstrap fields
        assert st["bootstrap_started_at"] is None
        assert st["bootstrap_completed_at"] is None
        assert st["bootstrap_packs_ingested"] == []
        assert st["bootstrap_reembed_count"] == 0
        assert st["bootstrap_lock_file"] == "/app/.bootstrap-lock"
        assert st["bootstrap_checkpoints"] == []

    def test_fresh_state_preserves_common_fields(self):
        st = _empty_state()
        assert "install_started_at" in st
        assert st["completed_steps"] == []
        assert st["harness_files_written"] == []
        assert st["models_pulled"] == []
        assert st["port"] == 47950
        assert st["last_verify_passed_at"] is None
        assert st["pending_pack_selection"] is None
        assert "deployment" in st


class TestV3ToV4Migration:
    """Test that v3 state files are migrated to v4 on load."""

    def test_v3_migrated_to_v4(self, tmp_path: Path):
        """A v3 state file loads with compose fields removed and runtime fields added."""
        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        old_config = os.environ.get("XDG_CONFIG_HOME")
        old_data = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            agentalloy_dir = config_dir / "agentalloy"
            agentalloy_dir.mkdir(parents=True)
            state_file = agentalloy_dir / "install-state.json"
            v3_state: dict[str, Any] = {
                "schema_version": 3,
                "install_started_at": "2025-01-01T00:00:00",
                "completed_steps": [],
                "harness_files_written": [],
                "models_pulled": [],
                "env_path": None,
                "port": 47950,
                "last_verify_passed_at": None,
                "pending_pack_selection": None,
                "deployment": "container",
                "compose_file": "/home/user/project/compose.yaml",
                "compose_binary": "podman compose",
                "compose_binary_path": "/usr/bin/podman",
            }
            state_file.write_text(json.dumps(v3_state))

            import importlib

            importlib.reload(install_state)
            st = install_state.load_state()

            # Schema version bumped (v3 → v5, hops both migrations)
            assert st["schema_version"] == 5
            # Compose fields removed
            assert "compose_file" not in st
            assert "compose_binary" not in st
            assert "compose_binary_path" not in st
            # Runtime fields added
            assert "runtime_binary" in st
            assert "image_tag" in st
            assert "container_name" in st
            assert "data_volume" in st
            # v5 bootstrap fields added
            assert st["bootstrap_started_at"] is None
            assert st["bootstrap_packs_ingested"] == []
            # deployment preserved
            assert st["deployment"] == "container"

        finally:
            if old_config is not None:
                os.environ["XDG_CONFIG_HOME"] = old_config
            elif "XDG_CONFIG_HOME" in os.environ:
                del os.environ["XDG_CONFIG_HOME"]
            if old_data is not None:
                os.environ["XDG_DATA_HOME"] = old_data
            elif "XDG_DATA_HOME" in os.environ:
                del os.environ["XDG_DATA_HOME"]

    def test_v3_preserves_existing_fields(self, tmp_path: Path):
        """Migration preserves existing v3 fields while adding v4 fields and removing compose fields."""
        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        old_config = os.environ.get("XDG_CONFIG_HOME")
        old_data = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            agentalloy_dir = config_dir / "agentalloy"
            agentalloy_dir.mkdir(parents=True)
            state_file = agentalloy_dir / "install-state.json"
            v3_state: dict[str, Any] = {
                "schema_version": 3,
                "install_started_at": "2025-01-01T00:00:00",
                "completed_steps": [
                    {"step": "wire-harness", "completed_at": "2025-01-01T00:01:00"}
                ],
                "harness_files_written": [],
                "models_pulled": ["ollama:nomic-embed-text"],
                "env_path": None,
                "port": 50000,
                "last_verify_passed_at": "2025-01-01T00:02:00",
                "pending_pack_selection": ["core"],
                "deployment": "container",
                "compose_file": "/home/user/compose.yaml",
                "compose_binary": "docker compose",
                "compose_binary_path": "/usr/bin/docker",
            }
            state_file.write_text(json.dumps(v3_state))

            import importlib

            importlib.reload(install_state)
            st = install_state.load_state()

            # Existing fields preserved
            assert st["port"] == 50000
            assert st["completed_steps"] == v3_state["completed_steps"]
            assert st["models_pulled"] == ["ollama:nomic-embed-text"]
            assert st["pending_pack_selection"] == ["core"]
            assert st["last_verify_passed_at"] == "2025-01-01T00:02:00"
            # deployment preserved
            assert st["deployment"] == "container"
            # Compose fields removed
            assert "compose_file" not in st
            assert "compose_binary" not in st
            assert "compose_binary_path" not in st
            # Runtime fields added
            assert "runtime_binary" in st
            assert "image_tag" in st
            assert "container_name" in st
            assert "data_volume" in st

        finally:
            if old_config is not None:
                os.environ["XDG_CONFIG_HOME"] = old_config
            elif "XDG_CONFIG_HOME" in os.environ:
                del os.environ["XDG_CONFIG_HOME"]
            if old_data is not None:
                os.environ["XDG_DATA_HOME"] = old_data
            elif "XDG_DATA_HOME" in os.environ:
                del os.environ["XDG_DATA_HOME"]

    def test_v3_migrate_direct(self):
        """Test _migrate() directly for v3 → v4 transition."""
        v3_state: dict[str, Any] = {
            "schema_version": 3,
            "deployment": "container",
            "compose_file": "/test/compose.yaml",
            "compose_binary": "podman compose",
            "compose_binary_path": "/usr/bin/podman",
            "install_started_at": "2025-01-01T00:00:00",
            "completed_steps": [],
            "harness_files_written": [],
            "models_pulled": [],
            "env_path": None,
            "port": 47950,
            "last_verify_passed_at": None,
            "pending_pack_selection": None,
        }
        result = install_state._migrate(v3_state, 3)
        assert result["schema_version"] == 5
        assert "compose_file" not in result
        assert "compose_binary" not in result
        assert "compose_binary_path" not in result
        assert "runtime_binary" in result
        assert "image_tag" in result
        assert "container_name" in result
        assert "data_volume" in result
        assert result["deployment"] == "container"
        # v5 bootstrap fields
        assert result["bootstrap_started_at"] is None
        assert result["bootstrap_packs_ingested"] == []

    def test_v4_state_migrated_to_v5(self, tmp_path: Path):
        """A v4 state file is migrated to v5 with bootstrap fields added."""
        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        old_config = os.environ.get("XDG_CONFIG_HOME")
        old_data = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            agentalloy_dir = config_dir / "agentalloy"
            agentalloy_dir.mkdir(parents=True)
            state_file = agentalloy_dir / "install-state.json"
            v4_state: dict[str, Any] = {
                "schema_version": 4,
                "install_started_at": "2025-01-01T00:00:00",
                "completed_steps": [],
                "harness_files_written": [],
                "models_pulled": [],
                "env_path": None,
                "port": 47950,
                "last_verify_passed_at": None,
                "pending_pack_selection": None,
                "deployment": "container",
                "runtime_binary": "podman",
                "image_tag": "agentalloy:latest",
                "container_name": "agentalloy",
                "data_volume": "agentalloy-data",
            }
            state_file.write_text(json.dumps(v4_state))

            import importlib

            importlib.reload(install_state)
            st = install_state.load_state()

            assert st["schema_version"] == 5
            assert st["runtime_binary"] == "podman"
            assert st["image_tag"] == "agentalloy:latest"
            assert st["container_name"] == "agentalloy"
            assert st["data_volume"] == "agentalloy-data"
            assert "compose_file" not in st
            assert "compose_binary" not in st
            assert "compose_binary_path" not in st
            # v5 bootstrap fields added with defaults
            assert st["bootstrap_started_at"] is None
            assert st["bootstrap_completed_at"] is None
            assert st["bootstrap_packs_ingested"] == []
            assert st["bootstrap_reembed_count"] == 0
            assert st["bootstrap_lock_file"] == "/app/.bootstrap-lock"
            assert st["bootstrap_checkpoints"] == []

        finally:
            if old_config is not None:
                os.environ["XDG_CONFIG_HOME"] = old_config
            elif "XDG_CONFIG_HOME" in os.environ:
                del os.environ["XDG_CONFIG_HOME"]
            if old_data is not None:
                os.environ["XDG_DATA_HOME"] = old_data
            elif "XDG_DATA_HOME" in os.environ:
                del os.environ["XDG_DATA_HOME"]


class TestV4ToV5Migration:
    """v4 → v5: add bootstrap_* fields, preserve everything else."""

    def test_v4_migrate_direct_adds_bootstrap_fields(self):
        v4_state: dict[str, Any] = {
            "schema_version": 4,
            "deployment": "container",
            "runtime_binary": "podman",
            "image_tag": "agentalloy:local",
            "container_name": "agentalloy",
            "data_volume": "agentalloy-data",
            "install_started_at": "2025-01-01T00:00:00",
            "completed_steps": [{"step": "wire-harness"}],
            "harness_files_written": [],
            "models_pulled": ["ollama:nomic"],
            "env_path": None,
            "port": 47950,
            "last_verify_passed_at": "2025-01-01T01:00:00",
            "pending_pack_selection": ["python"],
        }
        result = install_state._migrate(v4_state, 4)
        assert result["schema_version"] == 5
        # Preserved
        assert result["deployment"] == "container"
        assert result["runtime_binary"] == "podman"
        assert result["models_pulled"] == ["ollama:nomic"]
        assert result["completed_steps"] == [{"step": "wire-harness"}]
        assert result["pending_pack_selection"] == ["python"]
        assert result["last_verify_passed_at"] == "2025-01-01T01:00:00"
        # Added with defaults
        assert result["bootstrap_started_at"] is None
        assert result["bootstrap_completed_at"] is None
        assert result["bootstrap_packs_ingested"] == []
        assert result["bootstrap_reembed_count"] == 0
        assert result["bootstrap_lock_file"] == "/app/.bootstrap-lock"
        assert result["bootstrap_checkpoints"] == []

    def test_v5_state_passthrough(self):
        """A state file already at v5 is returned unchanged (no migration ran)."""
        v5_state: dict[str, Any] = {
            "schema_version": 5,
            "deployment": "container",
            "bootstrap_started_at": "2025-06-01T00:00:00Z",
            "bootstrap_completed_at": "2025-06-01T00:30:00Z",
            "bootstrap_packs_ingested": ["python", "nodejs"],
            "bootstrap_reembed_count": 2949,
            "bootstrap_lock_file": "/app/.bootstrap-lock",
            "bootstrap_checkpoints": [{"step": "pack_ingested", "pack": "python"}],
        }
        # _migrate is only called when from_version < current; calling with 5
        # would short-circuit all branches and only stamp schema_version.
        result = install_state._migrate(dict(v5_state), 5)
        assert result["schema_version"] == 5
        assert result["bootstrap_packs_ingested"] == ["python", "nodejs"]
        assert result["bootstrap_reembed_count"] == 2949
        assert result["bootstrap_checkpoints"] == [{"step": "pack_ingested", "pack": "python"}]
