# ruff: noqa: I001 -- testing private module members intentionally
"""Tests for state schema v3 -> v4 migration (UT-20).

UT-20: _migrate() removes compose fields and adds runtime fields
       when migrating from schema v3.
"""

from __future__ import annotations

from typing import Any

from agentalloy.install import state as install_state

# Local aliases for private helpers
_empty_state = install_state._empty_state  # pyright: ignore[reportPrivateUsage]


class TestUT20MigrateV3ToV4:
    """UT-20: _migrate() removes compose fields and adds runtime fields when migrating from schema v3."""

    def test_migrate_v3_to_v4(self):
        """v3 state -> v4 state transformation correct.

        - compose_file, compose_binary, compose_binary_path removed
        - runtime_binary, image_tag, container_name, data_volume added
        - schema_version bumped to 4
        - existing fields preserved
        """
        v3_state: dict[str, Any] = {
            "schema_version": 3,
            "deployment": "container",
            "compose_file": "/test/compose.yaml",
            "compose_binary": "podman compose",
            "compose_binary_path": "/usr/bin/podman",
            "install_started_at": "2025-01-01T00:00:00",
            "completed_steps": [{"step": "wire-harness", "completed_at": "2025-01-01T00:01:00"}],
            "harness_files_written": [],
            "models_pulled": ["ollama:nomic-embed-text"],
            "env_path": None,
            "port": 47950,
            "last_verify_passed_at": None,
            "pending_pack_selection": None,
        }
        result = install_state._migrate(v3_state, 3)

        # Schema version bumped to current (v3 → v4 → v5 hops).
        assert result["schema_version"] == install_state.CURRENT_SCHEMA_VERSION

        # Compose fields removed
        assert "compose_file" not in result
        assert "compose_binary" not in result
        assert "compose_binary_path" not in result

        # Runtime fields added
        assert "runtime_binary" in result
        assert "image_tag" in result
        assert "container_name" in result
        assert "data_volume" in result

        # Existing fields preserved
        assert result["deployment"] == "container"
        assert result["completed_steps"] == v3_state["completed_steps"]
        assert result["models_pulled"] == ["ollama:nomic-embed-text"]
        assert result["port"] == 47950
