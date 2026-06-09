"""Unit tests for the cline provider (Pattern B bug fix).

Covers:
  - HarnessSpec creation and registration
  - install_writer writes .cline/settings.json with proxy API fields
  - Preserves existing settings without overwriting
  - Malformed JSON does NOT overwrite the file with empty data
  - New file creation works correctly

Total: 10 unit tests.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, main

# Ensure the cline provider is imported so it registers itself in REGISTRY.
from agentalloy.providers.cline import REGISTRY  # noqa: F401


class TestClineHarnessSpec(TestCase):
    """Tests for the cline HarnessSpec registration."""

    def test_cline_registered(self):
        """The cline harness is registered in REGISTRY."""
        self.assertIn("cline", REGISTRY)

    def test_cline_spec_fields(self):
        """HarnessSpec has correct name, binary, capabilities, protocol."""
        from agentalloy.providers import Capability, Protocol

        spec = REGISTRY["cline"]
        self.assertEqual(spec.name, "cline")
        self.assertEqual(spec.binary, "cline")
        self.assertEqual(spec.capabilities, (Capability.PROXY,))
        self.assertEqual(spec.protocol, Protocol.OPENAI)

    def test_cline_env_builder(self):
        """env_builder returns empty dict (Cline uses file-based config)."""
        spec = REGISTRY["cline"]
        env = spec.env_builder(47950)
        self.assertEqual(env, {})

    def test_cline_install_writer_callable(self):
        """install_writer is a callable that returns list[WireRecord]."""
        spec = REGISTRY["cline"]
        self.assertIsNotNone(spec.install_writer)
        self.assertTrue(callable(spec.install_writer))

    def test_cline_hook_writer(self):
        """hook_writer is a callable for cline (returns empty list)."""
        spec = REGISTRY["cline"]
        self.assertIsNotNone(spec.hook_writer)
        self.assertTrue(callable(spec.hook_writer))


class TestClineInstall(TestCase):
    """Tests for the cline install module (apply_persistent_config)."""

    def test_apply_persistent_config_creates_settings(self):
        """install_writer creates .cline/settings.json with proxy API fields."""
        from agentalloy.providers.base import WireRecord
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            result = install.apply_persistent_config(7070, root)

            settings_path = root / ".cline" / "settings.json"
            self.assertTrue(settings_path.exists())
            config = json.loads(settings_path.read_text())

            self.assertEqual(config["apiProvider"], "openai")
            self.assertEqual(config["apiBaseUrl"], "http://localhost:7070/v1")
            self.assertEqual(config["apiKey"], "agentalloy")
            self.assertEqual(config["model"], "agentalloy-proxy")

            self.assertIsInstance(result, list)
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], WireRecord)
            self.assertEqual(result[0].marker_key, "cline.settings.proxy")

    def test_apply_persistent_config_preserves_existing_settings(self):
        """apply_persistent_config preserves existing settings without overwriting."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Pre-existing settings.json with user-defined settings
            settings_path = root / ".cline" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            existing = json.dumps(
                {
                    "modelId": "claude-3-sonnet",
                    "someOtherSetting": "keep this",
                }
            )
            settings_path.write_text(existing, encoding="utf-8")

            # Run install
            install.apply_persistent_config(7070, root)

            config = json.loads(settings_path.read_text())

            # Proxy fields added
            self.assertEqual(config["apiProvider"], "openai")
            self.assertEqual(config["apiBaseUrl"], "http://localhost:7070/v1")
            # Existing settings preserved
            self.assertEqual(config["modelId"], "claude-3-sonnet")
            self.assertEqual(config["someOtherSetting"], "keep this")

    def test_apply_persistent_config_new_file_action(self):
        """First run returns wrote_new_file action."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            result = install.apply_persistent_config(7070, root)
            self.assertEqual(result[0].action, "wrote_new_file")

    def test_apply_persistent_config_existing_file_action(self):
        """Second run returns injected_block action."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # First run
            install.apply_persistent_config(7070, root)

            # Second run
            result = install.apply_persistent_config(8080, root)
            self.assertEqual(result[0].action, "injected_block")

    def test_apply_persistent_config_creates_directory(self):
        """install_writer creates .cline directory if it doesn't exist."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            cline_dir = root / ".cline"
            self.assertFalse(cline_dir.exists())

            install.apply_persistent_config(7070, root)

            self.assertTrue(cline_dir.exists())
            self.assertTrue((cline_dir / "settings.json").exists())

    def test_apply_persistent_config_idempotent(self):
        """Re-running apply_persistent_config updates port."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # First run
            install.apply_persistent_config(7070, root)

            # Second run with different port
            install.apply_persistent_config(8080, root)

            config = json.loads((root / ".cline" / "settings.json").read_text())
            self.assertEqual(
                config["apiBaseUrl"],
                "http://localhost:8080/v1",
            )

    def test_apply_persistent_config_json_formatting(self):
        """The settings.json is properly formatted JSON with indentation."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            install.apply_persistent_config(7070, root)

            content = (root / ".cline" / "settings.json").read_text()

            # Should be valid JSON
            config = json.loads(content)
            self.assertIn("apiProvider", config)

            # Should be indented (2 spaces)
            self.assertIn("  ", content)

            # Should end with newline
            self.assertTrue(content.endswith("\n"))

    def test_apply_persistent_config_malformed_json_preserves_file(self):
        """Malformed JSON in settings.json does NOT overwrite with empty data.

        This is the Pattern B bug fix: previously, json.JSONDecodeError was
        caught and settings was set to {}, causing the entire file to be
        overwritten with empty settings. Now the function returns early
        (empty list) without writing anything, preserving the original file.
        """
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Pre-existing malformed settings.json
            settings_path = root / ".cline" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            original_content = "{ invalid json {{{"
            settings_path.write_text(original_content, encoding="utf-8")

            # Capture stderr to verify warning is printed
            import io
            import sys

            stderr_capture = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = stderr_capture

            try:
                result = install.apply_persistent_config(7070, root)
            finally:
                sys.stderr = old_stderr

            # Should return empty list (skip wiring)
            self.assertEqual(result, [])

            # File should still exist with original content (not overwritten)
            self.assertTrue(settings_path.exists())
            self.assertEqual(settings_path.read_text(), original_content)

            # Warning should be printed to stderr
            stderr_output = stderr_capture.getvalue()
            self.assertIn("WARNING", stderr_output)
            self.assertIn("not valid JSON", stderr_output)

    def test_apply_persistent_config_malformed_json_no_data_loss(self):
        """Malformed JSON content is preserved — no data loss.

        Even if the file contains valid-but-incomplete JSON that would
        normally be lost, the fix ensures the file is left untouched.
        """
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Pre-existing settings with some content that's not valid JSON
            settings_path = root / ".cline" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            original_content = '{"modelId": "claude-3-sonnet", "broken":'
            settings_path.write_text(original_content, encoding="utf-8")

            result = install.apply_persistent_config(7070, root)

            # Should return empty list
            self.assertEqual(result, [])

            # File should be unchanged
            self.assertEqual(settings_path.read_text(), original_content)


class TestClineWireRecord(TestCase):
    """Tests for WireRecord returned by cline install_writer."""

    def test_wire_record_path(self):
        """WireRecord path points to .cline/settings.json."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = install.apply_persistent_config(7070, root)
            self.assertIn(".cline/settings.json", result[0].path)

    def test_wire_record_marker_key(self):
        """WireRecord has correct marker_key for uninstall."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = install.apply_persistent_config(7070, root)
            self.assertEqual(result[0].marker_key, "cline.settings.proxy")

    def test_wire_record_to_dict(self):
        """WireRecord.to_dict() serializes correctly."""
        from agentalloy.providers.cline import install

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = install.apply_persistent_config(7070, root)
            d = result[0].to_dict()
            self.assertIn("path", d)
            self.assertIn("action", d)
            self.assertIn("content_sha256", d)
            self.assertIn("marker_key", d)


if __name__ == "__main__":
    main()
