# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""``update`` subcommand — keep an existing install current.

Operator-tier. Per the install spec, this:
  1. Reports git status (warns if behind ``origin/main`` or has uncommitted changes).
  2. Verifies the corpus is intact via the same checks as ``seed-corpus``.
  3. Detects schema-version drift between code and corpus DB; runs in-place
     migrations if any are registered for the source→target version.
  4. Reports model-variant drift: if the default model names from
     ``recommend-models`` for the recorded ``preset`` differ from what
     ``install-state.json`` shows was pulled, surface them so the user
     can re-run ``pull-models``.
  5. Returns a structured summary of all changes detected/applied.

Migration framework: a ``MIGRATIONS`` dict maps ``(from_version,
to_version) → callable``. v1 ships with no registered migrations
(corpus is at schema 1; nothing to migrate from). Adding migration N→N+1
is the responsibility of whichever PR bumps the schema.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, render_lifecycle_result, write_result
from agentalloy.install.subcommands import pull_web

SCHEMA_VERSION = 1
STEP_NAME = "update"

# Corpus-schema migrations registered in code. Empty for v1; add entries
# as ``(from_version, to_version): migration_function`` when a corpus
# schema change ships.
MIGRATIONS: dict[tuple[int, int], Callable[[Path], None]] = {}


def _git_status(repo_root: Path) -> dict[str, Any]:
    """Report git state without mutating it: branch, uncommitted, behind/ahead."""
    if not (repo_root / ".git").exists():
        return {"is_git": False}

    def run(*args: str) -> str:
        result = subprocess.run(  # noqa: S603 — fixed args, no shell
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()

    info: dict[str, Any] = {"is_git": True}
    info["branch"] = run("rev-parse", "--abbrev-ref", "HEAD")
    info["dirty"] = bool(run("status", "--porcelain"))
    # Try to figure out behind/ahead vs origin/main without fetching.
    try:
        rev = run("rev-list", "--left-right", "--count", "origin/main...HEAD")
        if rev:
            # `--left-right --count origin/main...HEAD` emits "<left> <right>":
            # left = commits in origin/main not in HEAD (behind), right = ahead.
            behind, ahead = rev.split()
            info["behind_origin"] = int(behind)
            info["ahead_origin"] = int(ahead)
    except (ValueError, subprocess.SubprocessError):
        info["behind_origin"] = None
        info["ahead_origin"] = None
    return info


def _read_corpus_schema_version(duck_path: Path) -> int | None:
    """Read schema_version from the ``corpus_meta`` table if present."""
    if not duck_path.exists():
        return None
    try:
        import duckdb

        con = duckdb.connect(str(duck_path), read_only=True)
        try:
            row = con.execute(
                "SELECT value FROM corpus_meta WHERE key = 'schema_version' LIMIT 1"
            ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except duckdb.CatalogException:
            return None
        finally:
            con.close()
    except Exception:
        return None
    return None


def _stamp_corpus_schema_version(duck_path: Path, version: int) -> bool:
    """Write the ``schema_version`` marker in place (brief writer open).

    Returns False when the writer lock is unavailable — a running service or a
    concurrent ingest holds the file — leaving the caller to warn instead. In
    the ``upgrade`` flow the service is stopped when this runs, so the marker
    lands here and the old post-upgrade advice to run a full ``reembed
    --force`` (30–40 min on CPU) just to stamp one row never fires.
    """
    try:
        from agentalloy.storage.card_index import META_KEY_SCHEMA_VERSION
        from agentalloy.storage.skill_store import DuckDBSkillStore

        with DuckDBSkillStore(str(duck_path)) as store:
            store.migrate()  # legacy corpora may predate the corpus_meta table
            store.set_meta(META_KEY_SCHEMA_VERSION, str(version))
        return True
    except Exception:
        return False


def _expected_corpus_schema_version() -> int:
    """Code's expected corpus schema version. Bump when migrations land."""
    from agentalloy.install.subcommands.seed_corpus import EXPECTED_CORPUS_SCHEMA_VERSION

    return EXPECTED_CORPUS_SCHEMA_VERSION


def _run_migrations(
    duck_path: Path,
    from_version: int,
    to_version: int,
) -> list[dict[str, Any]]:
    """Walk MIGRATIONS dict from from_version to to_version, applying each."""
    applied: list[dict[str, Any]] = []
    cur = from_version
    while cur < to_version:
        step = (cur, cur + 1)
        if step not in MIGRATIONS:
            applied.append(
                {
                    "from": cur,
                    "to": cur + 1,
                    "applied": False,
                    "error": f"No migration registered for {cur} → {cur + 1}",
                }
            )
            return applied
        try:
            MIGRATIONS[step](duck_path)
            applied.append({"from": cur, "to": cur + 1, "applied": True})
        except Exception as exc:  # noqa: BLE001
            applied.append({"from": cur, "to": cur + 1, "applied": False, "error": str(exc)})
            return applied
        cur += 1
    return applied


def _model_drift(state: dict[str, Any]) -> dict[str, Any]:
    """Compare models recorded in install-state to what recommend-models defaults
    would currently produce for the same hardware + host_target. Returns a
    structured report; doesn't mutate anything.

    Drift is informational: the user is the one who decides whether to re-pull.
    """
    completed = state.get("completed_steps", [])
    recommend = next((s for s in completed if s.get("step") == "recommend-models"), None)
    pulled = state.get("models_pulled") or []

    if not recommend or not recommend.get("selected"):
        return {"checked": False, "reason": "recommend-models has not run"}

    selected = recommend["selected"]
    expected = [selected.get("embed_model"), selected.get("ingest_model")]
    expected = [m for m in expected if m]

    # `models_pulled` entries are stored as "runner:model" strings (e.g.
    # "ollama:embeddinggemma"); strip the runner prefix before comparing
    # against the bare model names from `recommend-models.selected`.
    pulled_bare = {p.split(":", 1)[1] if ":" in p else p for p in pulled}
    drifted = [m for m in expected if m not in pulled_bare]
    return {
        "checked": True,
        "recorded_pulls": pulled,
        "expected_models": expected,
        "drifted_models": drifted,
        "remediation": (
            f"Run `python -m agentalloy.install pull-models` to re-pull: {drifted}"
            if drifted
            else None
        ),
    }


def update(root: Path | None = None) -> dict[str, Any]:
    """Run the update flow. Returns a contract-shaped summary dict."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    t0 = time.monotonic()

    state = install_state.load_state(root)
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "git": _git_status(root),
        "corpus": {},
        "migrations": [],
        "models": {},
        "warnings": [],
    }

    # 1. Corpus presence + schema version (user-scoped corpus dir)
    user_corpus = install_state.corpus_dir()
    duck_path = user_corpus / "agentalloy.duck"
    fragments_path = user_corpus / "fragments.lance"
    if not duck_path.exists() or not fragments_path.exists():
        summary["corpus"] = {"present": False}
        summary["warnings"].append(
            f"Corpus missing at {user_corpus} — run "
            "`python -m agentalloy.install seed-corpus` to seed from the bundled wheel."
        )
    else:
        recorded = _read_corpus_schema_version(duck_path)
        expected = _expected_corpus_schema_version()
        summary["corpus"] = {
            "present": True,
            "path": str(user_corpus),
            "recorded_schema_version": recorded,
            "expected_schema_version": expected,
        }

        # 2. Schema migrations
        if recorded is None:
            # Missing marker means "current" (no migrations to run) — so stamp
            # it in place rather than telling the user to run a full corpus
            # rebuild for one metadata row. Falls back to a warning when the
            # file is held open (a running service blocks the brief writer).
            if _stamp_corpus_schema_version(duck_path, expected):
                summary["corpus"]["recorded_schema_version"] = expected
                summary["corpus"]["schema_version_stamped"] = True
            else:
                summary["warnings"].append(
                    f"Corpus predates the schema_version marker; treating as "
                    f"v{expected} (current — harmless). Could not stamp it in "
                    "place (the corpus DB is held open, likely by the running "
                    "service); the marker is written by the next `agentalloy "
                    "upgrade` or reembed pass."
                )
        elif recorded < expected:
            summary["migrations"] = _run_migrations(duck_path, recorded, expected)
            failed = [m for m in summary["migrations"] if not m.get("applied")]
            if failed:
                summary["warnings"].append(
                    f"Migration failed at step {failed[0]['from']}→{failed[0]['to']}: "
                    f"{failed[0].get('error')}. Restore by removing "
                    f"{user_corpus} and re-running `seed-corpus`."
                )
        elif recorded > expected:
            summary["warnings"].append(
                f"Corpus is at schema v{recorded}, code expects v{expected}. "
                "You may be on an older code revision than the corpus was built for. "
                "Run `pip install -U agentalloy` to update the code."
            )

    # 3. Model drift (informational)
    summary["models"] = _model_drift(state)

    # 3.5 Web UI bundle. The pre-5.2.0 upgrade orchestrator predates the
    # pull-web step, but it does run the NEW binary's `update` — ensuring the
    # bundle here closes the 5.1.x → 5.2.x transition (the orchestrator
    # restarts the service afterwards, which mounts it). Instant no-op when a
    # dist already resolves (env override, repo build, or pulled bundle);
    # failure is a warning — the API works without the UI.
    summary["web_ui"] = _ensure_web_bundle()
    if summary["web_ui"].get("error"):
        summary["warnings"].append(
            f"web UI bundle unavailable ({summary['web_ui']['error']}) — "
            "run `agentalloy pull-web` later, then restart the service."
        )

    # 4. Record the update step — but only if no migration failed. A failed
    #    migration recorded as "completed" would mask the problem on next
    #    run and make the install state lie about its corpus.
    failed_migrations = [m for m in summary.get("migrations", []) if not m.get("applied")]
    if not failed_migrations:
        install_state.record_step(state, STEP_NAME, extra={"summary": summary})
        install_state.save_state(state, root)

    summary["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return summary


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def _ensure_web_bundle() -> dict[str, Any]:
    """Make sure a web UI dist is servable; pull the release asset if not."""
    from agentalloy.web.spa import _dist_dir  # pyright: ignore[reportPrivateUsage]

    if _dist_dir() is not None:
        return {"present": True, "pulled": False}
    result = pull_web.pull_web_dist()
    if result["success"]:
        return {"present": True, "pulled": True, "dest": result["dest"]}
    return {"present": False, "pulled": False, "error": result["error"]}


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "update",
        help="Update an existing install: corpus migrations, model drift, git status.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    result = update()
    write_result(result, args, human_fn=lambda r: render_lifecycle_result(r, "Update"))
    # Non-zero exit if migrations failed
    failed_migrations = [m for m in result.get("migrations", []) if not m.get("applied")]
    if failed_migrations:
        return 2
    return 0


# Quiet unused import for `shutil` retained for future migration helpers
_ = shutil
_ = sys
