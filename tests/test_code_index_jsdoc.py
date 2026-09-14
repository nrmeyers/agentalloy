"""JSDoc docstring extraction for JS/TS symbols (facade-level, end-to-end).

The vendored engine's JS/TS ``_get_docstring`` mixins are stubs; the concrete
implementation in ``definition_processor.DefinitionProcessor`` reads Python
string statements. The JSDoc fallback makes the ``/** ... */`` block comment
immediately preceding a JS/TS declaration the docstring source — verified
here through ``parse_repo`` on a fixture mini-repo, the same path the ingest
pipeline and the recall benchmark's offline ground truth use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.code_index.facade import parse_repo

TS_FIXTURE = """\
// plain line comment above a plain function
function noDoc() {
  return 1;
}

/* Regular block comment, not JSDoc. */
function plainBlock() {
  return 2;
}

/** Derive a default WS URL from the HTTP base URL. */
export function deriveWsUrl(httpBaseUrl: string): string {
  return httpBaseUrl;
}

/**
 * Backoff schedule in ms: 100, 300, 900.
 * @param attempt the 1-based attempt number
 */
function backoffMs(attempt: number): number {
  return 100 * attempt;
}

/** Session store. */
export class SessionStore {
  /** Put a session into the store. */
  put(id: string) {
    return id;
  }
}
"""

PY_FIXTURE = '''\
# A comment above a plain function.
def py_no_docstring():
    return 1


def py_docstring():
    """Greet the named party.

    More detail on the next line.
    """
    return "hi"
'''


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "sample.ts").write_text(TS_FIXTURE)
    (repo / "src" / "sample.py").write_text(PY_FIXTURE)
    return repo


def _docstrings(repo: Path) -> dict[str, str | None]:
    """qualified_name last segment -> docstring for all parsed symbols."""
    result = parse_repo(repo, cache_dir=repo / ".cache")
    out: dict[str, str | None] = {}
    for ps in result.symbols:
        out[ps.name] = ps.docstring
    return out


def test_jsdoc_single_line_export_function(fixture_repo: Path) -> None:
    docs = _docstrings(fixture_repo)
    assert docs["deriveWsUrl"] == "Derive a default WS URL from the HTTP base URL."


def test_jsdoc_multiline_with_tags(fixture_repo: Path) -> None:
    docs = _docstrings(fixture_repo)
    assert docs["backoffMs"] == (
        "Backoff schedule in ms: 100, 300, 900.\n@param attempt the 1-based attempt number"
    )


def test_jsdoc_class_and_method(fixture_repo: Path) -> None:
    docs = _docstrings(fixture_repo)
    assert docs["SessionStore"] == "Session store."
    assert docs["put"] == "Put a session into the store."


def test_non_jsdoc_comments_are_not_docstrings(fixture_repo: Path) -> None:
    docs = _docstrings(fixture_repo)
    # Line comment and plain block comment above the declaration.
    assert docs["noDoc"] is None
    assert docs["plainBlock"] is None


def test_python_behavior_unchanged(fixture_repo: Path) -> None:
    docs = _docstrings(fixture_repo)
    # Continuation lines keep their indentation — pre-existing engine
    # behavior (DOCSTRING_STRIP_CHARS only trims the whole string's ends).
    assert docs["py_docstring"] == "Greet the named party.\n\n    More detail on the next line."
    # A `#` comment above a def is not a docstring (JSDoc fallback must not
    # fire on Python nodes).
    assert docs["py_no_docstring"] is None
