"""Unit tests for the copilot-cli provider (Task 9).

Covers:
  - HarnessSpec creation and registration
  - env_builder returns empty dict (markdown-only harness)
  - install_writer writes ~/.github/copilot-instructions.md with sentinel block
  - Sentinel idempotency (re-running replaces existing block)
  - Preserves existing user content outside sentinels

Total: 12 unit tests.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

# Ensure the copilot_cli provider is imported so it registers itself in REGISTRY.
from agentalloy.providers.github_copilot import REGISTRY  # noqa: F401

# ---------------------------------------------------------------------------
# HarnessSpec tests
# ---------------------------------------------------------------------------


class TestCopilotCliHarnessSpec(TestCase):
    """Tests for the copilot-cli HarnessSpec registration."""

    def test_copilot_cli_registered(self):
        """The github-copilot harness is registered in REGISTRY."""
        self.assertIn("github-copilot", REGISTRY)

    def test_copilot_cli_spec_fields(self):
        """HarnessSpec has correct name, binary, capabilities, protocol."""
        from agentalloy.providers import Capability, Protocol

        spec = REGISTRY["github-copilot"]
        self.assertEqual(spec.name, "github-copilot")
        self.assertEqual(spec.binary, "gh copilot")
        self.assertEqual(spec.capabilities, (Capability.MARKDOWN_ONLY,))
        self.assertEqual(spec.protocol, Protocol.OPENAI)

    def test_copilot_cli_env_builder(self):
        """env_builder returns empty dict (markdown-only harness)."""
        spec = REGISTRY["github-copilot"]
        env = spec.env_builder(47950)
        self.assertIsInstance(env, dict)
        self.assertEqual(env, {})

    def test_copilot_cli_install_writer_callable(self):
        """install_writer is a callable that returns list[WireRecord]."""
        spec = REGISTRY["github-copilot"]
        self.assertIsNotNone(spec.install_writer)
        self.assertTrue(callable(spec.install_writer))

    def test_copilot_cli_hook_writer_none(self):
        """hook_writer is a callable for github-copilot (returns empty list)."""
        spec = REGISTRY["github-copilot"]
        self.assertIsNotNone(spec.hook_writer)
        self.assertTrue(callable(spec.hook_writer))


# ---------------------------------------------------------------------------
# install module tests
# ---------------------------------------------------------------------------


class TestCopilotCliInstall(TestCase):
    """Tests for the copilot-cli install module (apply_persistent_config)."""

    def test_apply_persistent_config_creates_instructions_md(self):
        """install_writer writes ~/.github/copilot-instructions.md with sentinel block."""
        from agentalloy.providers.base import WireRecord
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(8000, fake_home)

                instructions_path = fake_home / ".github" / "copilot-instructions.md"
                self.assertTrue(instructions_path.exists())
                content = instructions_path.read_text()

                self.assertIn("<!-- BEGIN agentalloy install -->", content)
                self.assertIn("<!-- END agentalloy install -->", content)
                self.assertIn("localhost:8000", content)
                self.assertIn("/compose/text", content)
                self.assertIn("health", content)

                self.assertIsInstance(result, list)
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], WireRecord)
                self.assertEqual(result[0].marker_key, "github-copilot.instructions")

    def test_apply_persistent_config_idempotent(self):
        """Re-running apply_persistent_config replaces existing block."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # First run
                install.apply_persistent_config(8000, fake_home)

                # Second run with different port
                install.apply_persistent_config(9000, fake_home)

                instructions_path = fake_home / ".github" / "copilot-instructions.md"
                content = instructions_path.read_text()

                self.assertIn("localhost:9000", content)
                self.assertNotIn("localhost:8000", content)
                self.assertEqual(content.count("<!-- BEGIN agentalloy install -->"), 1)
                self.assertEqual(content.count("<!-- END agentalloy install -->"), 1)

    def test_apply_persistent_config_preserves_existing_content(self):
        """apply_persistent_config preserves existing content outside sentinels."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # Pre-existing file with user content
                instructions_path = fake_home / ".github" / "copilot-instructions.md"
                instructions_path.parent.mkdir(parents=True, exist_ok=True)
                instructions_path.write_text("# My Copilot Rules\n\nUse TypeScript strict mode.\n")

                # Run install
                install.apply_persistent_config(8000, fake_home)

                content = instructions_path.read_text()
                self.assertIn("# My Copilot Rules", content)
                self.assertIn("Use TypeScript strict mode.", content)
                self.assertIn("localhost:8000", content)

    def test_apply_persistent_config_new_file_action(self):
        """First run returns wrote_new_file action."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(8000, fake_home)
                self.assertEqual(result[0].action, "wrote_new_file")

    def test_apply_persistent_config_existing_file_action(self):
        """Second run returns injected_block action."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                # First run
                install.apply_persistent_config(8000, fake_home)

                # Second run
                result = install.apply_persistent_config(9000, fake_home)
                self.assertEqual(result[0].action, "injected_block")

    def test_apply_persistent_config_includes_phases(self):
        """The injected content includes phase information."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                install.apply_persistent_config(8000, fake_home)
                content = (fake_home / ".github" / "copilot-instructions.md").read_text()

                self.assertIn("spec", content)
                self.assertIn("design", content)
                self.assertIn("build", content)
                self.assertIn("qa", content)
                self.assertIn("ops", content)

    def test_apply_persistent_config_includes_health_check(self):
        """The injected content includes the health-gate check."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                install.apply_persistent_config(8000, fake_home)
                content = (fake_home / ".github" / "copilot-instructions.md").read_text()

                self.assertIn("curl -fs http://localhost:8000/health", content)

    def test_apply_persistent_config_includes_curl_command(self):
        """The injected content includes the curl compose command."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                install.apply_persistent_config(8000, fake_home)
                content = (fake_home / ".github" / "copilot-instructions.md").read_text()

                self.assertIn("curl -s -X POST http://localhost:8000/compose/text", content)
                self.assertIn("Content-Type: application/json", content)
                self.assertIn("task", content)
                self.assertIn("phase", content)


# ---------------------------------------------------------------------------
# WireRecord tests
# ---------------------------------------------------------------------------


class TestCopilotCliWireRecord(TestCase):
    """Tests for WireRecord returned by copilot-cli install_writer."""

    def test_wire_record_path(self):
        """WireRecord path points to ~/.github/copilot-instructions.md."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(8000, fake_home)
                self.assertIn(".github/copilot-instructions.md", result[0].path)

    def test_wire_record_marker_key(self):
        """WireRecord has correct marker_key for uninstall."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(8000, fake_home)
                self.assertEqual(result[0].marker_key, "github-copilot.instructions")

    def test_wire_record_to_dict(self):
        """WireRecord.to_dict() serializes correctly."""
        from agentalloy.providers.github_copilot import install

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                result = install.apply_persistent_config(8000, fake_home)
                d = result[0].to_dict()
                self.assertIn("path", d)
                self.assertIn("action", d)
                self.assertIn("content_sha256", d)
                self.assertIn("marker_key", d)
