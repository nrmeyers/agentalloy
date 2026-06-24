#!/usr/bin/env python3
"""Propose skill-to-skill graph edges for the pack corpus (one-off tooling).

For every pack skill (``src/agentalloy/_packs/*/<skill>.yaml``) this script:

1. Builds a *skill card* (canonical_name + domain_tags + description) from the
   pack YAML — the same trio Stage 0 indexes.
2. Pulls that skill's top-N embedding neighbors. Card embeddings (one 768-dim
   vector per skill, ``fragment_type='card'``) live in the DuckDB corpus; we
   copy the DB file and open the copy ``read_only`` so a running service is
   never disturbed and no lock is contended.
3. Asks an OpenAI-compatible reasoning model (qwen3.6-27b) — **sequentially**,
   one call at a time so layers never spill to CPU — to classify each
   (skill, neighbor) pair as ``requires`` / ``related`` / ``none`` given both
   cards, demanding a one-line reason.
4. Caps each skill at ≤2 ``requires`` and ≤3 ``related`` (highest-confidence
   first), writes the accepted edges back into the pack YAMLs, logs every
   accepted edge + reason to a sidecar JSONL for review, and patch-bumps the
   ``version`` of every touched ``pack.yaml`` (propagation requires a bump).

Thinking is pinned OFF via ``chat_template_kwargs={"enable_thinking": false}``
(reasoning_effort is silently ignored by this server).

Usage::

    /home/nmeyers/scripts/load-model.sh 27B        # load the model first
    uv run python scripts/generate-skill-edges.py [--dry-run] [--limit N]

The proposed edges are LM-generated content and are expected to be reviewed
before shipping. ``--dry-run`` classifies and logs the sidecar but writes
nothing to the YAMLs or pack versions.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import httpx
import yaml

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = REPO_ROOT / "src" / "agentalloy" / "_packs"
CORPUS_DUCK = Path.home() / ".local" / "share" / "agentalloy" / "corpus" / "skills.duck"
SIDECAR_PATH = REPO_ROOT / "scripts" / "skill-edges-reasons.jsonl"

LM_BASE_URL = "http://192.168.4.26:60000"
LM_MODEL = "qwen3.6-27b"  # served id; resolved from /v1/models at startup
TOP_N_NEIGHBORS = 8
MAX_REQUIRES = 2
MAX_RELATED = 3
CARD_FRAGMENT_TYPE = "card"

SYSTEM_PROMPT = (
    "You classify the relationship between two software-engineering skills for a "
    "skill-graph used by a coding agent. You are given SKILL A and a NEIGHBOR "
    "(each as name + tags + description). Decide A's relationship TO the neighbor:\n"
    "- requires: A is a hard dependency on the neighbor — A's instructions only "
    "make sense if the neighbor's setup/concepts are already in place "
    "(e.g. 'FastAPI dependency injection' requires 'FastAPI app setup'). Use "
    "sparingly; this is a strong claim.\n"
    "- related: the neighbor is a useful conceptual companion an agent would "
    "benefit from seeing alongside A, but A does not depend on it.\n"
    "- none: unrelated, or merely same-domain with no real link.\n"
    "Respond with STRICT JSON only: "
    '{"relation": "requires|related|none", "confidence": 0.0-1.0, '
    '"reason": "<one concise line>"}. No prose outside the JSON.'
)


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


@dataclass
class SkillCard:
    skill_id: str
    canonical_name: str
    domain_tags: list[str]
    description: str
    pack: str
    yaml_path: Path

    def render(self) -> str:
        tags = ", ".join(self.domain_tags) if self.domain_tags else "(none)"
        desc = self.description or "(no description)"
        return f"name: {self.canonical_name}\ntags: {tags}\ndescription: {desc}"


@dataclass
class ProposedEdge:
    source_id: str
    target_id: str
    relation: str  # "requires" | "related"
    confidence: float
    reason: str


@dataclass
class PackEdits:
    """Accumulated edge edits per skill, grouped by pack for version bumping."""

    by_skill: dict[str, list[ProposedEdge]] = field(default_factory=dict)
    touched_packs: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------
# pack loading
# --------------------------------------------------------------------------


def load_cards() -> dict[str, SkillCard]:
    """Read every pack skill YAML into a SkillCard keyed by skill_id."""
    cards: dict[str, SkillCard] = {}
    for pack_dir in sorted(p for p in PACKS_DIR.iterdir() if p.is_dir()):
        manifest_path = pack_dir / "pack.yaml"
        if not manifest_path.is_file():
            continue
        for yaml_path in sorted(pack_dir.glob("*.yaml")):
            if yaml_path.name == "pack.yaml":
                continue
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            skill_id = str(data.get("skill_id", "")).strip()
            if not skill_id:
                continue
            tags_raw = data.get("domain_tags") or []
            tags = [str(t).strip() for t in tags_raw if t] if isinstance(tags_raw, list) else []
            cards[skill_id] = SkillCard(
                skill_id=skill_id,
                canonical_name=str(data.get("canonical_name", skill_id)).strip(),
                domain_tags=tags,
                description=str(data.get("description", "")).strip(),
                pack=pack_dir.name,
                yaml_path=yaml_path,
            )
    return cards


# --------------------------------------------------------------------------
# embedding neighbors (DuckDB card vectors, read from a copy)
# --------------------------------------------------------------------------


def neighbor_map(skill_ids: set[str]) -> dict[str, list[str]]:
    """Return ``{skill_id: [neighbor_id, ...]}`` (top-N by card-vector cosine).

    Copies the corpus DuckDB and opens the copy ``read_only`` so a running
    service is never blocked. Only skills with a card embedding participate."""
    if not CORPUS_DUCK.is_file():
        print(f"error: corpus not found at {CORPUS_DUCK}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="skill-edges-") as tmp:
        copy_path = Path(tmp) / "corpus.duck"
        shutil.copyfile(CORPUS_DUCK, copy_path)
        conn = duckdb.connect(str(copy_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT skill_id, embedding FROM fragment_embeddings WHERE fragment_type = ?",
                [CARD_FRAGMENT_TYPE],
            ).fetchall()
        finally:
            conn.close()

    vectors: dict[str, list[float]] = {}
    for sid, emb in rows:
        sid = str(sid)
        if sid in skill_ids and emb is not None:
            vectors[sid] = [float(x) for x in emb]

    # Card embeddings are L2-normalized at write time, so dot == cosine.
    ids = list(vectors.keys())
    out: dict[str, list[str]] = {}
    for sid in ids:
        v = vectors[sid]
        scored: list[tuple[float, str]] = []
        for other in ids:
            if other == sid:
                continue
            ov = vectors[other]
            dot = sum(a * b for a, b in zip(v, ov, strict=True))
            scored.append((dot, other))
        scored.sort(reverse=True)
        out[sid] = [oid for _, oid in scored[:TOP_N_NEIGHBORS]]
    return out


# --------------------------------------------------------------------------
# LM classification (sequential, thinking off)
# --------------------------------------------------------------------------


def resolve_model_id(client: httpx.Client) -> str:
    try:
        resp = client.get("/v1/models", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        served = data["data"][0]["id"]
        print(f"using model id: {served}")
        return str(served)
    except Exception as exc:  # noqa: BLE001 — startup probe
        print(f"warning: could not resolve model id ({exc}); using {LM_MODEL!r}", file=sys.stderr)
        return LM_MODEL


def classify_pair(
    client: httpx.Client, model: str, a: SkillCard, neighbor: SkillCard
) -> tuple[str, float, str]:
    """One sequential chat call. Returns (relation, confidence, reason)."""
    user = f"SKILL A:\n{a.render()}\n\nNEIGHBOR ({neighbor.skill_id}):\n{neighbor.render()}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
        "stream": False,
        # Thinking pinned OFF — reasoning_effort is silently ignored by this server.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = client.post("/v1/chat/completions", json=payload, timeout=120)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return parse_verdict(content)


def parse_verdict(content: str) -> tuple[str, float, str]:
    text = content.strip()
    # Tolerate code fences / stray prose around the JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return "none", 0.0, f"unparseable: {text[:80]}"
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return "none", 0.0, f"unparseable: {text[:80]}"
    relation = str(obj.get("relation", "none")).strip().lower()
    if relation not in ("requires", "related", "none"):
        relation = "none"
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(obj.get("reason", "")).strip()
    return relation, confidence, reason


# --------------------------------------------------------------------------
# YAML writing + version bumping
# --------------------------------------------------------------------------


def patch_bump(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return version  # leave non-semver versions untouched
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def _render_edge_block(field_name: str, targets: list[str]) -> list[str]:
    lines = [f"{field_name}:"]
    lines.extend(f"- {t}" for t in targets)
    return lines


def _strip_existing_edge_block(lines: list[str], field_name: str) -> list[str]:
    """Remove an existing top-level ``requires:``/``related:`` block (key + its
    ``- item`` continuation lines), leaving everything else byte-for-byte."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.rstrip() == f"{field_name}:" or line.startswith(f"{field_name}: "):
            i += 1
            # Drop the block's list items (either indentation style).
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                i += 1
            continue
        out.append(line)
        i += 1
    return out


def write_edges_into_yaml(card: SkillCard, edges: list[ProposedEdge]) -> None:
    """Surgically insert/replace ``requires`` and ``related`` block lists.

    Operates on raw text — the literal ``raw_prose: |-`` block, quoting, key
    order, and comments are preserved verbatim. New blocks are inserted right
    after the ``domain_tags`` block (the natural metadata neighborhood);
    existing blocks are replaced in place."""
    requires = sorted({e.target_id for e in edges if e.relation == "requires"})
    related = sorted({e.target_id for e in edges if e.relation == "related"})

    text = card.yaml_path.read_text(encoding="utf-8")
    trailing_nl = text.endswith("\n")
    lines = text.splitlines()

    # Drop any prior blocks so re-runs are idempotent.
    lines = _strip_existing_edge_block(lines, "requires")
    lines = _strip_existing_edge_block(lines, "related")

    new_block: list[str] = []
    if requires:
        new_block += _render_edge_block("requires", requires)
    if related:
        new_block += _render_edge_block("related", related)
    if not new_block:
        card.yaml_path.write_text("\n".join(lines) + ("\n" if trailing_nl else ""))
        return

    # Insert after the domain_tags block (key + its `- ` items). Fall back to
    # inserting before raw_prose, else at end-of-front-matter.
    insert_at = _domain_tags_end(lines)
    if insert_at is None:
        insert_at = next(
            (idx for idx, ln in enumerate(lines) if ln.startswith("raw_prose")), len(lines)
        )
    merged = lines[:insert_at] + new_block + lines[insert_at:]
    card.yaml_path.write_text("\n".join(merged) + ("\n" if trailing_nl else ""))


def _domain_tags_end(lines: list[str]) -> int | None:
    """Index just past the ``domain_tags:`` block, or None if absent.

    Handles both list-item indentation styles found in the packs: column-0
    (``- tag``) and indented (``  - tag``)."""
    for idx, ln in enumerate(lines):
        if ln.rstrip() == "domain_tags:" or ln.startswith("domain_tags: "):
            j = idx + 1
            while j < len(lines) and lines[j].lstrip().startswith("- "):
                j += 1
            return j
    return None


def bump_pack_versions(packs: set[str]) -> dict[str, tuple[str, str]]:
    """Patch-bump ``version:`` in each touched pack.yaml (surgical line edit).

    Replaces only the version line so the manifest's formatting/comments and the
    literal ``description: |`` block are preserved. Returns {pack: (old, new)}."""
    bumps: dict[str, tuple[str, str]] = {}
    for pack in sorted(packs):
        manifest_path = PACKS_DIR / pack / "pack.yaml"
        text = manifest_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        old = new = ""
        for idx, ln in enumerate(lines):
            if ln.startswith("version:"):
                old = ln.split(":", 1)[1].strip()
                new = patch_bump(old)
                lines[idx] = f"version: {new}"
                break
        if new and new != old:
            manifest_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))
        bumps[pack] = (old, new)
    return bumps


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Propose skill-to-skill graph edges.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify + write the reason sidecar but do not edit YAMLs or pack versions.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N source skills (smoke test). 0 = all.",
    )
    parser.add_argument(
        "--apply-sidecar",
        action="store_true",
        help=(
            "Skip classification entirely: re-apply the edges recorded in the "
            "existing reasons sidecar to the pack YAMLs (+ version bumps). "
            "Useful after fixing the YAML writer without re-running the LM."
        ),
    )
    args = parser.parse_args(argv)

    cards = load_cards()
    print(f"loaded {len(cards)} pack skill cards")

    if args.apply_sidecar:
        return _apply_sidecar(cards)
    neighbors = neighbor_map(set(cards.keys()))
    print(f"{len(neighbors)} skills have a card embedding (neighbor search basis)")

    source_ids = sorted(neighbors.keys())
    if args.limit > 0:
        source_ids = source_ids[: args.limit]

    edits = PackEdits()
    sidecar_lines: list[str] = []
    total_pairs = sum(len(neighbors[s]) for s in source_ids)
    print(f"classifying {total_pairs} (skill, neighbor) pairs sequentially …")

    client = httpx.Client(base_url=LM_BASE_URL)
    pair_no = 0
    accepted_requires = 0
    accepted_related = 0
    try:
        model = resolve_model_id(client)
        for sid in source_ids:
            card = cards[sid]
            proposals: list[ProposedEdge] = []
            for nid in neighbors[sid]:
                neighbor = cards.get(nid)
                if neighbor is None:
                    continue
                pair_no += 1
                try:
                    relation, confidence, reason = classify_pair(client, model, card, neighbor)
                except Exception as exc:  # noqa: BLE001 — log + continue, never abort the run
                    print(
                        f"  [{pair_no}/{total_pairs}] {sid} -> {nid}: ERROR {exc}",
                        file=sys.stderr,
                    )
                    continue
                if relation != "none" and reason:
                    proposals.append(ProposedEdge(sid, nid, relation, confidence, reason))
                print(f"  [{pair_no}/{total_pairs}] {sid} -> {nid}: {relation} ({confidence:.2f})")

            # Cap: highest-confidence first, ≤2 requires and ≤3 related.
            req = sorted(
                (p for p in proposals if p.relation == "requires"),
                key=lambda p: p.confidence,
                reverse=True,
            )[:MAX_REQUIRES]
            rel = sorted(
                (p for p in proposals if p.relation == "related"),
                key=lambda p: p.confidence,
                reverse=True,
            )[:MAX_RELATED]
            kept = req + rel
            if kept:
                edits.by_skill[sid] = kept
                edits.touched_packs.add(card.pack)
                accepted_requires += len(req)
                accepted_related += len(rel)
                for e in kept:
                    sidecar_lines.append(
                        json.dumps(
                            {
                                "source": e.source_id,
                                "target": e.target_id,
                                "relation": e.relation,
                                "confidence": round(e.confidence, 3),
                                "reason": e.reason,
                            }
                        )
                    )
    finally:
        client.close()

    SIDECAR_PATH.write_text("\n".join(sidecar_lines) + ("\n" if sidecar_lines else ""))
    print(f"\nwrote {len(sidecar_lines)} edge reasons to {SIDECAR_PATH}")
    print(f"accepted edges: {accepted_requires} requires, {accepted_related} related")

    if args.dry_run:
        print("dry-run: no YAML or pack.yaml changes written.")
        return 0

    for sid, kept in edits.by_skill.items():
        write_edges_into_yaml(cards[sid], kept)
    bumps = bump_pack_versions(edits.touched_packs)
    print(f"edited {len(edits.by_skill)} skill YAMLs across {len(bumps)} packs")
    for pack, (old, new) in bumps.items():
        print(f"  {pack}: {old} -> {new}")
    return 0


def _apply_sidecar(cards: dict[str, SkillCard]) -> int:
    """Re-apply the sidecar's accepted edges to the YAMLs without LM calls."""
    if not SIDECAR_PATH.is_file():
        print(f"error: sidecar not found at {SIDECAR_PATH}", file=sys.stderr)
        return 1
    edits = PackEdits()
    n_req = n_rel = 0
    for line in SIDECAR_PATH.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        edge = ProposedEdge(
            source_id=str(obj["source"]),
            target_id=str(obj["target"]),
            relation=str(obj["relation"]),
            confidence=float(obj.get("confidence", 0.0)),
            reason=str(obj.get("reason", "")),
        )
        card = cards.get(edge.source_id)
        if card is None:
            print(f"warning: sidecar source {edge.source_id} not in packs", file=sys.stderr)
            continue
        edits.by_skill.setdefault(edge.source_id, []).append(edge)
        edits.touched_packs.add(card.pack)
        if edge.relation == "requires":
            n_req += 1
        else:
            n_rel += 1

    for sid, kept in edits.by_skill.items():
        write_edges_into_yaml(cards[sid], kept)
    bumps = bump_pack_versions(edits.touched_packs)
    print(f"re-applied {n_req} requires + {n_rel} related edges from sidecar")
    print(f"edited {len(edits.by_skill)} skill YAMLs across {len(bumps)} packs")
    for pack, (old, new) in bumps.items():
        print(f"  {pack}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
