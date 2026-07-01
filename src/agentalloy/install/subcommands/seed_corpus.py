# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false, reportArgumentType=false
"""``seed-corpus`` subcommand — presence + integrity check.

The corpus lives at ``${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/`` as the
v5 two-engine store: ``agentalloy.duck`` (skill graph + corpus_meta),
``fragments.lance`` (vector + BM25 index), and ``telemetry.duck``. This
subcommand verifies the skill store exists, the schema version matches, and the
skill count meets the minimum threshold. No network calls.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from agentalloy.config import Settings, get_settings
from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result
from agentalloy.storage.card_index import CORPUS_SCHEMA_VERSION

SCHEMA_VERSION = 1

# The corpus schema version the code expects. Single source of truth lives in
# storage.card_index (stamped into corpus_meta at build time); re-exported here
# under the historical name the rest of install/ imports.
EXPECTED_CORPUS_SCHEMA_VERSION = CORPUS_SCHEMA_VERSION

# Sanity floor — flags truly empty corpora; not a quality bar.
MIN_SKILL_COUNT = 25


def corpus_skill_count() -> int:
    """Skill count in the user corpus; 0 if absent/empty/unreadable.

    Shared post-install/upgrade guard seam: callers compare against
    ``MIN_SKILL_COUNT`` to catch a silent half-install (install-packs reports
    success but the corpus never populated). Never raises.
    """
    settings = get_settings()
    if not Path(settings.duckdb_path).exists():
        return 0
    try:
        return int(_check_skill_store(settings).get("skill_count") or 0)
    except Exception:
        return 0


def corpus_embedding_count() -> int:
    """Vector count in ``fragments.lance``; 0 if absent/empty/unreadable.

    Post-upgrade guard seam: a same-dim engine migration (v4 stored vectors in
    DuckDB, v5 in Lance) leaves this at 0 even when ``corpus_skill_count()`` is
    healthy — install-packs writes fragment *metadata* to ``agentalloy.duck`` but
    the vector index is built by reembed. Callers use a 0 here to force a reembed
    the dim-mismatch check can't see. Never raises.
    """
    from agentalloy.storage.open import open_fragments

    settings = get_settings()
    try:
        vs = open_fragments(settings)
        try:
            return int(vs.count_embeddings())
        finally:
            vs.close()
    except Exception:
        return 0


def _check_skill_store(settings: Settings) -> dict[str, Any]:
    """Read skill/fragment counts + recorded schema_version from ``agentalloy.duck``.

    Returns ``corpus_schema_version_recorded=None`` if the corpus pre-dates the
    metadata kv (callers treat this as "implicit v1" with a soft warning).
    """
    from agentalloy.storage.open import open_skills

    store = open_skills(settings, read_only=True)
    try:
        try:
            skill_count = int(
                store.scalar("SELECT count(*) FROM skills WHERE deprecated = false") or 0
            )
            frag_count = int(store.scalar("SELECT count(*) FROM fragments") or 0)
        except Exception:
            skill_count = 0
            frag_count = 0
        recorded_raw = store.get_meta("schema_version")
    finally:
        store.close()

    recorded_version: int | None = None
    if recorded_raw is not None:
        try:
            recorded_version = int(recorded_raw)
        except (TypeError, ValueError):
            recorded_version = None

    return {
        "skill_count": skill_count,
        "fragment_count": frag_count,
        "corpus_schema_version_recorded": recorded_version,
    }


def _embedding_meta(settings: Settings) -> dict[str, Any]:
    """Best-effort embedding metadata from the Lance fragment store.

    ``embedding_dim`` is row-count gated (None on an empty dataset);
    ``embedding_model`` falls back to the configured runtime model since the
    Lance store exposes no public per-row model accessor.
    """
    from agentalloy.storage.open import open_fragments

    embedding_dim: int | None = None
    embedding_model: str | None = None
    try:
        vs = open_fragments(settings)
        try:
            embedding_dim = vs.embedding_dim()
            if embedding_dim is not None:
                embedding_model = settings.runtime_embedding_model
        finally:
            vs.close()
    except Exception:
        pass
    return {"embedding_dim": embedding_dim, "embedding_model": embedding_model}


def _initialize_empty_corpus(settings: Settings) -> None:
    """Initialize the three v5 stores in an empty corpus dir.

    Writer-mode opens create the file and run the (idempotent) schema migration,
    so the subsequent ``install-packs`` step has tables to write into. The Lance
    dataset and telemetry DB are created on first open.
    """
    from agentalloy.storage.open import open_fragments, open_skills, open_telemetry

    settings.ensure_data_dirs()
    skills = open_skills(settings, read_only=False)
    try:
        skills.migrate()
    finally:
        skills.close()
    open_fragments(settings).close()
    open_telemetry(settings, read_only=False).close()


def check_corpus(root: Path | None = None) -> dict[str, Any]:  # noqa: ARG001 — back-compat
    """Run the seed-corpus presence + integrity check.

    The corpus lives at ``${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/``
    (user-scoped). The wheel no longer ships a pre-built corpus; this step
    initializes an empty corpus (skill store + Lance + telemetry) so the
    subsequent ``install-packs`` step can populate it from chosen packs.
    """
    t0 = time.monotonic()

    settings = get_settings()
    user_corpus, _was_seeded = install_state.ensure_corpus_seeded()
    duck_path = Path(settings.duckdb_path)

    # New flow: if the skill store is absent (post-pack-refactor wheels),
    # initialize empty stores. Don't return missing_files — that's the old
    # behavior from when the wheel shipped a populated corpus.
    if not duck_path.exists():
        try:
            _initialize_empty_corpus(settings)
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
    # fresh-install case. Their remediation message points at install-packs
    # (which can repopulate from a known-good source).
    remediation = (
        "Corpus integrity check failed. Run `python -m agentalloy.migrate` to "
        "ensure the skill-store schema exists (idempotent), then `agentalloy "
        "install-packs` to repopulate from packs, or remove "
        "${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/ to start fresh."
    )

    # 2. Read skill-store metadata
    try:
        meta = _check_skill_store(settings)
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "missing_files",
            "error": f"Cannot read the skill store: {exc}",
            "remediation": remediation,
            "duration_ms": duration_ms,
        }
    meta.update(_embedding_meta(settings))

    # 3. Schema version check
    # The embed pass stamps schema_version into corpus_meta. Corpora built before
    # that change lack the marker — those are treated as implicit v1 (current)
    # with a soft, harmless note surfaced in the output.
    recorded = meta.get("corpus_schema_version_recorded")
    if recorded is None:
        corpus_schema_version = EXPECTED_CORPUS_SCHEMA_VERSION
        schema_warning: str | None = (
            f"corpus predates the schema_version marker; treating as v{EXPECTED_CORPUS_SCHEMA_VERSION} "
            "(current — harmless). The marker is written on the next corpus rebuild "
            "(`agentalloy reembed --force`)."
        )
    elif recorded != EXPECTED_CORPUS_SCHEMA_VERSION:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "schema_mismatch",
            "corpus_schema_version": recorded,
            "expected_corpus_schema_version": EXPECTED_CORPUS_SCHEMA_VERSION,
            "skill_count": meta["skill_count"],
            "fragment_count": meta["fragment_count"],
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
    skill_count = meta["skill_count"]
    if skill_count < MIN_SKILL_COUNT:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "missing_files",
            "corpus_schema_version": corpus_schema_version,
            "skill_count": skill_count,
            "fragment_count": meta["fragment_count"],
            "error": f"Skill count {skill_count} < minimum {MIN_SKILL_COUNT}",
            "remediation": remediation,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    duration_ms = int((time.monotonic() - t0) * 1000)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "action": "verified_present",
        "corpus_path": str(user_corpus),
        "corpus_schema_version": corpus_schema_version,
        "skill_count": skill_count,
        "fragment_count": meta["fragment_count"],
        "embedding_model": meta.get("embedding_model"),
        "embedding_dim": meta.get("embedding_dim"),
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
    settings = get_settings()
    st = install_state.load_state()
    if install_state.is_step_completed(st, "seed-corpus"):
        prev = install_state.get_step_output(st, "seed-corpus")
        duck_present = Path(settings.duckdb_path).exists()
        if prev and prev.get("output_path") and duck_present:
            p = Path(prev["output_path"])
            if p.exists():
                import json as _json

                cached: dict[str, Any] = _json.loads(p.read_text())
                # Re-verify the corpus is still readable before trusting cache.
                # A stale cache would otherwise report success on a deleted or
                # corrupted corpus (Pattern E: idempotency cache returns success
                # without verifying the artifact exists).
                try:
                    _check_skill_store(settings)
                except Exception as exc:
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
