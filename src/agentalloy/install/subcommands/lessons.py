"""``lessons promote`` subcommand — promote a codified lesson into the corpus.

Compound-engineering bridge, task 04. Reads ``docs/solutions/<slug>.md``, turns
it into a domain-skill pack (via :mod:`agentalloy.install.lesson_pack`), runs a
**pre-ingest dedup probe**, and — only if it clears — installs it through the
existing ``install_local_pack`` rail.

The probe is *prevention, not cleanup*: the install rail writes skill rows and
vectors before its own dedup pass runs and never rolls back, so a hard duplicate
must be caught BEFORE install. The probe embeds the candidate fragments and
compares them against the live corpus with the same classifier the rail uses; a
hard hit (cosine >= ``dedup_hard_threshold``) refuses the promotion unless
``--allow-duplicates`` is passed.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentalloy.install.lesson_pack import generate_lesson_pack
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1

# Injected seams (overridable in tests). A real embed takes a string and returns
# a raw vector; a real store is a FragmentStore; a real install runs the rail.
EmbedFn = Callable[[str], list[float]]
InstallFn = Callable[..., dict[str, Any]]
BlockerFn = Callable[[], "str | None"]
RouteFn = Callable[[], Any]  # -> CorpusWriteRoute (lazy import to avoid a cycle)
PushFn = Callable[..., dict[str, Any]]

# install_local_pack actions that leave the corpus in a good state. Anything
# else (ingested_with_errors, schema_invalid, …, or a future action we don't
# know) is treated as a failed install — fail closed, never report "promoted"
# for a skill that isn't actually in the corpus (#390).
_INSTALL_OK_ACTIONS = frozenset({"ingested", "already_installed", "version_unchanged"})


def probe_lesson_duplicates(
    fragment_texts: list[str],
    *,
    embed: EmbedFn,
    vector_store: Any,
    hard_similarity: float,
    soft_similarity: float,
) -> list[Any]:
    """Return the hard cross-pack near-duplicate hits for the candidate fragments.

    Embeds each fragment and runs :func:`agentalloy.dedup_gate.dedup_fragment`
    against the live corpus. The candidate is not yet ingested, so there is no
    self-match to exclude and any hit at/above ``hard_similarity`` is a genuine
    cross-pack duplicate. Returns the list of hard ``SimilarityHit``s (empty when
    clear).
    """
    from agentalloy.dedup_gate import dedup_fragment

    hard_hits: list[Any] = []
    for i, text in enumerate(fragment_texts):
        vec = embed(text)
        hard_hit, _soft = dedup_fragment(
            label=f"lesson-f{i}",
            query_vec=vec,
            vector_store=vector_store,
            hard_similarity=hard_similarity,
            soft_similarity=soft_similarity,
        )
        if hard_hit is not None:
            hard_hits.append(hard_hit)
    return hard_hits


def _corpus_write_blocker() -> str | None:
    """Why the corpus can't be written right now, or ``None`` when it can (#390).

    Two conditions make the install rail pointless or impossible, and both used
    to surface only as a buried ingest error (or not at all):

    - **Corpus lock** — DuckDB is single-writer per file and even the service's
      read-only handle excludes a CLI writer, so ingest can never succeed while
      a service is holding the store. Detected by briefly opening it read-write.
    - **Serving container** — the live corpus lives inside the container's data
      volume (``agentalloy-data:/app/data``); a host-side promote writes a host
      corpus the serving container never reads.

    Checked in that order, against *reality* rather than configured state: the
    lock probe catches whatever process actually holds the host corpus (e.g. a
    from-source native run on a container-configured box), and the container
    check only fires when a container deployment is configured AND something is
    actually serving the port — a stopped container blocks nothing.
    """
    from agentalloy.config import get_settings

    db = Path(get_settings().duckdb_path)
    if db.is_file():
        try:
            import duckdb

            duckdb.connect(str(db)).close()
        except Exception as exc:
            if "lock" in str(exc).lower():
                return (
                    f"the corpus ({db}) is locked by the running AgentAlloy service. "
                    "Stop it (`agentalloy server-stop`), re-run the promote, then "
                    "`agentalloy server-start`."
                )
            return f"the corpus at {db} could not be opened for writing: {exc}"

    try:
        from agentalloy.install.server_proc import port_reachable, resolve_deployment

        target = resolve_deployment()
        if target.deployment == "container" and port_reachable(target.port):
            return (
                "the serving AgentAlloy is a container — its live corpus lives inside "
                "the container's data volume, which a host-side promote cannot reach "
                "(service-mediated ingest is tracked in #390)."
            )
    except Exception:
        pass  # unreadable install state -> assume native; the lock probe above governs

    return None


def _install_failure_error(install_result: dict[str, Any]) -> str:
    """One-line failure summary from an install result (first ingest stderr wins)."""
    for r in install_result.get("ingest_results") or []:
        if isinstance(r, dict) and r.get("outcome") == "failed" and r.get("stderr_tail"):
            return str(r["stderr_tail"]).strip().splitlines()[-1]
    return str(install_result.get("error") or install_result.get("action") or "install failed")


def _default_embed() -> EmbedFn:
    from agentalloy.config import get_settings
    from agentalloy.embed_provider import get_embed_client

    settings = get_settings()
    client = get_embed_client(settings)
    model = settings.runtime_embedding_model

    def embed(text: str) -> list[float]:
        # ``search_document:`` is the same document prefix reembed uses, so the
        # probe's vectors live in the same space as the corpus it compares to.
        return client.embed(model=model, texts=[f"search_document: {text}"])[0]

    return embed


def promote_lesson(
    slug: str,
    *,
    root: Path,
    allow_duplicates: bool = False,
    embed: EmbedFn | None = None,
    vector_store: Any | None = None,
    install: InstallFn | None = None,
    route_fn: RouteFn | None = None,
    push_fn: PushFn | None = None,
) -> dict[str, Any]:
    """Promote ``docs/solutions/<slug>.md`` into the corpus. Returns a result dict.

    The ``embed`` / ``vector_store`` / ``install`` seams are injected in tests;
    in production they resolve to the real embed client, the open fragment
    store, and ``install_local_pack``. ``route_fn`` / ``push_fn`` select and
    drive the corpus-write route (host-direct vs. the running service); they
    default to :func:`decide_corpus_write_route` / :func:`push_pack_to_service`.
    The host-side dedup probe runs only on the ``write_host`` path — on
    ``via_service`` the endpoint owns the gate (and a container corpus can't be
    probed from the host anyway).
    """
    from agentalloy.config import get_settings
    from agentalloy.install.corpus_write_route import (
        decide_corpus_write_route,
        push_pack_to_service,
    )

    lesson_path = root / "docs" / "solutions" / f"{slug}.md"
    if not lesson_path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "lesson_not_found",
            "slug": slug,
            "error": f"no lesson at docs/solutions/{slug}.md — nothing to promote.",
        }

    gen = generate_lesson_pack(lesson_path, root / ".agentalloy" / "custom-skills")
    if gen.get("action") != "generated":
        return gen
    pack_dir = Path(gen["pack_dir"])
    fragment_texts: list[str] = gen["fragment_contents"]

    # --- corpus-write routing (#390) ---------------------------------------
    # Runs after generation (the pack on disk is still useful). The route picks
    # where the write goes: the running service (native or container), a direct
    # host write, or — when neither is possible — an honest block.
    route = (route_fn or decide_corpus_write_route)()
    if route.mode == "blocked":
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "install_blocked",
            "slug": slug,
            "skill_id": gen["skill_id"],
            "pack_dir": str(pack_dir),
            "error": f"pack generated but NOT installed: {route.reason}",
            "remediation": (
                f"once the corpus is writable, install it with `agentalloy install-pack {pack_dir}`."
            ),
        }

    if route.mode == "via_service":
        # The service owns the dedup gate and the install; the host never probes
        # (it can't reach a container corpus). Its dedup verdicts surface as-is;
        # everything else runs through the shared finalizer.
        install_result = (push_fn or push_pack_to_service)(
            pack_dir, route=route, allow_duplicates=allow_duplicates, reembed=True
        )
        action = install_result.get("action")
        if action in ("duplicate_refused", "dedup_probe_failed"):
            install_result.setdefault("schema_version", SCHEMA_VERSION)
            install_result.setdefault("slug", slug)
            install_result.setdefault("skill_id", gen["skill_id"])
            install_result.setdefault("pack_dir", str(pack_dir))
            return install_result
        return _finalize_promote(
            install_result, slug=slug, gen=gen, pack_dir=pack_dir, hard_hits=[]
        )

    # else: write_host — the direct host-side probe + install below.
    settings = get_settings()

    # --- pre-ingest dedup probe -------------------------------------------
    store = vector_store
    store_opened_here = False
    if store is None:
        try:
            from agentalloy.storage.open import open_fragments

            store = open_fragments(settings)
            store_opened_here = True
        except Exception:
            # No corpus yet (fresh install) → nothing to duplicate against.
            store = None

    hard_hits: list[Any] = []
    probe_error: str | None = None
    if store is not None:
        try:
            embed_fn = embed or _default_embed()
            hard_hits = probe_lesson_duplicates(
                fragment_texts,
                embed=embed_fn,
                vector_store=store,
                hard_similarity=settings.dedup_hard_threshold,
                soft_similarity=settings.dedup_soft_threshold,
            )
        except Exception as exc:  # embed server down, dim mismatch, corrupt store, ...
            probe_error = str(exc)
        finally:
            if store_opened_here:
                close = getattr(store, "close", None)
                if callable(close):
                    close()

    # Fail closed: if we could not run the dedup check, do NOT install (the rail
    # doesn't roll back, so an unchecked install could bloat the corpus). The user
    # can bypass the check explicitly with --allow-duplicates.
    if probe_error and not allow_duplicates:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "dedup_probe_failed",
            "slug": slug,
            "skill_id": gen["skill_id"],
            "pack_dir": str(pack_dir),
            "error": (
                f"could not check '{slug}' for duplicates ({probe_error}). Not installed. "
                "Re-run with --allow-duplicates to skip the dedup check."
            ),
        }

    if hard_hits and not allow_duplicates:
        dups = sorted({getattr(h, "skill_id", "?") for h in hard_hits})
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "duplicate_refused",
            "slug": slug,
            "skill_id": gen["skill_id"],
            "pack_dir": str(pack_dir),
            "duplicates": dups,
            "error": (
                f"lesson '{slug}' duplicates existing corpus skill(s) {dups} "
                f"(cosine >= {settings.dedup_hard_threshold}). Not installed. "
                "Re-run with --allow-duplicates to install anyway."
            ),
        }

    # --- install through the existing rail --------------------------------
    install_fn = install
    if install_fn is None:
        from agentalloy.install.subcommands.install_pack import install_local_pack

        install_fn = install_local_pack
    install_result = install_fn(pack_dir, root=root, strict=True, allow_duplicates=allow_duplicates)
    return _finalize_promote(
        install_result, slug=slug, gen=gen, pack_dir=pack_dir, hard_hits=hard_hits
    )


def _finalize_promote(
    install_result: dict[str, Any],
    *,
    slug: str,
    gen: dict[str, Any],
    pack_dir: Path,
    hard_hits: list[Any],
) -> dict[str, Any]:
    """Map an install result (host-direct or service-mediated) to a promote result.

    Fail closed on the install leg (#390): "promoted" previously reported
    success even when the rail rolled everything back (e.g. the running service
    held the corpus lock), leaving a skill retrieval can never surface. Any
    action outside the known-good set is a failed install (AC-10 — same shape
    whichever path produced the result).
    """
    install_action = install_result.get("action")
    if install_action is not None and install_action not in _INSTALL_OK_ACTIONS:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "install_failed",
            "slug": slug,
            "skill_id": gen["skill_id"],
            "pack_dir": str(pack_dir),
            "install": install_result,
            "error": (
                f"pack generated but the corpus install failed "
                f"({install_action}): {_install_failure_error(install_result)}"
            ),
            "remediation": str(
                install_result.get("remediation")
                or f"fix the failure and re-run `agentalloy install-pack {pack_dir}`."
            ),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "promoted",
        "slug": slug,
        "skill_id": gen["skill_id"],
        "pack_dir": str(pack_dir),
        "domain_tags": gen.get("domain_tags"),
        "soft_or_forced_duplicates": sorted({getattr(h, "skill_id", "?") for h in hard_hits})
        or None,
        "install": install_result,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser(
        "lessons",
        help="Promote codified compound-engineering lessons (docs/solutions/*.md) into the corpus.",
    )
    sub = p.add_subparsers(dest="lessons_cmd")
    pr = sub.add_parser(
        "promote",
        help="Turn docs/solutions/<slug>.md into a domain-skill pack and install it (dedup-gated).",
    )
    pr.add_argument("slug", help="Lesson slug — the basename of docs/solutions/<slug>.md.")
    pr.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Install even if a hard cross-pack near-duplicate (cosine >= 0.92) is found.",
    )
    add_json_flag(pr)
    pr.set_defaults(func=_run_promote)
    p.set_defaults(func=lambda _args: p.print_help() or 1)


def _render_human(result: dict[str, Any]) -> None:
    action = result.get("action")
    print_rich("\n  [bold]lessons promote[/bold]\n")
    if action == "promoted":
        print_rich(f"  [green]promoted[/green] {result.get('slug')} -> {result.get('skill_id')}")
        print_rich(f"  pack: {result.get('pack_dir')}")
        print_rich(f"  tags: {', '.join(result.get('domain_tags') or [])}")
        forced = result.get("soft_or_forced_duplicates")
        if forced:
            print_rich(f"  [yellow]note[/yellow]: installed over near-duplicate(s) {forced}")
    elif action == "duplicate_refused":
        print_rich(f"  [red]refused[/red]: {result.get('error')}")
    elif action in ("install_blocked", "install_failed"):
        print_rich(f"  [red]not installed[/red]: {result.get('error')}")
        if result.get("remediation"):
            print_rich(f"  remediation: {result.get('remediation')}")
    else:
        print_rich(f"  [red]FAILED[/red]: {result.get('error', action)}")
    print_rich()


def _run_promote(args: argparse.Namespace) -> int:
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    result = promote_lesson(
        args.slug,
        root=_repo_root(),
        allow_duplicates=getattr(args, "allow_duplicates", False),
    )
    write_result(result, args, human_fn=_render_human)
    return 0 if result.get("action") == "promoted" else 1
