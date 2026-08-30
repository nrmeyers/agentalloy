"""Lifecycle artifacts live in the store; only runtime code and tests go to disk.

The failure this fences off: an agent on the sdd-fast lane was told to "write the
sdd-fast artifact" with no store command named, while `approve` reported
``no exit artifact at 'docs/fast/*.md'``. So the agent created a `docs/fast/`
markdown file — gitignored, and invisible to the store-backed exit gate, which
stayed blocked. Any filesystem path for a *lifecycle* artifact that reaches
agent-facing text reproduces this, so the paths are pinned out of the packs.

Two carve-outs, both deliberate:

- ``src/**`` / ``tests/**`` gates are the point — runtime code and its tests ARE
  disk deliverables.
- ``add-skill`` declares ``.agentalloy/custom-skills/**/*.yaml``: a custom-skill
  pack YAML is tool-written configuration (``agentalloy new-skill-pack``), not a
  phase deliverable body an agent hand-writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import agentalloy

_SDD_PACK_DIR = Path(agentalloy.__file__).resolve().parent / "_packs" / "sdd"

# Phases whose deliverable is genuinely a file on disk.
_CODE_PATH_PREFIXES = ("src/", "tests/")

# The one pack allowed a lifecycle-adjacent disk path, with its reason above.
_DISK_PATH_EXEMPT = {"sdd-add-skill"}


def _sdd_packs() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for f in sorted(_SDD_PACK_DIR.glob("*.yaml")):
        if f.name == "pack.yaml":
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            out.append((f.stem, data))
    return out


def _walk(node: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(node, dict):
        d: dict[str, Any] = node
        if key in d:
            found.append(d[key])
        for v in d.values():
            found.extend(_walk(v, key))
    elif isinstance(node, list):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            found.extend(_walk(item, key))
    return found


@pytest.mark.parametrize("stem,skill", _sdd_packs(), ids=lambda v: v if isinstance(v, str) else "")
def test_no_lifecycle_disk_path_gates(stem: str, skill: dict[str, Any]) -> None:
    """No shipped SDD pack declares a filesystem `path:` gate for a lifecycle artifact.

    A `path:` gate is not merely unsatisfiable against the store — `signals.invariants`
    derives its literal directory prefix into a load-bearing prose invariant, i.e. a
    standing requirement that the prose tell the agent to write there.
    """
    if stem in _DISK_PATH_EXEMPT:
        pytest.skip(f"{stem}: tool-written pack YAML, exempt by design")
    for path in _walk(skill.get("exit_gates") or {}, "path"):
        assert isinstance(path, str)
        assert path.startswith(_CODE_PATH_PREFIXES), (
            f"{stem}: exit gate declares filesystem path {path!r}. Lifecycle artifacts "
            f"are store-backed — use `artifact_exists: {{phase: ..., name: ...}}`. Only "
            f"{_CODE_PATH_PREFIXES} are disk deliverables."
        )


@pytest.mark.parametrize("stem,skill", _sdd_packs(), ids=lambda v: v if isinstance(v, str) else "")
def test_no_disk_approval_globs(stem: str, skill: dict[str, Any]) -> None:
    """`approval_recorded` uses `since_name_glob` (store), never `since` (disk).

    The disk form is what surfaced `docs/fast/*.md` in `approve`'s error text.
    """
    if stem in _DISK_PATH_EXEMPT:
        pytest.skip(f"{stem}: tool-written pack YAML, exempt by design")
    for leaf in _walk(skill.get("exit_gates") or {}, "approval_recorded"):
        assert isinstance(leaf, dict)
        assert "since" not in leaf, (
            f"{stem}: approval_recorded declares disk glob `since: {leaf.get('since')!r}`. "
            f"Use `since_name_glob` so approval digests the store."
        )


def test_scaffolded_workflow_pack_is_store_backed() -> None:
    """`agentalloy new-skill-pack` must not mint disk-path gates.

    The scaffold historically shipped disk-path contract gates, so every custom
    workflow pack a user generated carried a derived prose invariant instructing
    the agent to write contracts to disk. Contracts are now store-backed.
    """
    from agentalloy.install.subcommands.new_skill_pack import (
        _build_skill_record,  # pyright: ignore[reportPrivateUsage]
    )

    skill = _build_skill_record("demo-phase", "workflow", "Demo Phase")
    for path in _walk(skill.get("exit_gates") or {}, "path"):
        assert isinstance(path, str)
        assert path.startswith(_CODE_PATH_PREFIXES), (
            f"scaffold emits filesystem path {path!r} — use a store-backed "
            f"`artifact_exists: {{phase, name}}` leaf instead"
        )


def test_sdd_fast_prose_names_the_store_command() -> None:
    """sdd-fast's prose must name the store verb, and the invariant must match it.

    `check_prose` is an exact substring test, so a prose_invariant that does not
    appear verbatim in the shipped prose would reject the shipped prose itself.
    """
    from agentalloy.signals.invariants import check_prose, derive_invariants, load_shipped_skill

    skill = load_shipped_skill("sdd-fast")
    assert skill is not None
    prose = skill["raw_prose"]
    assert "PUT /state/artifact" in prose
    # `docs/fast/` may appear ONLY as an explicit prohibition, never as a target.
    assert "Do **NOT** create `docs/fast/`" in prose
    # Shipped prose satisfies its own invariants — otherwise every override path
    # falls back and the customize CLI rejects the shipped text.
    assert check_prose(prose, derive_invariants(skill)) == []
