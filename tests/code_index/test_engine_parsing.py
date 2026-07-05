"""End-to-end tests for the vendored parsing engine via the facade."""

from pathlib import Path

import pytest

from agentalloy.code_index.facade import ParsedSymbol, ParseResult, parse_repo

PY_SOURCE = '''"""Utility module."""


def helper(x):
    """Add one to x."""
    return x + 1


def caller():
    """Calls helper."""
    return helper(41)


@staticmethod
class Greeter:
    """A greeter."""

    def greet(self):
        return caller()
'''

TS_SOURCE = """export function shout(msg: string): string {
  return msg.toUpperCase();
}

export function main(): string {
  return shout("hi");
}
"""


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "util.py").write_text(PY_SOURCE)
    (repo / "app.ts").write_text(TS_SOURCE)
    return repo


@pytest.fixture
def result(fixture_repo: Path, tmp_path: Path) -> ParseResult:
    return parse_repo(fixture_repo, cache_dir=tmp_path / "cache")


def _symbol(result: ParseResult, qualified_name: str) -> ParsedSymbol:
    matches = [s for s in result.symbols if s.qualified_name == qualified_name]
    assert matches, (
        f"symbol {qualified_name} not found in {[s.qualified_name for s in result.symbols]}"
    )
    return matches[0]


def test_python_function_symbols(result: ParseResult) -> None:
    helper = _symbol(result, "demo.pkg.util.helper")
    assert helper.kind == "Function"
    assert helper.name == "helper"
    assert helper.file_path == "pkg/util.py"
    assert (helper.start_line, helper.end_line) == (4, 6)
    assert helper.docstring == "Add one to x."
    assert helper.source_code is not None
    assert "return x + 1" in helper.source_code


def test_python_class_and_method_symbols(result: ParseResult) -> None:
    greeter = _symbol(result, "demo.pkg.util.Greeter")
    assert greeter.kind == "Class"
    assert greeter.docstring == "A greeter."
    assert greeter.decorators == ("@staticmethod",)

    greet = _symbol(result, "demo.pkg.util.Greeter.greet")
    assert greet.kind == "Method"
    assert greet.file_path == "pkg/util.py"


def test_typescript_symbols(result: ParseResult) -> None:
    shout = _symbol(result, "demo.app.shout")
    assert shout.kind == "Function"
    assert shout.file_path == "app.ts"
    assert (shout.start_line, shout.end_line) == (1, 3)
    assert shout.source_code is not None
    assert "msg.toUpperCase()" in shout.source_code


def test_calls_edges(result: ParseResult) -> None:
    calls = {(e.src, e.dst) for e in result.edges if e.kind == "CALLS"}
    assert ("demo.pkg.util.caller", "demo.pkg.util.helper") in calls
    assert ("demo.app.main", "demo.app.shout") in calls

    edge = next(
        e
        for e in result.edges
        if e.kind == "CALLS" and (e.src, e.dst) == ("demo.pkg.util.caller", "demo.pkg.util.helper")
    )
    assert edge.file_path == "pkg/util.py"
    assert edge.line_start == 11
    assert edge.confidence is not None and edge.confidence > 0


def test_defines_edges(result: ParseResult) -> None:
    defines = {(e.src, e.dst) for e in result.edges if e.kind == "DEFINES"}
    assert ("demo.pkg.util", "demo.pkg.util.caller") in defines
    method_defs = {(e.src, e.dst) for e in result.edges if e.kind == "DEFINES_METHOD"}
    assert ("demo.pkg.util.Greeter", "demo.pkg.util.Greeter.greet") in method_defs


def test_cache_dir_receives_caches_and_repo_stays_clean(fixture_repo: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    before = sorted(p.relative_to(fixture_repo) for p in fixture_repo.rglob("*"))
    parse_repo(fixture_repo, cache_dir=cache_dir)
    after = sorted(p.relative_to(fixture_repo) for p in fixture_repo.rglob("*"))

    assert before == after, "parse_repo must not write into the indexed repo"
    cache_names = {p.name for p in cache_dir.iterdir()}
    assert ".cgr-hash-cache.json" in cache_names
    assert ".cgr-stat-cache.json" in cache_names


def test_language_filter(fixture_repo: Path, tmp_path: Path) -> None:
    result = parse_repo(fixture_repo, cache_dir=tmp_path / "cache", languages=["python"])
    kinds = {s.qualified_name for s in result.symbols if s.kind == "Function"}
    assert "demo.pkg.util.helper" in kinds
    assert "demo.app.shout" not in kinds


def test_unknown_language_rejected(fixture_repo: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported language"):
        parse_repo(fixture_repo, cache_dir=tmp_path / "cache", languages=["cobol"])


def test_unreadable_file_skips_not_crashes(fixture_repo: Path, tmp_path: Path) -> None:
    """A permission-denied file must skip, not fail the whole parse.

    Real working trees contain unreadable corners (e.g. leftover rootless
    container-storage overlays from killed test runs). stat() succeeds on
    them but open() raises EACCES — found live indexing this very repo.
    """
    import os

    blocked = fixture_repo / "pkg" / "blocked.py"
    blocked.write_text("def hidden():\n    return 1\n")
    blocked.chmod(0o000)
    if os.access(blocked, os.R_OK):  # pragma: no cover — root ignores modes
        pytest.skip("running as privileged user; chmod 000 not enforced")
    try:
        result = parse_repo(fixture_repo, cache_dir=tmp_path / "cache")
    finally:
        blocked.chmod(0o644)

    names = {s.qualified_name for s in result.symbols}
    assert "demo.pkg.util.helper" in names, "readable files still parse"
    assert not any("blocked" in n for n in names), "unreadable file is skipped"
