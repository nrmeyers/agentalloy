"""Corpus-wide retrieval self-audit.

For every bundled skill, generate mechanical probe queries and check whether
the live ``/compose`` endpoint retrieves that skill (hit@k). Catches
eligibility walls (phase->category exclusions like #97/#100), tag vocabulary
gaps, and embedding/BM25 outliers — systematically, instead of waiting for a
benchmark task to trip over one.

Two probes per skill:

* **name** (easy): the canonical_name phrased as a task. Contains the skill's
  own words by design — a miss here is a hard eligibility failure.
* **topic** (realistic): the opening prose sentences with every token of the
  skill_id and canonical_name removed, so the probe describes the subject
  without naming it. Misses here indicate ranking weakness.

Usage:

    uv run python -m eval.retrieval_audit [--k 4] [--limit N] [--packs a,b]

Requires the AgentAlloy service running on $AGENTALLOY_URL (default
http://localhost:47950). Read-only; makes no model calls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS_ROOT = REPO_ROOT / "src" / "agentalloy" / "_packs"
RUNS_ROOT = REPO_ROOT / "eval" / "runs"

_VALID_PHASES = {"spec", "design", "build", "qa", "ops"}


@dataclass
class SkillProbe:
    skill_id: str
    pack: str
    category: str
    phases: list[str]
    probes: dict[str, str]  # probe_type -> query text
    hits: dict[str, list[str]] = field(default_factory=dict)  # "type@phase" -> retrieved


def _strip_parenthetical(text: str) -> str:
    return re.sub(r"\s*[\(—–-].*$", "", text).strip()


def _name_probe(canonical_name: str) -> str:
    return f"How should I approach: {_strip_parenthetical(canonical_name)}?"


def _topic_probe(skill_id: str, canonical_name: str, raw_prose: str) -> str | None:
    """First sentences of prose with all name/id tokens removed."""
    body = re.sub(r"^#.*$", "", raw_prose, flags=re.MULTILINE)  # drop headings
    body = " ".join(body.split())
    sentences = re.split(r"(?<=[.!?]) ", body)
    snippet = " ".join(sentences[:2]).strip()
    if len(snippet) < 40:
        return None
    ban = {
        tok.lower()
        for tok in re.split(r"[\s\-_/()]+", f"{skill_id} {canonical_name}")
        if len(tok) > 2
    }
    kept = [w for w in snippet.split() if re.sub(r"\W", "", w).lower() not in ban]
    probe = " ".join(kept)
    return probe if len(probe) >= 40 else None


def _load_skills(packs_filter: list[str] | None) -> list[SkillProbe]:
    out: list[SkillProbe] = []
    for pack_dir in sorted(PACKS_ROOT.iterdir()):
        if not (pack_dir / "pack.yaml").is_file():
            continue
        if packs_filter and pack_dir.name not in packs_filter:
            continue
        for f in sorted(pack_dir.glob("*.yaml")):
            if f.name == "pack.yaml":
                continue
            doc: Any = yaml.safe_load(f.read_text())
            if not isinstance(doc, dict) or doc.get("skill_type") not in ("domain", None):
                continue  # system/workflow skills are signal-layer, not retrieval
            sid = doc.get("skill_id")
            name = doc.get("canonical_name") or sid
            prose = doc.get("raw_prose") or ""
            if not isinstance(sid, str):
                continue
            raw_phases = doc.get("phase_scope") or ["build"]
            phases = sorted({p if p in _VALID_PHASES else "build" for p in raw_phases})
            probes = {"name": _name_probe(str(name))}
            topic = _topic_probe(sid, str(name), str(prose))
            if topic:
                probes["topic"] = topic
            out.append(
                SkillProbe(
                    skill_id=sid,
                    pack=pack_dir.name,
                    category=str(doc.get("category") or "?"),
                    phases=phases,
                    probes=probes,
                )
            )
    return out


def run_audit(k: int, limit: int | None, packs_filter: list[str] | None) -> dict[str, Any]:
    skills = _load_skills(packs_filter)
    if limit:
        skills = skills[:limit]

    calls = 0
    with httpx.Client(timeout=60.0) as client:
        for sp in skills:
            for ptype, query in sp.probes.items():
                for phase in sp.phases:
                    resp = client.post(
                        f"{AGENTALLOY_URL}/compose",
                        json={"task": query, "phase": phase, "k": k},
                    )
                    resp.raise_for_status()
                    got = resp.json().get("source_skills", []) or []
                    sp.hits[f"{ptype}@{phase}"] = got
                    calls += 1
                    if calls % 50 == 0:
                        print(f"  ...{calls} probes done", file=sys.stderr)
                    time.sleep(0.05)

    def _agg(keyfn: Any) -> dict[str, dict[str, Any]]:
        groups: dict[str, list[bool]] = {}
        for sp in skills:
            for probe_key, got in sp.hits.items():
                groups.setdefault(keyfn(sp, probe_key), []).append(sp.skill_id in got)
        return {
            g: {"hit_rate": sum(v) / len(v), "n": len(v)}
            for g, v in sorted(groups.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        }

    stranded = [
        sp.skill_id
        for sp in skills
        if sp.hits and not any(sp.skill_id in g for g in sp.hits.values())
    ]
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "k": k,
        "skills_audited": len(skills),
        "by_probe_type": _agg(lambda sp, pk: pk.split("@")[0]),
        "by_phase": _agg(lambda sp, pk: pk.split("@")[1]),
        "by_pack": _agg(lambda sp, pk: sp.pack),
        "by_category": _agg(lambda sp, pk: sp.category),
        "stranded_skills": stranded,
        "per_skill": [
            {
                "skill_id": sp.skill_id,
                "pack": sp.pack,
                "category": sp.category,
                "results": {pk: (sp.skill_id in got) for pk, got in sp.hits.items()},
                "retrieved": sp.hits,
            }
            for sp in skills
        ],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corpus-wide retrieval self-audit")
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--packs", type=str, default=None, help="comma-separated pack filter")
    args = parser.parse_args(argv)

    packs_filter = args.packs.split(",") if args.packs else None
    report = run_audit(args.k, args.limit, packs_filter)

    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_path = RUNS_ROOT / f"retrieval-audit-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2))

    print(f"\n=== Retrieval audit (k={report['k']}, {report['skills_audited']} skills) ===")
    for section in ("by_probe_type", "by_phase", "by_category"):
        print(f"\n{section}:")
        for g, stats in report[section].items():
            print(f"  {g:24s} hit_rate={stats['hit_rate']:.2f}  n={stats['n']}")
    print("\nworst packs:")
    for g, stats in list(report["by_pack"].items())[:10]:
        print(f"  {g:24s} hit_rate={stats['hit_rate']:.2f}  n={stats['n']}")
    print(f"\nstranded skills (0 hits anywhere): {len(report['stranded_skills'])}")
    for sid in report["stranded_skills"]:
        print(f"  {sid}")
    print(f"\nwrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
