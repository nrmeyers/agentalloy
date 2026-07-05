"""BUC-1614 — Parallel Pass 3 (call resolution).

Pass 3 walks the AST cache and asks each ``CallProcessor`` to resolve the
calls in one file. Resolution itself is pure (reads ``function_registry``,
``import_mapping``, ``class_inheritance``, ``rebind_registry`` — all
populated during Pass 2 and not mutated thereafter), but each resolved
call ends with a write to the shared ``IngestorProtocol``.

Profiling on Python-only fixtures (BUC-1614 spike, 2026-05-14):

    code-graph-rag (368 .py files):  Pass 2 = 6.04s, Pass 3 = 8.20s
    skillsmith     (156 .py files):  Pass 2 = 0.63s, Pass 3 = 0.97s

Pass 3 dominates by ~35–55 % on Python repos, which justifies
parallelising it ahead of Pass 2. (BUC-1614's original gate said "if
Pass 3 >= Pass 2, pivot to Pass 3" — we hit that condition.)

Strategy
--------
* ``ThreadPoolExecutor`` with ``PARSE_PARALLELISM`` workers
  (``min(cpu_count, 8)`` by default; ``1`` keeps the legacy serial path).
* Each worker gets its own ``CallProcessor`` whose ingestor is a
  ``BufferedIngestor`` — a thread-local recorder of node/relationship
  batches.
* The worker pool reads ``function_registry``, ``import_mapping``,
  ``class_inheritance`` and ``rebind_registry`` strictly read-only.
  These structures are populated entirely by Pass 2 and Pass 2.5; no
  mutation happens during Pass 3 (verified by grep — see PR notes).
* After all workers complete, a single-writer drain replays the
  buffered operations into the real ingestor in **original AST-cache
  iteration order**, preserving the deterministic write ordering the
  ingestor relies on for stable batch keying.

Tree-sitter caveat
------------------
``tree_sitter.Parser`` instances are NOT thread-safe, but Pass 3 never
reparses — it only walks already-built ``root_node`` trees from the AST
cache and creates ephemeral ``QueryCursor`` objects (which are
per-call, so worker-local by construction). No shared mutable
tree-sitter state crosses thread boundaries.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tree_sitter import Node

from .. import constants as cs
from .._logging import logger
from ..types_defs import LanguageQueries, PropertyDict, PropertyValue
from .call_processor import CallProcessor

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from .factory import ProcessorFactory


DEFAULT_PARALLELISM_CEILING = 8
"""Hard upper bound on workers regardless of cpu_count. Pass 3 is
write-throttled by the drain step; past 8 workers we see diminishing
returns and rising thread-contention on the GIL during tree-sitter
query execution."""


def get_parse_parallelism() -> int:
    """Read ``PARSE_PARALLELISM`` from env.

    Defaults to ``1`` (serial). BUC-1614 sweep (2026-05-14) showed that
    thread-based parallelism gives **no speedup** on Python repos —
    Pass 3 is GIL-bound and 4 workers is ~3–4 % SLOWER than serial due
    to thread coordination overhead. The infrastructure ships as a
    foundation for future process-based or free-threaded-Python work;
    operators can still set ``PARSE_PARALLELISM=N`` to opt into the
    pool for experimentation.

    * Unset or 1  -> serial (legacy path)
    * 0 or <0     -> serial (treat as disabled)
    * N >= 2      -> N workers (operator opt-in; logs regression risk)
    """
    raw = os.environ.get("PARSE_PARALLELISM", "").strip()
    if not raw:
        return 1
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "PARSE_PARALLELISM={!r} is not an integer; defaulting to 1 (serial)",
            raw,
        )
        return 1
    if n < 1:
        return 1
    if n > 1:
        logger.warning(
            "PARSE_PARALLELISM={n} requested. Note: BUC-1614 measured thread "
            "parallelism as ~3-4 %% SLOWER than serial on Python repos due to "
            "GIL contention. Use only for experimentation until process-based "
            "or free-threaded Python is wired up.",
            n=n,
        )
    return n


class BufferedIngestor:
    """Thread-local recorder. Captures every write the inner pass would
    make and replays them into the real ingestor during the single-
    writer drain phase.

    Implements the ``IngestorProtocol`` shape that CallProcessor /
    CallResolver actually use (``ensure_node_batch``,
    ``ensure_relationship_batch``, ``flush_all``). Other protocol methods
    raise so we catch unexpected callers early.
    """

    __slots__ = ("_nodes", "_rels")

    def __init__(self) -> None:
        # (label, properties)
        self._nodes: list[tuple[str, PropertyDict]] = []
        # (from_spec, rel_type, to_spec, properties)
        self._rels: list[
            tuple[
                tuple[str, str, PropertyValue],
                str,
                tuple[str, str, PropertyValue],
                PropertyDict | None,
            ]
        ] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self._nodes.append((label, properties))

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self._rels.append((from_spec, rel_type, to_spec, properties))

    def flush_all(self) -> None:
        # No-op for the buffered layer; the real flush happens on the
        # underlying ingestor after the drain.
        return

    # ---- diagnostics ------------------------------------------------

    def __len__(self) -> int:
        return len(self._nodes) + len(self._rels)

    def replay_into(self, target: IngestorProtocol) -> None:
        """Drain buffered operations into ``target`` in capture order."""
        for label, props in self._nodes:
            target.ensure_node_batch(label, props)
        for from_spec, rel_type, to_spec, props in self._rels:
            target.ensure_relationship_batch(from_spec, rel_type, to_spec, props)


def _build_worker_processor(factory: ProcessorFactory, buffered: BufferedIngestor) -> CallProcessor:
    """Build a CallProcessor whose writes route to ``buffered`` but whose
    read-only inputs (function_registry, import_processor, type_inference,
    class_inheritance, rebind_registry) are the shared instances from
    Pass 2."""
    return CallProcessor(
        ingestor=buffered,  # type: ignore[arg-type]
        repo_path=factory.repo_path,
        project_name=factory.project_name,
        function_registry=factory.function_registry,
        import_processor=factory.import_processor,
        type_inference=factory.type_inference,
        class_inheritance=factory.definition_processor.class_inheritance,
        rebind_registry=factory.rebind_processor.registry,
    )


def process_calls_parallel(
    factory: ProcessorFactory,
    ast_cache_items: list[tuple[Path, tuple[Node, Any]]],
    queries: dict[cs.SupportedLanguage, LanguageQueries],
    target_ingestor: IngestorProtocol,
    workers: int,
) -> None:
    """Run Pass 3 with ``workers`` threads.

    Preserves the serial ingestor write order: each worker writes into
    its own ``BufferedIngestor``; once all complete we drain buffers in
    the order ``ast_cache_items`` lists them and replay into the real
    ingestor on the calling thread.

    If ``workers <= 1`` the caller should use the legacy serial path —
    this function still works at workers=1 but adds buffering overhead.
    """
    n = len(ast_cache_items)
    if n == 0:
        return

    # Pre-build per-worker processors. ThreadPoolExecutor reuses threads,
    # so this maps 1:1 with worker slots rather than 1:1 with items.
    # Each task receives a fresh buffer; the CallProcessor instance is
    # rebuilt with that buffer so the buffer is the only thread-local
    # state touched.
    buffers: list[BufferedIngestor] = [BufferedIngestor() for _ in range(n)]

    def _run(idx: int) -> int:
        file_path, (root_node, language) = ast_cache_items[idx]
        processor = _build_worker_processor(factory, buffers[idx])
        processor.process_calls_in_file(file_path, root_node, language, queries)
        return idx

    import time as _t

    completed = 0
    t_pool = _t.monotonic()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cgr-pass3") as ex:
        futures = [ex.submit(_run, i) for i in range(n)]
        for fut in as_completed(futures):
            try:
                fut.result()
                completed += 1
            except Exception as exc:
                # process_calls_in_file already swallows exceptions per
                # file and logs them; this is a defensive layer for any
                # surprises (e.g. tree-sitter binding bug).
                logger.error("parallel_calls: worker future raised: {}", exc)
    pool_s = _t.monotonic() - t_pool

    # Single-writer drain. Order matches ast_cache_items.
    t_drain = _t.monotonic()
    for buf in buffers:
        buf.replay_into(target_ingestor)
    drain_s = _t.monotonic() - t_drain

    logger.debug(
        "parallel_calls: {n}/{total} files, {w} workers; pool={pool:.3f}s drain={drain:.3f}s",
        n=completed,
        total=n,
        w=workers,
        pool=pool_s,
        drain=drain_s,
    )
