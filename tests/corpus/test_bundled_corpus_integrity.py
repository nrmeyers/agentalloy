"""Complete-corpus integrity gates for the bundled packs.

These properties only hold across the *whole* corpus, not per-pack — install-packs
ingests pack-by-pack, so a single pack's ingest legitimately can't see another
pack's skills (cross-pack ``requires`` edges, including circular ones, are
resolved best-effort and warned about, never failed). Referential integrity is
therefore enforced here, where every bundled skill is visible at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

import agentalloy

_PACKS_ROOT = Path(agentalloy.__file__).resolve().parent / "_packs"


def _load_skill_docs() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for f in _PACKS_ROOT.rglob("*.yaml"):
        if f.name == "pack.yaml":
            continue
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("skill_id"):
            doc["__file"] = str(f.relative_to(_PACKS_ROOT))
            docs.append(doc)
    return docs


def test_every_requires_edge_resolves() -> None:
    """No `requires` edge points at a skill_id absent from the bundled corpus."""
    docs = _load_skill_docs()
    skill_ids = {str(d["skill_id"]) for d in docs}
    dangling: list[str] = []
    for d in docs:
        for target in d.get("requires") or []:
            if target not in skill_ids:
                dangling.append(f"{d['skill_id']} ({d['__file']}) -> '{target}'")
    assert not dangling, "dangling requires edges:\n  " + "\n  ".join(dangling)


def test_no_related_edges_remain() -> None:
    """`related`/REFERENCES_CONCEPTUAL was removed in Stage 3a — no pack may declare it."""
    offenders = [d["__file"] for d in _load_skill_docs() if "related" in d]
    assert not offenders, "skills still declaring `related`:\n  " + "\n  ".join(offenders)


def test_all_exit_gate_predicates_are_known() -> None:
    """Every predicate named in a bundled exit_gates spec is in the runtime registry."""
    from agentalloy.signals.classifier import SEMANTIC_PREDICATES
    from agentalloy.signals.gates import PREDICATES

    known = set(PREDICATES) | set(SEMANTIC_PREDICATES)

    def _leaf_predicates(spec: Any, acc: set[str]) -> None:
        if not isinstance(spec, dict):
            return
        composites = [k for k in ("all_of", "any_of", "not") if k in spec]
        if composites:
            for k in composites:
                v = spec[k]
                children = v if isinstance(v, list) else [v]
                for c in children:
                    _leaf_predicates(c, acc)
        else:
            acc.update(spec.keys())

    unknown: list[str] = []
    for d in _load_skill_docs():
        gates = d.get("exit_gates")
        if not isinstance(gates, dict):
            continue
        used: set[str] = set()
        _leaf_predicates(gates, used)
        for name in used - known:
            unknown.append(f"{d['skill_id']} ({d['__file']}): '{name}'")
    assert not unknown, "unknown gate predicates:\n  " + "\n  ".join(unknown)


def test_phase_scope_values_are_lifecycle() -> None:
    """phase_scope only uses the canonical SDD lifecycle (no stale `review`).

    Sourced from ``ingest._VALID_PHASES`` — the authoritative set ``ingest._validate``
    itself checks phase_scope against — rather than a hand-maintained duplicate.
    A hardcoded local copy previously excluded ``sdd-fast``/``add-skill`` even
    though both are canonical (see ``bootstrap.py``'s "Canonical SDD lifecycle"
    comment), which is exactly the drift this guard exists to prevent.
    """
    from agentalloy.ingest import _VALID_PHASES  # pyright: ignore[reportPrivateUsage]

    offenders: list[str] = []
    for d in _load_skill_docs():
        for p in d.get("phase_scope") or []:
            if p not in _VALID_PHASES:
                offenders.append(f"{d['skill_id']} ({d['__file']}): phase_scope '{p}'")
    assert not offenders, "non-lifecycle phase_scope values:\n  " + "\n  ".join(offenders)
