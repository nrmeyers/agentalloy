#!/usr/bin/env python
"""Recall benchmark for the v2 code index (live service, read-only).

Answers the question "is the code index actually good?" with numbers:

Part A — intent-style queries auto-derived from symbol docstrings
(query = docstring first sentence, ground truth = the symbol's fqn).
Part B — hand-written realistic developer queries.

Design constraints:
- Every search goes through the running service's ``POST /tool``
  ``code_search`` — the exact production retrieval path (hybrid dense +
  PageRank + BM25/RRF). This script never opens a second handle on the
  shared OverGraph index.
- Ground truth comes from an OFFLINE re-parse with the same vendored
  tree-sitter engine (``code_index.facade.parse_repo``) into a throwaway
  cache dir — no DB access, and fqn identity is guaranteed because it is
  the same parser that built the index.
- Symbols the live index no longer contains (repo drift since the last
  ingest) are checked via the ``symbols`` tool and excluded from the
  denominator; they are reported separately as "drift".

Metrics (symbol-level): recall@5, recall@10, MRR. Secondary:
file-level recall@10.

Usage:
  uv run python scripts/codeindex_recall_benchmark.py            # full run
  uv run python scripts/codeindex_recall_benchmark.py --survey   # dump Part A candidates
  uv run python scripts/codeindex_recall_benchmark.py --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_URL = os.environ.get("AGENTALLOY_SERVICE_URL", "http://127.0.0.1:48950")
K = 10  # hits fetched per query; recall@5/@10 both scored from this
PER_REPO_CAP = 40  # Part A candidates kept per repo


# ---------------------------------------------------------------------------
# Service calls (production path: service POST /tool)
# ---------------------------------------------------------------------------


def tool_call(name: str, args: dict) -> dict:
    """POST one tool execution to the live service. ``args`` is a JSON
    STRING on the wire (ToolRequest contract)."""
    body = json.dumps({"name": name, "args": json.dumps(args)}).encode()
    req = urllib.request.Request(
        f"{SERVICE_URL}/tool",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        envelope = json.loads(resp.read())
    if not envelope.get("ok"):
        raise RuntimeError(f"tool {name} failed: {envelope.get('error')}")
    return json.loads(envelope["result"])


def code_search(query: str) -> list[dict]:
    return tool_call("code_search", {"query": query, "k": K})["results"]


def fqn_in_index(fqn: str) -> bool:
    """Exact-fqn presence via the substring-matching symbols tool."""
    out = tool_call("symbols", {"fqn": fqn})
    return any(s.get("fqn") == fqn for s in out.get("symbols", []))


# ---------------------------------------------------------------------------
# Offline ground truth (same engine as ingest, no DB access)
# ---------------------------------------------------------------------------


def load_repos() -> list[Path]:
    raw = json.loads((REPO_ROOT / "repos.json").read_text())
    return [Path(r).expanduser().resolve() for r in raw]


@dataclass
class Candidate:
    part: str  # "A" or "B"
    qid: str
    query: str
    fqn: str
    file: str  # absolute POSIX path
    kind: str = ""
    repo: str = ""
    note: str = ""


def first_sentence(docstring: str) -> str:
    line = docstring.strip().splitlines()[0].strip()
    return re.split(r"(?<=\.)\s+", line, maxsplit=1)[0].strip()


def _is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    return any(p == "tests" or p.startswith("test_") or p.startswith("test-") for p in parts)


def _kind_is_searchable(kind: str) -> bool:
    return bool(re.search(r"funct|method|class", kind, re.IGNORECASE))


def build_part_a(candidates_by_repo: dict[str, list[Candidate]]) -> list[Candidate]:
    """Deterministic sample: sha256 order of the sentence, per-repo cap."""
    picked: dict[str, int] = {}
    seen: set[str] = set()
    ordered = sorted(
        (c for cands in candidates_by_repo.values() for c in cands),
        key=lambda c: hashlib.sha256(c.query.lower().encode()).hexdigest(),
    )
    out: list[Candidate] = []
    for c in ordered:
        key = c.query.lower()
        if key in seen:
            continue
        if picked.get(c.repo, 0) >= PER_REPO_CAP:
            continue
        seen.add(key)
        picked[c.repo] = picked.get(c.repo, 0) + 1
        out.append(c)
    return out


def parse_offline(repos: list[Path], cache: Path) -> tuple[dict[str, list[Candidate]], set[str]]:
    """Re-parse every registered repo; return (Part A candidates per repo,
    full fqn set for Part B target verification)."""
    from newagent.code_index.facade import parse_repo

    candidates: dict[str, list[Candidate]] = {}
    all_fqns: set[str] = set()
    for repo in repos:
        result = parse_repo(repo, cache_dir=cache)
        all_fqns.update(ps.qualified_name for ps in result.symbols)
        cands: list[Candidate] = []
        for ps in result.symbols:
            if ps.docstring is None or ps.file_path is None or ps.name is None:
                continue
            if not _kind_is_searchable(ps.kind) or ps.name.startswith("_"):
                continue
            if _is_test_path(ps.file_path):
                continue
            sentence = first_sentence(ps.docstring)
            words = sentence.split()
            if not (4 <= len(words) <= 14) or len(sentence) > 100:
                continue
            if sentence.lower().rstrip(".") == ps.name.lower():
                continue
            cands.append(
                Candidate(
                    part="A",
                    qid="",
                    query=sentence,
                    fqn=ps.qualified_name,
                    file=(repo / ps.file_path).as_posix(),
                    kind=ps.kind,
                    repo=repo.name,
                )
            )
        candidates[repo.name] = cands
        print(
            f"[survey] {repo.name}: {len(result.symbols)} symbols parsed, "
            f"{len(cands)} Part A candidates",
            file=sys.stderr,
        )
    return candidates, all_fqns


# ---------------------------------------------------------------------------
# Part B — hand-written realistic queries (targets verified against the
# offline parse at build time; a typo fails the run before any live call)
# ---------------------------------------------------------------------------

PART_B: list[dict] = [
    # TheForge (TypeScript) — stable symbols from the 2026-09-08 offline parse.
    {
        "query": "derive the websocket URL from the API base URL",
        "fqn": "TheForge.src.cli.api-client.deriveWsUrl",
        "file": "/home/nmeyers/dev/TheForge/src/cli/api-client.ts",
    },
    {
        "query": "exponential backoff delay between retries",
        "fqn": "TheForge.src.cli.api-client.backoffMs",
        "file": "/home/nmeyers/dev/TheForge/src/cli/api-client.ts",
    },
    {
        "query": "decide whether a failed request is worth retrying",
        "fqn": "TheForge.src.cli.api-client.shouldRetry",
        "file": "/home/nmeyers/dev/TheForge/src/cli/api-client.ts",
    },
    {
        "query": "detect which forge mode the repository is in",
        "fqn": "TheForge.src.cli.mode-detect.detectForgeMode",
        "file": "/home/nmeyers/dev/TheForge/src/cli/mode-detect.ts",
    },
    {
        "query": "evict the oldest events when the activity feed exceeds its cap",
        "fqn": "TheForge.src.services.activity-feed.createActivityFeed.evictOldestIfOverCap",
        "file": "/home/nmeyers/dev/TheForge/src/services/activity-feed.ts",
    },
    {
        "query": "create the in-memory agent session store",
        "fqn": "TheForge.src.services.agent-session-store.createAgentSessionStore",
        "file": "/home/nmeyers/dev/TheForge/src/services/agent-session-store.ts",
    },
    {
        "query": "build the file tree shown to the agent",
        "fqn": "TheForge.src.services.agent-loop.buildFileTree",
        "file": "/home/nmeyers/dev/TheForge/src/services/agent-loop.ts",
    },
    {
        "query": "execute a tool call inside the agent loop",
        "fqn": "TheForge.src.services.agent-loop.executeTool",
        "file": "/home/nmeyers/dev/TheForge/src/services/agent-loop.ts",
    },
    {
        "query": "resolve the CLI output format from TTY and flags",
        "fqn": "TheForge.src.cli.output.format.resolveFormat",
        "file": "/home/nmeyers/dev/TheForge/src/cli/output/format.ts",
    },
    {
        "query": "github personal access token adapter",
        "fqn": "TheForge.src.adapters.github.pat.createGitHubPatAdapter",
        "file": "/home/nmeyers/dev/TheForge/src/adapters/github/pat.ts",
    },
    {
        "query": "compute the cache key for an LLM request",
        "fqn": "TheForge.src.adapters.llm.types.cacheKeyFromRequest",
        "file": "/home/nmeyers/dev/TheForge/src/adapters/llm/types.ts",
    },
    {
        "query": "chain multiple identity providers together",
        "fqn": "TheForge.src.adapters.identity.index.createChainedIdentityProvider",
        "file": "/home/nmeyers/dev/TheForge/src/adapters/identity/index.ts",
    },
    # newagent (Python)
    {
        "query": "persist a new upstream URL into the instance env file",
        "fqn": "newagent.src.newagent.instance_env.update_env_vars",
        "file": "/home/nmeyers/dev/newagent/src/newagent/instance_env.py",
    },
    {
        "query": "import the full v1 skill corpus",
        "fqn": "newagent.src.newagent.corpus_importer.import_corpus",
        "file": "/home/nmeyers/dev/newagent/src/newagent/corpus_importer.py",
    },
    {
        "query": "recompute PageRank from the call graph edges",
        "fqn": "newagent.src.newagent.code_index.store.pagerank.refresh_centrality",
        "file": "/home/nmeyers/dev/newagent/src/newagent/code_index/store/pagerank.py",
    },
    {
        "query": "check whether a phase transition gate is satisfied",
        "fqn": "newagent.src.newagent.phase_machine.PhaseMachine.check_gate",
        "file": "/home/nmeyers/dev/newagent/src/newagent/phase_machine.py",
    },
    {
        "query": "promote a captured QA lesson into an injected skill",
        "fqn": "newagent.src.newagent.compound.CompoundEngine.promote_lesson",
        "file": "/home/nmeyers/dev/newagent/src/newagent/compound.py",
    },
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def rank_of(hits: list[dict], key: str, value: str) -> int:
    """1-based rank of the first hit matching key==value, else 0."""
    for i, h in enumerate(hits, start=1):
        if h.get(key) == value:
            return i
    return 0


@dataclass
class QueryResult:
    cand: Candidate
    symbol_rank: int
    file_rank: int
    in_index: bool
    top_hits: list[str]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(results: list[QueryResult], label: str) -> dict:
    scored = [r for r in results if r.in_index]
    n = len(scored)
    if n == 0:
        return {"label": label, "n": 0, "drift": len(results)}
    r5 = sum(1 for r in scored if 0 < r.symbol_rank <= 5)
    r10 = sum(1 for r in scored if 0 < r.symbol_rank <= 10)
    mrr = sum(1.0 / r.symbol_rank for r in scored if r.symbol_rank) / n
    fr10 = sum(1 for r in scored if 0 < r.file_rank <= 10) / n
    return {
        "label": label,
        "n": n,
        "drift": len(results) - n,
        "recall@5": round(r5 / n, 3),
        "recall@10": round(r10 / n, 3),
        "mrr": round(mrr, 3),
        "file_recall@10": round(fr10, 3),
    }


def report(results: list[QueryResult], json_path: str | None) -> int:
    part_a = summarize([r for r in results if r.cand.part == "A"], "Part A (docstring intent)")
    part_b = summarize([r for r in results if r.cand.part == "B"], "Part B (hand-written)")
    overall = summarize(results, "OVERALL")

    print("\n" + "=" * 74)
    print(f"Code index recall benchmark — service {SERVICE_URL}, k={K}")
    print("=" * 74)
    for s in (part_a, part_b, overall):
        if s["n"] == 0:
            continue
        print(
            f"{s['label']:<28} n={s['n']:<3} drift={s['drift']:<2} "
            f"recall@5={s['recall@5']:.3f} recall@10={s['recall@10']:.3f} "
            f"MRR={s['mrr']:.3f} file@10={s['file_recall@10']:.3f}"
        )

    print("\nMisses (target not in top-10):")
    misses = [r for r in results if r.in_index and r.symbol_rank == 0]
    for r in misses:
        c = r.cand
        print(f"  {c.part}/{c.qid:<4} {c.query[:70]}")
        print(f"           want {c.fqn}")
        for h in r.top_hits:
            print(f"           got  {h}")
    if not misses:
        print("  (none)")

    if json_path:
        payload = {
            "summary": [part_a, part_b, overall],
            "results": [
                {
                    "part": r.cand.part,
                    "qid": r.cand.qid,
                    "query": r.cand.query,
                    "target": r.cand.fqn,
                    "symbol_rank": r.symbol_rank,
                    "file_rank": r.file_rank,
                    "in_index": r.in_index,
                }
                for r in results
            ],
        }
        Path(json_path).write_text(json.dumps(payload, indent=2))
        print(f"\nJSON results written to {json_path}")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--survey", action="store_true", help="dump Part A candidates and exit")
    ap.add_argument("--json", metavar="PATH", help="also write full results as JSON")
    args = ap.parse_args()

    repos = load_repos()
    print(f"[setup] repos: {', '.join(str(r) for r in repos)}", file=sys.stderr)
    cache = Path(tempfile.mkdtemp(prefix="cgr-bench-"))
    try:
        candidates_by_repo, all_fqns = parse_offline(repos, cache)
        if args.survey:
            for repo, cands in candidates_by_repo.items():
                print(f"\n=== {repo}: {len(cands)} candidates ===")
                for c in sorted(cands, key=lambda c: c.fqn)[:200]:
                    print(f"  {c.fqn} [{c.kind}] {c.query[:80]}")
            return 0

        part_a = build_part_a(candidates_by_repo)

        # Part B: verify every target fqn exists in the offline parse.
        part_b: list[Candidate] = []
        for i, spec in enumerate(PART_B, start=1):
            fqn = spec["fqn"]
            if fqn not in all_fqns:
                raise SystemExit(f"Part B target not found in offline parse: {fqn}")
            part_b.append(
                Candidate(
                    part="B",
                    qid=f"b-{i:02d}",
                    query=spec["query"],
                    fqn=fqn,
                    file=spec["file"],
                    repo=spec.get("repo", ""),
                    note=spec.get("note", ""),
                )
            )

        cands = part_a + part_b
        # Drift check + search for every candidate.
        print(f"\n[run] {len(cands)} queries (A={len(part_a)}, B={len(part_b)})", file=sys.stderr)
        results: list[QueryResult] = []
        total = len(cands)
        for i, c in enumerate(cands, start=1):
            if not fqn_in_index(c.fqn):
                print(f"[{i}/{total}] {c.part}/{c.qid:<4} DRIFT  {c.query[:60]}", file=sys.stderr)
                results.append(QueryResult(c, 0, 0, False, []))
                continue
            hits = code_search(c.query)
            r = QueryResult(
                c,
                rank_of(hits, "qualified_name", c.fqn),
                rank_of(hits, "file", c.file),
                True,
                [f"{h.get('qualified_name')} ({h.get('repo', '')})" for h in hits[:3]],
            )
            results.append(r)
            mark = f"rank {r.symbol_rank}" if r.symbol_rank else "MISS"
            print(f"[{i}/{total}] {c.part}/{c.qid:<4} {mark:<7} {c.query[:60]}", file=sys.stderr)

        return report(results, args.json)
    finally:
        shutil.rmtree(cache, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
