# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false, reportArgumentType=false
"""``seed-corpus`` subcommand — presence + integrity check.

The corpus ships in the repo at ``data/skills.duck`` and ``data/ladybug``.
This subcommand verifies both files exist, the schema version matches,
and the skill count meets the minimum threshold. No network calls.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1

# The corpus schema version the code expects.
# Bump this when a migration changes the DB schema.
EXPECTED_CORPUS_SCHEMA_VERSION = 1

# Sanity floor — flags truly empty corpora; not a quality bar.
MIN_SKILL_COUNT = 25


def _check_duckdb(duck_path: Path) -> dict[str, Any]:
    """Query DuckDB for skill/fragment counts, embedding metadata, and embedded
    schema_version (if the ``corpus_meta`` table is present).

    Returns ``corpus_schema_version_recorded=None`` if the corpus pre-dates the
    metadata table (callers treat this as "implicit v1" with a soft warning).
    """
    import duckdb

    con = duckdb.connect(str(duck_path), read_only=True)
    try:
        frag_count = con.execute("SELECT count(*) FROM fragment_embeddings").fetchone()[0]  # type: ignore[index]
        skill_count = con.execute(
            "SELECT count(DISTINCT skill_id) FROM fragment_embeddings"
        ).fetchone()[0]  # type: ignore[index]
        emb_model_row = con.execute(
            "SELECT DISTINCT embedding_model FROM fragment_embeddings LIMIT 1"
        ).fetchone()
        embedding_model = emb_model_row[0] if emb_model_row else None  # type: ignore[index]
        # Probe embedding dimension from first row
        dim_row = con.execute(
            "SELECT array_length(embedding) FROM fragment_embeddings LIMIT 1"
        ).fetchone()
        embedding_dim = dim_row[0] if dim_row else None  # type: ignore[index]

        # Read schema_version from corpus_meta table if it exists.
        # The table is written by the ingest CLI; corpora ingested before this
        # was added will not have the table — that's treated as "unrecorded".
        recorded_version: int | None = None
        try:
            row = con.execute(
                "SELECT value FROM corpus_meta WHERE key = 'schema_version' LIMIT 1"
            ).fetchone()
            if row and row[0] is not None:
                recorded_version = int(row[0])  # type: ignore[index]
        except duckdb.CatalogException:
            recorded_version = None
    finally:
        con.close()

    return {
        "skill_count": skill_count,
        "fragment_count": frag_count,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "corpus_schema_version_recorded": recorded_version,
    }


def _check_ladybug(ladybug_path: Path) -> int:
    """Query LadybugDB for skill count."""
    import ladybug

    db = ladybug.Database(str(ladybug_path))
    conn = ladybug.Connection(db)
    result = conn.execute("MATCH (s:Skill) RETURN count(s) AS c")
    count = 0
    if result.has_next():
        count = result.get_next()[0]
    return count


def _initialize_empty_corpus(user_corpus: Path) -> None:
    """Initialize LadybugDB schema + DuckDB stores in an empty corpus dir.

    LadybugDB requires explicit ``migrate()`` to create the Skill /
    SkillVersion / Fragment node tables and edge tables. Without this
    step, ``ingest`` fails with "Table Skill does not exist". DuckDB
    schemas are created automatically by ``open_or_create``.
    """
    user_corpus.mkdir(parents=True, exist_ok=True)
    duck_path = user_corpus / "skills.duck"
    ladybug_path = user_corpus / "ladybug"

    from agentalloy.storage.ladybug import LadybugStore
    from agentalloy.storage.vector_store import open_or_create

    with LadybugStore(str(ladybug_path)) as store:
        store.migrate()
    with open_or_create(duck_path) as _:
        pass


def check_corpus(root: Path | None = None) -> dict[str, Any]:  # noqa: ARG001 — back-compat
    """Run the seed-corpus presence + integrity check.

    The corpus lives at ``${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/``
    (user-scoped). The wheel no longer ships a pre-built corpus; this step
    initializes an empty corpus (LadybugDB + DuckDB) so the subsequent
    ``install-packs`` step can populate it from chosen packs.
    """
    t0 = time.monotonic()

    user_corpus, was_seeded = install_state.ensure_corpus_seeded()

    duck_path = user_corpus / "skills.duck"
    ladybug_path = user_corpus / "ladybug"

    # New flow: if the bundled corpus is empty (post-pack-refactor wheels),
    # initialize empty stores. Don't return missing_files — that's the old
    # behavior from when the wheel shipped a populated corpus.
    if not duck_path.exists() or not ladybug_path.exists():
        try:
            _initialize_empty_corpus(user_corpus)
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "init_failed",
                "error": f"Could not initialize empty corpus stores: {exc}",
                "remediation": (
                    "Verify ${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/ "
                    "is writable and re-run `agentalloy seed-corpus`."
                ),
                "duration_ms": duration_ms,
            }
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "initialized_empty",
            "corpus_path": str(user_corpus),
            "skill_count": 0,
            "fragment_count": 0,
            "remediation": (
                "Empty corpus initialized. Run `agentalloy install-packs` to opt into skill packs."
            ),
            "duration_ms": duration_ms,
        }

    # The remaining paths (failed metadata read, under-minimum skill count)
    # are integrity issues on a pre-existing populated corpus, not the
    # fresh-install case. Their remediation message points at install-pack
    # (which can repopulate from a known-good source).
    remediation = (
        "Corpus integrity check failed. Run `python -m agentalloy.migrate` to "
        "ensure the graph schema exists (idempotent), then `agentalloy "
        "install-packs` to repopulate from packs, or remove "
        "${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/ to start fresh."
    )

    # 2. Read DuckDB metadata
    try:
        duck_meta = _check_duckdb(duck_path)
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "missing_files",
            "error": f"Cannot read DuckDB: {exc}",
            "remediation": remediation,
            "duration_ms": duration_ms,
        }

    # 3. Schema version check
    # The ingest CLI writes schema_version into a `corpus_meta` table. Corpora
    # built before that change won't have the table — those are treated as
    # implicit v1 with a soft warning surfaced in the output.
    recorded = duck_meta.get("corpus_schema_version_recorded")
    if recorded is None:
        # Implicit v1 — pre-dates corpus_meta. Pass through but flag.
        corpus_schema_version = EXPECTED_CORPUS_SCHEMA_VERSION
        schema_warning: str | None = (
            "corpus_meta table not present; treating corpus as implicit "
            f"v{EXPECTED_CORPUS_SCHEMA_VERSION}. Run `python -m agentalloy.ingest "
            "--write-corpus-meta` (or re-ingest) to make the schema version explicit."
        )
    elif recorded != EXPECTED_CORPUS_SCHEMA_VERSION:
        # Real schema mismatch — abort with the contract's documented action.
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "schema_mismatch",
            "corpus_schema_version": recorded,
            "expected_corpus_schema_version": EXPECTED_CORPUS_SCHEMA_VERSION,
            "skill_count": duck_meta["skill_count"],
            "fragment_count": duck_meta["fragment_count"],
            "error": (
                f"Corpus is at schema v{recorded}, but this code expects "
                f"v{EXPECTED_CORPUS_SCHEMA_VERSION}."
            ),
            "remediation": (
                "Run `python -m agentalloy.install update` to migrate the corpus "
                "in-place, or reinstall agentalloy to restore the bundled corpus."
            ),
            "duration_ms": duration_ms,
        }
    else:
        corpus_schema_version = recorded
        schema_warning = None

    # 4. Skill count check
    skill_count = duck_meta["skill_count"]
    if skill_count < MIN_SKILL_COUNT:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "missing_files",
            "corpus_schema_version": corpus_schema_version,
            "skill_count": skill_count,
            "fragment_count": duck_meta["fragment_count"],
            "error": f"Skill count {skill_count} < minimum {MIN_SKILL_COUNT}",
            "remediation": remediation,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    duration_ms = int((time.monotonic() - t0) * 1000)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "action": "verified_present" if not was_seeded else "seeded",
        "corpus_path": str(user_corpus),
        "corpus_schema_version": corpus_schema_version,
        "skill_count": skill_count,
        "fragment_count": duck_meta["fragment_count"],
        "embedding_model": duck_meta["embedding_model"],
        "embedding_dim": duck_meta["embedding_dim"],
        "duration_ms": duration_ms,
    }
    if schema_warning:
        result["warning"] = schema_warning
    return result


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p: argparse.ArgumentParser = subparsers.add_parser(
        "seed-corpus",
        help="Verify the in-repo seed corpus is present and valid.",
    )
    add_json_flag(p)
    p.set_defaults(func=run)


def _render_seed_corpus(result: dict[str, Any]) -> None:
    """Render seed corpus result in human-readable format."""
    action = result.get("action", "unknown")
    skill_count = result.get("skill_count", 0)
    fragment_count = result.get("fragment_count", 0)

    action_colors = {
        "verified_present": "green",
        "seeded": "green",
        "initialized_empty": "yellow",
        "missing_files": "red",
        "schema_mismatch": "red",
    }
    color = action_colors.get(action, "dim")

    print_rich("\n  [bold]Seed Corpus[/bold]\n")
    print_rich(f"  Status: [{color}]{action}[/{color}]")
    print_rich(f"  Skills: {skill_count}")
    print_rich(f"  Fragments: {fragment_count}")

    error = result.get("error")
    if error:
        print_rich(f"  Error: {error}")

    remediation = result.get("remediation")
    if remediation:
        print_rich(f"  Remediation: {remediation}")

    print_rich()


def run(args: argparse.Namespace) -> int:
    """Execute the seed-corpus subcommand."""
    st = install_state.load_state()
    if install_state.is_step_completed(st, "seed-corpus"):
        prev = install_state.get_step_output(st, "seed-corpus")
        user_corpus = install_state.corpus_dir()
        duck_present = (user_corpus / "skills.duck").exists()
        ladybug_present = (user_corpus / "ladybug").exists()
        if prev and prev.get("output_path") and duck_present and ladybug_present:
            p = Path(prev["output_path"])
            if p.exists():
                import json as _json

                cached: dict[str, Any] = _json.loads(p.read_text())
                # Re-verify corpus files are still readable before trusting cache.
                # A stale cache would otherwise report success on a deleted or
                # corrupted corpus (Pattern E: idempotency cache returns success
                # without verifying artifact exists).
                try:
                    _check_duckdb(user_corpus / "skills.duck")
                except Exception as exc:
                    # Corpus is present but unreadable — cache is stale.
                    print(
                        f"WARN: cached result exists but corpus is unreadable ({exc}); "
                        "re-verifying…",
                        file=sys.stderr,
                    )
                else:
                    write_result(cached, args, human_fn=_render_seed_corpus)
                    return 4  # EXIT_NOOP

    result = check_corpus()
    action = result["action"]

    fp, digest = install_state.save_output_file(result, "seed-corpus.json")

    if action in ("verified_present", "seeded", "initialized_empty"):
        install_state.record_step(
            st,
            "seed-corpus",
            extra={
                "output_digest": digest,
                "output_path": str(fp),
                "skill_count": result.get("skill_count"),
                "fragment_count": result.get("fragment_count"),
            },
        )
        install_state.save_state(st)
        write_result(result, args, human_fn=_render_seed_corpus)
        return 0

    write_result(result, args, human_fn=_render_seed_corpus)

    remediation = result.get("remediation", "")
    error = result.get("error", "")
    if action == "missing_files":
        print("\nERROR: Corpus files missing or incomplete", file=sys.stderr)
        if error:
            print(f"CAUSE: {error}", file=sys.stderr)
        print(f"FIX:   {remediation}", file=sys.stderr)
        return 1
    if action == "schema_mismatch":
        print("\nERROR: Corpus schema version mismatch", file=sys.stderr)
        if error:
            print(f"CAUSE: {error}", file=sys.stderr)
        print("FIX:   python -m agentalloy.install update", file=sys.stderr)
        return 3

    return 1
