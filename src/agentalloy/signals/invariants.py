"""Load-bearing invariants for customized system/workflow skills.

A user may customize a system/workflow skill's *language* (``raw_prose``), but
parts of that prose are **load-bearing**: the file/contract paths and the
command strings the SDD machine depends on (e.g. ``agentalloy phase set build``,
``.agentalloy/contracts/build/``). If a customization drops them, the phase
machine silently stops working.

The invariant set for a skill is:

  (a) literal path tokens **derived** from the shipped skill's ``exit_gates``
      (the deterministic mechanics — a gate that checks ``.agentalloy/contracts/
      build/*.md`` implies the prose must still tell the agent to write there),
      PLUS
  (b) an authored ``prose_invariants`` list on the shipped skill for command
      strings that are not derivable from any gate path.

Section headings and other common words are deliberately NOT auto-derived
(``Approach``/``Tasks`` collide with ordinary English); if a heading is
load-bearing in prose, the shipped author adds it to ``prose_invariants`` with a
``## `` prefix. The result is a precise, low-false-positive substring check.

Consumers: the customize CLI (reject prose that drops a token), the runtime
apply path (fall back to shipped prose when a token is missing), and the upgrade
re-validation step (disable a now-stale override + warn).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from agentalloy.signals.prefilter import extract_gate_paths

_GLOB_CHARS = "*?[]"


def load_shipped_skill(skill_id: str) -> dict[str, Any] | None:
    """Load the shipped (product-default) skill YAML for ``skill_id``.

    Filename-stem match across the bundled ``_packs`` (shipped skills are named
    ``<skill_id>.yaml``). Returns ``None`` when the skill is not bundled — e.g.
    an override of a skill that was removed in an upgrade (an orphan, which the
    upgrade re-validation surfaces). Corpus/DB-free: reads the YAML directly.
    """
    if not skill_id:
        return None
    try:
        import yaml

        import agentalloy

        packs_root = Path(agentalloy.__file__).resolve().parent / "_packs"
        for f in packs_root.rglob(f"{skill_id}.yaml"):
            if f.name == "pack.yaml":
                continue
            data: Any = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return cast("dict[str, Any]", data)
    except Exception:
        return None
    return None


def _has_glob(segment: str) -> bool:
    return any(c in segment for c in _GLOB_CHARS)


def _normalize_gate_path(glob: str) -> str | None:
    """Reduce a gate ``path`` glob to the stable literal token it implies.

    Examples::

        docs/design/**/approach.md      -> "approach.md"     (concrete filename leaf)
        .agentalloy/contracts/build/*.md-> ".agentalloy/contracts/build/"  (dir prefix)
        src/**                          -> "src/"
        tasks.md                        -> "tasks.md"        (fully literal path)
        **/*.md                         -> None              (no literal anchor)

    Returns ``None`` when the glob has no literal anchor a human would write in
    prose (so it never becomes an always-missing invariant).
    """
    if not glob or not glob.strip():
        return None
    segs = [s for s in glob.split("/") if s]
    if not segs:
        return None

    # Leading run of glob-free segments = the literal prefix.
    prefix: list[str] = []
    for s in segs:
        if _has_glob(s):
            break
        prefix.append(s)

    if len(prefix) == len(segs):
        # Fully literal path (file or dir) — use it verbatim.
        return "/".join(prefix)

    leaf = segs[-1]
    if not _has_glob(leaf) and "." in leaf:
        # Concrete filename behind a wildcard dir (e.g. **/approach.md): the
        # filename is the token a human writes in prose.
        return leaf

    # Wildcard leaf (e.g. */*.md): the literal directory prefix is the token.
    if not prefix:
        return None
    return "/".join(prefix) + "/"


def derive_invariants(shipped_skill: dict[str, Any]) -> list[str]:
    """Load-bearing literal tokens a customized prose MUST retain.

    = path tokens derived from ``exit_gates`` + authored ``prose_invariants``.
    Order-preserving, de-duplicated, empties dropped. A skill with neither
    gates nor authored invariants yields ``[]`` (every check becomes a no-op).
    """
    tokens: list[str] = []
    for glob in extract_gate_paths(shipped_skill.get("exit_gates") or {}):
        tok = _normalize_gate_path(glob)
        if tok:
            tokens.append(tok)
    for tok in cast("list[Any]", shipped_skill.get("prose_invariants") or []):
        if isinstance(tok, str) and tok.strip():
            tokens.append(tok)

    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def check_prose(prose: str, invariants: list[str]) -> list[str]:
    """Return the invariants NOT present as an exact substring of ``prose``."""
    p = prose or ""
    return [inv for inv in invariants if inv not in p]


def overlay_prose(
    shipped: dict[str, Any],
    override_prose: str | None,
    override_domain_tags: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Effective skill = shipped structured fields + override prose.

    The override's prose is applied IFF it retains every load-bearing invariant;
    otherwise the shipped prose is kept (the runtime fall-back guard). Returns
    ``(effective_skill, missing_tokens)`` — ``missing_tokens`` is empty on
    success and names what was dropped when the override is rejected. Never
    mutates ``shipped``.
    """
    if override_prose is None:
        return shipped, []
    missing = check_prose(override_prose, derive_invariants(shipped))
    if missing:
        return shipped, missing
    eff = dict(shipped)
    eff["raw_prose"] = override_prose
    if override_domain_tags:
        eff["domain_tags"] = list(override_domain_tags)
    return eff, missing
