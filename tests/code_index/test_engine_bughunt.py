"""Regression tests for vendored-engine defects from the bughunt (REPORT.md §3).

Each test pins one line-by-line-verified (V) HIGH/MEDIUM finding from the
bughunt. They call the specific engine method that was broken (unit level)
rather than the whole ``parse_repo`` facade, so a future regression re-breaks
the exact code path instead of being masked by end-to-end behavior. The one
exception is :func:`test_3_3_nested_class_calls_use_nested_qn`, which runs the
facade end-to-end because the nested-class CALLS fix spans the call processor
and the identity builder.

Run:
    uv run pytest tests/code_index/test_engine_bughunt.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentalloy.code_index.engine import constants as cs
from agentalloy.code_index.engine.graph_updater import FunctionRegistryTrie, GraphUpdater
from agentalloy.code_index.engine.parser_loader import load_parsers
from agentalloy.code_index.engine.parsers.class_ingest.method_override import (
    process_all_method_overrides,
)
from agentalloy.code_index.engine.parsers.import_processor import ImportProcessor
from agentalloy.code_index.engine.types_defs import NodeType

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _RecordingIngestor:
    """Minimal ``IngestorProtocol`` stand-in that records relationship batches.

    ``process_all_method_overrides`` only ever calls ``ensure_relationship_batch``;
    the other methods are no-ops so the stub satisfies the protocol surface the
    tests actually touch.
    """

    def __init__(self) -> None:
        self.relationships: list[tuple[Any, str, Any]] = []
        self.nodes: list[tuple[str, dict[str, Any]]] = []

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, Any],
        rel_type: str,
        to_spec: tuple[str, str, Any],
        properties: dict[str, Any] | None = None,
    ) -> None:
        self.relationships.append((from_spec, str(rel_type), to_spec))

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
        self.nodes.append((str(label), dict(properties)))

    def flush_all(self) -> None:
        return


class _FakeNode:
    """Minimal tree-sitter Node stand-in for string-literal include parsing.

    The C++ grammar binding is not installed in the test environment, so the
    3.2 test drives ``_parse_cpp_include`` with a hand-built node tree instead
    of a real parse. The method only reads ``type`` / ``text`` / ``children``.
    """

    def __init__(
        self,
        type: str,
        text: bytes | None,
        children: list[_FakeNode] | None = None,
    ) -> None:
        self.type = type
        self.text = text
        self.children = children or []


@pytest.fixture(scope="module")
def _python_parser() -> Any:
    parsers, _queries = load_parsers()
    return parsers[cs.SupportedLanguage.PYTHON]


def _relative_import_node(parser: Any, source: str) -> Any:
    """Extract the ``relative_import`` node from a ``from ... import`` stmt."""
    tree = parser.parse(source.encode("utf-8"))
    for child in tree.root_node.children:
        if child.type == "import_from_statement":
            node = child.child_by_field_name("module_name")
            assert node is not None
            return node
    raise AssertionError(f"no import_from_statement in {source!r}")


# ---------------------------------------------------------------------------
# 3.1 — relative import inside an __init__.py resolves to the package itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "module_qn", "expected", "is_init_file"),
    [
        # ``from . import x`` in pkg/__init__.py stays in the package.
        ("from . import x", "project.pkg", "pkg", True),
        # ``from .submod import y`` in pkg/__init__.py targets a sibling module.
        ("from .submod import y", "project.pkg", "pkg.submod", True),
        # ``from . import x`` in pkg/sub.py goes one level up to the package.
        ("from . import x", "project.pkg.sub", "pkg", False),
    ],
)
def test_3_1_relative_import(
    tmp_path: Path,
    _python_parser: Any,
    source: str,
    module_qn: str,
    expected: str,
    is_init_file: bool,
) -> None:
    module_parts = module_qn.split(".")[1:]
    if is_init_file:
        (tmp_path / Path(*module_parts)).mkdir(parents=True)
        (tmp_path / Path(*module_parts) / "__init__.py").write_text(
            source + "\n", encoding="utf-8"
        )
    else:
        (tmp_path / Path(*module_parts[:-1])).mkdir(parents=True, exist_ok=True)
        (tmp_path / Path(*module_parts[:-1]) / f"{module_parts[-1]}.py").write_text(
            source + "\n", encoding="utf-8"
        )

    processor = ImportProcessor(
        repo_path=tmp_path,
        project_name="project",
        function_registry=FunctionRegistryTrie(),
    )
    node = _relative_import_node(_python_parser, source)
    assert processor._resolve_relative_import(node, module_qn) == expected


# ---------------------------------------------------------------------------
# 3.2 — C++ include strips exactly ONE trailing extension
# ---------------------------------------------------------------------------


def _cpp_include_node(header: str, system: bool = False) -> _FakeNode:
    child_type = cs.TS_SYSTEM_LIB_STRING if system else cs.TS_STRING_LITERAL
    text = f"<{header}>" if system else f'"{header}"'
    return _FakeNode(
        type="preproc_include",
        text=None,
        children=[_FakeNode(type=child_type, text=text.encode("utf-8"))],
    )


def test_3_2_cpp_include_strips_one_extension(tmp_path: Path) -> None:
    processor = ImportProcessor(
        repo_path=tmp_path,
        project_name="project",
        function_registry=FunctionRegistryTrie(),
    )
    module_qn = "project.src"
    processor.import_mapping[module_qn] = {}

    # ``#include "foo.hpp"`` -> ``project.foo`` (the old ``.replace(".h","")``
    # mangled this to ``project.foopp``).
    processor._parse_cpp_include(_cpp_include_node("foo.hpp"), module_qn)
    assert processor.import_mapping[module_qn]["foo"] == "project.foo"

    # A dotted path must keep every interior segment (old code produced
    # ``project.mathash``).
    processor._parse_cpp_include(_cpp_include_node("math/hash.h"), module_qn)
    assert processor.import_mapping[module_qn]["hash"] == "project.math.hash"


# ---------------------------------------------------------------------------
# 3.3 — CALLS edges from a nested class use the nested qualified name
# ---------------------------------------------------------------------------


def test_3_3_nested_class_calls_use_nested_qn(tmp_path: Path) -> None:
    from agentalloy.code_index.facade import parse_repo

    pkg = tmp_path / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "Outer.java").write_text(
        "package com.example;\n"
        "\n"
        "public class Outer {\n"
        "    void helper() {}\n"
        "\n"
        "    static class Inner {\n"
        "        void doWork() {\n"
        "            helper();\n"
        "        }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    result = parse_repo(tmp_path, languages=["java"])
    module_qn = next(s.qualified_name for s in result.symbols if s.kind == "Module")
    nested_src = f"{module_qn}.Outer.Inner.doWork"
    buggy_src = f"{module_qn}.Inner.doWork"
    call_srcs = {e.src for e in result.edges if e.kind == "CALLS"}
    assert nested_src in call_srcs, (
        f"expected nested CALLS src {nested_src!r}; got {sorted(call_srcs)}"
    )
    assert buggy_src not in call_srcs, (
        f"buggy non-nested CALLS src {buggy_src!r} is present"
    )


# ---------------------------------------------------------------------------
# 3.4 — remove_file_from_state uses the package prefix for __init__.py
# ---------------------------------------------------------------------------


def test_3_4_remove_init_file_uses_package_prefix(tmp_path: Path) -> None:
    updater = GraphUpdater(
        ingestor=_RecordingIngestor(),
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    proj = updater.project_name
    for qn in (
        f"{proj}.src.foo",
        f"{proj}.src.foo.init_func",
        f"{proj}.src.foo.Cls",
        f"{proj}.src.foo.Cls.method",
        # A sibling package that must survive.
        f"{proj}.src.foobar.other",
    ):
        updater.function_registry[qn] = NodeType.FUNCTION

    updater.remove_file_from_state(tmp_path / "src" / "foo" / "__init__.py")

    remaining = set(updater.function_registry.keys())
    assert f"{proj}.src.foo" not in remaining
    assert f"{proj}.src.foo.init_func" not in remaining
    assert f"{proj}.src.foo.Cls.method" not in remaining
    assert f"{proj}.src.foobar.other" in remaining


def test_3_4_remove_regular_file_uses_module_prefix(tmp_path: Path) -> None:
    updater = GraphUpdater(
        ingestor=_RecordingIngestor(),
        repo_path=tmp_path,
        parsers={},
        queries={},
    )
    proj = updater.project_name
    for qn in (
        f"{proj}.src.foo.bar",
        f"{proj}.src.foo.bar.func",
        # A sibling module that must survive.
        f"{proj}.src.foo.baz.func",
    ):
        updater.function_registry[qn] = NodeType.FUNCTION

    updater.remove_file_from_state(tmp_path / "src" / "foo" / "bar.py")

    remaining = set(updater.function_registry.keys())
    assert f"{proj}.src.foo.bar" not in remaining
    assert f"{proj}.src.foo.bar.func" not in remaining
    assert f"{proj}.src.foo.baz.func" in remaining


# ---------------------------------------------------------------------------
# 3.5 — FunctionRegistryTrie.find_with_prefix returns dunder methods
# ---------------------------------------------------------------------------


def test_3_5_trie_prefix_finds_dunder_method() -> None:
    trie = FunctionRegistryTrie()
    trie["project.mod.Cls.__str__"] = NodeType.METHOD
    trie["project.mod.Cls.render"] = NodeType.METHOD

    found = dict(trie.find_with_prefix("project.mod.Cls"))
    # The old ``startswith("__")`` filter treated ``__str__`` as an internal
    # metadata key and dropped it.
    assert found.get("project.mod.Cls.__str__") == NodeType.METHOD
    assert found.get("project.mod.Cls.render") == NodeType.METHOD


# ---------------------------------------------------------------------------
# 3.6 — method override detection with a dotted parameter type
# ---------------------------------------------------------------------------


def test_3_6_method_override_with_dotted_param_type() -> None:
    trie = FunctionRegistryTrie()
    trie["com.foo.Base.m(Map.Entry, String)"] = NodeType.METHOD
    trie["com.foo.Sub.m(Map.Entry, String)"] = NodeType.METHOD

    ingestor = _RecordingIngestor()
    process_all_method_overrides(
        trie,
        {"com.foo.Sub": ["com.foo.Base"]},
        ingestor,
    )

    overrides = [
        (from_spec[2], to_spec[2])
        for from_spec, _rel, to_spec in ingestor.relationships
        if _rel == str(cs.RelationshipType.OVERRIDES)
    ]
    # The old ``rsplit(".", 1)`` split inside the parens (``Map.Entry``) and
    # never matched the inheritance map, so no OVERRIDES edge was emitted.
    assert ("com.foo.Sub.m(Map.Entry, String)", "com.foo.Base.m(Map.Entry, String)") in overrides


# ---------------------------------------------------------------------------
# 3.7-O — JS/TS object-literal / member-assignment fn-expr & arrow register a
#         single FUNCTION node (no duplicate spans)
# ---------------------------------------------------------------------------


def test_3_7o_js_fn_expr_and_arrow_register_once(tmp_path: Path) -> None:
    from agentalloy.code_index.facade import parse_repo

    js_src = (
        "const obj = {\n"
        "  bar: function () { return 1; },   // object-literal fn-expr (pair value)\n"
        "  baz: () => { return 2; },          // object-literal arrow (pair value)\n"
        "  qux() { return 4; },               // object method_definition\n"
        "};\n"
        "\n"
        "const standalone = () => { return 5; };   // variable declarator arrow (generic only)\n"
        "\n"
        "const target = {};\n"
        "target.fn = () => { return 6; };          // member assignment arrow (JS pass)\n"
        "target.fn2 = function () { return 7; };   // member assignment fn-expr (JS pass)\n"
        "\n"
        "function top() { return 3; }               // function declaration (generic only)\n"
    )
    (tmp_path / "app.js").write_text(js_src, encoding="utf-8")

    result = parse_repo(tmp_path, languages=["javascript"])
    fns = [s for s in result.symbols if s.kind == "Function"]

    # The core regression: no source span may be registered under more than
    # one qualified name. The generic ``_ingest_all_functions`` pass and the
    # dedicated JS passes used to both register the same fn-expr/arrow node
    # (``anonymous_N_M`` vs the property/member name).
    by_span: dict[tuple[int, int], list[str]] = {}
    for s in fns:
        by_span.setdefault((s.start_line or 0, s.end_line or 0), []).append(s.qualified_name)
    duplicated = {span: sorted(qns) for span, qns in by_span.items() if len(qns) > 1}
    assert not duplicated, f"duplicated FUNCTION spans: {duplicated}"

    # The dedup must keep the MEANINGFUL (named) node from the dedicated JS
    # pass, not the ``anonymous_N_M`` node from the generic pass.
    names = {s.name for s in fns}
    for expected in ("bar", "baz", "qux", "standalone", "fn", "fn2", "top"):
        assert expected in names, f"expected Function {expected!r}; got {sorted(names)}"
    assert not any(cs.PREFIX_ANONYMOUS in s.qualified_name for s in fns), (
        "anonymous FUNCTION node survived the dedup"
    )
