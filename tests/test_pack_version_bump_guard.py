"""CI guard: pack content edits must bump the pack's version field.

This test is a no-op when PACK_GUARD_BASE_REF is unset (local runs without a
base ref must not fail).  In CI it is driven by
``PACK_GUARD_BASE_REF=${{ github.event.pull_request.base.sha }}``.

Propagation is version-gated BY DESIGN to preserve the SkillVersion rollback
chain (see PR #99/#104).  Editing pack files without a version bump means the
change silently never reaches installs.
"""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

# ---------------------------------------------------------------------------
# Core logic — pure, no git I/O; tested directly below
# ---------------------------------------------------------------------------

_PACKS_PREFIX = "src/agentalloy/_packs/"


class PackFailure(NamedTuple):
    pack: str
    changed_files: list[str]
    version: str


def check_pack_version_bumps(
    changed_files: list[str],
    version_at_head: Callable[[str], str | None],
    version_at_base: Callable[[str], str | None],
) -> list[PackFailure]:
    """Return one PackFailure per pack whose content changed without a version bump.

    Args:
        changed_files: paths relative to repo root (output of ``git diff --name-only``).
        version_at_head: callable(pack_name) -> version string, or None if the pack
            does not exist at HEAD (deleted packs are skipped).
        version_at_base: callable(pack_name) -> version string, or None if the pack
            did not exist at base ref (new packs are skipped).
    """
    by_pack: dict[str, list[str]] = defaultdict(list)
    for path in changed_files:
        if not path.startswith(_PACKS_PREFIX):
            continue
        remainder = path[len(_PACKS_PREFIX) :]
        pack_name = remainder.split("/")[0]
        if pack_name:
            by_pack[pack_name].append(path)

    failures: list[PackFailure] = []
    for pack, files in sorted(by_pack.items()):
        head_ver = version_at_head(pack)
        if head_ver is None:
            # Pack deleted at HEAD — skip
            continue
        base_ver = version_at_base(pack)
        if base_ver is None:
            # New pack — skip
            continue
        if head_ver == base_ver:
            failures.append(PackFailure(pack=pack, changed_files=files, version=head_ver))
    return failures


def _format_failures(failures: list[PackFailure]) -> str:
    lines: list[str] = [
        "Pack content changed without a version bump.  "
        "Propagation is version-gated to preserve the SkillVersion rollback chain "
        "(see PR #99/#104).  For each pack below, edit its pack.yaml and bump `version`.",
        "",
    ]
    for f in failures:
        shown = f.changed_files[:10]
        more = len(f.changed_files) - len(shown)
        files_str = "\n    ".join(shown)
        if more:
            files_str += f"\n    … and {more} more"
        lines.append(
            f"  Pack '{f.pack}': pack.yaml version is still {f.version!r} — bump it.\n"
            f"  Changed files:\n    {files_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_diff_names(base_ref: str, repo_root: Path) -> list[str]:
    """Return changed file paths between base_ref and HEAD under _packs/."""
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            f"{base_ref}...HEAD",
            "--",
            _PACKS_PREFIX,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=repo_root,
    )
    if result.returncode != 0:
        pytest.skip(f"git diff failed (shallow clone or bad ref?): {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_show_version(ref: str, pack: str, repo_root: Path) -> str | None:
    """Return the version field from a pack's pack.yaml at a given git ref.

    Returns None when the file does not exist at that ref.
    """
    pack_yaml_path = f"{_PACKS_PREFIX}{pack}/pack.yaml"
    result = subprocess.run(
        ["git", "show", f"{ref}:{pack_yaml_path}"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=repo_root,
    )
    if result.returncode != 0:
        # File doesn't exist at this ref — pack is new or deleted
        return None
    data: dict[str, object] = yaml.safe_load(result.stdout)
    return str(data["version"])


# ---------------------------------------------------------------------------
# The guard test
# ---------------------------------------------------------------------------


def test_pack_version_bump_guard() -> None:
    """Fail if any pack's content changed but its version was not bumped."""
    base_ref = os.environ.get("PACK_GUARD_BASE_REF", "").strip()
    if not base_ref:
        pytest.skip("PACK_GUARD_BASE_REF not set — skipping pack version bump guard")

    repo_root = Path(__file__).parent.parent

    changed_files = _git_diff_names(base_ref, repo_root)
    if not changed_files:
        return  # nothing under _packs/ changed

    def head_ver(pack: str) -> str | None:
        return _git_show_version("HEAD", pack, repo_root)

    def base_ver(pack: str) -> str | None:
        return _git_show_version(base_ref, pack, repo_root)

    failures = check_pack_version_bumps(changed_files, head_ver, base_ver)
    if failures:
        pytest.fail(_format_failures(failures))


# ---------------------------------------------------------------------------
# Unit tests for the core logic (no git I/O, always run)
# ---------------------------------------------------------------------------


def _make_versions(
    **packs: tuple[str, str],
) -> tuple[Callable[[str], str | None], Callable[[str], str | None]]:
    """Build (head_ver_fn, base_ver_fn) from keyword args: pack=(head, base)."""

    def head_ver(pack: str) -> str | None:
        return packs[pack][0] if pack in packs else None

    def base_ver(pack: str) -> str | None:
        return packs[pack][1] if pack in packs else None

    return head_ver, base_ver


def test_unit_same_version_returns_failure() -> None:
    head_ver, base_ver = _make_versions(mypack=("1.0.0", "1.0.0"))
    changed = [f"{_PACKS_PREFIX}mypack/skills/foo.yaml"]
    failures = check_pack_version_bumps(changed, head_ver, base_ver)
    assert len(failures) == 1
    assert failures[0].pack == "mypack"
    msg = _format_failures(failures)
    assert "mypack" in msg
    assert "1.0.0" in msg
    assert "SkillVersion rollback chain" in msg
    assert f"{_PACKS_PREFIX}mypack/skills/foo.yaml" in msg


def test_unit_bumped_version_passes() -> None:
    head_ver, base_ver = _make_versions(mypack=("1.0.1", "1.0.0"))
    changed = [f"{_PACKS_PREFIX}mypack/skills/foo.yaml"]
    failures = check_pack_version_bumps(changed, head_ver, base_ver)
    assert failures == []


def test_unit_new_pack_passes() -> None:
    """Pack that did not exist at base should not be flagged."""
    # head has a version, base returns None (new pack)
    head_ver, base_ver = _make_versions(newpack=("1.0.0", ""))

    # Override base to return None
    def base_none(pack: str) -> str | None:
        return None

    changed = [f"{_PACKS_PREFIX}newpack/skills/foo.yaml"]
    failures = check_pack_version_bumps(changed, head_ver, base_none)
    assert failures == []


def test_unit_deleted_pack_passes() -> None:
    """Pack that no longer exists at HEAD should not be flagged."""

    def head_none(pack: str) -> str | None:
        return None

    def base_ver(pack: str) -> str | None:
        return "1.0.0"

    changed = [f"{_PACKS_PREFIX}oldpack/skills/foo.yaml"]
    failures = check_pack_version_bumps(changed, head_none, base_ver)
    assert failures == []


def test_unit_non_pack_changes_only_passes() -> None:
    """Changes outside _packs/ must never trigger the guard."""
    head_ver, base_ver = _make_versions()
    changed = [
        "src/agentalloy/api/compose_router.py",
        "tests/test_something.py",
        "pyproject.toml",
    ]
    failures = check_pack_version_bumps(changed, head_ver, base_ver)
    assert failures == []


def test_unit_multiple_packs_only_some_bumped() -> None:
    """Only the un-bumped pack appears in failures."""
    head_ver, base_ver = _make_versions(
        alpha=("2.0.0", "1.0.0"),  # bumped — OK
        beta=("3.1.0", "3.1.0"),  # NOT bumped — should fail
    )
    changed = [
        f"{_PACKS_PREFIX}alpha/skills/x.yaml",
        f"{_PACKS_PREFIX}beta/skills/y.yaml",
    ]
    failures = check_pack_version_bumps(changed, head_ver, base_ver)
    assert len(failures) == 1
    assert failures[0].pack == "beta"


def test_unit_skip_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When PACK_GUARD_BASE_REF is empty/unset the test must skip, not fail."""
    monkeypatch.delenv("PACK_GUARD_BASE_REF", raising=False)
    with pytest.raises(pytest.skip.Exception):  # type: ignore[attr-defined]
        test_pack_version_bump_guard()


def test_unit_changed_files_capped_at_ten_in_message() -> None:
    """Failure message shows at most 10 files, then '… and N more'."""
    head_ver, base_ver = _make_versions(bigpack=("9.0.0", "9.0.0"))
    changed = [f"{_PACKS_PREFIX}bigpack/skills/skill{i}.yaml" for i in range(15)]
    failures = check_pack_version_bumps(changed, head_ver, base_ver)
    assert len(failures) == 1
    msg = _format_failures(failures)
    assert "… and 5 more" in msg
