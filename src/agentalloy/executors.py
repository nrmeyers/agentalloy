"""Tool executors: dispatch (tool, args) → code graph index / stores → observation.

State executors use the real DuckDB StateStore when injected via set_store(),
fall back to in-memory fake for testing without a store.

READ executors use the code graph index (OverGraph + tantivy) when wired via
set_graph_index(), falling back to a Python-side text walk for testing
without the index.
"""

import json
import logging
import threading
from typing import Any

from agentalloy.skill_engine import SkillEngine
from agentalloy.state_store import LIFECYCLE_START, PHASE_ORDER, PhaseAdvanceError, StateStore

logger = logging.getLogger(__name__)

# Module-level store reference — set by interpreter via set_store()
_store: StateStore | None = None

# Fallback in-memory store for when no real store is injected
_fallback: dict[str, Any] = {
    "contracts": {},
    "artifacts": {},
    "phases": {"current": LIFECYCLE_START},
}


def set_store(store: StateStore | None) -> None:
    """Inject the real DuckDB StateStore. Call before interpreter.run()."""
    global _store
    _store = store


# Per-run store override, thread-local: concurrent composes for different
# projects run on separate threadpool threads, and each must see its own
# project-scoped view — a shared global would cross-write scopes.
_run_store = threading.local()


def set_run_store(store: StateStore | None) -> None:
    """Bind a (possibly project-scoped) store to the current thread's run.
    Pass None to clear. Falls back to the global set_store() default."""
    _run_store.store = store


def _current_store() -> StateStore | None:
    store = getattr(_run_store, "store", None)
    return store if store is not None else _store


# Graph index (OverGraph + tantivy) — set at startup via set_graph_index()
_graph_store: Any = None
_searcher: Any = None


def set_graph_index(store: Any, searcher: Any) -> None:
    """Inject the shared code graph store + hybrid searcher. Call at startup."""
    global _graph_store, _searcher
    _graph_store = store
    _searcher = searcher


# Skill engine (loaded corpus) — set at startup via set_skill_engine()
_skill_engine: SkillEngine | None = None


def set_skill_engine(engine: SkillEngine | None) -> None:
    """Inject the loaded skill engine. Call at startup."""
    global _skill_engine
    _skill_engine = engine


# Telemetry store — set at startup via set_telemetry()
_telemetry_store: Any = None


def set_telemetry(store: Any) -> None:
    """Inject the telemetry store so the `telemetry` tool serves real traces."""
    global _telemetry_store
    _telemetry_store = store


def execute_tool(tool_name: str, args: str) -> Any:
    """Execute a tool call and return the observation."""
    parsed_args = json.loads(args) if isinstance(args, str) else args

    # READ tools
    if tool_name == "code_search":
        return _code_search(parsed_args)
    elif tool_name == "symbols":
        return _symbols(parsed_args)
    elif tool_name == "graph_query":
        return _graph_query(parsed_args)
    elif tool_name == "knowledge_why":
        return _knowledge_why(parsed_args)
    elif tool_name == "knowledge_related":
        return _knowledge_related(parsed_args)
    elif tool_name == "knowledge_entities":
        return _knowledge_entities(parsed_args)
    elif tool_name == "artifact_body":
        return _artifact_body(parsed_args)
    elif tool_name == "contract_detail":
        return _contract_detail(parsed_args)
    elif tool_name == "telemetry":
        return _telemetry(parsed_args)
    elif tool_name == "get_skill_for":
        return _get_skill_for(parsed_args)
    elif tool_name == "assemble_skill":
        return _assemble_skill(parsed_args)
    # STATE tools
    elif tool_name == "contract_add":
        return _contract_add(parsed_args)
    elif tool_name == "artifact_record":
        return _artifact_record(parsed_args)
    elif tool_name == "phase_advance":
        return _phase_advance(parsed_args)
    elif tool_name == "phase_reset":
        return _phase_reset(parsed_args)
    else:
        raise ValueError(f"Unknown tool: {tool_name}")


# --- READ tool executors ---


def _content(text: str | None) -> str:
    """Snippet for a wire row: stripped, capped at 200 chars + '...'."""
    text = (text or "").strip()
    return text if len(text) <= 200 else text[:200] + "..."


def _code_search(args: dict[str, Any]) -> str:
    """Code search: hybrid graph index (dense + PageRank + BM25) when wired,
    text walk as the last resort."""
    query = args.get("query", "")
    k = int(args.get("k", 10))

    if not query:
        return json.dumps({"results": [], "query": query, "count": 0})

    # Graph index: hybrid search over the shared multi-repo graph.
    if _searcher is not None:
        try:
            hits = _searcher.search(query, k=k)
        except Exception:
            # An error must be distinguishable from a genuine empty result
            # on the wire ("error" is an allowed additive field).
            logger.exception("code_search failed for %r", query)
            return json.dumps(
                {
                    "results": [],
                    "query": query,
                    "count": 0,
                    "mode": _searcher.last_mode,
                    "error": "search failed",
                }
            )
        results = [
            {
                "file": h.file_path,
                "line": h.start_line,
                "content": _content(h.snippet),
                "symbol": h.qualified_name,
                "score": round(h.score, 4),
                "kind": h.kind,
                "end_line": h.end_line,
                "repo": h.repo,
                "centrality": round(h.centrality, 4) if h.centrality is not None else None,
                "qualified_name": h.qualified_name,
            }
            for h in hits
        ]
        return json.dumps(
            {"results": results, "query": query, "count": len(results), "mode": _searcher.last_mode}
        )

    # Last resort (no graph index wired): simple text walk
    import os

    query_lower = query.lower()
    repo_root = os.environ.get("AGENTALLOY_REPO_ROOT", ".")
    results: list[dict[str, Any]] = []
    extensions = {".py", ".rs", ".ts", ".js", ".go", ".java", ".c", ".cpp", ".h"}

    for root, _dirs, files in os.walk(repo_root):
        rel_root = os.path.relpath(root, repo_root)
        if rel_root != "." and any(
            p.startswith(".") or p in ("node_modules", "__pycache__", ".venv", "target")
            for p in rel_root.split(os.sep)
        ):
            continue
        for fname in files:
            ext = os.path.splitext(fname)[1]
            if ext not in extensions:
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, errors="ignore") as f:
                    lines = f.readlines()
                for i, line in enumerate(lines):
                    if query_lower in line.lower():
                        rel = os.path.relpath(fpath, repo_root)
                        # Row contract: {file, line, content, symbol} — the
                        # degraded path still carries symbol (null).
                        results.append(
                            {
                                "file": rel,
                                "line": i + 1,
                                "content": line.strip()[:200],
                                "symbol": None,
                            }
                        )
                        if len(results) >= k:
                            break
            except OSError:
                continue
            if len(results) >= k:
                break
        if len(results) >= k:
            break

    return json.dumps({"results": results[:k], "query": query, "count": len(results)})


def _symbols(args: dict[str, Any]) -> str:
    """Symbol lookup: substring match over the graph index when wired,
    empty fallback otherwise."""
    fqn = args.get("fqn", "")
    if _graph_store is not None:
        try:
            syms = _graph_store.symbols_matching(fqn)
        except Exception:
            syms = []
        results = [
            {"fqn": s.qualified_name, "file": s.file_path, "line": s.start_line, "kind": s.kind}
            for s in syms
        ]
        return json.dumps({"symbols": results, "fqn": fqn, "count": len(results)})
    return json.dumps({"symbols": [], "fqn": fqn})


def _graph_query(args: dict[str, Any]) -> str:
    """Graph exploration: subgraph around hybrid-search seeds, or a
    top-centrality overview when the query is empty."""
    if _graph_store is None or _searcher is None:
        return json.dumps(
            {
                "error": "graph index unavailable",
                "nodes": [],
                "relationships": [],
                "row_count": 0,
                "has_more": False,
            }
        )
    # Lazy: keeps tantivy / tree-sitter out of module-import time.
    from agentalloy.code_index.graph_query import graph_query

    result = graph_query(
        _graph_store,
        _searcher,
        args.get("query", ""),
        limit=int(args.get("limit", 20)),
        hops=int(args.get("hops", 1)),
        repo=args.get("repo") or None,
    )
    return json.dumps(result)


def _knowledge_why(args: dict[str, Any]) -> str:
    """The best decision governing a symbol (``knowledge_why`` tool)."""
    fqn = args.get("fqn", "")
    if _graph_store is None or not fqn:
        return json.dumps({"decision": None, "fqn": fqn})
    # Lazy: keeps the knowledge retrieval import out of module load time.
    from agentalloy.code_index.retrieval.knowledge import governing_decision

    try:
        decision = governing_decision(_graph_store, fqn)
    except Exception:
        logger.exception("knowledge_why failed for %s", fqn)
        decision = None
    return json.dumps({"decision": decision, "fqn": fqn})


def _knowledge_related(args: dict[str, Any]) -> str:
    """Decisions relevant to a free-text query (``knowledge_related`` tool)."""
    query = args.get("query", "")
    k = int(args.get("k", 10))
    if _graph_store is None or _searcher is None or not query:
        return json.dumps({"decisions": [], "query": query})
    from agentalloy.code_index.retrieval.knowledge import related_decisions

    try:
        decisions = related_decisions(_graph_store, _searcher, query, k=k)
    except Exception:
        logger.exception("knowledge_related failed for %s", query)
        decisions = []
    return json.dumps({"decisions": decisions, "query": query})


def _knowledge_entities(args: dict[str, Any]) -> str:
    """Typed entity edges around a symbol or chunk (``knowledge_entities``
    tool)."""
    fqn = args.get("fqn", "")
    kind = args.get("kind") or None
    if _graph_store is None or not fqn:
        return json.dumps({"entities": [], "fqn": fqn})
    from agentalloy.code_index.retrieval.knowledge import entity_edges

    try:
        entities = entity_edges(_graph_store, fqn, kind=kind)
    except Exception:
        logger.exception("knowledge_entities failed for %s", fqn)
        entities = []
    return json.dumps({"entities": entities, "fqn": fqn})


def _artifact_body(args: dict[str, Any]) -> str:
    # JSON envelope, not the raw markdown: /tool clients JSON-parse every
    # result string.
    phase = args.get("phase", "")
    name = args.get("name", "")
    store = _current_store()
    if store:
        body = store.get_artifact(phase, name) or ""
    else:
        body = str(_fallback["artifacts"].get(f"{phase}::{name}", ""))
    return json.dumps({"phase": phase, "name": name, "body": body})


def _contract_detail(args: dict[str, Any]) -> str:
    slug = args.get("slug", "")
    store = _current_store()
    if store:
        contract = store.get_contract(slug)
        return json.dumps(contract or {})
    return json.dumps(_fallback["contracts"].get(slug, {}))


def _telemetry(args: dict[str, Any]) -> str:
    """Recent interpreter traces (``telemetry`` tool)."""
    if _telemetry_store is None:
        return json.dumps({"traces": []})
    k = int(args.get("k", 5))
    phase = args.get("phase") or None
    try:
        traces = _telemetry_store.get_traces(k, phase)
    except Exception:
        logger.exception("telemetry read failed")
        traces = []
    return json.dumps({"traces": traces})


# Technology vocabulary for catalog filtering: a skill/pack whose name
# names one of these but the repo doesn't use it is dropped from the
# catalog the orchestrator sees. Rows without a technology name pass.
_TECH_ALIASES: dict[str, str] = {
    "ts": "typescript",
    "js": "javascript",
    "py": "python",
    "golang": "go",
    "node": "javascript",
    "nodejs": "javascript",
}

# Detected-tags cache per project (repos don't change stack mid-session).
_repo_tags_cache: dict[str, set[str] | None] = {}


def _tech_vocabulary() -> set[str]:
    from agentalloy.profiles import _DEP_FRAMEWORKS, _EXT_LANG

    return set(_EXT_LANG.values()) | set(_DEP_FRAMEWORKS.values()) | set(_TECH_ALIASES)


def _repo_tags_for_current_project() -> set[str] | None:
    """Language/framework tags of the current run's project repo.

    None = unknown project or unregistered repo → no filtering. Resolved via
    the registry (project keys derive from registered repo paths).
    """
    store = _current_store()
    project = getattr(store, "project", "") if store else ""
    if not project:
        return None
    if project in _repo_tags_cache:
        return _repo_tags_cache[project]
    tags: set[str] | None = None
    try:
        from pathlib import Path

        from agentalloy.profiles import detect_repo_tags
        from agentalloy.registry import load_repos, project_key

        for repo in load_repos(store.db_path):
            if project_key(repo) == project:
                tags = detect_repo_tags(Path(repo))
                break
    except Exception:
        logger.exception("repo tag detection failed for %s", project)
        tags = None
    if len(_repo_tags_cache) > 64:
        _repo_tags_cache.clear()
    _repo_tags_cache[project] = tags
    return tags


def _row_tokens(row: dict[str, Any]) -> set[str]:
    import re as _re

    text = f"{row.get('pack', '')} {row.get('id', '')} {row.get('name', '')}".lower()
    return set(_re.split(r"[^a-z0-9@/]+", text))


def _filter_rows_by_repo(
    rows: list[dict[str, Any]], repo_tags: set[str] | None
) -> list[dict[str, Any]]:
    """Drop rows named after a technology the repo doesn't use.

    A row survives when it names no known technology, or names one the repo
    has. Never filters down to nothing — an over-aggressive match falls back
    to the unfiltered rows.
    """
    if repo_tags is None:
        return rows
    vocab = _tech_vocabulary()
    kept = []
    for row in rows:
        tokens = {_TECH_ALIASES.get(t, t) for t in _row_tokens(row)}
        named = tokens & vocab
        if not named or named & repo_tags:
            kept.append(row)
    return kept or rows


def _get_skill_for(args: dict[str, Any]) -> str:
    """Fragment-native skill selection (``get_skill_for`` tool).

    Two modes for the LFM's two selection steps:
    - no ``packs`` → the pack catalog (one row per pack); when only the
      built-in corpus is loaded (no packs) this degrades to skill rows.
    - ``packs`` given → skill rows for those packs, each with a
      fragment-type breakdown so the LFM can pick skills + types.

    The catalog is pre-filtered by the project's detected stack so the
    orchestrator never sees (and can't recommend) skills for languages or
    frameworks the repo doesn't use.
    """
    task = args.get("task", "")
    phase = args.get("phase", "")
    packs = args.get("packs")
    if _skill_engine is None or not task:
        return json.dumps({"packs": [], "skills": [], "task": task, "phase": phase, "count": 0})
    repo_tags = _repo_tags_for_current_project()
    try:
        if packs is None:
            catalog = _filter_rows_by_repo(_skill_engine.pack_catalog(), repo_tags)
            if catalog:
                return json.dumps(
                    {"packs": catalog, "task": task, "phase": phase, "count": len(catalog)}
                )
            rows = _skill_engine.skills_catalog(None)
        else:
            rows = _skill_engine.skills_catalog([str(p) for p in packs])
        rows = _filter_rows_by_repo(rows, repo_tags)
    except Exception:
        logger.exception("get_skill_for failed for %s", task)
        rows = []
    return json.dumps(
        {"packs": [], "skills": rows, "task": task, "phase": phase, "count": len(rows)}
    )


def _assemble_skill(args: dict[str, Any]) -> str:
    """Deterministic expansion of the LFM's skill + type selection
    (``assemble_skill`` tool). The assembled skill is injected into the
    main model's context by /compose — the LFM only writes the brief.
    """
    skills = [str(s) for s in args.get("skills", [])]
    types = args.get("types")
    phase = args.get("phase", "")
    if _skill_engine is None:
        return json.dumps({"skill": "", "fragments": [], "source_skills": []})
    try:
        result = _skill_engine.assemble_skill(skills, types, phase)
    except Exception:
        logger.exception("assemble_skill failed for %s", skills)
        result = {"skill": "", "fragments": [], "source_skills": []}
    return json.dumps(result)


# --- STATE tool executors (real DuckDB when store injected) ---


def _contract_add(args: dict[str, Any]) -> str:
    slug = args.get("slug", "")
    domain_tags = args.get("domain_tags", [])
    touches = args.get("touches", "")
    store = _current_store()
    if store:
        store.add_contract(slug, domain_tags, touches)
    else:
        _fallback["contracts"][slug] = {
            "slug": slug,
            "domain_tags": domain_tags,
            "touches": touches,
        }
    return json.dumps({"status": "ok", "slug": slug})


def _artifact_record(args: dict[str, Any]) -> str:
    phase = args.get("phase", "")
    name = args.get("name", "")
    body = args.get("body", "")

    # An empty body is a placeholder, not evidence — the store rejects it at
    # the write (the advance gate reads this row); reject here too, in both
    # store and fallback modes, so the LLM gets an actionable message.
    if not body or not body.strip():
        return json.dumps(
            {
                "status": "rejected",
                "phase": phase,
                "name": name,
                "reason": "body must not be empty — record the concrete evidence this phase produced",
            }
        )

    store = _current_store()
    if store:
        try:
            digest = store.record_artifact(phase, name, body)
        except ValueError as exc:
            return json.dumps(
                {"status": "rejected", "phase": phase, "name": name, "reason": str(exc)}
            )
    else:
        digest = None
        key = f"{phase}::{name}"
        _fallback["artifacts"][key] = body
    return json.dumps({"status": "ok", "phase": phase, "name": name, "digest": digest})


def _phase_advance(args: dict[str, Any]) -> str:
    target = args.get("target", "")
    approved = bool(args.get("approved", False))

    # Fail-closed at the tool surface: an unknown target is rejected here,
    # before it can reach the store's write-side validation.
    if target not in PHASE_ORDER:
        return json.dumps(
            {
                "status": "rejected",
                "target": target,
                "reason": f"unknown phase {target!r}",
                "legal_phases": list(PHASE_ORDER),
            }
        )

    store = _current_store()
    if store is None:
        # No store wired (standalone tests): no state leg to consult, so the
        # approved flag is the caller's claim, as before.
        if approved:
            _fallback["phases"]["current"] = target
        return json.dumps({"status": "ok" if approved else "rejected", "target": target})

    current = store.get_current_phase()
    if current not in PHASE_ORDER:
        # Corrupt state — recovery is an explicit operator reset, not an
        # advance. The store refuses the write too; this is the friendly path.
        return json.dumps(
            {
                "status": "rejected",
                "target": target,
                "reason": f"current phase {current!r} is not part of the lifecycle; "
                "use phase_reset to recover",
                "legal_phases": list(PHASE_ORDER),
            }
        )

    cur_idx = PHASE_ORDER.index(current)
    tgt_idx = PHASE_ORDER.index(target)

    if tgt_idx > cur_idx + 1:
        return json.dumps(
            {
                "status": "rejected",
                "target": target,
                "reason": f"cannot advance {current} → {target}: the lifecycle is "
                "walked one phase at a time",
            }
        )

    if tgt_idx > cur_idx:
        # Hard gate (artifacts): the store re-enforces this at the write; the
        # pre-check exists so the LLM gets an actionable rejection, not an
        # error raised out of the tool.
        if not store.has_exit_artifact(current):
            return json.dumps(
                {
                    "status": "rejected",
                    "target": target,
                    "reason": f"no substantive exit artifact for phase '{current}' — "
                    f"call artifact_record(phase='{current}', name='{current}-exit', "
                    f"body=<the concrete evidence this phase produced>) first",
                }
            )

        # Soft gate (approval): the LLM claims the user's approval; the store
        # pins it to the exit artifact's digest, so editing the artifact
        # voids it (AC-9). Only the gated transitions are checked.
        from agentalloy.phase_machine import APPROVAL_GATES

        transition = f"{current}→{target}"
        if transition in APPROVAL_GATES:
            exit_digest = store.get_exit_artifact_digest(current) or ""
            if not store.is_approved(transition, exit_digest):
                if not approved:
                    return json.dumps(
                        {
                            "status": "rejected",
                            "target": target,
                            "reason": f"transition '{transition}' requires the user's "
                            "approval — pass approved=true only after the user has "
                            "explicitly approved the work",
                            "digest": exit_digest,
                        }
                    )
                store.record_approval(transition, exit_digest)

    try:
        store.advance_phase(target)
    except PhaseAdvanceError as exc:
        # The store is authoritative; this fires only on a state change
        # between the pre-checks above and the write.
        return json.dumps({"status": "rejected", "target": target, "reason": str(exc)})

    return json.dumps({"status": "ok", "from": current, "target": target})


def _phase_reset(args: dict[str, Any]) -> str:
    """Reset the lifecycle to intake and clear approvals (operator-only).

    Contracts and artifacts are kept — they are project knowledge, not
    lifecycle state. This is the recovery path for stores whose phase
    value predates write-side validation.
    """
    store = _current_store()
    if store:
        phase = store.reset_phase()
    else:
        phase = LIFECYCLE_START
        _fallback["phases"]["current"] = phase
    return json.dumps({"status": "reset", "phase": phase})
