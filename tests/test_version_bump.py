"""Unit tests for the automatic version-bump logic (scripts/version_bump.py).

Pure-function coverage only — the git/pyproject I/O in ``main`` is exercised by
the live end-to-end path (a throwaway PR), not here. Mirrors the tested-logic
ethos of ``test_pack_version_bump_guard.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package; put it on the path so version_bump imports cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import version_bump as vb  # noqa: E402


class TestTierFromTitle:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("feat: add responses surface", "minor"),
            ("feat(code): index worktrees", "minor"),
            ("feat!: drop the hook transport", "major"),
            ("feat(api)!: rename /compose", "major"),
            ("fix(install): correct --from ordering", "patch"),
            ("perf: cache embeddings", "patch"),
            ("refactor: extract retrieve pipeline", "patch"),
            ("chore: bump deps", "patch"),
            ("docs: clarify RELEASE.md", "patch"),
            ("not a conventional title at all", "patch"),
            ("fix: something\n\nBREAKING CHANGE: config moved", "major"),
        ],
    )
    def test_mapping(self, title: str, expected: str) -> None:
        assert vb.tier_from_title(title) == expected

    def test_type_is_case_insensitive_for_feat(self) -> None:
        assert vb.tier_from_title("FEAT: shout") == "minor"


class TestTouchesShippedSurface:
    @pytest.mark.parametrize(
        "changed",
        [
            ["src/agentalloy/api/router.py"],
            ["src/agentalloy/_packs/fastapi/pack.yaml"],
            ["frontend/src/App.tsx"],
            ["container/entrypoint.sh"],
            ["Containerfile"],
            ["Containerfile.dev"],
            ["pyproject.toml"],
            ["uv.lock"],
            ["docs/x.md", "src/agentalloy/z.py"],  # mixed → still shipped
        ],
    )
    def test_shipped(self, changed: list[str]) -> None:
        assert vb.touches_shipped_surface(changed) is True

    @pytest.mark.parametrize(
        "changed",
        [
            ["docs/proxy-architecture.md"],
            [".github/workflows/ci.yml"],
            ["tests/test_compose_handler.py"],
            ["RELEASE.md", "README.md"],
            ["scripts/generate-skill-edges.py"],
            [],
            [""],
        ],
    )
    def test_not_shipped(self, changed: list[str]) -> None:
        assert vb.touches_shipped_surface(changed) is False


class TestBump:
    @pytest.mark.parametrize(
        ("version", "tier", "expected"),
        [
            ("7.0.7", "patch", "7.0.8"),
            ("7.0.7", "minor", "7.1.0"),
            ("7.0.7", "major", "8.0.0"),
            ("7.2.5", "minor", "7.3.0"),
            ("0.9.9", "major", "1.0.0"),
        ],
    )
    def test_semver_resets(self, version: str, tier: str, expected: str) -> None:
        assert vb.bump(version, tier) == expected  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["7.0", "7.0.0.1", "v7.0.0", "7.0.x"])
    def test_rejects_non_xyz(self, bad: str) -> None:
        with pytest.raises(ValueError):
            vb.bump(bad, "patch")


class TestDecide:
    def test_shipped_feat_minor(self) -> None:
        assert vb.decide("feat: x", ["src/a.py"], "7.0.7") == "7.1.0"

    def test_shipped_breaking_major(self) -> None:
        assert vb.decide("feat!: x", ["src/a.py"], "7.0.7") == "8.0.0"

    def test_shipped_fix_patch(self) -> None:
        assert vb.decide("fix: x", ["src/a.py"], "7.0.7") == "7.0.8"

    def test_shipped_nonconventional_defaults_patch(self) -> None:
        assert vb.decide("just some words", ["frontend/a.tsx"], "7.0.7") == "7.0.8"

    def test_docs_only_no_bump(self) -> None:
        assert vb.decide("feat: x", ["docs/a.md"], "7.0.7") is None

    def test_ci_only_no_bump(self) -> None:
        assert vb.decide("fix: x", [".github/workflows/ci.yml"], "7.0.7") is None
