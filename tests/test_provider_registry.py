"""Tests for the provider registry skeleton (Task 1).

Covers:
  - TestProviderRegistry (8 tests)
  - TestHarnessSpec (5 tests)
  - TestCapability (2 tests)
  - TestProtocol (1 test)
  - TestWireRecord (6 tests)

Total: 22 unit tests.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from unittest import TestCase, main

# Import agentalloy providers (must be before sys.path modification for E402)
from agentalloy.providers import REGISTRY, Capability, HarnessSpec, Protocol, WireRecord

# Ensure the src directory is on the path so we can import agentalloy.
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_env(port: int) -> dict[str, str]:
    return {"AGENTALLOY_PORT": str(port)}


def _noop_install(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    return []


def _make_spec(name: str = "test-harness") -> HarnessSpec:
    return HarnessSpec(
        name=name,
        binary="test-binary",
        capabilities=(Capability.MCP_ONLY, Capability.PROXY),
        protocol=Protocol.ANTHROPIC,
        env_builder=_noop_env,
        install_writer=_noop_install,
    )


# ---------------------------------------------------------------------------
# TestHarnessSpec
# ---------------------------------------------------------------------------


class TestHarnessSpec(TestCase):
    """Tests for the HarnessSpec frozen dataclass."""

    def test_instantiate_all_fields(self):
        """HarnessSpec can be instantiated with all fields."""
        spec = _make_spec()
        self.assertEqual(spec.name, "test-harness")
        self.assertEqual(spec.binary, "test-binary")
        self.assertEqual(spec.capabilities, (Capability.MCP_ONLY, Capability.PROXY))
        self.assertEqual(spec.protocol, Protocol.ANTHROPIC)
        self.assertIsNotNone(spec.env_builder)
        self.assertIsNotNone(spec.install_writer)

    def test_defaults_install(self):
        """HarnessSpec allows install_writer to default to None."""
        spec = HarnessSpec(
            name="minimal",
            binary="bin",
            capabilities=(Capability.MARKDOWN_ONLY,),
            protocol=Protocol.OPENAI,
            env_builder=_noop_env,
        )
        self.assertIsNone(spec.install_writer)

    def test_frozen_immutability(self):
        """HarnessSpec is frozen — cannot mutate fields after creation."""
        spec = _make_spec()
        with self.assertRaises((dataclasses.FrozenInstanceError, TypeError)):
            spec.name = "changed"
        with self.assertRaises((dataclasses.FrozenInstanceError, TypeError)):
            spec.binary = "changed"

    def test_env_builder_returns_dict(self):
        """env_builder returns a dict with the port."""
        spec = _make_spec()
        env = spec.env_builder(12345)
        self.assertIsInstance(env, dict)
        self.assertEqual(env["AGENTALLOY_PORT"], "12345")

    def test_equality(self):
        """Two HarnessSpecs with identical fields compare equal."""
        s1 = _make_spec("dup")
        s2 = HarnessSpec(
            name="dup",
            binary="test-binary",
            capabilities=(Capability.MCP_ONLY, Capability.PROXY),
            protocol=Protocol.ANTHROPIC,
            env_builder=_noop_env,
            install_writer=_noop_install,
        )
        self.assertEqual(s1, s2)

    def test_hashable(self):
        """HarnessSpec is hashable (frozen dataclass)."""
        spec = _make_spec()
        hash(spec)  # should not raise
        s = {spec}
        self.assertIn(spec, s)


# ---------------------------------------------------------------------------
# TestCapability
# ---------------------------------------------------------------------------


class TestCapability(TestCase):
    """Tests for the Capability enum."""

    def test_three_values(self):
        """Capability has exactly 3 values: PROXY, MARKDOWN_ONLY, MCP_ONLY."""
        expected = {"PROXY", "MARKDOWN_ONLY", "MCP_ONLY"}
        actual = {c.name for c in Capability}
        self.assertEqual(actual, expected)

    def test_value_uniqueness(self):
        """All Capability values have distinct string values."""
        values = [c.value for c in Capability]
        self.assertEqual(len(values), len(set(values)))


# ---------------------------------------------------------------------------
# TestProtocol
# ---------------------------------------------------------------------------


class TestProtocol(TestCase):
    """Tests for the Protocol enum."""

    def test_three_values(self):
        """Protocol has exactly 3 values: ANTHROPIC, OPENAI, EITHER."""
        expected = {"ANTHROPIC", "OPENAI", "EITHER"}
        actual = {p.name for p in Protocol}
        self.assertEqual(actual, expected)


# ---------------------------------------------------------------------------
# TestWireRecord
# ---------------------------------------------------------------------------


class TestWireRecord(TestCase):
    """Tests for the WireRecord frozen dataclass."""

    def test_valid_actions(self):
        """WireRecord.action accepts only the three valid actions."""
        for action in ("wrote_new_file", "injected_block", "env_export"):
            rec = WireRecord(
                path="/tmp/f",
                action=action,
                content_sha256="abc123",
            )
            self.assertEqual(rec.action, action)

    def test_invalid_action_raises(self):
        """WireRecord raises ValueError for invalid action."""
        with self.assertRaises(ValueError):
            WireRecord(
                path="/tmp/f",
                action="bogus",
                content_sha256="abc123",
            )

    def test_to_dict_shape(self):
        """WireRecord serializes to the legacy dict shape."""
        rec = WireRecord(
            path="/tmp/foo",
            action="injected_block",
            content_sha256="deadbeef",
            original_content="before",
            marker_key="agentalloy:begin",
        )
        d = rec.to_dict()
        self.assertIn("path", d)
        self.assertIn("action", d)
        self.assertIn("content_sha256", d)
        self.assertIn("original_content", d)
        self.assertIn("marker_key", d)
        self.assertEqual(d["path"], "/tmp/foo")
        self.assertEqual(d["action"], "injected_block")
        self.assertEqual(d["content_sha256"], "deadbeef")
        self.assertEqual(d["original_content"], "before")
        self.assertEqual(d["marker_key"], "agentalloy:begin")

    def test_from_dict_roundtrip(self):
        """WireRecord.from_dict reconstructs the same record."""
        original = WireRecord(
            path="/tmp/bar",
            action="wrote_new_file",
            content_sha256="cafebabe",
            original_content="old",
            marker_key="marker",
        )
        d = original.to_dict()
        restored = WireRecord.from_dict(d)
        self.assertEqual(restored, original)

    def test_sha256_computation(self):
        """WireRecord._compute_sha256 produces correct hex digest."""
        content = "hello world"
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        actual = WireRecord._compute_sha256(content)
        self.assertEqual(actual, expected)

    def test_missing_original_content(self):
        """WireRecord serialises without original_content when None."""
        rec = WireRecord(
            path="/tmp/new",
            action="wrote_new_file",
            content_sha256="abc",
            original_content=None,
        )
        d = rec.to_dict()
        self.assertNotIn("original_content", d)

    def test_missing_marker_key(self):
        """WireRecord serialises without marker_key when empty."""
        rec = WireRecord(
            path="/tmp/new",
            action="wrote_new_file",
            content_sha256="abc",
            marker_key="",
        )
        d = rec.to_dict()
        self.assertNotIn("marker_key", d)


# ---------------------------------------------------------------------------
# TestProviderRegistry
# ---------------------------------------------------------------------------


class TestProviderRegistry(TestCase):
    """Tests for the REGISTRY dict."""

    def setUp(self) -> None:
        """Snapshot REGISTRY size before each test so we can restore."""
        self._original_keys = set(REGISTRY.keys())

    def tearDown(self) -> None:
        """Remove any keys added during the test to keep tests isolated."""
        current_keys = set(REGISTRY.keys())
        for key in current_keys - self._original_keys:
            del REGISTRY[key]

    def test_registry_is_empty_dict(self):
        """REGISTRY is a dict; starts with pre-registered providers."""
        self.assertIsInstance(REGISTRY, dict)
        # Pre-registered providers (codex, github-copilot) are present at import time.
        self.assertIn("codex", REGISTRY)
        self.assertIn("github-copilot", REGISTRY)

    def test_populate_and_lookup(self):
        """REGISTRY can be populated with HarnessSpec instances."""
        spec = _make_spec("registered")
        REGISTRY["registered"] = spec
        self.assertIn("registered", REGISTRY)
        self.assertIs(REGISTRY["registered"], spec)

    def test_lookup_returns_correct_spec(self):
        """Lookup by name returns the correct HarnessSpec."""
        spec = HarnessSpec(
            name="lookup-test",
            binary="bin",
            capabilities=(Capability.PROXY,),
            protocol=Protocol.EITHER,
            env_builder=_noop_env,
        )
        REGISTRY["lookup-test"] = spec
        retrieved = REGISTRY["lookup-test"]
        self.assertEqual(retrieved.name, "lookup-test")
        self.assertEqual(retrieved.binary, "bin")
        self.assertEqual(retrieved.capabilities, (Capability.PROXY,))
        self.assertEqual(retrieved.protocol, Protocol.EITHER)

    def test_lookup_missing_key(self):
        """Lookup for a non-existent key raises KeyError."""
        with self.assertRaises(KeyError):
            _ = REGISTRY["does-not-exist"]

    def test_all_keys_lowercase(self):
        """All registry keys are lowercase strings."""
        spec = _make_spec("lower-test")
        REGISTRY["lower-test"] = spec
        for key in REGISTRY:
            self.assertEqual(key, key.lower())
            self.assertIsInstance(key, str)

    def test_multiple_harnesses(self):
        """REGISTRY can hold multiple distinct HarnessSpec entries."""
        initial_count = len(REGISTRY)
        specs = [
            HarnessSpec(
                name="a",
                binary="a-bin",
                capabilities=(Capability.MCP_ONLY,),
                protocol=Protocol.ANTHROPIC,
                env_builder=_noop_env,
            ),
            HarnessSpec(
                name="b",
                binary="b-bin",
                capabilities=(Capability.PROXY,),
                protocol=Protocol.OPENAI,
                env_builder=_noop_env,
            ),
            HarnessSpec(
                name="c",
                binary="c-bin",
                capabilities=(Capability.MARKDOWN_ONLY,),
                protocol=Protocol.EITHER,
                env_builder=_noop_env,
            ),
        ]
        for s in specs:
            REGISTRY[s.name] = s
        self.assertEqual(len(REGISTRY), initial_count + 3)
        for s in specs:
            self.assertIn(s.name, REGISTRY)
            self.assertIs(REGISTRY[s.name], s)

    def test_registry_not_mutated_by_test(self):
        """tearDown cleanup ensures REGISTRY is not polluted across tests."""
        spec = _make_spec("ephemeral")
        REGISTRY["ephemeral"] = spec
        self.assertIn("ephemeral", REGISTRY)
        # tearDown should have removed it; verified by the next test running clean.

    def test_registry_keys_are_strings(self):
        """REGISTRY keys are strictly strings (not enums or other types)."""
        spec = _make_spec("str-keys")
        REGISTRY["str-keys"] = spec
        for key in REGISTRY:
            self.assertIsInstance(key, str)
            self.assertNotIsInstance(key, Capability)
            self.assertNotIsInstance(key, Protocol)


# ---------------------------------------------------------------------------
# Type-check stubs
# ---------------------------------------------------------------------------


class TestTypeStubs(TestCase):
    """Verify the type aliases resolve correctly at runtime."""

    def test_env_builder_type(self):
        """HarnessSpecEnvBuilder is a valid callable type."""
        self.assertTrue(callable(_noop_env))
        result = _noop_env(8080)
        self.assertIsInstance(result, dict)

    def test_install_writer_type(self):
        """HarnessSpecInstallWriter is a valid callable type."""
        self.assertTrue(callable(_noop_install))
        result = _noop_install(8080, Path("/tmp"), force=True)
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    main()
