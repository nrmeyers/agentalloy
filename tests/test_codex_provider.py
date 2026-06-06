"""Unit tests for the codex provider (Task 8).

Covers:
  - HarnessSpec creation and registration
  - env_builder returns correct OPENAI_BASE_URL and OPENAI_API_KEY
  - install_writer creates ~/.codex/config.toml with apiBaseUrl sentinel
  - wire_harness integration for codex
  - Sentinel idempotency (re-running replaces existing block)

Total: 12 unit tests.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

# Ensure the codex provider is imported so it registers itself in REGISTRY.
from agentalloy.providers.codex import REGISTRY  # noqa: F401

# ---------------------------------------------------------------------------
# HarnessSpec tests
# ---------------------------------------------------------------------------


class TestCodexHarnessSpec(TestCase):
    """Tests for the codex HarnessSpec registration."""

    def test_codex_registered(self):
        """The codex harness is registered in REGISTRY."""
        self.assertIn("codex", REGISTRY)

    def test_codex_spec_fields(self):
        """HarnessSpec has correct name, binary, capabilities, protocol."""
        from agentalloy.providers import Capability, Protocol

        spec = REGISTRY["codex"]
        self.assertEqual(spec.name, "codex")
        self.assertEqual(spec.binary, "codex")
        self.assertEqual(spec.capabilities, (Capability.PROXY,))
        self.assertEqual(spec.protocol, Protocol.OPENAI)

    def test_codex_env_builder(self):
        """env_builder returns OPENAI_BASE_URL and OPENAI_API_KEY."""
        spec = REGISTRY["codex"]
        env = spec.env_builder(47950)
        self.assertIsInstance(env, dict)
        self.assertEqual(env["OPENAI_BASE_URL"], "http://localhost:47950/v1")
        self.assertEqual(env["OPENAI_API_KEY"], "agentalloy")

    def test_codex_install_writer_callable(self):
        """install_writer is a callable that returns list[WireRecord]."""
        spec = REGISTRY["codex"]
        self.assertIsNotNone(spec.install_writer)
        self.assertTrue(callable(spec.install_writer))


# ---------------------------------------------------------------------------
# install module tests
# ---------------------------------------------------------------------------


class TestCodexInstall(TestCase):
    """Tests for the codex install module (apply_persistent_config)."""

    def test_apply_persistent_config_creates_config_toml(self):
        """install_writer creates ~/.codex/config.toml with apiBaseUrl."""
        from agentalloy.providers.base import WireRecord
        from agentalloy.providers.codex import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(7070, fake_home)

                config_path = fake_home / ".codex" / "config.toml"
                self.assertTrue(config_path.exists())
                content = config_path.read_text()

                self.assertIn("# <!-- BEGIN agentalloy install -->", content)
                self.assertIn("# <!-- END agentalloy install -->", content)
                self.assertIn("[codex]", content)
                self.assertIn('apiBaseUrl = "http://localhost:7070/v1"', content)
                self.assertIn('apiKey = "agentalloy"', content)

                self.assertIsInstance(result, list)
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], WireRecord)
                self.assertEqual(result[0].marker_key, "codex.apiBaseUrl")

    def test_apply_persistent_config_idempotent(self):
        """Re-running apply_persistent_config replaces existing block."""
        from agentalloy.providers.codex import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # First run
                install.apply_persistent_config(7070, fake_home)

                # Second run with different port
                install.apply_persistent_config(8080, fake_home)

                config_path = fake_home / ".codex" / "config.toml"
                content = config_path.read_text()

                self.assertIn("localhost:8080", content)
                self.assertNotIn("localhost:7070", content)
                self.assertEqual(content.count("# <!-- BEGIN agentalloy install -->"), 1)
                self.assertEqual(content.count("# <!-- END agentalloy install -->"), 1)

    def test_apply_persistent_config_preserves_existing_content(self):
        """apply_persistent_config preserves existing content outside sentinels."""
        from agentalloy.providers.codex import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # Pre-existing config (create the .codex directory first)
                config_path = fake_home / ".codex" / "config.toml"
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text("# Existing codex config\nsome_key = true\n")

                # Run install
                install.apply_persistent_config(7070, fake_home)

                content = config_path.read_text()
                self.assertIn("# Existing codex config", content)
                self.assertIn("some_key = true", content)
                self.assertIn("apiBaseUrl", content)

    def test_apply_persistent_config_new_file_action(self):
        """First run returns wrote_new_file action."""
        from agentalloy.providers.codex import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(7070, fake_home)
                self.assertEqual(result[0].action, "wrote_new_file")

    def test_apply_persistent_config_existing_file_action(self):
        """Second run returns injected_block action."""
        from agentalloy.providers.codex import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # First run
                install.apply_persistent_config(7070, fake_home)

                # Second run
                result = install.apply_persistent_config(8080, fake_home)
                self.assertEqual(result[0].action, "injected_block")


# ---------------------------------------------------------------------------
# wire_harness integration tests
# ---------------------------------------------------------------------------


class TestCodexWireHarness(TestCase):
    """Tests for wire_harness integration with codex."""

    def test_wire_harness_codex_creates_config(self):
        """wire_compat('codex') creates ~/.codex/config.toml."""
        from tests._wire_compat import wire_compat

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = wire_compat("codex", port=7070, root=fake_home)

                config_path = fake_home / ".codex" / "config.toml"
                self.assertTrue(config_path.exists())
                self.assertEqual(result["integration_vector"], "proxy")
                self.assertEqual(result["harness"], "codex")

    def test_wire_harness_codex_has_api_base_url(self):
        """The config.toml contains the correct apiBaseUrl."""
        from tests._wire_compat import wire_compat

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                wire_compat("codex", port=9999, root=fake_home)

                config_path = fake_home / ".codex" / "config.toml"
                content = config_path.read_text()
                self.assertIn('apiBaseUrl = "http://localhost:9999/v1"', content)

    def test_wire_harness_codex_unknown_harness(self):
        """wire_harness rejects unknown harness names."""
        from tests._wire_compat import wire_compat

        with self.assertRaises(SystemExit):
            wire_compat("nonexistent-harness", port=7070, root=Path("/tmp"))

    def test_wire_harness_codex_idempotent(self):
        """Re-running wire_harness for codex replaces existing block."""
        from tests._wire_compat import wire_compat

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                wire_compat("codex", port=7070, root=fake_home)
                wire_compat("codex", port=8080, root=fake_home)

                config_path = fake_home / ".codex" / "config.toml"
                content = config_path.read_text()
                self.assertIn("localhost:8080", content)
                self.assertNotIn("localhost:7070", content)

    def test_wire_harness_codex_validates_harness(self):
        """codex is a valid harness name."""
        from agentalloy.install.subcommands.wire_harness import VALID_HARNESSES

        self.assertIn("codex", VALID_HARNESSES)

    def test_wire_harness_codex_sentinel_markers(self):
        """The config file uses sentinel markers for uninstall."""
        from tests._wire_compat import wire_compat

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                wire_compat("codex", port=7070, root=fake_home)

                config_path = fake_home / ".codex" / "config.toml"
                content = config_path.read_text()
                self.assertIn("# <!-- BEGIN agentalloy install -->", content)
                self.assertIn("# <!-- END agentalloy install -->", content)
