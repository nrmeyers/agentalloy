"""Determinism regression tests for the vendored engine's symbol attribution.

Regression for a real nondeterminism bug: ``QueryCursor.captures()`` returns
one flat node list per capture name, and those lists are neither positionally
aligned with each other nor stably ordered between cursor runs within a single
process. The JS/TS ingest path zipped ``@method_name`` against
``@arrow_function``, so named property closures (``mutationFn:``, ``retry:``,
``queryFn:``) inside different enclosing hooks were attributed to each other's
qualified names — and which closure got which name flipped between two parses
of the SAME unchanged tree. Fixed by pairing captures structurally (shared
parent node) in ``engine/parsers/js_ts/ingest.py``.
"""

from pathlib import Path

from agentalloy.code_index.facade import ParseResult, parse_repo

# Mimics the shape that reproduced the bug on this repo's own frontend
# (frontend/src/hooks/useWizard.ts): several hooks in one module, each holding
# named property arrows with colliding simple names across hooks.
TS_HOOKS_SOURCE = """import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getPack, postScaffold, putFile, ApiError } from '../lib/api';

export function useAlphaPack(repo: string, pack: string) {
  return useQuery({
    queryKey: ['alpha', repo, pack],
    queryFn: () => getPack(repo, pack),
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 1;
    },
  });
}

export function useAlphaScaffold() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { repo: string }) => postScaffold(body),
    onSuccess: (_result, vars) => {
      queryClient.invalidateQueries({ queryKey: ['alpha', vars.repo] });
    },
  });
}

export function useBetaSaveFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { file: string }) => putFile(body),
    onSuccess: (_result, vars) => {
      queryClient.invalidateQueries({ queryKey: ['beta', vars.file] });
    },
    retry: (failureCount) => failureCount < 2,
  });
}
"""


# A @property/@setter pair shares one qualified name; MERGE semantics keep the
# last emission, so unstable captures() ordering flipped which of the two
# definitions' start_line/end_line survived (the ComposeOrchestrator.lm case).
PY_PROPERTY_SOURCE = '''class Orchestrator:
    """Holds a lazily-wired client."""

    @property
    def lm(self):
        """Getter."""
        return self._lm

    @lm.setter
    def lm(self, value):
        self._lm = value
'''


def _parse(repo: Path, cache_root: Path, run: int) -> ParseResult:
    return parse_repo(repo, cache_dir=cache_root / f"cache-{run}")


def test_ts_property_arrow_symbols_are_deterministic(tmp_path: Path) -> None:
    """Parsing the same unchanged tree twice yields identical symbols.

    Full field equality — not just names — because the bug manifested as
    swapped start_line/end_line (and therefore source_code) between distinct
    closures that kept their qualified names.
    """
    repo = tmp_path / "demo"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "useThings.ts").write_text(TS_HOOKS_SOURCE)
    (repo / "orchestrator.py").write_text(PY_PROPERTY_SOURCE)

    first = {s.qualified_name: s for s in _parse(repo, tmp_path, 1).symbols}
    second = {s.qualified_name: s for s in _parse(repo, tmp_path, 2).symbols}

    assert set(first) == set(second)
    unstable = [qn for qn in first if first[qn] != second[qn]]
    assert unstable == [], f"symbol properties flipped between identical parses: {unstable}"

    # The property/setter pair collapses to one QN; the winner must be the
    # deterministic source-order last definition (the setter).
    lm = first["demo.orchestrator.Orchestrator.lm"]
    assert (lm.start_line, lm.end_line) == (10, 11)


def test_ts_property_arrows_attributed_to_own_enclosing_hook(tmp_path: Path) -> None:
    """Each property arrow's source belongs to its own qualified name.

    This is the stronger half of the regression: even when the capture order
    happens to be stable, positional zip pairing binds a property name to an
    arrow from a DIFFERENT object literal, so e.g. ``useAlphaPack.retry``
    carries ``useBetaSaveFile``'s closure body.
    """
    repo = tmp_path / "demo"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "useThings.ts").write_text(TS_HOOKS_SOURCE)

    symbols = {s.qualified_name: s for s in _parse(repo, tmp_path, 1).symbols}

    expected = {
        "demo.hooks.useThings.useAlphaPack.queryFn": "queryFn:",
        "demo.hooks.useThings.useAlphaPack.retry": "retry: (failureCount, error)",
        "demo.hooks.useThings.useAlphaScaffold.mutationFn": "mutationFn: (body: { repo: string })",
        "demo.hooks.useThings.useAlphaScaffold.onSuccess": "onSuccess:",
        "demo.hooks.useThings.useBetaSaveFile.mutationFn": "mutationFn: (body: { file: string })",
        "demo.hooks.useThings.useBetaSaveFile.retry": "retry: (failureCount) => failureCount < 2",
    }
    for qualified_name, source_prefix in expected.items():
        symbol = symbols.get(qualified_name)
        assert symbol is not None, f"missing symbol {qualified_name}"
        assert symbol.source_code is not None, f"no source for {qualified_name}"
        assert symbol.source_code.lstrip().startswith(source_prefix), (
            f"{qualified_name} carries a foreign closure's source: "
            f"{symbol.source_code.lstrip()[:80]!r}"
        )


# Exercises the CommonJS/prototype/ES6-export capture sites that positionally
# zip()-ed multiple captures() lists (js_ts/module_system.py exports +
# module.exports + export-const patterns; js_ts/ingest.py prototype
# inheritance + prototype methods), now paired structurally on the shared
# assignment_expression / variable_declarator ancestor.
CJS_ZOO_SOURCE = """'use strict';

function Animal(name) {
  this.name = name;
}

Animal.prototype.speak = function () {
  return 'generic noise from ' + this.name;
};

Animal.prototype.eat = function () {
  return this.name + ' is eating';
};

function Dog(name) {
  Animal.call(this, name);
}

Dog.prototype = Object.create(Animal.prototype);

Dog.prototype.speak = function () {
  return 'woof woof';
};

exports.makeAnimal = function (name) {
  return new Animal(name);
};

exports.makeDog = (name) => new Dog(name);

module.exports.release = function (animal) {
  return animal.name + ' released to the wild';
};

module.exports.adopt = (animal) => 'adopted ' + animal.name;

module.exports = { makeAnimal: exports.makeAnimal, makeDog: exports.makeDog };
"""

ES6_EXPORT_SOURCE = """export const upper = (value: string) => value.toUpperCase();

export const lower = function (value: string) {
  return value.toLowerCase();
};
"""


def _write_commonjs_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "zoo.js").write_text(CJS_ZOO_SOURCE)
    (repo / "esm.ts").write_text(ES6_EXPORT_SOURCE)
    return repo


def test_commonjs_export_and_prototype_symbols_are_deterministic(tmp_path: Path) -> None:
    """Two parses of an unchanged CommonJS tree yield identical symbols/edges."""
    repo = _write_commonjs_repo(tmp_path)

    first = _parse(repo, tmp_path, 1)
    second = _parse(repo, tmp_path, 2)

    first_symbols = {s.qualified_name: s for s in first.symbols}
    second_symbols = {s.qualified_name: s for s in second.symbols}
    assert set(first_symbols) == set(second_symbols)
    unstable = [qn for qn in first_symbols if first_symbols[qn] != second_symbols[qn]]
    assert unstable == [], f"symbol properties flipped between identical parses: {unstable}"

    assert sorted(first.edges, key=repr) == sorted(second.edges, key=repr)


def test_commonjs_exports_attributed_to_own_function(tmp_path: Path) -> None:
    """Each exports.X / module.exports.X name maps to its OWN function body.

    Positional zip over captures() lists could bind an export name to a
    function from a different assignment statement.
    """
    repo = _write_commonjs_repo(tmp_path)
    symbols = {s.qualified_name: s for s in _parse(repo, tmp_path, 1).symbols}

    expected = {
        "demo.lib.zoo.makeAnimal": "new Animal(",
        "demo.lib.zoo.makeDog": "new Dog(",
        "demo.lib.zoo.release": "released to the wild",
        "demo.lib.zoo.adopt": "'adopted '",
        "demo.esm.upper": "toUpperCase",
        "demo.esm.lower": "toLowerCase",
    }
    for qualified_name, body_marker in expected.items():
        symbol = symbols.get(qualified_name)
        assert symbol is not None, f"missing symbol {qualified_name}"
        assert symbol.source_code is not None, f"no source for {qualified_name}"
        assert body_marker in symbol.source_code, (
            f"{qualified_name} carries a foreign function's source: {symbol.source_code[:80]!r}"
        )


def test_prototype_methods_and_inheritance_attributed_correctly(tmp_path: Path) -> None:
    """Prototype methods bind to their own constructor; inheritance is Dog→Animal."""
    repo = _write_commonjs_repo(tmp_path)
    result = _parse(repo, tmp_path, 1)
    symbols = {s.qualified_name: s for s in result.symbols}

    expected = {
        "demo.lib.zoo.Animal.speak": "generic noise",
        "demo.lib.zoo.Animal.eat": "is eating",
        "demo.lib.zoo.Dog.speak": "woof woof",
    }
    for qualified_name, body_marker in expected.items():
        symbol = symbols.get(qualified_name)
        assert symbol is not None, f"missing symbol {qualified_name}"
        assert symbol.source_code is not None, f"no source for {qualified_name}"
        assert body_marker in symbol.source_code, (
            f"{qualified_name} carries a foreign method's source: {symbol.source_code[:80]!r}"
        )

    inherits = {(e.src, e.dst) for e in result.edges if e.kind == "INHERITS"}
    assert ("demo.lib.zoo.Dog", "demo.lib.zoo.Animal") in inherits
    assert ("demo.lib.zoo.Dog", "demo.lib.zoo.Dog") not in inherits
    assert ("demo.lib.zoo.Animal", "demo.lib.zoo.Dog") not in inherits
