"""Re-embed pass: populate DuckDB ``fragment_embeddings`` from LadybugDB
``Fragment`` nodes.

The migration model separates ingest (graph writes to LadybugDB) from embedding
(vector writes to DuckDB). This CLI is the embedding half — it runs after
ingest and can be re-run safely: fragments whose ids already have DuckDB rows
are skipped.

Usage::

    python -m agentalloy.reembed                    # embed everything missing
    python -m agentalloy.reembed --limit 10         # cap work (testing)
    python -m agentalloy.reembed --skill-id <id>    # one skill only
    python -m agentalloy.reembed --force            # re-embed everything (delete + insert)

Retries: 3 attempts with 1s/2s/4s exponential backoff on transient LM Studio
failures (timeouts, 5xx). A hard failure after retries halts the run and
leaves already-embedded fragments in place (idempotency means the next run
picks up where this one stopped).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agentalloy.config import Settings, get_settings
from agentalloy.dedup_gate import DedupGateResult, run_dedup_gate
from agentalloy.embed_provider import EmbedClient, get_embed_client
from agentalloy.install.container_service import (
    is_in_container,
    restart_service_in_container,
    stop_service_in_container,
)
from agentalloy.lm_client import (
    LMBadResponse,
    LMClientError,
    LMTimeout,
    LMUnavailable,
)
from agentalloy.storage.card_index import (
    CARD_FRAGMENT_TYPE,
    META_KEY_CARD_INDEX,
    CardIndexMode,
    apply_prefix,
    build_card_text,
    card_fragment_id,
)
from agentalloy.storage.ladybug import (
    LOCK_HELD_REMEDIATION,
    LadybugStore,
    is_lock_held_error,
)
from agentalloy.storage.vector_store import (
    FragmentEmbedding,
    VectorStore,
    open_or_create,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_LLM = 2
EXIT_DB = 3
EXIT_DEDUP = 4

_RETRY_DELAYS = (1.0, 2.0, 4.0)
_TRANSIENT_ERRORS = (LMTimeout, LMUnavailable)

# ---------------------------------------------------------------------------
# Service management — stop the background server before reembed so it doesn't
# hold database locks (LadybugDB/Kuzu + DuckDB).
# ---------------------------------------------------------------------------


def _detect_service_manager() -> str | None:
    """Return 'systemd', 'launchd', or None."""
    if platform.system().lower() == "linux" and shutil.which("systemctl") is not None:
        return "systemd"
    if platform.system().lower() == "darwin" and shutil.which("launchctl") is not None:
        return "launchd"
    return None


def _systemd_unit_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "systemd" / "user" / "agentalloy.service"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "ai.agentalloy.plist"


def _is_service_running() -> bool:
    """Check if the agentalloy service is active."""
    sm = _detect_service_manager()
    if sm == "systemd":
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "is-active",
                    "agentalloy.service",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env={
                    **os.environ,
                    "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
                },
            )
            return result.stdout.strip() == "active"
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif sm == "launchd":
        plist = _launchd_plist_path()
        if plist.exists():
            try:
                result = subprocess.run(
                    ["launchctl", "list", "ai.agentalloy"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # launchctl list returns 0 when the job exists (loaded or running).
                # Output is tab-separated: PID \t exitcode \t label
                # PID column is '-' when the job is loaded but not currently running.
                if result.returncode != 0 or "ai.agentalloy" not in result.stdout:
                    return False
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if parts and parts[-1] == "ai.agentalloy" and parts[0] != "-":
                        return True
                return False
            except (OSError, subprocess.TimeoutExpired):
                pass
    return False


def _stop_service() -> bool:
    """Stop the agentalloy service. Returns True if something was stopped."""
    sm = _detect_service_manager()
    if sm == "systemd":
        try:
            logger.info("stopping systemd service: agentalloy.service")
            env = {
                **os.environ,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
            }
            subprocess.run(
                ["systemctl", "--user", "stop", "agentalloy.service"],
                check=True,
                timeout=15,
                env=env,
            )
            return True
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            logger.debug("could not stop systemd service: %s", exc)
    elif sm == "launchd":
        plist = _launchd_plist_path()
        if plist.exists():
            try:
                logger.info("stopping launchd service: ai.agentalloy")
                subprocess.run(
                    ["launchctl", "bootout", "ai.agentalloy"],
                    check=True,
                    timeout=15,
                )
                # Fallback for older macOS
                if not _is_service_running():
                    return True
            except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
                pass
            try:
                subprocess.run(
                    ["launchctl", "unload", str(plist)],
                    check=True,
                    timeout=15,
                )
                return True
            except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                logger.debug("could not stop launchd service: %s", exc)
    return False


def _restart_service() -> None:
    """Restart the agentalloy service."""
    sm = _detect_service_manager()
    if sm == "systemd":
        try:
            env = {
                **os.environ,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
            }
            subprocess.run(
                ["systemctl", "--user", "start", "agentalloy.service"],
                check=True,
                timeout=15,
                env=env,
            )
            logger.info("restarted systemd service: agentalloy.service")
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            logger.warning("failed to restart systemd service: %s", exc)
    elif sm == "launchd":
        plist = _launchd_plist_path()
        if plist.exists():
            try:
                # Use non-persistent load (no -w) so we don't override user
                # enablement/disabled state. Prefer modern kickstart when available.
                subprocess.run(
                    ["launchctl", "kickstart", "gui/", "ai.agentalloy"],
                    check=True,
                    timeout=15,
                )
                logger.info("restarted launchd service: ai.agentalloy")
            except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
                # Fallback: launchctl load without -w (older macOS)
                try:
                    subprocess.run(
                        ["launchctl", "load", str(plist)],
                        check=True,
                        timeout=15,
                    )
                    logger.info("restarted launchd service: ai.agentalloy")
                except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                    logger.warning("failed to restart launchd service: %s", exc)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FragmentNeedingEmbedding:
    """A Fragment node pulled from LadybugDB with its parent Skill metadata.

    The denormalized columns (``skill_id``, ``category``, ``fragment_type``)
    carry through to the DuckDB row so compose-time filtered search doesn't
    need a cross-engine join.

    ``canonical_name`` / ``domain_tags`` / ``description`` carry the parent
    skill's identity so Stage 0 card indexing can build a header without a
    second graph read. They default empty so older call sites and corpora
    missing the ``description`` column stay tolerant.
    """

    fragment_id: str
    content: str
    fragment_type: str
    skill_id: str
    category: str
    canonical_name: str = ""
    domain_tags: tuple[str, ...] = ()
    description: str | None = None


@dataclass
class ReembedStats:
    discovered: int = 0
    skipped_already_present: int = 0
    embedded: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=lambda: [])

    def log_summary(self) -> None:
        logger.info(
            "re-embed complete: discovered=%d skipped=%d embedded=%d failed=%d",
            self.discovered,
            self.skipped_already_present,
            self.embedded,
            self.failed,
        )
        for fid, err in self.failures[:10]:
            logger.warning("  ✗ %s: %s", fid, err)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


_DISCOVERY_CYPHER_ALL = """
MATCH (s:Skill)-[:CURRENT_VERSION]->(v:SkillVersion)-[:DECOMPOSES_TO]->(f:Fragment)
WHERE v.status = 'active' AND s.deprecated = false
RETURN f.fragment_id, f.content, f.fragment_type, s.skill_id, s.category,
       s.canonical_name, s.domain_tags, s.description
ORDER BY s.skill_id, f.sequence
"""

_DISCOVERY_CYPHER_SKILL = """
MATCH (s:Skill {skill_id: $skill_id})-[:CURRENT_VERSION]->(v:SkillVersion)
    -[:DECOMPOSES_TO]->(f:Fragment)
WHERE v.status = 'active' AND s.deprecated = false
RETURN f.fragment_id, f.content, f.fragment_type, s.skill_id, s.category,
       s.canonical_name, s.domain_tags, s.description
ORDER BY f.sequence
"""


def discover_unembedded_fragments(
    store: LadybugStore,
    vector_store: VectorStore,
    *,
    skill_id: str | None = None,
    force: bool = False,
) -> list[FragmentNeedingEmbedding]:
    """Pull Fragment nodes from LadybugDB; filter out those already in DuckDB.

    ``force=True`` returns every fragment regardless of DuckDB state — useful
    for "wipe and re-embed" scenarios (caller is expected to have called
    ``vector_store.delete_skill`` first, otherwise the primary-key constraint
    on fragment_embeddings will raise).
    """
    if skill_id is not None:
        rows = store.execute(_DISCOVERY_CYPHER_SKILL, {"skill_id": skill_id})
    else:
        rows = store.execute(_DISCOVERY_CYPHER_ALL)

    all_fragments = [
        FragmentNeedingEmbedding(
            fragment_id=str(row[0]),
            content=str(row[1]),
            fragment_type=str(row[2]),
            skill_id=str(row[3]),
            category=str(row[4]),
            canonical_name=str(row[5]) if len(row) > 5 and row[5] is not None else "",
            domain_tags=tuple(row[6]) if len(row) > 6 and row[6] else (),
            description=(
                str(row[7]).strip() or None if len(row) > 7 and row[7] is not None else None
            ),
        )
        for row in rows
    ]

    if force:
        return all_fragments

    present = vector_store.fragment_ids_present([f.fragment_id for f in all_fragments])
    return [f for f in all_fragments if f.fragment_id not in present]


# ---------------------------------------------------------------------------
# Embedding with retry
# ---------------------------------------------------------------------------


def _embed_with_retry(
    embed_fn: Callable[[str], list[float]],
    content: str,
    *,
    delays: tuple[float, ...] = _RETRY_DELAYS,
) -> list[float]:
    """Call ``embed_fn(content)``; retry transient failures with backoff.

    Non-transient errors (``LMBadResponse``, unknown errors) fail fast — they
    indicate a real problem that retrying won't fix.
    """
    last_exc: LMClientError | None = None
    for attempt, delay in enumerate([0.0, *delays]):
        if delay > 0.0:
            time.sleep(delay)
        try:
            return embed_fn(content)
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            logger.warning("embed transient failure (attempt %d): %s", attempt + 1, exc)
        except LMBadResponse:
            # Malformed response is not retry-able.
            raise
    raise last_exc if last_exc else LMClientError("embed failed after retries")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _indexed_text(frag: FragmentNeedingEmbedding, mode: CardIndexMode) -> str:
    """Return the text to embed/index for ``frag`` under ``mode``.

    ``prefix``/``both`` prepend a one-line skill-card header; ``off``/``cards``
    leave the fragment body untouched. CRITICAL: this only affects the indexed
    representation (the embedded vector and the BM25 ``prose`` column). The
    stored fragment ``content`` returned by ``/compose`` is never derived from
    this — ``off`` therefore reproduces today's index byte-for-byte.
    """
    if not mode.with_prefix:
        return frag.content
    header = build_card_text(frag.canonical_name, frag.domain_tags, frag.description)
    return apply_prefix(header, frag.content)


def reembed_fragments(
    fragments: list[FragmentNeedingEmbedding],
    *,
    embed_fn: Callable[[str], list[float]],
    vector_store: VectorStore,
    embedding_model: str,
    progress_tty: bool = False,
    on_embedded: Callable[[FragmentNeedingEmbedding, list[float]], None] | None = None,
    card_index: CardIndexMode = CardIndexMode.OFF,
) -> ReembedStats:
    """Embed each fragment and insert to DuckDB. Returns run stats.

    ``embed_fn`` takes a content string and returns a raw (non-normalized)
    vector. The vector_store normalizes on insert. Injected rather than
    hard-wired to the LM client so tests can pass a fake.

    ``on_embedded`` (optional) is invoked once per successfully inserted
    fragment with the fragment and its raw vector. The dedup gate uses this
    instead of wrapping ``embed_fn``: the retry path may call ``embed_fn``
    more than once per fragment, so call-order correlation mis-attributes
    vectors.

    ``card_index`` (Stage 0): when ``prefix``/``both``, each fragment's
    *indexed* text (the embedded vector and the BM25 ``prose`` column) is
    prefixed with a one-line skill-card header. The returned ``content`` is
    untouched. ``cards``/``both`` additionally inserts synthetic card documents
    — done by the caller after this batch (see ``_insert_cards``). ``off`` is a
    byte-for-byte no-op vs the pre-Stage-0 index.

    Transactional: the entire batch is wrapped in a DuckDB transaction.
    If any fragment fails to embed or insert, the whole batch is rolled back
    so the caller can retry without leaving partial state.
    """
    stats = ReembedStats(discovered=len(fragments))
    now = int(time.time())
    if not fragments:
        return stats

    # Wrap the entire batch in a transaction for atomicity
    vector_store.begin_transaction()
    try:
        for frag in fragments:
            indexed = _indexed_text(frag, card_index)
            try:
                vec = _embed_with_retry(embed_fn, indexed)
            except LMClientError as exc:
                stats.failed += 1
                stats.failures.append((frag.fragment_id, str(exc)))
                logger.error("failed %s: %s", frag.fragment_id, exc)
                # Roll back the entire batch on any embed failure
                with contextlib.suppress(Exception):
                    vector_store.rollback_transaction()
                raise  # Re-raise to trigger top-level rollback
            except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
                stats.failed += 1
                stats.failures.append((frag.fragment_id, f"unexpected: {exc}"))
                logger.error("unexpected error on %s: %s", frag.fragment_id, exc)
                # Roll back the entire batch on any embed failure
                with contextlib.suppress(Exception):
                    vector_store.rollback_transaction()
                raise  # Re-raise to trigger top-level rollback

            try:
                vector_store.insert_embeddings(
                    [
                        FragmentEmbedding(
                            fragment_id=frag.fragment_id,
                            embedding=vec,
                            skill_id=frag.skill_id,
                            category=frag.category,
                            fragment_type=frag.fragment_type,
                            embedded_at=now,
                            embedding_model=embedding_model,
                            prose=indexed,
                        )
                    ]
                )
            except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
                stats.failed += 1
                stats.failures.append((frag.fragment_id, f"insert: {exc}"))
                logger.error("insert failed for %s: %s", frag.fragment_id, exc)
                # Roll back the entire batch on any insert failure
                with contextlib.suppress(Exception):
                    vector_store.rollback_transaction()
                raise  # Re-raise to trigger top-level rollback

            stats.embedded += 1
            if on_embedded is not None:
                on_embedded(frag, vec)
            if progress_tty:
                print(
                    f"\r  embedded {stats.embedded}/{stats.discovered}",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
            elif stats.embedded % 10 == 0:
                logger.info("  embedded %d/%d", stats.embedded, stats.discovered)

        # All fragments processed — commit the batch
        vector_store.commit_transaction()

    except Exception as exc:
        # Top-level error — rollback the entire batch
        with contextlib.suppress(Exception):
            vector_store.rollback_transaction()
        logger.error(
            "reembed batch failed after %d embedded, %d failed — rolled back: %s",
            stats.embedded,
            stats.failed,
            exc,
        )
        raise

    finally:
        if progress_tty:
            print(file=sys.stderr)  # newline after progress
    return stats


def _distinct_skill_cards(
    fragments: list[FragmentNeedingEmbedding],
) -> list[FragmentNeedingEmbedding]:
    """One representative fragment per skill (first in rank order).

    The representative carries the skill identity (``canonical_name`` /
    ``domain_tags`` / ``description``) needed to build that skill's card.
    """
    seen: dict[str, FragmentNeedingEmbedding] = {}
    for frag in fragments:
        seen.setdefault(frag.skill_id, frag)
    return list(seen.values())


def insert_cards(
    fragments: list[FragmentNeedingEmbedding],
    *,
    embed_fn: Callable[[str], list[float]],
    vector_store: VectorStore,
    embedding_model: str,
) -> int:
    """Embed and insert one synthetic card document per distinct skill.

    Cards carry ``fragment_type='card'`` and a ``card::<skill_id>`` id so they
    are trivially identifiable in both DuckDB and the fused candidate list.
    They participate in dense + BM25 retrieval (boosting their skill's rank)
    but are excluded from ``/compose`` assembly — no LadybugDB Fragment node
    hydrates them, and ``retrieval.domain`` drops card ids before selection.

    Caller is responsible for clearing pre-existing cards (``delete_cards``)
    when re-running, mirroring the ``--force`` fragment path. Returns the count
    of cards inserted. Wrapped in its own transaction for atomicity.
    """
    cards = _distinct_skill_cards(fragments)
    if not cards:
        return 0
    now = int(time.time())
    inserted = 0
    vector_store.begin_transaction()
    try:
        for rep in cards:
            text = build_card_text(rep.canonical_name, rep.domain_tags, rep.description)
            vec = _embed_with_retry(embed_fn, text)
            vector_store.insert_embeddings(
                [
                    FragmentEmbedding(
                        fragment_id=card_fragment_id(rep.skill_id),
                        embedding=vec,
                        skill_id=rep.skill_id,
                        category=rep.category,
                        fragment_type=CARD_FRAGMENT_TYPE,
                        embedded_at=now,
                        embedding_model=embedding_model,
                        prose=text,
                    )
                ]
            )
            inserted += 1
        vector_store.commit_transaction()
    except Exception:
        with contextlib.suppress(Exception):
            vector_store.rollback_transaction()
        raise
    return inserted


# ---------------------------------------------------------------------------
# Dedup gate reporting
# ---------------------------------------------------------------------------


def _report_dedup(gate_result: DedupGateResult, *, allow_duplicates: bool) -> int:
    """Print dedup findings and return the appropriate exit code.

    Soft matches → WARNING to stderr, continue (always EXIT_OK from this path).
    Hard matches → ERROR to stderr; EXIT_DEDUP unless ``allow_duplicates`` is
    set, in which case downgrade to WARNING and return EXIT_OK.

    Vectors are always written regardless — the gate is a quality check.
    """
    for match in gate_result.soft:
        logger.warning(
            "SOFT near-duplicate: %s ↔ %s  similarity=%.4f  (fragments %s / %s)",
            match.incoming_skill_id,
            match.existing_skill_id,
            match.similarity,
            match.fragment_id_incoming,
            match.fragment_id_existing,
        )

    if not gate_result.has_hard:
        return EXIT_OK

    for match in gate_result.hard:
        msg = (
            f"HARD duplicate: skill '{match.incoming_skill_id}' is nearly identical to "
            f"existing skill '{match.existing_skill_id}' "
            f"(similarity={match.similarity:.4f}, "
            f"fragments {match.fragment_id_incoming} / {match.fragment_id_existing}). "
            f"Remediation: differentiate the prose or deprecate one skill; "
            f"pack edits require a version bump."
        )
        if allow_duplicates:
            logger.warning("(--allow-duplicates) %s", msg)
        else:
            print(f"ERROR: {msg}", file=sys.stderr)

    if allow_duplicates:
        return EXIT_OK
    return EXIT_DEDUP


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _duckdb_path(settings: Settings) -> Path:
    """Locate the DuckDB file. Derived from LadybugDB path's parent dir."""
    ladybug_path = Path(settings.ladybug_db_path)
    return ladybug_path.parent / "skills.duck"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Silence httpx per-request INFO logs during the embedding phase
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        prog="python -m agentalloy.reembed",
        description=(
            "Compute embeddings for LadybugDB fragments and write them to "
            "the DuckDB vector store. Idempotent on re-run."
        ),
    )
    parser.add_argument(
        "--skill-id",
        help="Only embed fragments for this skill_id (default: all skills)",
    )
    parser.add_argument(
        "--card-index",
        choices=[m.value for m in CardIndexMode],
        default=CardIndexMode.OFF.value,
        help=(
            "Stage 0 skill-card indexing (deterministic document expansion). "
            "'off' (default) reproduces today's index byte-for-byte. 'prefix' "
            "prepends a one-line 'skill: <name> — tags: ... — <description>' "
            "header to each fragment's INDEXED text (embedding + BM25 only; the "
            "prose returned by /compose is unchanged). 'cards' adds one "
            "synthetic card document per skill that boosts skill ranking but is "
            "never emitted as a fragment. 'both' does both. The chosen mode is "
            "recorded in corpus_meta for auditability."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Cap the number of fragments processed (after skip-filtering)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete existing embeddings for the scope and re-embed from scratch",
    )
    parser.add_argument(
        "--model",
        help="Override the embedding model id (default: runtime_embedding_model from config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be embedded without calling LM Studio or writing DuckDB",
    )
    parser.add_argument(
        "--rebuild-fts",
        action="store_true",
        help=(
            "Force rebuild of the BM25 FTS index after the embed pass, "
            "even when no fragments needed embedding. Use to recover from a "
            "previous install where the FTS rebuild warning fired."
        ),
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart the agentalloy service after reembed completes",
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help=(
            "Downgrade hard cross-pack duplicates from errors to warnings. "
            "Vectors are always written; this flag controls only the exit code."
        ),
    )
    args = parser.parse_args(argv)
    card_mode = CardIndexMode(args.card_index)

    # Stop service in container mode before DB operations
    container_was_stopped = False
    if is_in_container():
        if args.no_restart:
            logger.info("skipping container service stop (no_restart requested)")
        else:
            print(
                "Stopping agentalloy service (container mode) to release database locks...",
                file=sys.stderr,
            )
            container_was_stopped = stop_service_in_container(no_restart=False)
            if not container_was_stopped:
                logger.warning(
                    "no running agentalloy service found in container; "
                    "proceeding without stop/restart"
                )

    settings = get_settings()
    model_id = args.model or settings.runtime_embedding_model
    duck_path = _duckdb_path(settings)
    Path(settings.ladybug_db_path).parent.mkdir(parents=True, exist_ok=True)

    # Pre-flight: stop the background service if running — it holds database locks
    # (LadybugDB/Kuzu + DuckDB) that conflict with our exclusive access.
    # Even in dry-run mode we must stop the service, otherwise opening the DBs
    # will fail with the same lock errors we're trying to avoid.
    service_was_running = False
    service_was_stopped = False
    if _is_service_running():
        service_was_running = True
        service_was_stopped = _stop_service()
        if service_was_stopped:
            logger.info("stopped agentalloy service to release database locks")
        else:
            logger.warning(
                "agentalloy service appears active but could not be stopped via "
                "service manager — reembed may fail with lock errors"
            )
    else:
        logger.debug("agentalloy service is not running")

    def _maybe_restart() -> None:
        """Restart the service if we stopped it and --no-restart is not set."""
        if service_was_stopped and not args.no_restart:
            _restart_service()
        elif not args.no_restart and service_was_running:
            logger.warning(
                "service was running but could not be stopped; "
                "it may still need a restart. Run 'agentalloy serve --restart' manually."
            )

    try:
        with LadybugStore(settings.ladybug_db_path) as store, open_or_create(duck_path) as vs:
            # Apply schema migrations before discovery — the discovery cypher
            # references columns (e.g. Stage 0's ``s.description``) that an
            # older corpus won't have until the ALTERs run. Idempotent.
            store.migrate()
            # --force: clear scope first so the primary-key constraint doesn't trip.
            # Wrap in a transaction so rollback is possible if embedding fails.
            if args.force:
                vs.begin_transaction()
                try:
                    if args.skill_id:
                        n = vs.delete_skill(args.skill_id)
                        logger.info(
                            "--force: deleted %d existing embeddings for %s", n, args.skill_id
                        )
                    else:
                        # Full wipe: required when changing embedding models (dimension change
                        # makes existing vectors incompatible with the new index).
                        n = vs.count_embeddings()
                        vs._conn.execute("DELETE FROM fragment_embeddings")  # pyright: ignore[reportPrivateUsage]
                        logger.info(
                            "--force: deleted all %d existing embeddings for full reindex", n
                        )
                    vs.commit_transaction()
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        vs.rollback_transaction()
                    logger.error("--force delete failed, rolled back: %s", exc)
                    raise

            fragments = discover_unembedded_fragments(
                store, vs, skill_id=args.skill_id, force=args.force
            )
            if args.limit is not None:
                fragments = fragments[: args.limit]

            logger.info(
                "discovered %d fragment(s) to embed (model=%s, target=%s)",
                len(fragments),
                model_id,
                duck_path,
            )

            if args.dry_run:
                for f in fragments[:20]:
                    logger.info(
                        "  would embed: %s (%s, %s)", f.fragment_id, f.skill_id, f.fragment_type
                    )
                if len(fragments) > 20:
                    logger.info("  ... and %d more", len(fragments) - 20)
                return EXIT_OK

            if not fragments and not args.rebuild_fts:
                logger.info("nothing to do — all fragments already embedded")
                return EXIT_OK

            stats: ReembedStats
            # fragment_id → (skill_id, raw_vector) for the dedup gate.
            embedded_vecs: dict[str, tuple[str, list[float]]] = {}
            if fragments:
                embed_client: EmbedClient = get_embed_client(settings)
                try:

                    def _embed(text: str) -> list[float]:
                        vectors = embed_client.embed(model=model_id, texts=[text])
                        return vectors[0]

                    def _record(frag: FragmentNeedingEmbedding, vec: list[float]) -> None:
                        embedded_vecs[frag.fragment_id] = (frag.skill_id, vec)

                    stats = reembed_fragments(
                        fragments,
                        embed_fn=_embed,
                        vector_store=vs,
                        embedding_model=model_id,
                        progress_tty=sys.stderr.isatty(),
                        on_embedded=_record,
                        card_index=card_mode,
                    )
                    stats.log_summary()

                    # Stage 0 'cards'/'both': rebuild the synthetic card layer.
                    # Reps are derived at the pass's scope (all skills, or the
                    # single ``--skill-id``). Drop stale cards at the SAME scope
                    # first so re-runs stay idempotent — a skill-scoped pass must
                    # only replace its own card, never wipe every other skill's.
                    if card_mode.with_cards:
                        all_frags = discover_unembedded_fragments(
                            store, vs, skill_id=args.skill_id, force=True
                        )
                        removed = vs.delete_cards(skill_id=args.skill_id)
                        n_cards = insert_cards(
                            all_frags,
                            embed_fn=_embed,
                            vector_store=vs,
                            embedding_model=model_id,
                        )
                        logger.info(
                            "card index: replaced %d card(s) with %d (mode=%s)",
                            removed,
                            n_cards,
                            card_mode.value,
                        )
                finally:
                    embed_client.close()
            else:
                # --rebuild-fts with no fragments to embed
                logger.info("no fragments to embed; running --rebuild-fts only")
                stats = ReembedStats()

            # When cards are NOT requested, ensure none linger from a prior
            # 'cards'/'both' build — keeps 'off'/'prefix' free of card rows so
            # 'off' matches the pre-Stage-0 index. Scoped to match the pass: a
            # full-scope pass drops every stale card; a skill-scoped pass drops
            # only that skill's card, leaving other skills' cards untouched.
            if not card_mode.with_cards:
                dropped = vs.delete_cards(skill_id=args.skill_id)
                if dropped:
                    logger.info(
                        "card index: removed %d stale card(s) (mode=%s)", dropped, card_mode.value
                    )

            # Record the indexed-representation mode for auditability. Soft-fail:
            # a metadata write must never fail the embed pass.
            try:
                vs.set_meta(META_KEY_CARD_INDEX, card_mode.value)
            except Exception as exc:  # noqa: BLE001 — audit metadata is best-effort
                logger.warning("could not record card_index meta: %s", exc)

            # Keep authored phase eligibility in sync with the graph on every
            # pass (cheap UPDATEs, vectors untouched). NULL scope rows fall
            # back to the phase->category map at query time.
            try:
                from agentalloy.migrate import phase_scope_by_skill

                scope_by_skill = phase_scope_by_skill(store)
                if scope_by_skill:
                    updated = vs.backfill_phase_scope(scope_by_skill)
                    logger.info("phase_scope synced on %d fragment row(s)", updated)
            except Exception as exc:  # noqa: BLE001 — sync is best-effort; migrate also does it
                logger.warning("phase_scope sync skipped: %s", exc)

            # Dedup gate — only fires when new fragments were actually embedded.
            dedup_exit: int = EXIT_OK
            if embedded_vecs:
                new_skill_ids = {sid for sid, _ in embedded_vecs.values()}
                gate_result: DedupGateResult = run_dedup_gate(
                    new_skill_ids=new_skill_ids,
                    new_fragment_vecs=embedded_vecs,
                    vector_store=vs,
                    hard_similarity=settings.dedup_hard_threshold,
                    soft_similarity=settings.dedup_soft_threshold,
                )
                dedup_exit = _report_dedup(gate_result, allow_duplicates=args.allow_duplicates)

            if stats.embedded > 0 or args.rebuild_fts:
                if stats.embedded > 0:
                    logger.info(
                        "rebuilding BM25 FTS index after embedding %d fragment(s)", stats.embedded
                    )
                else:
                    logger.info("rebuilding BM25 FTS index (--rebuild-fts requested)")
                try:
                    vs.rebuild_fts_index()
                except Exception as exc:  # noqa: BLE001 — FTS rebuild is best-effort
                    logger.warning(
                        "FTS index rebuild failed (BM25 leg degraded): %s. "
                        "If the background service was holding the DB, stop it and re-run "
                        "with `agentalloy reembed --rebuild-fts`.",
                        exc,
                    )
            if stats.failed > 0:
                return EXIT_LLM
            return dedup_exit
    except Exception as exc:
        # LadybugDB enforces a single writer; a running service (including a
        # manually-launched uvicorn the preflight can't see) holds the lock
        # and the DB open above fails. Tell the user what to stop (issue #84).
        if not is_lock_held_error(str(exc)):
            raise
        logger.error("database lock is held: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"FIX:   {LOCK_HELD_REMEDIATION}", file=sys.stderr)
        return EXIT_DB
    finally:
        _maybe_restart()
        if is_in_container():
            if args.no_restart:
                logger.info("skipping container service restart (no_restart requested)")
            elif container_was_stopped:
                print("Operation complete, restarting agentalloy service...", file=sys.stderr)
                if not restart_service_in_container(no_restart=False):
                    logger.warning(
                        "failed to restart agentalloy service after operation. "
                        "Run `podman restart agentalloy` manually."
                    )
            else:
                logger.debug("skipping container restart — no service was stopped")


if __name__ == "__main__":
    sys.exit(main())
