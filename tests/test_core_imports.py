"""Guard: core code must not import extras-only packages at module level.

Motivating bug (v6.3.0): ``providers/codex/install.py`` did a module-level
``import toml``, but ``toml`` is declared only under the ``[code-index]``
optional extra. Every bare ``uv tool install agentalloy`` then printed
``provider 'codex' failed to load: No module named 'toml'`` on every CLI
invocation — the codex provider was dead on the default native path. CI never
saw it because the dev environment installs all extras.

This test derives the extras-only package set from ``pyproject.toml`` itself
and AST-scans core source for module-level imports of those packages, so the
guard can't drift from the declared dependencies. ``code_index/`` is excluded:
it is the module the ``[code-index]`` extra exists for, and it degrades to
``unavailable`` when the extra is absent. Imports inside ``try`` blocks or
function bodies are allowed — that's the sanctioned lazy/guarded pattern.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "agentalloy"

# Source trees that exist BECAUSE of an extra and are import-guarded at their
# seam (module resolution degrades gracefully when the extra is absent).
EXTRA_GATED_DIRS = ("code_index",)


def _dist_to_module(requirement: str) -> str:
    """Normalize a requirement string to its importable top-level module name."""
    name = requirement.split(";")[0].split("[")[0]
    for sep in ("==", ">=", "<=", "~=", ">", "<", "!="):
        name = name.split(sep)[0]
    return name.strip().lower().replace("-", "_")


def _extras_only_modules() -> set[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    core = {_dist_to_module(r) for r in pyproject["project"]["dependencies"]}
    extras: set[str] = set()
    for group, reqs in pyproject["project"]["optional-dependencies"].items():
        if group == "dev":
            continue
        extras |= {_dist_to_module(r) for r in reqs}
    return extras - core


def _module_level_import_roots(tree: ast.Module) -> set[str]:
    """Top-level import roots, excluding imports inside try blocks (lazy guards)."""
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_core_source_never_imports_extras_only_packages():
    extras_only = _extras_only_modules()
    assert extras_only, "pyproject optional-dependencies vanished — guard is vacuous"

    offenders: list[str] = []
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        rel = py_file.relative_to(SRC_ROOT)
        if rel.parts[0] in EXTRA_GATED_DIRS:
            continue
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        hits = _module_level_import_roots(tree) & extras_only
        offenders += [f"{rel}: import {mod}" for mod in sorted(hits)]

    assert not offenders, (
        "Core modules import extras-only packages at module level; a bare install "
        "(no extras) breaks at import time:\n  " + "\n  ".join(offenders)
    )
