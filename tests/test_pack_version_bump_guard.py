"""CI guard: pack content edits must bump the pack's version field.

The base ref is ``PACK_GUARD_BASE_REF`` if set; otherwise it defaults to
``origin/main``.  Either way the guard merge-bases it against HEAD, so local
runs, pre-commit, push, and CI all ask the same question ("which packs did this
branch change relative to main?") and retargeting a PR cannot change the
verdict.  CI no longer sets the env — both sides compute the merge-base.  Only
when ``origin/main`` cannot be resolved (shallow clone with no network, fresh
orphan worktree) does the guard skip, and loudly — never a silent pass.

The comparison is against the **working tree**, not HEAD: committed, staged,
unstaged, and untracked pack files all count, and the version is read from
``pack.yaml`` on disk.  This is deliberate.  A HEAD-anchored guard cannot see
an uncommitted pack edit, so it was structurally incapable of failing before
the commit that introduces the problem — it could only ever fail in CI, after
a push.  Locally the guard therefore sees *more* than CI does, which is the
point; CI checks out clean, so worktree == HEAD there and the verdict is
identical.

Practical consequence: the suite goes red the moment you edit a pack and stays
red until you bump that pack's version.  Bump it as the first edit of a pack
change, not the last.

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
    """Return pack files that differ between ``base_ref`` and the WORKING TREE.

    Two-dot against the resolved merge-base, so the comparison reaches the
    worktree and covers committed, staged, and unstaged edits in one command.
    Untracked files are unioned in separately — ``git diff`` cannot see them,
    and a new skill YAML dropped into an existing pack is exactly the case the
    guard exists to catch.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "--", _PACKS_PREFIX],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=repo_root,
    )
    if result.returncode != 0:
        pytest.skip(f"git diff failed (shallow clone or bad ref?): {result.stderr.strip()}")
    names = {line.strip() for line in result.stdout.splitlines() if line.strip()}

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", _PACKS_PREFIX],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=repo_root,
    )
    if untracked.returncode != 0:
        pytest.skip(f"git ls-files failed: {untracked.stderr.strip()}")
    names.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    return sorted(names)


def _worktree_version(pack: str, repo_root: Path) -> str | None:
    """Return the version field from a pack's pack.yaml **on disk**.

    Returns None when the file is absent, which is the correct answer mid-
    deletion: falling back to ``git show HEAD`` there would resurrect a stale
    version and flag a pack being removed as un-bumped.
    """
    path = repo_root / _PACKS_PREFIX / pack / "pack.yaml"
    if not path.is_file():
        return None
    data: dict[str, object] = yaml.safe_load(path.read_text())
    return str(data["version"])


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


def _git_merge_base(ref: str, repo_root: Path) -> str | None:
    """Return ``merge-base(HEAD, ref)`` SHA, or None if ``ref`` is absent / git fails."""
    result = subprocess.run(
        ["git", "merge-base", "HEAD", ref],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _git_fetch(remote: str, branch: str, repo_root: Path) -> bool:
    """Best-effort ``git fetch <remote> <branch>``; False on failure (offline, no remote)."""
    result = subprocess.run(
        ["git", "fetch", "--quiet", remote, branch],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repo_root,
    )
    return result.returncode == 0


def _resolve_base_ref(repo_root: Path, ref: str = "origin/main") -> str | None:
    """Resolve the pack-guard base ref to ``merge-base(HEAD, ref)``.

    Tries the ref as-is first; if it cannot be resolved (shallow clone, fresh
    worktree without ``origin/main``), attempts a best-effort ``git fetch origin
    main`` and retries.  Returns None only when it cannot be resolved at all —
    the caller then skips *loudly*, never silently.

    An explicit ``PACK_GUARD_BASE_REF`` is merge-based too, not used raw: the
    diff is two-dot against the working tree, so a divergent base would
    otherwise report files changed on the *base* side as this branch's work.
    """
    mb = _git_merge_base(ref, repo_root)
    if mb:
        return mb
    if _git_fetch("origin", "main", repo_root):
        return _git_merge_base(ref, repo_root)
    return None


# ---------------------------------------------------------------------------
# The guard test
# ---------------------------------------------------------------------------


def test_pack_version_bump_guard() -> None:
    """Fail if any pack's content changed but its version was not bumped.

    Compares the **working tree** against ``merge-base(HEAD, base)``, where base
    is ``PACK_GUARD_BASE_REF`` if set, else ``origin/main``.  Worktree-anchored
    so an uncommitted pack edit fails here, before it can reach CI; the old
    HEAD-anchored form could only fail after the offending commit was pushed.
    Never skips silently on an unset env.  It skips only when the base genuinely
    cannot be resolved, and then loudly.
    """
    repo_root = Path(__file__).parent.parent

    env_ref = os.environ.get("PACK_GUARD_BASE_REF", "").strip()
    base_ref = (
        _resolve_base_ref(repo_root, env_ref) if env_ref else _resolve_base_ref(repo_root)
    ) or ""
    if not base_ref:
        pytest.skip(
            "Could not resolve a base ref for the pack version bump guard: "
            f"merge-base(HEAD, {env_ref or 'origin/main'}) is unavailable "
            "(ref missing and fetch failed or was offline). "
            "Set PACK_GUARD_BASE_REF explicitly or run with network access."
        )

    changed_files = _git_diff_names(base_ref, repo_root)
    if not changed_files:
        return  # nothing under _packs/ changed

    def head_ver(pack: str) -> str | None:
        return _worktree_version(pack, repo_root)

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


def test_unit_skip_loudly_when_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env unset AND origin/main unresolvable → skip loudly, never a silent pass."""
    monkeypatch.delenv("PACK_GUARD_BASE_REF", raising=False)

    def _no_merge_base(ref: str, root: Path) -> str | None:
        return None

    def _no_fetch(remote: str, branch: str, root: Path) -> bool:
        return False

    monkeypatch.setattr(f"{__name__}._git_merge_base", _no_merge_base)
    monkeypatch.setattr(f"{__name__}._git_fetch", _no_fetch)
    with pytest.raises(pytest.skip.Exception):  # type: ignore[attr-defined]
        test_pack_version_bump_guard()


def test_unit_env_set_is_merge_based_not_used_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    """PACK_GUARD_BASE_REF chooses the ref, but is still merge-based against HEAD.

    The diff is two-dot against the working tree, so feeding a divergent base in
    raw would report changes made on the *base* side as this branch's work.
    """
    monkeypatch.setenv("PACK_GUARD_BASE_REF", "some-other-branch")
    seen: list[str] = []

    def _record_merge_base(ref: str, root: Path) -> str | None:
        seen.append(ref)
        return "mb-sha"

    def _expect_mb(base: str, root: Path) -> list[str]:
        assert base == "mb-sha", f"guard diffed against {base!r}, not the merge-base"
        return []

    monkeypatch.setattr(f"{__name__}._git_merge_base", _record_merge_base)
    monkeypatch.setattr(f"{__name__}._git_diff_names", _expect_mb)
    test_pack_version_bump_guard()  # returns early — no pack changes
    assert seen == ["some-other-branch"]


def test_unit_changed_files_capped_at_ten_in_message() -> None:
    """Failure message shows at most 10 files, then '… and N more'."""
    head_ver, base_ver = _make_versions(bigpack=("9.0.0", "9.0.0"))
    changed = [f"{_PACKS_PREFIX}bigpack/skills/skill{i}.yaml" for i in range(15)]
    failures = check_pack_version_bumps(changed, head_ver, base_ver)
    assert len(failures) == 1
    msg = _format_failures(failures)
    assert "… and 5 more" in msg
