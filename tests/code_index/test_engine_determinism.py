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
