"""TB2 — No filesystem glob over .agentalloy/contracts remains in the signal path.

This test scans the source tree for `.agentalloy/contracts` glob patterns in
executable code (not docstrings, not comments) to enforce the migration
contract requirement.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_CONTRACTS_GLOB_PATTERN = re.compile(r"\.agentalloy/contracts")


def _find_glob_literals_in_code(file_path: Path) -> list[tuple[int, str]]:
    """Find string literals containing '.agentalloy/contracts' in executable code.

    Uses AST parsing to distinguish docstrings from executable string literals.
    Returns list of (line_no, line_text) for violations.
    """
    violations: list[tuple[int, str]] = []

    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return violations

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return violations

    lines = source.splitlines()

    # Collect docstring line ranges
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            doc_node = node.body[0]
            for ln in range(doc_node.lineno, doc_node.end_lineno or doc_node.lineno + 1):
                docstring_lines.add(ln)

    # Find all string constants in the AST that are NOT docstrings
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in docstring_lines:
                continue
            if _CONTRACTS_GLOB_PATTERN.search(node.value):
                line_text = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                violations.append((node.lineno, line_text))

    return violations


def _get_signal_py_files() -> list[Path]:
    """Discover all Python files under src/agentalloy/signals/."""
    signals_dir = Path("src/agentalloy/signals")
    if not signals_dir.exists():
        return []
    return sorted(signals_dir.glob("*.py"))


@pytest.mark.parametrize(
    "py_file",
    _get_signal_py_files(),
    ids=lambda p: str(p.relative_to(Path("src/agentalloy/signals"))),
)
def test_no_agentalloy_contracts_glob_in_signals_source(py_file: Path) -> None:
    """No .agentalloy/contracts glob literal in executable code under signals/."""
    violations = _find_glob_literals_in_code(py_file)
    assert not violations, (
        f"{py_file} contains .agentalloy/contracts glob in executable code:\n"
        + "\n".join(f"  line {ln}: {text}" for ln, text in violations)
    )


def test_no_agentalloy_contracts_glob_in_predicates() -> None:
    """Specifically verify predicates.py has no .agentalloy/contracts glob in executable code."""
    violations = _find_glob_literals_in_code(Path("src/agentalloy/signals/predicates.py"))
    assert not violations, (
        "predicates.py contains .agentalloy/contracts glob in executable code:\n"
        + "\n".join(f"  line {ln}: {text}" for ln, text in violations)
    )


def test_no_agentalloy_contracts_glob_in_gates() -> None:
    """Specifically verify gates.py has no .agentalloy/contracts glob in executable code."""
    violations = _find_glob_literals_in_code(Path("src/agentalloy/signals/gates.py"))
    assert not violations, (
        "gates.py contains .agentalloy/contracts glob in executable code:\n"
        + "\n".join(f"  line {ln}: {text}" for ln, text in violations)
    )


def test_no_agentalloy_contracts_glob_in_skill_loader() -> None:
    """Specifically verify skill_loader.py has no .agentalloy/contracts glob in executable code."""
    violations = _find_glob_literals_in_code(Path("src/agentalloy/signals/skill_loader.py"))
    assert not violations, (
        "skill_loader.py contains .agentalloy/contracts glob in executable code:\n"
        + "\n".join(f"  line {ln}: {text}" for ln, text in violations)
    )
