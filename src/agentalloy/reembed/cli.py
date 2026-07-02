"""Re-embed pass: build the Lance ``fragments`` dataset from the skill store's
active fragments.

The migration model separates ingest (graph writes to ``agentalloy.duck``) from
embedding (vector writes to the Lance ``fragments`` dataset). This CLI is the
embedding half — it runs after ingest and can be re-run safely: fragments whose
ids already have Lance rows are skipped.

Reembed needs the corpus writer lock, and DuckDB grants a writer only while no
other process holds ``agentalloy.duck`` — including the running service's
lifetime read-only handle. In native mode the CLI therefore stops the main API
service for the duration and restarts it afterwards (the restart also reloads
the service's in-memory cache, so it serves the corpus this pass wrote). Pass
``--no-restart`` when a caller manages the service itself (``upgrade``) or the
call is in-process (the web UI, which releases its own handle instead). Lance
is MVCC and telemetry is a separate file, so nothing else needs pausing.

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
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from agentalloy.config import get_settings
from agentalloy.dedup_gate import DedupGateResult, run_dedup_gate
from agentalloy.embed_provider import EmbedClient, get_embed_client
from agentalloy.lm_client import (
    LMBadResponse,
    LMClientError,
    LMTimeout,
    LMUnavailable,
)
from agentalloy.storage.card_index import (
    CARD_FRAGMENT_TYPE,
    CORPUS_SCHEMA_VERSION,
    META_KEY_CARD_INDEX,
    META_KEY_SCHEMA_VERSION,
    CardIndexMode,
    apply_prefix,
    build_card_text,
    card_fragment_id,
)
from agentalloy.storage.open import open_fragments, open_skills
from agentalloy.storage.protocols import FragmentEmbedding
from agentalloy.storage.skill_store import LockHeldError, is_lock_held_error

if TYPE_CHECKING:
    from agentalloy.storage.fragment_store import LanceFragmentStore
    from agentalloy.storage.skill_store import DuckDBSkillStore

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_LLM = 2
EXIT_DB = 3
EXIT_DEDUP = 4

_RETRY_DELAYS = (1.0, 2.0, 4.0)
_TRANSIENT_ERRORS = (LMTimeout, LMUnavailable)

# Shown when the corpus DB is held by another process. The usual holder is the
# running agentalloy service (its read-only handle blocks writers for its whole
# lifetime); a concurrent ingest/reembed is the transient case.
LOCK_HELD_REMEDIATION = (
    "Another process is holding the corpus DB (agentalloy.duck) open. A running "
    "agentalloy service blocks writers for its whole lifetime — reembed "
    "stops/restarts it automatically unless --no-restart was passed; for other "
    "commands run `agentalloy server-stop` first, then `agentalloy server-start` "
    "after. If a concurrent ingest/reembed briefly holds the lock instead, wait "
    "and re-run."
)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FragmentNeedingEmbedding:
    """An active fragment pulled from the skill store with its parent skill metadata.

    The denormalized columns (``skill_id``, ``category``, ``fragment_type``)
    carry through to the Lance row so compose-time filtered search doesn't
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


# Active fragments + parent-skill metadata, folded out of the old Cypher graph:
# CURRENT_VERSION -> skills.current_version_id; DECOMPOSES_TO -> fragments.version_id.
_DISCOVERY_SQL_ALL = """
SELECT f.fragment_id, f.content, f.fragment_type, s.skill_id, s.category,
       s.canonical_name, s.domain_tags, s.description
FROM skills s
JOIN skill_versions v ON v.version_id = s.current_version_id
JOIN fragments f ON f.version_id = v.version_id
WHERE v.status = 'active' AND s.deprecated = false
ORDER BY s.skill_id, f.sequence
"""

_DISCOVERY_SQL_SKILL = """
SELECT f.fragment_id, f.content, f.fragment_type, s.skill_id, s.category,
       s.canonical_name, s.domain_tags, s.description
FROM skills s
JOIN skill_versions v ON v.version_id = s.current_version_id
JOIN fragments f ON f.version_id = v.version_id
WHERE v.status = 'active' AND s.deprecated = false AND s.skill_id = $skill_id
ORDER BY f.sequence
"""


def discover_unembedded_fragments(
    store: DuckDBSkillStore,
    vector_store: LanceFragmentStore,
    *,
    skill_id: str | None = None,
    force: bool = False,
) -> list[FragmentNeedingEmbedding]:
    """Pull active fragments from the skill store; filter out those already in Lance.

    ``force=True`` returns every fragment regardless of Lance state — useful for
    "wipe and re-embed" scenarios (Lance ``insert_embeddings`` upserts on
    ``fragment_id``, so a duplicate id replaces rather than conflicts).
    """
    if skill_id is not None:
        rows = store.execute(_DISCOVERY_SQL_SKILL, {"skill_id": skill_id})
    else:
        rows = store.execute(_DISCOVERY_SQL_ALL)

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
    vector_store: LanceFragmentStore,
    embedding_model: str,
    progress_tty: bool = False,
    on_embedded: Callable[[FragmentNeedingEmbedding, list[float]], None] | None = None,
    card_index: CardIndexMode = CardIndexMode.OFF,
) -> ReembedStats:
    """Embed each fragment and upsert it into the Lance dataset. Returns run stats.

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
    — done by the caller after this batch (see ``insert_cards``). ``off`` is a
    byte-for-byte no-op vs the pre-Stage-0 index.

    Lance is MVCC with no exclusive writer lock, so each ``insert_embeddings``
    is its own atomic upsert (keyed on ``fragment_id``). A failure mid-run
    leaves the already-inserted rows committed; idempotency means a re-run picks
    up the rest, and ``fragment_id`` upserts make replays safe.
    """
    stats = ReembedStats(discovered=len(fragments))
    now = int(time.time())
    if not fragments:
        return stats

    try:
        for frag in fragments:
            indexed = _indexed_text(frag, card_index)
            try:
                vec = _embed_with_retry(embed_fn, indexed)
            except LMClientError as exc:
                stats.failed += 1
                stats.failures.append((frag.fragment_id, str(exc)))
                logger.error("failed %s: %s", frag.fragment_id, exc)
                raise
            except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
                stats.failed += 1
                stats.failures.append((frag.fragment_id, f"unexpected: {exc}"))
                logger.error("unexpected error on %s: %s", frag.fragment_id, exc)
                raise

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
                raise

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

    except Exception as exc:
        logger.error(
            "reembed batch failed after %d embedded, %d failed: %s",
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
    vector_store: LanceFragmentStore,
    embedding_model: str,
) -> int:
    """Embed and insert one synthetic card document per distinct skill.

    Cards carry ``fragment_type='card'`` and a ``card::<skill_id>`` id so they
    are trivially identifiable in both the Lance dataset and the fused candidate
    list. They participate in dense + BM25 retrieval (boosting their skill's
    rank) but are excluded from ``/compose`` assembly — no skill-store fragment
    hydrates them, and ``retrieval.domain`` drops card ids before selection.

    Caller is responsible for clearing pre-existing cards (``delete_cards``)
    when re-running, mirroring the ``--force`` fragment path. Returns the count
    of cards inserted (each an atomic Lance upsert keyed on ``fragment_id``).
    """
    cards = _distinct_skill_cards(fragments)
    if not cards:
        return 0
    now = int(time.time())
    inserted = 0
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


def build_parser() -> argparse.ArgumentParser:
    """The reembed CLI parser. Exposed so tests can assert real defaults."""
    parser = argparse.ArgumentParser(
        prog="python -m agentalloy.reembed",
        description=(
            "Compute embeddings for the skill store's active fragments and write "
            "them to the Lance fragment store. Idempotent on re-run."
        ),
    )
    parser.add_argument(
        "--skill-id",
        help="Only embed fragments for this skill_id (default: all skills)",
    )
    parser.add_argument(
        "--card-index",
        choices=[m.value for m in CardIndexMode],
        # 'both' measured +0.067 mean domain score (oracle-lift capture
        # 45% → 74%) on the 2026-06-12 LFM leg with the regression gate
        # green — so card indexing is the build default. 'off' remains the
        # explicit opt-out and reproduces the pre-Stage-0 index
        # byte-for-byte (the library-level default is still OFF; only the
        # CLI — and therefore install-packs' bulk pass — opts in).
        default=CardIndexMode.BOTH.value,
        help=(
            "Stage 0 skill-card indexing (deterministic document expansion). "
            "'both' (default) prepends a one-line 'skill: <name> — tags: ... — "
            "<description>' header to each fragment's INDEXED text (embedding + "
            "BM25 only; the prose returned by /compose is unchanged) AND adds "
            "one synthetic card document per skill that boosts skill ranking "
            "but is never emitted as a fragment. 'prefix'/'cards' apply just "
            "one of the two. 'off' reproduces the pre-Stage-0 index "
            "byte-for-byte. The chosen mode is recorded in corpus_meta for "
            "auditability."
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
        help="Report what would be embedded without calling LM Studio or writing the Lance store",
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
        help=(
            "Do not stop/restart the agentalloy service around the write pass. "
            "For callers that already manage the service (e.g. `agentalloy "
            "upgrade`) and in-process callers (the web UI). Without this flag, "
            "a running native service — whose open corpus handle blocks the "
            "writer lock — is stopped for the pass and restarted (picking up "
            "the new corpus) afterwards."
        ),
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help=(
            "Downgrade hard cross-pack duplicates from errors to warnings. "
            "Vectors are always written; this flag controls only the exit code."
        ),
    )
    return parser


def main(argv: list[str] | None = None, *, result_sink: dict[str, Any] | None = None) -> int:
    """Run the reembed CLI. ``result_sink``, if given, is populated with

    ``dedup_hard``/``dedup_soft`` (plain-dict serializations of the
    ``DedupMatch`` records the dedup gate found) so in-process callers that
    need match detail — not just the exit code — can retrieve it without
    parsing stderr. The argv-based CLI contract (bare exit code) is
    unchanged for callers that don't pass this.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Silence httpx per-request INFO logs during the embedding phase
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    card_mode = CardIndexMode(args.card_index)

    settings = get_settings()
    model_id = args.model or settings.runtime_embedding_model
    settings.ensure_data_dirs()

    # Lance is MVCC and telemetry is a separate file; only the skill store
    # needs the exclusive DuckDB writer. A writer is granted only while no
    # other process holds the file — and a running native service keeps a
    # read-only handle for its whole lifetime — so on a lock conflict we stop
    # the service, take the lock, and restart it in the finally below (the
    # restart also reloads its in-memory cache, so it serves this pass's
    # corpus). ``--no-restart`` opts out for callers that manage the service
    # themselves (`upgrade`) or run in-process (the web UI).
    store: DuckDBSkillStore | None = None
    vs: LanceFragmentStore | None = None
    service_mode: str | None = None
    try:
        try:
            store = open_skills(settings, read_only=False)
        except LockHeldError:
            if args.no_restart:
                raise
            service_mode = _stop_main_service()
            if service_mode is None:
                raise
            store = _reopen_after_stop(settings)
        vs = open_fragments(settings)
        # Writer-mode open already migrated; re-assert idempotently.
        store.migrate()

        # --force: drop existing Lance rows for the scope before re-embedding.
        # A full wipe is required when changing embedding models (a dimension
        # change makes existing vectors incompatible with the new index).
        if args.force:
            if args.skill_id:
                n = vs.delete_skill(args.skill_id)
                logger.info("--force: deleted %d existing embeddings for %s", n, args.skill_id)
            else:
                n = vs.count_embeddings()
                vs.delete_all()
                logger.info("--force: deleted all %d existing embeddings for full reindex", n)

        fragments = discover_unembedded_fragments(
            store, vs, skill_id=args.skill_id, force=args.force
        )
        if args.limit is not None:
            fragments = fragments[: args.limit]

        logger.info(
            "discovered %d fragment(s) to embed (model=%s, target=%s)",
            len(fragments),
            model_id,
            settings.fragments_lance_path,
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
                    payload = f"search_document: {text}"
                    # nomic-embed-text-v1.5 serves at n_ctx_train=2048; inputs
                    # over that overflow the (u)batch. Truncate long fragments
                    # to 2040 tokens via llama-server's tokenizer; short ones
                    # (<1500 chars, <=~1500 tok worst case) skip the round-trip.
                    if len(payload) > 1500:

                        def _ntok(s: str) -> int:
                            resp = embed_client._post_json(  # type: ignore[attr-defined]
                                "/tokenize", {"content": s}
                            )
                            return len(resp.get("tokens", []))

                        try:
                            for _ in range(6):
                                n = _ntok(payload)
                                if n <= 2040:
                                    break
                                payload = payload[: int(len(payload) * 2040 / max(n, 1) * 0.95)]
                        except Exception:
                            payload = payload[:4000]
                    vectors = embed_client.embed(model=model_id, texts=[payload])
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

        # Record the indexed-representation mode for auditability (on the skill
        # store's corpus_meta). Soft-fail: metadata must never fail the embed pass.
        try:
            store.set_meta(META_KEY_CARD_INDEX, card_mode.value)
        except Exception as exc:  # noqa: BLE001 — audit metadata is best-effort
            logger.warning("could not record card_index meta: %s", exc)

        # Stamp the corpus schema version so `update`/`seed-corpus` read an
        # explicit marker instead of assuming "implicit v1". Soft-fail.
        try:
            store.set_meta(META_KEY_SCHEMA_VERSION, str(CORPUS_SCHEMA_VERSION))
        except Exception as exc:  # noqa: BLE001 — audit metadata is best-effort
            logger.warning("could not record schema_version meta: %s", exc)

        # Keep authored phase eligibility in sync with the Lance rows on every
        # pass (cheap UPDATEs, vectors untouched). NULL scope rows fall back to
        # the phase->category map at query time.
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
            if result_sink is not None:
                result_sink["dedup_hard"] = [asdict(m) for m in gate_result.hard]
                result_sink["dedup_soft"] = [asdict(m) for m in gate_result.soft]

        if stats.embedded > 0 or args.rebuild_fts:
            if stats.embedded > 0:
                logger.info(
                    "rebuilding BM25 FTS index after embedding %d fragment(s)", stats.embedded
                )
            else:
                logger.info("rebuilding BM25 FTS index (--rebuild-fts requested)")
            try:
                vs.rebuild_fts_index()
                logger.info("BM25 FTS index rebuilt")
            except Exception as exc:  # noqa: BLE001 — FTS rebuild is best-effort
                logger.warning("FTS index rebuild failed (BM25 leg degraded): %s.", exc)
        # reembed_fragments raises on any failure (see its handlers), so
        # stats.failed is always 0 here — no EXIT_LLM branch is reachable.
        return dedup_exit
    except Exception as exc:
        # The skill store enforces a single writer; a concurrent ingest/reembed
        # may briefly hold the lock and the open above fails. In v5 this is
        # transient — tell the user to retry rather than crash.
        if not is_lock_held_error(str(exc)):
            raise
        logger.error("database lock is held: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"FIX:   {LOCK_HELD_REMEDIATION}", file=sys.stderr)
        return EXIT_DB
    finally:
        if store is not None:
            store.close()
        if vs is not None:
            vs.close()
        if service_mode is not None:
            _start_main_service(service_mode)


def _stop_main_service() -> str | None:
    """Stop a running native main-API service so the corpus writer lock can be
    taken. Returns the mode used (``"systemd"`` | ``"port"``), or None when
    nothing was stopped (not running, container deployment, or the stop
    failed). The embed/rerank llama-servers stay up — the embed pass needs
    them.
    """
    from agentalloy.install import server_proc
    from agentalloy.install.subcommands.upgrade import (  # pyright: ignore[reportPrivateUsage]
        _is_systemd,
        _systemctl,
    )

    try:
        if _is_systemd() and _systemctl("is-active", "agentalloy.service") == 0:
            if _systemctl("stop", "agentalloy.service") != 0:
                return None
            logger.info("stopped agentalloy.service (it held the corpus DB open)")
            return "systemd"
        target = server_proc.resolve_deployment(None)
        if target.deployment == "container":
            return None
        pid = server_proc.find_listening_pid(target.port)
        if pid is None:
            return None
        server_proc.stop(pid, timeout_s=10.0)
        logger.info("stopped agentalloy server pid %d (it held the corpus DB open)", pid)
        return "port"
    except Exception as exc:  # noqa: BLE001 — fall through to the lock-error path
        logger.warning("could not stop the running service: %s", exc)
        return None


def _start_main_service(mode: str) -> None:
    """Restart what :func:`_stop_main_service` stopped. Best-effort: a restart
    failure must never fail the (already completed) embed pass."""
    from agentalloy.install import server_proc
    from agentalloy.install.subcommands.upgrade import (  # pyright: ignore[reportPrivateUsage]
        _systemctl,
    )

    try:
        if mode == "systemd":
            if _systemctl("start", "agentalloy.service") != 0:
                raise RuntimeError("systemctl start returned non-zero")
            logger.info("restarted agentalloy.service")
            return
        port = server_proc.configured_port()
        server_proc.start_background(port)
        logger.info("restarted agentalloy server on port %d", port)
    except Exception as exc:  # noqa: BLE001 — never fail the pass over the restart
        logger.warning(
            "could not restart the service (%s) — start it with `agentalloy server-start`", exc
        )


def _reopen_after_stop(settings: Any) -> DuckDBSkillStore:
    """Retry the writer open after stopping the service; the OS may take a
    moment to release the old process's file handle."""
    last: Exception | None = None
    for delay in (0.2, 0.5, 1.0, 2.0):
        try:
            return open_skills(settings, read_only=False)
        except LockHeldError as exc:
            last = exc
            time.sleep(delay)
    assert last is not None
    raise last


def run_bulk_reembed(
    no_restart: bool = False,
    allow_duplicates: bool = False,
    *,
    result_sink: dict[str, Any] | None = None,
) -> int:
    """Run the reembed CLI in-process. Returns its exit code.

    Shared entry point for callers that need to trigger a bulk reembed pass
    after ingesting new skills — ``install-packs`` (bundled-corpus bootstrap)
    and ``install-pack``/``install_local_pack`` (third-party path) both call
    this instead of each keeping a private copy.

    ``result_sink``, if given, is forwarded to :func:`main` and populated
    with ``dedup_hard``/``dedup_soft`` match detail — see its docstring.

    Never raises: a reembed failure (including a held DuckDB writer lock)
    is logged/printed with remediation and surfaced as exit code 2, not
    propagated as an exception, so callers can fold it into their own result
    dict without a try/except of their own.
    """
    try:
        argv: list[str] = []
        if no_restart:
            argv.append("--no-restart")
        if allow_duplicates:
            argv.append("--allow-duplicates")
        return main(argv, result_sink=result_sink)
    except Exception as exc:  # noqa: BLE001 — surface but don't crash the caller
        print(f"reembed raised: {exc}", file=sys.stderr)
        from agentalloy.install.subcommands.install_pack import LOCK_HELD_REMEDIATION
        from agentalloy.storage.skill_store import is_lock_held_error

        if is_lock_held_error(str(exc)):
            print(f"FIX:   {LOCK_HELD_REMEDIATION}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
