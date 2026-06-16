"""Unit tests for the openclaw provider (Task 10).

Covers:
  - HarnessSpec creation and registration
  - env_builder returns correct OPENAI_BASE_URL and OPENAI_API_KEY
  - install_writer writes ~/.openclaw/plugins.json with agentalloy plugin entry
  - Sentinel idempotency (re-running replaces existing block)
  - Preserves existing content outside agentalloy plugin

Total: 12 unit tests.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

# Ensure the openclaw provider is imported so it registers itself in REGISTRY.
from agentalloy.providers.openclaw import REGISTRY  # noqa: F401

# ---------------------------------------------------------------------------
# HarnessSpec tests
# ---------------------------------------------------------------------------


class TestOpenclawHarnessSpec(TestCase):
    """Tests for the openclaw HarnessSpec registration."""

    def test_openclaw_registered(self):
        """The openclaw harness is registered in REGISTRY."""
        self.assertIn("openclaw", REGISTRY)

    def test_openclaw_spec_fields(self):
        """HarnessSpec has correct name, binary, capabilities, protocol."""
        from agentalloy.providers import Capability, Protocol

        spec = REGISTRY["openclaw"]
        self.assertEqual(spec.name, "openclaw")
        self.assertEqual(spec.binary, "openclaw")
        self.assertEqual(spec.capabilities, (Capability.PROXY,))
        self.assertEqual(spec.protocol, Protocol.OPENAI)

    def test_openclaw_env_builder(self):
        """env_builder returns OPENAI_BASE_URL and OPENAI_API_KEY."""
        spec = REGISTRY["openclaw"]
        env = spec.env_builder(47950)
        self.assertIsInstance(env, dict)
        self.assertEqual(env["OPENAI_BASE_URL"], "http://localhost:47950/v1")
        self.assertEqual(env["OPENAI_API_KEY"], "agentalloy")

    def test_openclaw_install_writer_callable(self):
        """install_writer is a callable that returns list[WireRecord]."""
        spec = REGISTRY["openclaw"]
        self.assertIsNotNone(spec.install_writer)
        self.assertTrue(callable(spec.install_writer))

    def test_openclaw_hook_writer_none(self):
        """hook_writer is None for openclaw (no hook-based wiring)."""
        spec = REGISTRY["openclaw"]
        self.assertIsNone(spec.hook_writer)


# ---------------------------------------------------------------------------
# install module tests
# ---------------------------------------------------------------------------


class TestOpenclawInstall(TestCase):
    """Tests for the openclaw install module (apply_persistent_config)."""

    def test_apply_persistent_config_creates_plugins_json(self):
        """install_writer creates ~/.openclaw/plugins.json with agentalloy plugin."""
        from agentalloy.providers.base import WireRecord
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(7070, fake_home)

                config_path = fake_home / ".openclaw" / "plugins.json"
                self.assertTrue(config_path.exists())
                plugins = json.loads(config_path.read_text())

                self.assertIn("plugins", plugins)
                self.assertIn("agentalloy", plugins["plugins"])
                agentalloy_plugin = plugins["plugins"]["agentalloy"]
                self.assertEqual(agentalloy_plugin["enabled"], True)
                self.assertEqual(agentalloy_plugin["type"], "proxy")
                self.assertEqual(agentalloy_plugin["baseUrl"], "http://localhost:7070/v1")
                self.assertEqual(agentalloy_plugin["apiKey"], "agentalloy")

                self.assertIsInstance(result, list)
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], WireRecord)
                self.assertEqual(result[0].marker_key, "openclaw.plugins.agentalloy")

    def test_apply_persistent_config_idempotent(self):
        """Re-running apply_persistent_config updates port."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # First run
                install.apply_persistent_config(7070, fake_home)

                # Second run with different port
                install.apply_persistent_config(8080, fake_home)

                config_path = fake_home / ".openclaw" / "plugins.json"
                plugins = json.loads(config_path.read_text())

                self.assertEqual(
                    plugins["plugins"]["agentalloy"]["baseUrl"],
                    "http://localhost:8080/v1",
                )
                self.assertNotIn("7070", config_path.read_text())

    def test_apply_persistent_config_preserves_existing_plugins(self):
        """apply_persistent_config preserves existing plugins in the file."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # Pre-existing plugins.json with another plugin
                config_path = fake_home / ".openclaw" / "plugins.json"
                config_path.parent.mkdir(parents=True, exist_ok=True)
                existing = json.dumps(
                    {
                        "plugins": {
                            "other-plugin": {
                                "enabled": True,
                                "type": "local",
                                "path": "/usr/local/bin/other-plugin",
                            }
                        }
                    }
                )
                config_path.write_text(existing, encoding="utf-8")

                # Run install
                install.apply_persistent_config(7070, fake_home)

                plugins = json.loads(config_path.read_text())

                # Other plugin should still be there
                self.assertIn("other-plugin", plugins["plugins"])
                self.assertEqual(plugins["plugins"]["other-plugin"]["type"], "local")
                # Agentalloy plugin should be added
                self.assertIn("agentalloy", plugins["plugins"])
                self.assertEqual(
                    plugins["plugins"]["agentalloy"]["baseUrl"],
                    "http://localhost:7070/v1",
                )

    def test_apply_persistent_config_rejects_corrupt_json(self):
        """apply_persistent_config fails loud on corrupt JSON rather than clobbering it."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # Pre-existing corrupt file
                config_path = fake_home / ".openclaw" / "plugins.json"
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text("not valid json{{{", encoding="utf-8")

                # Must refuse rather than silently overwrite the user's config.
                with self.assertRaises(SystemExit):
                    install.apply_persistent_config(7070, fake_home)

                # The corrupt file is preserved untouched, not clobbered with {}.
                self.assertEqual(config_path.read_text(encoding="utf-8"), "not valid json{{{")

    def test_apply_persistent_config_new_file_action(self):
        """First run returns wrote_new_file action."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(7070, fake_home)
                self.assertEqual(result[0].action, "wrote_new_file")

    def test_apply_persistent_config_existing_file_action(self):
        """Second run returns injected_block action."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # First run
                install.apply_persistent_config(7070, fake_home)

                # Second run
                result = install.apply_persistent_config(8080, fake_home)
                self.assertEqual(result[0].action, "injected_block")

    def test_apply_persistent_config_creates_directory(self):
        """install_writer creates ~/.openclaw directory if it doesn't exist."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # Ensure .openclaw doesn't exist
                openclaw_dir = fake_home / ".openclaw"
                self.assertFalse(openclaw_dir.exists())

                install.apply_persistent_config(7070, fake_home)

                self.assertTrue(openclaw_dir.exists())
                self.assertTrue((openclaw_dir / "plugins.json").exists())

    def test_apply_persistent_config_json_formatting(self):
        """The plugins.json is properly formatted JSON with indentation."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                install.apply_persistent_config(7070, fake_home)

                config_path = fake_home / ".openclaw" / "plugins.json"
                content = config_path.read_text()

                # Should be valid JSON
                plugins = json.loads(content)
                self.assertIn("plugins", plugins)

                # Should be indented (2 spaces)
                self.assertIn("  ", content)

                # Should end with newline
                self.assertTrue(content.endswith("\n"))


# ---------------------------------------------------------------------------
# WireRecord tests
# ---------------------------------------------------------------------------


class TestOpenclawWireRecord(TestCase):
    """Tests for WireRecord returned by openclaw install_writer."""

    def test_wire_record_path(self):
        """WireRecord path points to ~/.openclaw/plugins.json."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(7070, fake_home)
                self.assertIn(".openclaw/plugins.json", result[0].path)

    def test_wire_record_marker_key(self):
        """WireRecord has correct marker_key for uninstall."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(7070, fake_home)
                self.assertEqual(result[0].marker_key, "openclaw.plugins.agentalloy")

    def test_wire_record_to_dict(self):
        """WireRecord.to_dict() serializes correctly."""
        from agentalloy.providers.openclaw import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(7070, fake_home)
                d = result[0].to_dict()
                self.assertIn("path", d)
                self.assertIn("action", d)
                self.assertIn("content_sha256", d)
                self.assertIn("marker_key", d)


if __name__ == "__main__":
    main()
