"""REST API server: FastAPI endpoints for AgentAlloy service.

Exposes:
- POST /chat — interpreter endpoint (real LFM-driven tool calling)
- GET /health — health check
- GET /status — current phase + stats
- GET /usage — token usage summary
- GET /dashboard — web UI
"""

import contextlib
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI
from pydantic import BaseModel

from agentalloy.config import Config
from agentalloy.executors import set_graph_index, set_skill_engine, set_store, set_telemetry
from agentalloy.interpreter import Interpreter
from agentalloy.skill_engine import SkillEngine
from agentalloy.state_store import StateStore
from agentalloy.telemetry_store import TelemetryStore


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    startup()
    yield
    shutdown()


app = FastAPI(title="AgentAlloy v2.0", version="0.3.0", lifespan=lifespan)

# API contract version advertised in GET /status (AgentAlloy v2 major.minor).
# Bump on breaking /status or /tool shape changes, not on tool additions.
API_VERSION = "2.0"

# Global state (initialized in startup)
config: Config | None = None
state_store: StateStore | None = None
telemetry_store: TelemetryStore | None = None
skill_engine: SkillEngine | None = None
interpreter: Interpreter | None = None

# Code graph index (OverGraph) — the single code index.
graph_store: Any = None
graph_searcher: Any = None
fts_index: Any = None
embed_client: Any = None

# Capability readiness. `graph_ready` flips only after the searcher is fully
# wired (set_graph_index); `knowledge_ready` only after a successful
# ingest_knowledge. A partially initialized index must never advertise
# capabilities it cannot serve.
graph_ready: bool = False
knowledge_ready: bool = False

# Serializes index mutations (startup ingest, /reindex) against each other.
# Read paths stay lock-free: search latency includes an embed HTTP call and
# the store tolerates concurrent reads; writers must not overlap.
_index_lock = threading.Lock()


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    max_steps: int = 5


class ChatResponse(BaseModel):
    answer: str | None
    stop_reason: str
    steps: int
    tool_calls: int
    phase: str


class ComposeRequest(BaseModel):
    prompt: str
    new_session: bool = False
    # Conversation identity from the proxy (its conversation hash) so phase
    # tracking is per conversation, not process-global. Optional: legacy
    # callers share the "" slot.
    session_key: str = ""
    # Project scope (registry.project_key of the wired repo). "" = legacy
    # instance-global scope. Scopes phase, contracts, artifacts, approvals.
    project: str = ""


class ComposeResponse(BaseModel):
    context: str
    context_type: int  # 0=none, 1=turn, 2=activation
    phase: str
    stop_reason: str
    tool_calls: int
    # Dynamic skill assembled by the LFM via assemble_skill. Additive:
    # already embedded in `context`, so the proxy consumes context only.
    skill: str = ""
    fragments: list[str] = []
    source_skills: list[str] = []


# Orchestrator system prompt: LFM does the state work (contracts, skills,
# phase) via its tools, then writes the steering brief the proxy injects
# into the MAIN session model's context.
ORCHESTRATOR_SYSTEM_PROMPT = (
    "You are the AgentAlloy orchestrator, a pre-handoff coordinator. A developer "
    "is starting a turn in a coding session; a much larger main model does the "
    "actual work right after you. Before the handoff, do your state work with your tools:\n"
    "- contract_add: record key requirements, decisions, or constraints from the request.\n"
    "- contract_detail: review active contracts when the request interacts with them.\n"
    "Phase advance — strict protocol, the service rejects anything else:\n"
    "1. The lifecycle is walked one phase at a time: intake→spec→design→plan→build→qa→ship. Never skip a phase.\n"
    "2. Before phase_advance(target), record the evidence: artifact_record(phase=<current>, name='<current>-exit', body=<the concrete evidence that phase produced>). An advance without a non-empty exit artifact is rejected.\n"
    "3. Gated transitions (spec→design, design→plan, plan→build) additionally require approved=true — pass it only when the user has explicitly approved the presented work. Never assume approval.\n"
    "Advance only when the phase's work is actually done and the request moves past it.\n"
    "Skill selection (pick what the task needs, then assemble):\n"
    "1. get_skill_for WITHOUT packs → the pack catalog (one row per pack).\n"
    "2. get_skill_for WITH packs=[the 1-4 relevant packs] → skill rows with "
    "fragment-type breakdowns (execution, example, rationale, verification, "
    "guardrail, setup).\n"
    "3. assemble_skill(skills=[2-6 skill ids, most relevant first], "
    "types=[the fragment types this task needs], phase) → the dynamic skill "
    "is assembled for you and injected into the main model's context — the "
    "main model receives it, not you. Do not re-pull skills or restate the "
    "assembled skill in the brief.\n"
    "Be economical: at most 3 tool calls for skill selection "
    "(catalog → skills → assemble), plus at most 3 for state work "
    "(contract_add, and the advance pair: artifact_record + phase_advance). "
    "Never repeat a call: each tool at most once per distinct target — ONE "
    "contract_add per new requirement (slugs are unique; re-adding an "
    "existing or just-added slug, or the same requirement under a reworded "
    "slug, is wasted budget). If a tool already returned a result this "
    "turn, use it — do not call again to confirm. "
    "Discovery (you are the main model's only access to the knowledge "
    "graph): when the request touches existing code, symbols, or past "
    "decisions, spend up to 2 read calls — code_search for where things "
    "live, knowledge_why/knowledge_related for why they are that way, "
    "symbols for exact definitions — and distill only the relevant hits "
    "into the brief as file:line plus a one-line takeaway each. Never "
    "paste raw tool output. Skip discovery for greenfield requests that "
    "touch nothing existing. Do not call telemetry in a routine handoff. "
    "Finish with a short steering brief for the main model (max 150 words): "
    "what the user wants, how the active constraints bear on it, and how the "
    "selected skills apply. Do NOT state the current phase or enumerate "
    "contracts — those facts are stamped into the handoff mechanically from "
    "the store; a wrong guess would contradict them. The main model also has "
    "direct access to agentalloy tools via MCP, so do not over-instruct. If "
    "nothing needs recording and no skills apply, answer exactly: "
    "No steering needed."
)

# Activation tracking for /compose: last phase seen PER conversation
# (keyed by the proxy's session_key; "" for legacy callers). A process-global
# slot let two interleaved conversations cross-contaminate each other's
# phase-change detection. Bounded — oldest conversation evicted.
_COMPOSE_STATE_MAX = 64
_compose_state: dict[str, str] = {}


def startup() -> None:
    """Initialize global state: stores, code index, interpreter."""
    global config, state_store, telemetry_store, skill_engine, interpreter
    global graph_store, graph_searcher, fts_index, embed_client
    global graph_ready, knowledge_ready

    config = Config.from_env()
    state_store = StateStore(config.state_duck)
    telemetry_store = TelemetryStore(str(Path(config.state_duck).with_name("telemetry.duck")))

    # Initialize skill engine with full v1 corpus (355 skills / 41 packs)
    skill_engine = SkillEngine(load_corpus=True)

    # Inject store + skill engine + telemetry into executors
    set_store(state_store)
    set_skill_engine(skill_engine)
    set_telemetry(telemetry_store)

    # Code index. Multi-repo: every registered repo ingests into one shared
    # index; no registry yet → fall back to the service's own repo.
    from agentalloy.registry import load_repos

    os.makedirs(config.index_dir, exist_ok=True)
    repos = load_repos(config.state_duck) or [config.repo_root]

    # Code graph index (OverGraph + tantivy), delta ingested.
    try:
        from agentalloy.code_index.embed_client import EmbedClient
        from agentalloy.code_index.fts import FtsIndex
        from agentalloy.code_index.open import fts_dir, open_codegraph
        from agentalloy.code_index.pipeline import ingest_all_repos
        from agentalloy.code_index.retrieval.hybrid import CodeSearcher

        graph_store = open_codegraph(config.index_dir)
        embed_client = EmbedClient(config.embed_url, model=config.embed_model)
        # Repo key is the resolved absolute path: basenames collide across
        # parents (/a/proj vs /b/proj) and would clobber each other's rows.
        repo_pairs = [(str(Path(p).resolve()), Path(p)) for p in repos]
        with _index_lock:
            reports = ingest_all_repos(
                graph_store,
                repo_pairs,
                embed_client=embed_client,
                index_dir=config.index_dir,
            )
            # Knowledge pass: decision docs + SDD lifecycle state projected
            # into the graph, then the whole-index FTS + PageRank
            # projections that consume knowledge rows. A failure here
            # degrades to a code-only index, not a boot failure.
            try:
                from agentalloy.code_index.knowledge import ingest_knowledge

                knowledge_reports = ingest_knowledge(
                    graph_store,
                    repo_pairs,
                    state_store,
                    embed_client=embed_client,
                    index_dir=config.index_dir,
                )
                knowledge_ready = True
                print(
                    "Knowledge index initialized: "
                    f"{sum(r.docs for r in knowledge_reports)} docs, "
                    f"{sum(r.chunks for r in knowledge_reports)} chunks, "
                    f"{sum(r.edges for r in knowledge_reports)} edges"
                )
            except Exception as e:
                print(f"Knowledge ingest failed; serving code-only index: {e}")
            fts_index = FtsIndex(fts_dir(config.index_dir))
            graph_searcher = CodeSearcher(graph_store, fts_index, embed_client)
            set_graph_index(graph_store, graph_searcher)
            graph_ready = True
        embedded = sum(r.embedded for r in reports)
        mode = "hybrid" if embed_client.is_available() else "lexical-only"
        print(
            f"Graph index initialized: {len(repos)} repo(s), "
            f"{sum(graph_store.counts_by_kind().values())} symbols, "
            f"{fts_index.count()} chunks, {embedded} embedded, mode={mode}"
        )
    except Exception as e:
        # A half-wired index must not be served (or advertised): tear it
        # down so every consumer sees the same "no index" state.
        print(f"Graph index init skipped: {e}")
        if graph_store is not None:
            with contextlib.suppress(Exception):
                graph_store.close()
        graph_store = None
        graph_searcher = None
        fts_index = None
        graph_ready = False
        knowledge_ready = False
        set_graph_index(None, None)

    # Initialize interpreter with live LFM client
    try:
        client = OpenAI(
            base_url=f"http://localhost:{config.model_port}/v1",
            api_key=config.model_key,
            timeout=30.0,
            max_retries=1,
        )
        interpreter = Interpreter(
            client=client,
            max_steps=config.max_steps,
            hard_cap=config.hard_cap,
            state_store=state_store,
            model=config.interp_model,
        )
    except Exception as e:
        print(f"Interpreter init skipped: {e}")


def shutdown() -> None:
    """Cleanup."""
    if interpreter:
        with contextlib.suppress(Exception):
            interpreter.client.close()
    if state_store:
        state_store.close()
    if telemetry_store:
        telemetry_store.close()
    if graph_store:
        with contextlib.suppress(Exception):
            graph_store.close()
    if embed_client:
        embed_client.close()


@app.get("/health")
def health() -> dict[str, str]:
    """Health check."""
    return {"status": "ok"}


def _capabilities() -> dict[str, bool]:
    """Feature flags advertised in GET /status for client capability negotiation.

    `graph` flips only once the index is fully wired (store + searcher via
    set_graph_index); `knowledge` only after a successful knowledge ingest.
    A partial startup must not advertise features that would return
    empty/null forever. The rest are explicit until their code paths land:
    rerank is unused (RRF fusion), reindex is synchronous (no job model).
    """
    return {
        "graph": graph_ready,
        "knowledge": knowledge_ready,
        "rerank": False,
        "jobs": False,
    }


@app.get("/status")
def status(project: str = Query("")) -> dict[str, Any]:
    """Get current status.

    ``project`` scopes the phase to one work item (additive; empty = the
    legacy global machine). ``phase_set`` reports whether that scope actually
    has a phase row, and ``next_gate`` is the machine's gate for the phase's
    outgoing transition — together they let a steering client pick and read
    the machine a work item lives on in one round trip.
    """
    base: dict[str, Any] = {
        "api_version": API_VERSION,
        "capabilities": _capabilities(),
    }
    if not state_store or not config:
        return {
            **base,
            "phase": "uninitialized",
            "phase_set": False,
            "service_port": 48950,
            "model_port": 50001,
        }

    from agentalloy.phase_machine import gate_status

    store = state_store.scoped(project) if project else state_store
    phase = store.get_current_phase()
    result: dict[str, Any] = {
        **base,
        "phase": phase,
        "phase_set": store.has_phase_row(),
        "next_gate": gate_status(store, phase),
        "service_port": config.service_port,
        "model_port": config.model_port,
    }

    # Add code-index stats if available
    if graph_store is not None:
        result["symbols"] = sum(graph_store.counts_by_kind().values())
        if fts_index is not None:
            result["chunks"] = fts_index.count()

    # Add skill corpus stats
    if skill_engine:
        result["skills"] = len(skill_engine.skills)
        result["corpus_loaded"] = skill_engine.corpus_loaded

    return result


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """Chat endpoint — runs the interpreter with real LFM tool calling."""
    if not interpreter or not state_store:
        return ChatResponse(
            answer="Service not initialized",
            stop_reason="error",
            steps=0,
            tool_calls=0,
            phase="unknown",
        )

    result = interpreter.run(request.messages)

    phase = state_store.get_current_phase()
    _record_telemetry("chat", result.tool_calls, result.stop_reason, phase)

    return ChatResponse(
        answer=result.answer,
        stop_reason=result.stop_reason,
        steps=result.steps,
        tool_calls=len(result.tool_calls),
        phase=phase,
    )


def _record_telemetry(
    session_id: str, tool_calls: list[dict[str, Any]], stop_reason: str, phase: str | None
) -> None:
    """Record an interpreter run's traces; never fail the request for it."""
    if not telemetry_store:
        return
    try:
        telemetry_store.record_run(session_id, tool_calls, stop_reason, phase)
    except Exception as e:
        print(f"telemetry record failed: {e}")


@app.post("/compose", response_model=ComposeResponse)
def compose(request: ComposeRequest) -> ComposeResponse:
    """Handoff compose: run the LFM orchestrator loop, then return the steering
    context the proxy injects into the MAIN session model's context.

    LFM does the state work via its tools (contracts, skills, phase); its final
    answer becomes the steering brief. context_type: 2 = activation (phase
    change / first turn → persona + brief), 1 = turn (brief only), 0 = none.
    """
    if not interpreter or not state_store:
        return ComposeResponse(
            context="", context_type=0, phase="unknown", stop_reason="error", tool_calls=0
        )

    # Project-scoped view: phase/contract/artifact state for this compose
    # stays inside the requesting project's scope ("" = legacy global).
    store = state_store.scoped(request.project) if request.project else state_store

    if request.new_session:
        # Register the session so /sessions reflects live conversations.
        # Never fail the compose for bookkeeping.
        try:
            from agentalloy.sessions import SessionManager

            key = f"sess-{time.time_ns():x}"
            SessionManager(store).create(key)
        except Exception as e:
            print(f"compose session registration failed: {e}")

    phase = store.get_current_phase()
    messages = [
        {"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT},
        {"role": "user", "content": request.prompt},
    ]

    brief = ""
    stop_reason = "error"
    tool_calls = 0
    skill_text = ""
    skill_frags: list[str] = []
    skill_sources: list[str] = []
    history = messages
    run_tool_calls: list[dict[str, Any]] = []
    try:
        result = interpreter.run(messages, state_store=store)
        # The interpreter no longer mutates the input list; the follow-up
        # below needs the run's tool history to write an informed brief.
        history = result.messages or messages
        brief = (result.answer or "").strip()
        stop_reason = result.stop_reason
        run_tool_calls = result.tool_calls
        tool_calls = len(result.tool_calls)
        # The dynamic skill is captured from the LFM's last assemble_skill
        # observation (the interpreter logs every tool call + result), so
        # no global state and no extra round-trip.
        for call in reversed(result.tool_calls):
            if call["tool"] == "assemble_skill":
                try:
                    data = json.loads(call.get("result") or "")
                except (json.JSONDecodeError, TypeError):
                    data = {}
                skill_text = str(data.get("skill", "")).strip()
                skill_frags = data.get("fragments", [])
                skill_sources = data.get("source_skills", [])
                break
    except Exception as e:
        print(f"compose interpreter error: {e}")

    # LFM often spends its whole tool budget without a final answer.
    # Force one no-tools call so the handoff brief always exists.
    if not brief:
        try:
            # The follow-up goes to the SIDECAR (interpreter client), so it
            # uses the interp model name, not the main upstream model.
            follow = interpreter.client.chat.completions.create(
                model=config.interp_model if config else "minicpm5-2b",
                messages=history
                + [
                    {
                        "role": "user",
                        "content": (
                            "Now write the steering brief for the main model "
                            "(max 150 words) as your final answer. Do NOT state "
                            "the current phase or enumerate contracts — they are "
                            "stamped mechanically. If nothing needs recording "
                            "and no skills apply, answer exactly: "
                            "No steering needed."
                        ),
                    }
                ],
                temperature=0.0,
            )
            brief = (follow.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"compose brief follow-up error: {e}")

    if brief.lower().startswith("no steering needed"):
        brief = ""

    _record_telemetry(request.session_key or "compose", run_tool_calls, stop_reason, phase)

    # Deterministic fact block: phase and active contracts come from the
    # store, never from the model's free text — briefs were observed
    # asserting a phase that contradicted the store (confabulation). The
    # model's contribution is constrained to the task summary in the brief.
    fact_lines = [f"Phase: {phase}"]
    try:
        slugs = [c["slug"] for c in store.list_contracts()]
        if slugs:
            fact_lines.append("Active contracts: " + ", ".join(slugs[:8]))
    except Exception as e:
        print(f"compose contract listing failed: {e}")
    facts = "# Project State\n" + "\n".join(fact_lines)

    # The assembled dynamic skill (if the LFM called assemble_skill) is
    # embedded in the context itself — the proxy injects `context` verbatim
    # into the leading system message, so it needs no separate field.
    brief_part = f"# Turn Brief\n{brief}" if brief else ""
    if request.new_session or _compose_state.get(request.session_key) != phase:
        # Activation: session start (proxied flag), first turn, or phase
        # change → persona + skill + brief
        from agentalloy.phase_aware_steering import PHASE_PERSONAS

        persona = PHASE_PERSONAS.get(phase, "")
        context = "\n\n".join(p for p in (persona, facts, skill_text, brief_part) if p)
        context_type = 2
    elif skill_text or brief:
        context = "\n\n".join(p for p in (facts, skill_text, brief_part) if p)
        context_type = 1
    else:
        context = ""
        context_type = 0

    if phase:
        _compose_state[request.session_key] = phase
        while len(_compose_state) > _COMPOSE_STATE_MAX:
            del _compose_state[next(iter(_compose_state))]

    return ComposeResponse(
        context=context,
        context_type=context_type,
        phase=phase,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        skill=skill_text,
        fragments=skill_frags,
        source_skills=skill_sources,
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> Any:
    """SSE streaming chat endpoint — real-time token delivery."""
    import json as json_mod

    from fastapi.responses import StreamingResponse

    if not interpreter or not state_store:

        async def error_stream() -> AsyncIterator[str]:
            err = json_mod.dumps({"type": "error", "message": "not initialized"})
            yield f"data: {err}\n\n"

        return StreamingResponse(error_stream(), media_type="text/event-stream")

    def event_stream() -> Any:
        events = interpreter.run_streaming(request.messages)
        for event in events:
            yield f"data: {json_mod.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class ToolRequest(BaseModel):
    name: str
    args: str  # JSON string, same shape the interpreter passes
    # Project scope for state tools (additive — legacy callers get "").
    project: str = ""


def _approver_header_valid(presented: str | None) -> bool:
    """Constant-time check of X-AgentAlloy-Approver against
    AGENTALLOY_APPROVER_TOKEN (unset → never valid; the lock is then off)."""
    import hmac

    expected = os.environ.get("AGENTALLOY_APPROVER_TOKEN", "")
    return bool(expected) and presented is not None and hmac.compare_digest(presented, expected)


@app.post("/tool")
def tool_exec(
    request: ToolRequest,
    x_agentalloy_approver: str | None = Header(default=None),
) -> dict[str, Any]:
    """Single-tool execution for the MCP bridge (no LFM loop, no steering).

    The MCP server never opens state.duck (the service holds the only RW
    handle); it delegates every tool call here over HTTP.

    ``X-AgentAlloy-Approver`` (additive): when AGENTALLOY_APPROVER_TOKEN is
    set, only a request carrying it may record approvals or reset the
    lifecycle — see executors._approval_locked.
    """
    from agentalloy.executors import (
        execute_tool,
        reset_approver_authorized,
        set_approver_authorized,
        set_run_store,
    )

    approver_token = set_approver_authorized(_approver_header_valid(x_agentalloy_approver))
    try:
        return _tool_exec(request, execute_tool, set_run_store)
    finally:
        reset_approver_authorized(approver_token)


def _tool_exec(request: ToolRequest, execute_tool: Any, set_run_store: Any) -> dict[str, Any]:
    try:
        if request.project and state_store:
            set_run_store(state_store.scoped(request.project))
        try:
            result = execute_tool(request.name, request.args)
        finally:
            set_run_store(None)
        # MCP calls bypass the interpreter loop — without this row, state
        # moves made over the MCP bridge (phase_advance, artifact_record)
        # leave no trace at all. Telemetry must never break the call.
        if telemetry_store and state_store:
            scope = state_store.scoped(request.project) if request.project else state_store
            with contextlib.suppress(Exception):
                telemetry_store.record_trace(
                    f"mcp:{request.project or 'global'}",
                    0,
                    request.name,
                    request.args[:500],
                    (result if isinstance(result, str) else json.dumps(result))[:500],
                    stop_reason="tool_call",
                    phase=scope.get_current_phase(),
                )
        # The contract says `result` is a JSON string the client parses;
        # non-string executor results must be JSON-encoded, never repr'd.
        return {"ok": True, "result": result if isinstance(result, str) else json.dumps(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ReindexRequest(BaseModel):
    repo: str


def _allowlist_roots() -> list[Path]:
    """Roots /reindex may register, from AGENTALLOY_REPO_ALLOWLIST (colon-separated).

    Unset → no restriction: the service binds localhost by default, so the
    allowlist is defense-in-depth for deliberately exposed deployments.
    """
    raw = os.environ.get("AGENTALLOY_REPO_ALLOWLIST", "")
    return [Path(p).expanduser().resolve() for p in raw.split(":") if p.strip()]


@app.post("/reindex")
def reindex(request: ReindexRequest) -> dict[str, Any]:
    """Register a repo and (re)index it into the shared multi-repo index.

    Synchronous: a full forced re-ingest of the one repo, 200 in both
    outcomes. Idempotent per repo: re-indexing a repo that is already present
    replaces its entries. The registry is persisted next to state.duck so the
    index rebuilds across service restarts.

    The path is validated before it is registered: a bad path in the
    registry would be re-attempted on every restart. Optional
    AGENTALLOY_REPO_ALLOWLIST (colon-separated roots) restricts what may be
    registered at all.
    """
    global knowledge_ready

    from agentalloy.registry import load_repos, upsert_repo

    if not config:
        return {"status": "error", "message": "service not initialized"}

    repo_path = Path(request.repo).expanduser().resolve()
    if not repo_path.is_dir():
        return {
            "status": "error",
            "message": f"repo not a directory: {request.repo}",
            "repos": load_repos(config.state_duck),
        }
    allowed = _allowlist_roots()
    if allowed and not any(repo_path.is_relative_to(root) for root in allowed):
        return {
            "status": "error",
            "message": f"repo outside allowed roots: {request.repo}",
            "repos": load_repos(config.state_duck),
        }

    repos = upsert_repo(config.state_duck, request.repo)

    if graph_store is None:
        return {"status": "error", "message": "code index unavailable", "repos": repos}
    if state_store is None:
        return {"status": "error", "message": "state store unavailable", "repos": repos}

    try:
        from agentalloy.code_index.pipeline import ingest_repo

        with _index_lock:
            report = ingest_repo(
                graph_store,
                str(repo_path),
                repo_path,
                embed_client=embed_client,
                index_dir=config.index_dir,
                force_full=True,
            )
            # Knowledge re-derivation for the reindexed repo + the SDD
            # projection + the whole-index FTS / PageRank pass.
            try:
                from agentalloy.code_index.knowledge import ingest_knowledge

                ingest_knowledge(
                    graph_store,
                    [(str(repo_path), repo_path)],
                    state_store,
                    embed_client=embed_client,
                    index_dir=config.index_dir,
                )
                knowledge_ready = True
            except Exception as e:
                print(f"Knowledge ingest failed on reindex: {e}")
        return {
            "status": "ok",
            "symbols": sum(graph_store.counts_by_kind().values()),
            "chunks": fts_index.count() if fts_index is not None else 0,
            "mode": "hybrid" if report.embed_available else "lexical-only",
            "repos": repos,
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "repos": repos}


def _proxy_usage_get(route: str) -> dict[str, Any] | None:
    """Fetch a usage route from the proxy process, which holds the only
    long-lived RW handle on usage.duck. Opening the file here while the
    proxy runs hits DuckDB's single-writer lock and silently reads zeros —
    HTTP delegation is the correct path; None → proxy unreachable."""
    import httpx

    port = config.proxy_port if config else 48953
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}{route}", timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


@app.get("/usage")
def usage() -> dict[str, Any]:
    """Token usage summary — delegates to the proxy's usage tracker."""
    delegated = _proxy_usage_get("/usage")
    if delegated is not None:
        return delegated
    tracker = None
    try:
        from agentalloy.usage_tracker import UsageTracker

        tracker = UsageTracker(config.usage_duck if config else "./usage.duck")
        return tracker.get_totals()
    except Exception:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "injected_tokens": 0,
            "total_requests": 0,
            "total_tokens": 0,
        }
    finally:
        if tracker is not None:
            tracker.close()


@app.get("/usage/history")
def usage_history(limit: int = 50) -> dict[str, Any]:
    """Per-request usage history — delegates to the proxy's usage tracker."""
    delegated = _proxy_usage_get(f"/usage/history?limit={int(limit)}")
    if delegated is not None:
        return delegated
    tracker = None
    try:
        from agentalloy.usage_tracker import UsageTracker

        tracker = UsageTracker(config.usage_duck if config else "./usage.duck")
        history = tracker.get_history(limit)
        return {
            "history": [
                {
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "injected_tokens": r.injected_tokens,
                    "timestamp": r.timestamp,
                }
                for r in history
            ]
        }
    except Exception:
        return {"history": []}
    finally:
        if tracker is not None:
            tracker.close()


@app.get("/profiles")
def profiles() -> dict[str, Any]:
    """List configured profiles and active profile."""
    from pathlib import Path

    from agentalloy.profiles import detect_profile, list_profiles

    cwd = Path(config.repo_root) if config else None
    active = detect_profile(cwd)
    all_profiles = list_profiles(cwd)
    return {
        "active": {
            "name": active.name,
            "packs": active.packs,
            "domain_tags": active.domain_tags,
            "is_default": active.is_default,
        },
        "all": all_profiles,
    }


@app.get("/gates")
def gates(project: str = Query("")) -> dict[str, Any]:
    """Check approval gate status for all phase transitions.

    ``project`` scopes the gates to one work item (additive; empty = the
    legacy global machine).
    """
    if not state_store:
        return {"gates": []}

    from agentalloy.phase_machine import APPROVAL_GATES, PHASE_ORDER, PhaseMachine

    machine = PhaseMachine(state_store.scoped(project) if project else state_store)
    gate_results = []
    for phase in PHASE_ORDER:
        gate_info = machine.check_gate(phase)
        gate_results.append(gate_info)

    return {
        "gates": gate_results,
        "approval_gates": sorted(APPROVAL_GATES),
    }


@app.get("/sessions")
def sessions_list() -> dict[str, Any]:
    """List all sessions."""
    if not state_store:
        return {"sessions": []}
    from agentalloy.sessions import SessionManager

    mgr = SessionManager(state_store)
    return {
        "sessions": [
            {
                "session_key": s.session_key,
                "status": s.status,
                "phase": s.phase,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in mgr.list_sessions()
        ]
    }


@app.get("/sessions/{session_key}")
def session_detail(session_key: str) -> dict[str, Any]:
    """Get session details."""
    if not state_store:
        return {"error": "service not initialized"}
    from agentalloy.sessions import SessionManager

    mgr = SessionManager(state_store)
    return mgr.status(session_key)


@app.post("/sessions/{session_key}/stash")
def session_stash(session_key: str) -> dict[str, Any]:
    """Stash a session."""
    if not state_store:
        return {"error": "service not initialized"}
    from agentalloy.sessions import SessionManager

    mgr = SessionManager(state_store)
    snapshot = mgr.stash(session_key)
    if snapshot:
        return {"status": "stashed", "session_key": session_key}
    return {"status": "not_found", "session_key": session_key}


@app.post("/sessions/{session_key}/resume")
def session_resume(session_key: str) -> dict[str, Any]:
    """Resume a stashed session."""
    if not state_store:
        return {"error": "service not initialized"}
    from agentalloy.sessions import SessionManager

    mgr = SessionManager(state_store)
    result = mgr.resume(session_key)
    if result:
        return {"status": "resumed", "session_key": session_key, "phase": result.get("phase")}
    return {"status": "not_found", "session_key": session_key}


@app.post("/sessions/{session_key}/archive")
def session_archive(session_key: str) -> dict[str, Any]:
    """Archive a session."""
    if not state_store:
        return {"error": "service not initialized"}
    from agentalloy.sessions import SessionManager

    mgr = SessionManager(state_store)
    ok = mgr.archive(session_key)
    return {"status": "archived" if ok else "not_found", "session_key": session_key}


@app.post("/sessions/{session_key}/cancel")
def session_cancel(session_key: str) -> dict[str, Any]:
    """Cancel a session."""
    if not state_store:
        return {"error": "service not initialized"}
    from agentalloy.sessions import SessionManager

    mgr = SessionManager(state_store)
    ok = mgr.cancel(session_key)
    return {"status": "cancelled" if ok else "not_found", "session_key": session_key}


@app.get("/analytics")
def analytics() -> dict[str, Any]:
    """Telemetry analytics: tool usage, phase activity, error rates."""
    if not telemetry_store:
        return {"error": "telemetry not initialized"}
    return {
        "tool_usage": telemetry_store.tool_usage_summary(),
        "phase_activity": telemetry_store.phase_activity(),
        "stop_reasons": telemetry_store.stop_reason_distribution(),
        "error_rate": telemetry_store.error_rate(),
    }


@app.get("/lessons")
def lessons_list() -> dict[str, Any]:
    """List captured QA lessons."""
    if not state_store:
        return {"lessons": []}
    from agentalloy.compound import CompoundEngine

    engine = CompoundEngine(state_store, skill_engine or SkillEngine())
    return {
        "lessons": [
            {
                "slug": les.slug,
                "title": les.title,
                "phase": les.phase,
                "reference_count": les.reference_count,
                "promoted": les.promoted,
            }
            for les in engine.list_lessons()
        ]
    }


@app.get("/dashboard")
def dashboard() -> HTMLResponse:
    """Web UI dashboard."""
    from agentalloy.dashboard import DASHBOARD_HTML

    return HTMLResponse(content=DASHBOARD_HTML)


def start_server(host: str = "127.0.0.1", port: int = 48950) -> None:
    """Start the FastAPI server.

    Localhost by default: every endpoint is unauthenticated (the TheForge
    contract assumes a same-host client), so exposing beyond loopback is an
    explicit operator choice (--host / AGENTALLOY_HOST).
    """
    uvicorn.run(app, host=host, port=port)
