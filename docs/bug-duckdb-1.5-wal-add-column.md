# UPSTREAM (duckdb/duckdb): 1.5.x cannot replay `ALTER TABLE ... ADD COLUMN` from the WAL when the writer died before checkpointing

Status: **open — upstream** · Drafted: 2026-11-19 · Upstream: duckdb/duckdb 1.5.x (we pin `duckdb>=1.5.4`, lock 1.5.4) · Severity: **medium** (store becomes unopenable after a crash in a narrow migration window)

This is a draft of the bug report to file upstream, kept here so the context
survives. Before filing: capture the exact replay error text from an incident
log (checklist at the bottom) and confirm against the latest 1.5.x / 1.6.x
point releases — the limitation may already be fixed in a release newer than
ours.

## Symptom

A DuckDB file whose WAL contains an `ALTER TABLE ... ADD COLUMN` statement
cannot be reopened after the writing process dies before a checkpoint: WAL
replay fails on the `ADD COLUMN` and the database is unusable, even though
the file was healthy at crash time.

Two trigger shapes were seen:

1. **First-boot** — schema created and then extended in the same, still-uncheckpointed
   WAL (`CREATE TABLE` followed by `ALTER ... ADD COLUMN` during startup
   migration). This is the original incident: a fresh store's first boot died
   before checkpoint, and the next boot could not replay the WAL.
2. **Upgrade-in-place** — a store from an older app version (table already
   checkpointed) is opened, migration issues `ADD COLUMN`, the process dies
   before checkpoint, next boot fails replay.

## Repro (draft)

```python
import duckdb, os

DB = "/tmp/wal_add_col.duck"

# Process A: create + extend in one uncheckpointed session, then hard-kill.
con = duckdb.connect(DB)
con.execute("CREATE TABLE t (a INTEGER)")
con.execute("INSERT INTO t VALUES (1), (2)")
con.execute("ALTER TABLE t ADD COLUMN b TEXT")   # lands in the WAL only
con.close()  # checkpoint happens here — for the repro, kill the process
             # before close() (e.g. os._exit(1)) so the WAL survives
```

Then reopen `DB` in a fresh process: WAL replay is expected to fail on the
`ADD COLUMN` statement (capture the exact error — see checklist).

If the create+alter in the same WAL does not trigger it, use the
upgrade-in-place shape: create `t` in process A and checkpoint; in process B
issue only the `ALTER TABLE t ADD COLUMN b TEXT` and kill before close;
reopen in process C.

## Impact for AgentAlloy

- Affected tables: `sdd_artifact.status` (`state_store.migrate()`,
  `ALTER TABLE sdd_artifact ADD COLUMN status TEXT` + backfill UPDATE) and
  `sdd_state.stream_id` (same pattern). A crash between the `ALTER` and the
  next checkpoint made `state.duck` unopenable — the per-repo service could
  not start and no state could be read.
- Past incident handling: the WAL was quarantined (moved aside) and the
  schema rebuilt, so the uncheckpointed tail of the WAL was lost — no data
  was actually lost in practice because the migration's own writes were the
  only uncheckpointed content, but the quarantine step is a manual,
  data-reducing recovery.

## Workaround in place

New stores no longer hit the first-boot trigger: `status` (and
`stream_id`) are part of the base `CREATE TABLE` DDL, so a fresh store's
first-boot WAL carries no `ADD COLUMN` record (see the DDL comment in
`src/agentalloy/storage/state_store.py`). Upgrade-in-place of a genuinely
old store still issues the `ADD COLUMN`, so the window remains for those;
we keep the crash-window small (checkpoint right after migrate) and the
quarantine procedure documented.

## Filing checklist

- [ ] Capture exact replay error text + DuckDB version from an incident log
- [ ] Test the repro on the latest 1.5.x and 1.6.x releases (may be fixed)
- [ ] Note Python client vs C++ behavior if different (we hit it via the
      Python client 1.5.4)
- [ ] File at duckdb/duckdb with both trigger shapes; tag `WAL`, `DDL`
