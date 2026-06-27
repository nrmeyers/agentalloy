# TODO #8 — LOG_LEVEL fix (§A): app loggers pinned at WARNING

**Batch:** CODE (no corpus re-embed, no SkillVersion bump). **Effort:** S. **Risk:** low.
**Depends on:** nothing. Can land first / in parallel.

## Problem (confirmed against source)

`LOG_LEVEL` is parsed into `Settings.log_level` (`config.py:68`) but **never applied** to the
`agentalloy.*` logger namespace in any deployed entrypoint:

- The only `logging.basicConfig` is in `__main__.py:15`, reachable **only** via `python -m
  agentalloy`. The systemd unit (`enable_service.py:150/153`), the launchd plist
  (`enable_service.py:439-444`), and the container entrypoint (`container_runtime.py:613`,
  baked at `container/entrypoint.sh:222`) all launch `uvicorn agentalloy.app:app` **directly**,
  so `__main__.main()` never runs.
- `app = create_app()` (`app.py:277`, module-level, runs at import) configures **no logging**.
- uvicorn's `--log-level` only touches `uvicorn.*` loggers — never root, never `agentalloy.*`.
  The container even hardcodes `--log-level info` (a blind spot for app diagnostics) **and**
  hardcodes the container process env `"LOG_LEVEL": "info"` (`container_runtime.py:709`), so the
  container can never honor a `DEBUG` request even after the app-side fix.

Net: every `agentalloy.*` logger sits at the WARNING default (no handler accepts INFO/DEBUG, and
the `logging.lastResort` handler is WARNING-level), so INFO/DEBUG app diagnostics are invisible in
journalctl / launchd logs / container logs. There is also **no Stage B verdict log line** at
INFO/DEBUG today (only telemetry + `logger.warning` on error), so even once the level is fixed
there is nothing to surface.

## Decisions (made here — confirm before coding)

1. **Helper lives in `config.py`** (not `app.py`). `config.py` owns `get_settings`, is imported by
   every entrypoint, and lets `__main__.py` reuse it. App stays import-light.
2. **Container uvicorn flag is parameterized, not removed.** Entrypoint `--log-level info` →
   `--log-level "${LOG_LEVEL:-info}"`; the container env `LOG_LEVEL` is made host-honoring **and
   lowercased** (`os.environ.get("LOG_LEVEL","info").lower()`) so uvicorn (which requires lowercase
   level names) never chokes on a `DEBUG`/`INFO` value from the presets (presets emit uppercase
   `LOG_LEVEL: "INFO"`).
3. **systemd/launchd generators are NOT source-edited.** Both already pass **no** `--log-level` and
   feed `LOG_LEVEL` through the EnvironmentFile / inlined plist env; the app-side helper fixes them
   transparently. Per §A: *do not* re-add a `--log-level` flag (that rescues only systemd and
   re-couples observability to the ExecStart string). The "fix" for `enable_service.py` is a
   **config-consistency guard test** that locks the app-side approach (asserts no `--log-level`
   flag re-appears). This is the correct reading of §A's recommendation.
4. **Log format** reuses `__main__`'s string `"%(levelname)s %(name)s: %(message)s"`.
5. **Stage B INFO line** logs `outcome / kept / dropped / k / candidates` at the arbitrate decision
   site (`domain.py:597`), firing on HIT **and** on DISABLED/TIMEOUT/ERROR fall-through.

---

## Exact changes

### EDIT 1 — `src/agentalloy/config.py`: add `configure_logging()` helper

Add to `__all__` (line 12): `"configure_logging"`. Add the function after `get_settings`
(`config.py:154-161`):

```python
def configure_logging(level: str | None = None) -> None:
    """Install a root handler and pin the ``agentalloy`` namespace to LOG_LEVEL.

    Called at the top of ``create_app`` (and from ``__main__``) so every entrypoint —
    ``python -m agentalloy``, ``uvicorn agentalloy.app:app`` (systemd/launchd), and the
    container ``uv run uvicorn`` — applies ``LOG_LEVEL`` to ``agentalloy.*`` loggers.
    uvicorn's ``--log-level`` only touches ``uvicorn.*``; this is the missing piece.

    Idempotent: ``basicConfig`` adds at most one root handler (its handler is NOTSET, so
    it passes every record it receives); the explicit ``setLevel`` re-applies on every call
    so a later ``create_app`` with a changed ``LOG_LEVEL`` still takes effect, and wins even
    when uvicorn or pytest installed a handler first (uvicorn's dictConfig has no ``root``
    key and ``disable_existing_loggers=False``, so it never resets the ``agentalloy`` logger).
    """
    name = (level or get_settings().log_level).upper()
    lvl = getattr(logging, name, logging.INFO)
    logging.basicConfig(level=lvl, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("agentalloy").setLevel(lvl)
```

`logging` is already imported (`config.py:5`). No new imports.

### EDIT 2 — `src/agentalloy/app.py`: call it first in `create_app()`

- Line 31: `from agentalloy.config import get_settings` → `from agentalloy.config import configure_logging, get_settings`.
- Insert as the **first statement** of `create_app()` (between the docstring ending line 235 and
  `app = FastAPI(...)` at line 236):

```python
    configure_logging()
    app = FastAPI(
```

Because `app = create_app()` runs at import (`app.py:277`), this fires for **every** entrypoint and
survives future ExecStart edits.

### EDIT 3 — `src/agentalloy/__main__.py`: replace inline basicConfig with the helper

Before (`__main__.py:5,9,13-18`):
```python
import logging
...
from agentalloy.config import get_settings
...
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
```
After:
```python
from agentalloy.config import configure_logging, get_settings
...
    settings = get_settings()
    configure_logging(settings.log_level)
```
Drop the now-unused `import logging` (line 5). Keep `settings` (still feeds
`uvicorn.run(..., log_level=settings.log_level.lower())` at line 23, which governs `uvicorn.*`).

### EDIT 4 — `src/agentalloy/retrieval/domain.py:597`: companion Stage B INFO line

`logger` is already `logging.getLogger(__name__)` = `agentalloy.retrieval.domain` (line 54). Insert
immediately after the `_maybe_lm_arbitrate` call (current line 597), before `if lm_selected is not None:`:

```python
        lm_selected, lm_outcome, lm_detail = _maybe_lm_arbitrate(ranked, query, k)
        logger.info(
            "stage-b verdict: outcome=%s kept=%d dropped=%d k=%d candidates=%d",
            lm_outcome.value,
            len(lm_detail.kept_ids) if lm_detail else 0,
            len(lm_detail.dropped_ids) if lm_detail else 0,
            k,
            len(ranked),
        )
```

This gives journalctl/launchd/container logs a Stage B verdict on HIT (kept/dropped populated) and
on DISABLED/TIMEOUT/ERROR (kept=0, dropped=0) — the thing the level fix surfaces. **Note for the
batch:** §D (Stage B selection) edits this exact `597-602` region; the INFO line must be rebased
above whatever selection logic §D lands (keep it directly after the `_maybe_lm_arbitrate` return).

### EDIT 5 — `src/agentalloy/install/subcommands/container_runtime.py:613`: parameterize uvicorn level

Inside `_build_entrypoint_script` (def at `container_runtime.py:305`), the generated line:
```python
            "uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &",
```
→
```python
            'uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level "${LOG_LEVEL:-info}" &',
```
(switch the Python string to single quotes so the embedded `"${LOG_LEVEL:-info}"` shell quoting is literal.)

### EDIT 6 — `src/agentalloy/install/subcommands/container_runtime.py:709`: honor host LOG_LEVEL

In `run_container`'s env dict:
```python
        "LOG_LEVEL": "info",
```
→
```python
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "info").lower(),
```
`os` is imported (`container_runtime.py:10`). `.lower()` guarantees the container env value (and the
`${LOG_LEVEL}` uvicorn now reads) is uvicorn-safe even when the host exports `LOG_LEVEL=DEBUG`.
`Settings.log_level` inside the container reads this value; `configure_logging` `.upper()`s it again.

### EDIT 7 — regenerate `container/entrypoint.sh` (drift guard)

`container/entrypoint.sh:222` must stay **byte-identical** to `_build_entrypoint_script("")` or
`tests/test_container_edge_cases.py::TestBakedEntrypoint::test_baked_entrypoint_matches_generated`
(line 1485) fails. After EDIT 5, regenerate:

```bash
~/.local/share/uv/tools/agentalloy/bin/python -c "from agentalloy.install.subcommands.container_runtime import _build_entrypoint_script; open('container/entrypoint.sh','w').write(_build_entrypoint_script(''))"
```
(run from the repo root; the in-repo `.venv` python also works.) Confirm line 222 becomes
`uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level "${LOG_LEVEL:-info}" &`.

### EDIT 8 — `enable_service.py`: NO source change (guard test only)

`_render_systemd_unit` (147) and `_render_launchd_plist` (422) already pass no `--log-level` and
carry `LOG_LEVEL` via env. Add the guard in EDIT-T2 below so the app-side fix can't regress.

---

## Tests

### NEW `tests/test_app_logging.py`

- `test_create_app_pins_agentalloy_logger_to_debug` — `monkeypatch.setenv("LOG_LEVEL","DEBUG")`,
  clear `get_settings` cache if any, `create_app(use_default_lifespan=False)`, assert
  `logging.getLogger("agentalloy").getEffectiveLevel() == logging.DEBUG`.
- `test_configure_logging_idempotent_single_root_handler` — save/restore `logging.root.handlers`;
  set them to `[]`; call `configure_logging("INFO")` twice; assert `len(logging.root.handlers) == 1`.
- `test_caplog_captures_agentalloy_info_record` — `caplog.set_level(logging.INFO, logger="agentalloy")`;
  `configure_logging("INFO")`; `logging.getLogger("agentalloy.x").info("hello")`; assert the record
  is captured (proves an INFO record on the `agentalloy.*` namespace survives to a handler).
- `test_changed_level_on_second_call` — `configure_logging("INFO")` then `configure_logging("DEBUG")`;
  assert `getLogger("agentalloy").getEffectiveLevel() == logging.DEBUG` (explicit `setLevel` wins
  even though `basicConfig` no-ops on the second call).
- `test_stage_b_verdict_logged_at_info` — drive `retrieve_domain_candidates` (or the narrowest unit
  that reaches `domain.py:597`) with a stub source so `_maybe_lm_arbitrate` returns DISABLED;
  `caplog.set_level(logging.INFO, logger="agentalloy.retrieval.domain")`; assert a record matching
  `"stage-b verdict: outcome=disabled"` is present. (Reuse stub-source patterns from the existing
  domain/retrieval tests.)

### EDIT-T1 `tests/test_container_edge_cases.py`

- Add `test_entrypoint_uvicorn_log_level_from_env`: `_build_entrypoint_script("")` contains
  `--log-level "${LOG_LEVEL:-info}"` and **not** `--log-level info &`.
- `test_baked_entrypoint_matches_generated` (existing, line 1485) passes automatically after EDIT 7.

### EDIT-T2 `tests/test_config_consistency.py` (config-consistency guards — the §A "fix-app-side" lock)

- `test_systemd_unit_has_no_log_level_flag` — import `_render_systemd_unit`; render with a tmp
  `repo_root`/`env_path`/`port`; assert `"--log-level" not in unit`.
- `test_launchd_plist_has_no_log_level_flag` — import `_render_launchd_plist`; assert
  `"--log-level" not in plist`.
- `test_container_env_log_level_lowercased` — `monkeypatch.setenv("LOG_LEVEL","DEBUG")`; assert the
  `run_container` env dict value is `"debug"` (extract the dict via a small refactor or by asserting
  on the `-e LOG_LEVEL=debug` arg in the built command; prefer reading the literal at
  `container_runtime.py:709` through a tiny helper if `run_container` is hard to invoke hermetically).
- `test_preset_log_level_present` (optional) — each preset YAML under
  `install/presets/{cpu,nvidia,radeon,apple-silicon}.yaml` carries `LOG_LEVEL` so the EnvironmentFile
  always supplies it.

### Manual / on-host check
After deploy: `journalctl -u agentalloy -f` with `LOG_LEVEL=DEBUG` shows the `stage-b verdict:` line
on each compose; container: `podman logs agentalloy | grep 'stage-b verdict'`.

---

## Files

| Path | Action |
|---|---|
| `src/agentalloy/config.py` | EDIT — add `configure_logging()` + `__all__` |
| `src/agentalloy/app.py` | EDIT — import + call at top of `create_app` (line 31, 236) |
| `src/agentalloy/__main__.py` | EDIT — call helper, drop inline `basicConfig`/`import logging` |
| `src/agentalloy/retrieval/domain.py` | EDIT — Stage B INFO line at :597 (shared region with §D) |
| `src/agentalloy/install/subcommands/container_runtime.py` | EDIT — :613 flag, :709 env (shared with §C) |
| `container/entrypoint.sh` | REGEN — drift guard (shared with §C) |
| `tests/test_app_logging.py` | NEW |
| `tests/test_container_edge_cases.py` | EDIT — entrypoint flag assertion |
| `tests/test_config_consistency.py` | EDIT — no-`--log-level` guards (locks `enable_service.py`) |

`enable_service.py` is **referenced** (guard test imports its renderers) but **not modified** — by
design (§A: keep the fix app-side).

## Cross-item conflicts (batch coordination)

- **`retrieval/domain.py`** — §D (Stage B selection) rewrites the `597-602` arbitrate block; §E
  (retrieval budget/fusion) edits nearby. Land the INFO line as a 1-statement insert directly after
  `_maybe_lm_arbitrate(...)` returns so it rebases cleanly on top of §D's selection changes.
- **`container_runtime.py` / `container/entrypoint.sh`** — §C may also touch the container reranker
  launch in the same entrypoint generator. **Regenerate `entrypoint.sh` once, after both §A and §C
  land**, to avoid two competing baked-file diffs.
- **`config.py`** — isolated additive helper; no overlap with §D's `LM_ASSIST_KEEP_THRESHOLD` or
  §H's predicate work.
- No corpus / SkillVersion / re-embed involvement: pure code batch.
