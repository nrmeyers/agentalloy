"""Repro for 3.7-O: JS/TS fn-expr/arrow register two FUNCTION nodes.

The generic ``_ingest_all_functions`` pass and the JS-specific
``_ingest_object_literal_methods`` / ``_ingest_assignment_arrow_functions``
passes both capture the same ``function_expression`` / ``arrow_function``
node but compute different qualified names, so the node is registered twice.
Grouping by source span (start/end line) exposes the duplicates.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agentalloy.code_index.facade import parse_repo  # noqa: E402

JS_SRC = """\
const obj = {
  bar: function () { return 1; },   // object-literal fn-expr (pair value)
  baz: () => { return 2; },          // object-literal arrow (pair value)
  qux() { return 4; },               // object method_definition
};

const standalone = () => { return 5; };   // variable declarator arrow (generic only)

const target = {};
target.fn = () => { return 6; };          // member assignment arrow (JS pass)
target.fn2 = function () { return 7; };   // member assignment fn-expr (JS pass)

function top() { return 3; }               // function declaration (generic only)
"""


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "app.js").write_text(JS_SRC, encoding="utf-8")
        result = parse_repo(root, languages=["javascript"])
        fns = [s for s in result.symbols if s.kind == "Function"]
        print(f"total Function symbols: {len(fns)}")
        for s in sorted(fns, key=lambda x: (x.start_line or 0, x.qualified_name)):
            print(
                f"  lines={s.start_line:>2}-{s.end_line:<2}  {s.qualified_name!r:55} name={s.name!r}"
            )

        # Group by source span (start_line, end_line) to expose duplicates of
        # the same node registered under multiple QNs.
        by_span: dict[tuple, list[str]] = {}
        for s in fns:
            by_span.setdefault((s.start_line, s.end_line), []).append(s.qualified_name)
        print("\nDuplicated spans (same source range, multiple QNs):")
        found = False
        for (sl, el), qns in sorted(by_span.items()):
            if len(qns) > 1:
                found = True
                print(f"  lines={sl}-{el} -> {len(qns)} QNs: {sorted(qns)}")
        if not found:
            print("  (none)")


if __name__ == "__main__":
    main()
