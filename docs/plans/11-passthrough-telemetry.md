# Plan #11 — Passthrough telemetry write-gap (§B)

**Batch:** CODE (no SkillVersion bump, no corpus re-embed, no image rebuild).
**Effort:** M (mechanical). **Risk:** low. **Sequencing:** PLAN-OF-ATTACK slot #2 — an
instrument, lands right after #1 (LOG_LEVEL). No dependency on any other todo.

## Problem (recap, verified against source)

The live native transport `POST /proj/{token}/v1/messages`
(`src/agentalloy/api/proxy_passthrough_router.py:221-284`,
`passthrough_anthropic_messages`) writes **zero** telemetry. It has no
`vector_store` dependency, never imports `write_proxy_trace`, and discards the
already-computed `InjectOutcome.telemetry: ProxyComposeTelemetry`. As a result
`composition_traces` is empty and `GET /telemetry/{traces,savings}` returns
nothing for every real Claude Code request. The OpenAI surface
(`proxy_router.py:347-408`, `_write_flow_telemetry`) does this correctly; this
plan ports the *write* onto the passthrough surface's existing `on_status` seam.

The fix also makes the Stage B `lm_assist_outcome='timeout'` finally land in the
store (the merged telemetry already carries it via
`_merge_compose_telemetry`, `proxy_apply.py:221`).

## Exact seam

`on_status(status: int)` already fires **exactly once per forward** at the moment
upstream status is known, on both paths:
- non-streaming: `_forward_once` → `on_status(upstream.status_code)`
  (`proxy_passthrough_router.py:311`)
- streaming: `_forward_streaming` → `on_status(upstream.status_code)` at stream
  open, before any chunk relays (`proxy_passthrough_router.py:350`)

It is built in the handler at `proxy_passthrough_router.py:264-274`:
```python
on_status: Callable[[int], None] = _noop_status          # 264
if payload is not None:
    try:
        session_id = extract_session_header(inbound_headers)
        injected, outcome = await _maybe_inject(...)      # 268-270
        if injected is not None:
            body_to_send = json.dumps(injected).encode("utf-8")
        if outcome is not None:
            on_status = _commit_on_2xx(decode_proj_token(token), outcome)  # 274
    except Exception:
        ...
```
The 2xx cadence-commit lives in `_commit_on_2xx`
(`proxy_passthrough_router.py:122-130`). We fold the telemetry write into this
same builder so it fires on the identical 2xx-gated seam.

Non-2xx (529/5xx) → `on_status` runs but `200 <= status < 300` is False → no
commit **and** no telemetry row. Connection-level failures (502 in
`_forward_once`/`_forward_streaming`) never call `on_status` → no row. Both are
the intended, deliberately-deferred error-path behavior (see "Deferred", below).

## Where phase/session/task come from — return `signal` from `_maybe_inject`

`_write_flow_telemetry` (the mirror) needs `phase`, `task_prompt`, `session_key`,
`session_source`, `repo`, the gate fields, and the merged compose telemetry. Every
one of these is already resolved **once** inside `evaluate_signal` and carried on
`SignalResult` (`proxy_signal.py:60-129`): `.phase`, `.task`, `.repo`,
`.session_key`, `.session_source`, `.pre_filter_matched`, `.gates_met`,
`.gates_unmet`, `.qwen_calls`, `.phase_gate_embed_failed`. All have safe dataclass
defaults, so even the early-return `SignalResult(should_compose=False)`
(`proxy_signal.py:389,410`) exposes them without `AttributeError`.

`_maybe_inject` currently computes the `SignalResult` and throws it away (returns
only `(payload, outcome)`). The minimal, non-duplicative wiring is to **return it**.
The `outcome.telemetry` (a `ProxyComposeTelemetry`) supplies the skill/fragment
provenance and `lm_assist_outcome`; `outcome.injected is not None` is the
`proxy_composed` discriminator (mirrors `proxy_router.py:525-527`, where the banner
does NOT flip `composed`).

## Change set — `src/agentalloy/api/proxy_passthrough_router.py`

### 1. Imports

`L41` add `get_vector_store`:
```python
from agentalloy.api.proxy_router import (
    get_embed_client,
    get_orchestrator_for_proxy,
    get_vector_store,
)
```
`L43` add `write_proxy_trace` + `SignalResult`:
```python
from agentalloy.api.proxy_signal import SignalResult, evaluate_signal
from agentalloy.api.proxy_telemetry import write_proxy_trace
```
`L45-47` TYPE_CHECKING block — add:
```python
    from agentalloy.storage.vector_store import VectorStore
```

### 2. `_maybe_inject` returns the signal — `L133-203`

Signature (`L139`) before→after:
```python
) -> tuple[dict[str, Any] | None, InjectOutcome[dict[str, Any]] | None]:
# →
) -> tuple[dict[str, Any] | None, InjectOutcome[dict[str, Any]] | None, SignalResult]:
```
`signal` is already bound at `L151` (`signal = await evaluate_signal(...)`).
Change the return at `L202-203`:
```python
    injected_payload = current if current is not payload else None
    return injected_payload, outcome, signal
```
Update the docstring tuple description (`L142-148`) to mention the third element
(the resolved `SignalResult`, used for the telemetry row).

### 3. Replace `_commit_on_2xx` with a unified `_make_on_status` — `L122-130`

`_commit_on_2xx` only committed markers and only when `outcome is not None`.
Generalize it so the same 2xx seam **also** writes one consolidated trace,
always (composed or passthrough), best-effort:

```python
def _make_on_status(
    project_dir: Path,
    outcome: InjectOutcome[dict[str, Any]] | None,
    vector_store: VectorStore | None,
    signal: SignalResult,
) -> Callable[[int], None]:
    """on_status for the forward: on a 2xx response commit the deferred cadence
    markers (iff a workflow block composed) AND write one consolidated proxy
    trace. Best-effort telemetry — the arg-construction is guarded and
    write_proxy_trace is internally soft-failing, so neither can break the
    forward. Non-2xx commits nothing and records nothing (the model never
    processed the turn)."""

    def on_status(status: int) -> None:
        ok = 200 <= status < 300
        if outcome is not None:
            commit_outcome(project_dir, outcome, upstream_ok=ok)
        if ok and vector_store is not None:
            try:
                _write_passthrough_trace(vector_store, signal, outcome)
            except Exception:  # noqa: BLE001 — telemetry never breaks the forward
                logger.warning("passthrough telemetry write failed", exc_info=True)

    return on_status
```
Keep `_noop_status` (`L117-119`) for the non-JSON / `payload is None` branch.

### 4. New `_write_passthrough_trace` (mirror of `_write_flow_telemetry`)

Place directly after `_make_on_status`. This is the byte-for-byte analogue of
`proxy_router.py:347-408`, sourcing every field from `signal` + `outcome.telemetry`:
```python
def _write_passthrough_trace(
    vector_store: VectorStore,
    signal: SignalResult,
    outcome: InjectOutcome[dict[str, Any]] | None,
) -> None:
    """One consolidated CompositionTrace for a passthrough forward.

    status = 'proxy_composed' when the workflow block was injected, else
    'proxy_passthrough'. The banner alone does NOT count as composed (it produces
    no outcome). Mirrors proxy_router._write_flow_telemetry."""
    composed = outcome is not None and outcome.injected is not None
    tel = outcome.telemetry if outcome is not None else None
    scores_json = (
        json.dumps(tel.lm_assist_scores) if tel and tel.lm_assist_scores else None
    )
    write_proxy_trace(
        vector_store,
        phase=signal.phase or "unspecified",
        task_prompt=signal.task or "",
        status="proxy_composed" if composed else "proxy_passthrough",
        pre_filter_matched=signal.pre_filter_matched,
        gates_met=signal.gates_met,
        gates_unmet=signal.gates_unmet,
        qwen_calls=signal.qwen_calls,
        total_latency_ms=None,  # not timed on this surface (parity-deferred)
        source_skill_ids=tel.returned_skill_ids if tel else None,
        system_skill_ids=tel.header_fragment_ids if tel else None,
        workflow_skill_ids=tel.workflow_skill_ids if tel else None,
        selected_fragment_ids=tel.selected_fragment_ids if tel else None,
        tokens_returned=tel.tokens_returned if tel else 0,
        tokens_flat_equivalent=tel.tokens_flat_equivalent if tel else 0,
        reranked=tel.reranked if tel else False,
        lm_assist_outcome=tel.lm_assist_outcome if tel else "disabled",
        lm_assist_model=tel.lm_assist_model if tel else None,
        lm_assist_kept_ids=tel.lm_assist_kept_ids if tel else None,
        lm_assist_dropped_ids=tel.lm_assist_dropped_ids if tel else None,
        lm_assist_scores=scores_json,
        dense_leg_degraded=tel.dense_leg_degraded if tel else False,
        phase_gate_embed_failed=signal.phase_gate_embed_failed,
        repo=signal.repo,
        session_key=signal.session_key,
        session_source=signal.session_source,
    )
```
Note: `task_prompt` uses `signal.task` (the same first-user-message text
`evaluate_signal` already extracted via `_extract_task_from_messages`); no need to
re-derive or call `proxy_router._extract_task_prompt`. `write_proxy_trace`
truncates to 500 chars itself (`proxy_telemetry.py:82`).

### 5. Handler signature + on_status wiring — `L221-284`

Add the dep (`L225-227` block):
```python
    vector_store: VectorStore | None = Depends(get_vector_store),
```
Rewire `L264-277`:
```python
    on_status: Callable[[int], None] = _noop_status
    if payload is not None:
        try:
            session_id = extract_session_header(inbound_headers)
            injected, outcome, signal = await _maybe_inject(
                payload, token, embed_client, orchestrator, session_id
            )
            if injected is not None:
                body_to_send = json.dumps(injected).encode("utf-8")
            on_status = _make_on_status(
                decode_proj_token(token), outcome, vector_store, signal
            )
        except Exception:
            logger.warning("passthrough compose/inject failed; forwarding original", exc_info=True)
            body_to_send = raw_body
```
Key change from today: `on_status` is now set **unconditionally** when
`_maybe_inject` succeeds (previously only when `outcome is not None`), so the
passthrough (nothing-composed) case also gets a row. The `except` branch leaves
`on_status = _noop_status` → a compose-path exception writes no row (acceptable;
error-path parity is deferred).

`decode_proj_token(token)` is called once more here (as it is today at L274) — a
pure, cheap token→Path parse. Left as-is to keep the diff minimal.

## Decision required before coding

**None blocking.** One confirm: error-path parity is **out of scope** (OpenAI
writes ERROR rows on 5xx/timeout via `_write_flow_telemetry`; passthrough's
502/non-2xx paths write nothing). PLAN-OF-ATTACK §B "Open scope" + §7 mark this
deliberately deferred — proceed unless the owner says otherwise. (If later wanted,
add an ERROR-row write in the `except httpx.HTTPError` branches of
`_forward_once`/`_forward_streaming` — separate follow-up.)

## Tests — `tests/test_proxy_passthrough_native.py`

The file's `_make_app` (`L73-91`) currently sets `app.state.vector_store =
MagicMock()`. Existing tests stay green unchanged: on 2xx the handler now calls
`write_proxy_trace(MagicMock(), ...)` → `MagicMock.record_composition_trace` is a
no-op; none of those tests assert telemetry. Add a real-store variant for the new
cases.

Add at top: `from agentalloy.storage.vector_store import VectorStore, open_or_create`.
Add a helper that swaps a real store in and returns it (pattern from
`tests/test_proxy_compose_telemetry.py:166-184`):
```python
def _make_app_with_store(captured, store, *, orchestrator=None, sse=None, status=200):
    app = _make_app(captured, orchestrator=orchestrator, sse=sse, status=status)
    app.state.vector_store = store
    return app
```
Patch target for a known signal is the existing `_SIGNAL =
"agentalloy.api.proxy_passthrough_router.evaluate_signal"` (`L29`). Build signals
carrying `repo`/`session_key`/`session_source` so the row asserts them.

New test names + intent:

- **`test_tc_passthrough_writes_single_passthrough_row`** — patch `_SIGNAL` →
  `SignalResult(should_compose=False, phase="build", repo=str(tmp_path),
  session_key="sess-1", session_source="header", task="the real task")`. POST
  non-stream. Assert `rows = store.query_traces(limit=10)`; `len(rows)==1`;
  `row.status=="proxy_passthrough"`; `row.event_type=="proxy_request"`;
  `row.session_key=="sess-1"`; `row.session_source=="header"`;
  `row.repo==str(tmp_path)`; `row.source_skill_ids==[]`;
  `row.lm_assist_outcome=="disabled"`.

- **`test_tc_composed_writes_composed_row_with_skills`** — orchestrator from the
  existing `_orchestrator("WF")` helper; patch `_SIGNAL` →
  `SignalResult(should_compose=True, phase="build", announce=True,
  workflow_prose="OPERATE LIKE THIS", workflow_skill_id="wf-build",
  repo=str(tmp_path), session_key="sess-1", session_source="header",
  task="t")`. POST non-stream. Assert 1 row; `row.status=="proxy_composed"`;
  `row.workflow_skill_ids==["wf-build"]` (header populated). (No
  `current_contract` → `source_skill_ids` empty; asserting the workflow header is
  enough to prove the composed branch + telemetry merge fired.)

- **`test_tc_streaming_writes_exactly_one_row`** — `sse=b"data: {...}\n\n"`,
  `stream=True`, patch `_SIGNAL` to the composed signal above. POST. Assert
  `len(store.query_traces(limit=10))==1` (written once at stream open via
  `_forward_streaming`'s `on_status`, not per chunk). Drain the response first so
  the relay generator runs.

- **`test_tc_non2xx_writes_no_row`** — `status=529`, patch `_SIGNAL` to the
  composed signal. POST non-stream. Assert `resp.status_code==529` and
  `store.query_traces(limit=10)==[]` (2xx gate suppresses the write; mirrors the
  existing `test_announce_marker_not_committed_on_upstream_529` at `L350`).

- **`test_tc_compose_exception_still_forwards_no_row`** (regression for the
  `except` branch) — patch `_SIGNAL` to raise. POST. Assert `resp.status_code==200`
  (original forwarded) and `query_traces()==[]`. Confirms a compose-path
  exception never breaks the forward and leaves `on_status=_noop_status`.

Use `open_or_create(tmp_path / "tele.duck")` as a context manager (or a fixture
like `test_proxy_compose_telemetry.py:165-168`) so the DuckDB file is closed.

## Files touched

- `src/agentalloy/api/proxy_passthrough_router.py` — imports (L41,43,45-47),
  `_maybe_inject` return (L139,202-203), replace `_commit_on_2xx`→`_make_on_status`
  (L122-130) + new `_write_passthrough_trace`, handler dep + wiring (L225-227,
  L264-277).
- `tests/test_proxy_passthrough_native.py` — 5 new tests + 1 real-store helper +
  1 import.

## Cross-item conflict notes

- **`_maybe_inject` return-shape change** is contained to this file; no other
  surface imports it (`proxy_router.py` has its own inline compose). The re-exported
  `__all__` (`L55`) is `["_ComposedBlock", "_compose_block", "router"]` — unchanged.
- **Sibling todos that touch this file:** none directly. Stage B work (§C/§D, todos
  touching `lm_assist.py`/`domain.py`/`rerank.py`) only changes the *values* inside
  `ProxyComposeTelemetry.lm_assist_*`; this plan just persists whatever those legs
  emit, so it composes cleanly and is what makes their `timeout`/`hit` outcomes
  observable. No shared file edits.
- **`proxy_router.py`** is read-only here (we import `get_vector_store`,
  `_write_flow_telemetry` stays the mirror). The §A LOG_LEVEL todo edits `app.py`,
  not this router — no overlap. The §A "rerank INFO line" companion may add a log
  call in `lm_assist.py`/`domain.py` — disjoint from this file.

## Risk / expected behavior shift

- Telemetry will **suddenly start recording `timeout` `lm_assist_outcome` rows**
  for the live transport (PLAN-OF-ATTACK risk #6). Real, not a new bug — it is the
  point of this change.
- Volume: one `composition_traces` insert per forwarded `/v1/messages` request.
  `record_composition_trace` is the same write the OpenAI surface already does per
  request; no new contention beyond the existing DuckDB writer.
