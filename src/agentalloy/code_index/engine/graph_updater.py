import hashlib
import json
import os
import sys
import time as _time
from collections import OrderedDict, defaultdict
from collections.abc import Callable, ItemsView, KeysView
from dataclasses import asdict, dataclass
from pathlib import Path

from tree_sitter import Node, Parser

from . import constants as cs
from . import logs as ls
from ._logging import logger
from .engine_config import DEFAULT_ENGINE_CONFIG, EngineConfig
from .ingest_queries import (
    CYPHER_DELETE_MODULE_DEFINES,
    CYPHER_DELETE_MODULE_METHODS,
    CYPHER_DELETE_MODULE_NODE,
    CYPHER_DELETE_ORPHAN_PACKAGES,
)
from .language_spec import get_language_spec
from .parsers.factory import ProcessorFactory
from .services import IngestorProtocol
from .types_defs import (
    FunctionRegistry,
    LanguageQueries,
    NodeType,
    QualifiedName,
    SimpleNameLookup,
    TrieNode,
)
from .utils.path_utils import should_prune_dir, should_skip_path

type FileHashCache = dict[str, str]


@dataclass(slots=True)
class StatEntry:
    """One row of the stat-cache: enough to decide whether SHA-256 is required.

    `mtime_ns` is the POSIX modification time in nanoseconds (st_mtime_ns).
    `size` is the file size in bytes (st_size).
    `sha` is the SHA-256 hex digest captured the last time the file's stat
    fingerprint matched. The SHA stays the source of truth — stat is only a
    pre-filter that lets us skip the read+hash on unchanged files.

    Edge cases handled by the comparison logic in `_should_compute_sha`:

    * **Mtime regression** (e.g. `git clone --depth` resets mtime to checkout
      time, or a file is restored from backup with an older mtime). The cached
      mtime no longer matches — SHA is recomputed, which is the safe action.
    * **Mtime liar** (some filesystems round mtime to 1–2 s precision; a fast
      edit can preserve mtime). We tolerate sub-millisecond jitter via
      `STAT_CACHE_MTIME_TOLERANCE_NS` but still require `size` equality, so an
      edit that changes file length will always invalidate the entry. An edit
      that preserves both mtime (within tolerance) AND size will slip through
      — this is the inherent ceiling of stat-based detection; the SHA cache
      remains the authority for any code path that reads back the hash.
    """

    mtime_ns: int
    size: int
    sha: str


type FileStatCache = dict[str, StatEntry]

# Sidecar cache files that should never be scanned as input. Used by
# `_collect_eligible_files` to skip self-referential entries.
_SIDECAR_CACHE_FILENAMES: frozenset[str] = frozenset(
    {cs.HASH_CACHE_FILENAME, cs.STAT_CACHE_FILENAME}
)


class FunctionRegistryTrie:
    __slots__ = ("root", "_entries", "_simple_name_lookup")

    def __init__(self, simple_name_lookup: SimpleNameLookup | None = None) -> None:
        self.root: TrieNode = {}
        self._entries: FunctionRegistry = {}
        self._simple_name_lookup = simple_name_lookup

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self._entries[qualified_name] = func_type

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        current: TrieNode = self.root

        for part in parts:
            if part not in current:
                current[part] = {}
            child = current[part]
            assert isinstance(child, dict)
            current = child

        current[cs.TRIE_TYPE_KEY] = func_type
        current[cs.TRIE_QN_KEY] = qualified_name

    def get(
        self, qualified_name: QualifiedName, default: NodeType | None = None
    ) -> NodeType | None:
        return self._entries.get(qualified_name, default)

    def __contains__(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._entries

    def __getitem__(self, qualified_name: QualifiedName) -> NodeType:
        return self._entries[qualified_name]

    def __setitem__(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self.insert(qualified_name, func_type)

    def __delitem__(self, qualified_name: QualifiedName) -> None:
        if qualified_name not in self._entries:
            return

        del self._entries[qualified_name]

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        self._cleanup_trie_path(parts, self.root)

    def _cleanup_trie_path(self, parts: list[str], node: TrieNode) -> bool:
        if not parts:
            node.pop(cs.TRIE_QN_KEY, None)
            node.pop(cs.TRIE_TYPE_KEY, None)
            return not node

        part = parts[0]
        if part not in node:
            return False

        child = node[part]
        assert isinstance(child, dict)
        if self._cleanup_trie_path(parts[1:], child):
            del node[part]

        is_endpoint = cs.TRIE_QN_KEY in node
        has_children = any(not key.startswith(cs.TRIE_INTERNAL_PREFIX) for key in node)
        return not has_children and not is_endpoint

    def _navigate_to_prefix(self, prefix: str) -> TrieNode | None:
        parts = prefix.split(cs.SEPARATOR_DOT) if prefix else []
        current: TrieNode = self.root
        for part in parts:
            if part not in current:
                return None
            child = current[part]
            assert isinstance(child, dict)
            current = child
        return current

    def _collect_from_subtree(
        self,
        node: TrieNode,
        filter_fn: Callable[[QualifiedName], bool] | None = None,
    ) -> list[tuple[QualifiedName, NodeType]]:
        results: list[tuple[QualifiedName, NodeType]] = []

        def dfs(n: TrieNode) -> None:
            if cs.TRIE_QN_KEY in n:
                qn = n[cs.TRIE_QN_KEY]
                func_type = n[cs.TRIE_TYPE_KEY]
                assert isinstance(qn, str) and isinstance(func_type, NodeType)
                if filter_fn is None or filter_fn(qn):
                    results.append((qn, func_type))

            for key, child in n.items():
                if not key.startswith(cs.TRIE_INTERNAL_PREFIX):
                    assert isinstance(child, dict)
                    dfs(child)

        dfs(node)
        return results

    def keys(self) -> KeysView[QualifiedName]:
        return self._entries.keys()

    def items(self) -> ItemsView[QualifiedName, NodeType]:
        return self._entries.items()

    def __len__(self) -> int:
        return len(self._entries)

    def find_with_prefix_and_suffix(self, prefix: str, suffix: str) -> list[QualifiedName]:
        node = self._navigate_to_prefix(prefix)
        if node is None:
            return []
        suffix_pattern = f".{suffix}"
        matches = self._collect_from_subtree(node, lambda qn: qn.endswith(suffix_pattern))
        return [qn for qn, _ in matches]

    def find_ending_with(self, suffix: str) -> list[QualifiedName]:
        if self._simple_name_lookup is not None and suffix in self._simple_name_lookup:
            # (H) O(1) lookup using the simple_name_lookup index
            return list(self._simple_name_lookup[suffix])
        # (H) Fallback to linear scan if no index available
        return [qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]:
        node = self._navigate_to_prefix(prefix)
        return [] if node is None else self._collect_from_subtree(node)


class BoundedASTCache:
    __slots__ = ("cache", "max_entries", "max_memory_bytes", "_config")

    def __init__(
        self,
        max_entries: int | None = None,
        max_memory_mb: int | None = None,
        config: EngineConfig = DEFAULT_ENGINE_CONFIG,
    ):
        self.cache: OrderedDict[Path, tuple[Node, cs.SupportedLanguage]] = OrderedDict()
        self._config = config
        self.max_entries = max_entries if max_entries is not None else config.cache_max_entries
        max_mem = max_memory_mb if max_memory_mb is not None else config.cache_max_memory_mb
        self.max_memory_bytes = max_mem * cs.BYTES_PER_MB

    def __setitem__(self, key: Path, value: tuple[Node, cs.SupportedLanguage]) -> None:
        if key in self.cache:
            del self.cache[key]

        self.cache[key] = value

        self._enforce_limits()

    def __getitem__(self, key: Path) -> tuple[Node, cs.SupportedLanguage]:
        value = self.cache[key]
        self.cache.move_to_end(key)
        return value

    def __delitem__(self, key: Path) -> None:
        if key in self.cache:
            del self.cache[key]

    def __contains__(self, key: Path) -> bool:
        return key in self.cache

    def items(self) -> ItemsView[Path, tuple[Node, cs.SupportedLanguage]]:
        return self.cache.items()

    def _enforce_limits(self) -> None:
        while len(self.cache) > self.max_entries:
            self.cache.popitem(last=False)  # (H) Remove least recently used

        if self._should_evict_for_memory():
            entries_to_remove = max(1, len(self.cache) // self._config.cache_eviction_divisor)
            for _ in range(entries_to_remove):
                if self.cache:
                    self.cache.popitem(last=False)

    def _should_evict_for_memory(self) -> bool:
        try:
            cache_size = sum(sys.getsizeof(v) for v in self.cache.values())
            return cache_size > self.max_memory_bytes
        except Exception:
            return len(self.cache) > self.max_entries * self._config.cache_memory_threshold_ratio


def _hash_file(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with filepath.open("rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_hash_cache(cache_path: Path) -> FileHashCache:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logger.info(ls.HASH_CACHE_LOADED, count=len(data), path=cache_path)
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(ls.HASH_CACHE_LOAD_FAILED, path=cache_path, error=e)
    return {}


def _save_hash_cache(cache_path: Path, hashes: FileHashCache) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2)
        logger.info(ls.HASH_CACHE_SAVED, count=len(hashes), path=cache_path)
    except OSError as e:
        logger.warning(ls.HASH_CACHE_SAVE_FAILED, path=cache_path, error=e)


def _load_stat_cache(cache_path: Path) -> FileStatCache:
    """Load the stat-cache sidecar. Missing or malformed → empty dict.

    Rows missing any required field are silently dropped — never raise here;
    a bad stat cache must degrade gracefully to a full re-hash, not fail the
    index run.
    """
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open(encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out: FileStatCache = {}
        for key, val in raw.items():
            if not isinstance(val, dict):
                continue
            mtime = val.get("mtime_ns")
            size = val.get("size")
            sha = val.get("sha")
            if isinstance(mtime, int) and isinstance(size, int) and isinstance(sha, str):
                out[key] = StatEntry(mtime_ns=mtime, size=size, sha=sha)
        logger.info(ls.STAT_CACHE_LOADED, count=len(out), path=cache_path)
        return out
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(ls.STAT_CACHE_LOAD_FAILED, path=cache_path, error=e)
        return {}


def _save_stat_cache(cache_path: Path, entries: FileStatCache) -> None:
    """Persist the stat-cache as JSON. Failures are non-fatal."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {key: asdict(entry) for key, entry in entries.items()}
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
        logger.info(ls.STAT_CACHE_SAVED, count=len(entries), path=cache_path)
    except OSError as e:
        logger.warning(ls.STAT_CACHE_SAVE_FAILED, path=cache_path, error=e)


def _stat_matches(cached: StatEntry, mtime_ns: int, size: int) -> bool:
    """Decide whether a cached stat entry still describes the on-disk file.

    Size must match exactly. Mtime must match within
    `STAT_CACHE_MTIME_TOLERANCE_NS` to tolerate filesystem-level rounding
    (FAT/exFAT, some network mounts). A cached mtime *greater* than the
    current mtime — the regression case, e.g. a `git clone --depth` checkout
    or a file restored from backup — fails this check, so SHA is recomputed.
    That is the safe behaviour.
    """
    if cached.size != size:
        return False
    return abs(cached.mtime_ns - mtime_ns) <= cs.STAT_CACHE_MTIME_TOLERANCE_NS


class GraphUpdater:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        parsers: dict[cs.SupportedLanguage, Parser],
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        unignore_paths: frozenset[str] | None = None,
        exclude_paths: frozenset[str] | None = None,
        progress_cb: Callable[[dict], None] | None = None,
        cache_dir: Path | None = None,
        config: EngineConfig = DEFAULT_ENGINE_CONFIG,
    ):
        # NOTE (agentalloy vendoring): the upstream in-process embedding pass
        # (skip_embeddings flag + _generate_semantic_embeddings) was removed —
        # embedding is always handled externally by the caller.
        self.ingestor = ingestor
        self._config = config
        # When set, the hash/stat sidecar caches are written here instead of
        # the indexed repo root (keeps the indexed tree pristine).
        self.cache_dir = cache_dir
        self._single_file: Path | None = None
        if repo_path.is_file():
            resolved = repo_path.resolve()
            self._single_file = resolved
            repo_path = resolved.parent
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_name = repo_path.resolve().name
        self.simple_name_lookup: SimpleNameLookup = defaultdict(set)
        self.function_registry = FunctionRegistryTrie(simple_name_lookup=self.simple_name_lookup)
        self.ast_cache = BoundedASTCache(config=config)
        self.unignore_paths = unignore_paths
        self.exclude_paths = exclude_paths
        self._progress_cb = progress_cb

        self.factory = ProcessorFactory(
            ingestor=self.ingestor,
            repo_path=self.repo_path,
            project_name=self.project_name,
            queries=self.queries,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            ast_cache=self.ast_cache,
            unignore_paths=self.unignore_paths,
            exclude_paths=self.exclude_paths,
        )

    def _emit_progress(self, event: dict) -> None:
        """Fire the progress callback safely — errors never abort indexing."""
        if self._progress_cb is not None:
            try:
                self._progress_cb(event)
            except Exception:
                # Re-raise only CancelledError-style signals; swallow everything else
                # so a buggy callback cannot break the index run.
                raise

    def _is_dependency_file(self, file_name: str, filepath: Path) -> bool:
        return (
            file_name.lower() in cs.DEPENDENCY_FILES or filepath.suffix.lower() == cs.CSPROJ_SUFFIX
        )

    def run(self, force: bool = False) -> None:
        self._emit_progress({"phase": "discovering"})
        self.ingestor.ensure_node_batch(cs.NODE_PROJECT, {cs.KEY_NAME: self.project_name})
        logger.info(ls.ENSURING_PROJECT, name=self.project_name)

        logger.info(ls.PASS_1_STRUCTURE)
        self.factory.structure_processor.identify_structure()

        logger.info(ls.PASS_2_FILES)
        self._process_files(force=force)

        logger.info(ls.FOUND_FUNCTIONS, count=len(self.function_registry))

        # BUC-1611: discover module-level method rebindings (Python
        # monkey-patching) BEFORE call resolution, so the call resolver
        # can swap rebound qnames in-flight.  Edge emission happens at
        # the end of this pass — independent of CALLS resolution.
        self._discover_method_rebindings()

        logger.info(ls.PASS_3_CALLS)
        self._process_function_calls()

        self.factory.definition_processor.process_all_method_overrides()

        logger.info(ls.ANALYSIS_COMPLETE)
        self._emit_progress({"phase": "writing"})
        self.ingestor.flush_all()

        # Embedding is always external in the vendored engine (see VENDORED.md).
        self._emit_progress({"phase": "finalizing", "progress_pct": 98.0})
        self._emit_progress({"phase": "done", "progress_pct": 100.0})

    def remove_file_from_state(self, file_path: Path) -> None:
        logger.debug(ls.REMOVING_STATE, path=file_path)

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug(ls.REMOVED_FROM_CACHE)

        relative_path = file_path.relative_to(self.repo_path)
        path_parts = (
            relative_path.parent.parts
            if file_path.name == cs.INIT_PY
            else relative_path.with_suffix("").parts
        )
        module_qn_prefix = cs.SEPARATOR_DOT.join([self.project_name, *path_parts])

        qns_to_remove = set()

        for qn in list(self.function_registry.keys()):
            if qn.startswith(f"{module_qn_prefix}.") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(ls.REMOVING_QNS, count=len(qns_to_remove))

        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(ls.CLEANED_SIMPLE_NAME, name=simple_name)

        # ------------------------------------------------------------------
        # Graph DB cleanup — remove Module and its descendants, then prune
        # any Package nodes that have become orphans.
        # Skipped gracefully when the ingestor does not support writes (e.g.
        # in unit tests using a stub ingestor).
        # ------------------------------------------------------------------
        if hasattr(self.ingestor, "execute_write"):
            params = {"qn": module_qn_prefix}
            try:
                self.ingestor.execute_write(CYPHER_DELETE_MODULE_METHODS, params)
                self.ingestor.execute_write(CYPHER_DELETE_MODULE_DEFINES, params)
                self.ingestor.execute_write(CYPHER_DELETE_MODULE_NODE, params)
                self.ingestor.execute_write(CYPHER_DELETE_ORPHAN_PACKAGES, {})
                logger.debug(
                    "Removed graph nodes for module %s and pruned orphan packages",
                    module_qn_prefix,
                )
            except Exception as exc:
                logger.warning(
                    "graph_updater: could not remove graph nodes for %s: %s",
                    module_qn_prefix,
                    exc,
                )

    def _collect_eligible_files(self) -> list[Path]:
        if self._single_file is not None:
            if not should_skip_path(
                self._single_file,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                return [self._single_file]
            return []

        eligible: list[Path] = []
        # Prune heavy / ignored directories AT TRAVERSAL TIME instead of
        # walking the entire on-disk tree and rejecting per-file afterwards.
        #
        # The previous implementation used ``repo_path.rglob("*")``, which
        # descends into and stats every entry — including ``node_modules``,
        # ``.git``, and nested git worktrees under ``.claude/worktrees`` (each
        # a full repo checkout with its own ``node_modules``). For a local
        # working tree that can be tens of thousands of files / tens of GB of
        # pure noise; merely enumerating them stalled the "discovering" phase
        # long enough for the job watchdog to reap the job. (GitHub repos were
        # unaffected because they index from a clean ``git clone`` with no
        # node_modules / worktrees.)
        #
        # ``os.walk`` with in-place ``dirs[:]`` filtering lets us drop those
        # subtrees before descending, so the scan cost is proportional to the
        # indexed source tree rather than the whole working tree. The
        # subsequent per-file ``should_skip_path`` check is retained as a
        # backstop for file-level rules (suffix/basename excludes) the
        # directory prune does not cover.
        repo_path_str = str(self.repo_path)
        for dirpath, dirnames, filenames in os.walk(repo_path_str, topdown=True, followlinks=False):
            dir_path = Path(dirpath)
            # In-place prune: removing entries from ``dirnames`` stops os.walk
            # from ever descending into them (topdown=True is required).
            dirnames[:] = [
                d
                for d in dirnames
                if not should_prune_dir(
                    dir_path / d,
                    self.repo_path,
                    exclude_paths=self.exclude_paths,
                    unignore_paths=self.unignore_paths,
                )
            ]
            for name in filenames:
                if name in _SIDECAR_CACHE_FILENAMES:
                    continue
                filepath = dir_path / name
                try:
                    if filepath.is_file() and not should_skip_path(
                        filepath,
                        self.repo_path,
                        exclude_paths=self.exclude_paths,
                        unignore_paths=self.unignore_paths,
                    ):
                        eligible.append(filepath)
                except (UnicodeDecodeError, ValueError, OSError) as exc:
                    # Filenames with non-UTF-8 bytes produce surrogate-escaped
                    # Path objects on Linux; str() on them raises
                    # UnicodeDecodeError. OSError can occur when the file
                    # disappears mid-scan.
                    logger.warning("Skipping file with unreadable path during scan: %s", exc)
        return eligible

    def _process_files(self, force: bool = False) -> None:
        cache_root = self.cache_dir if self.cache_dir is not None else self.repo_path
        cache_path = cache_root / cs.HASH_CACHE_FILENAME
        stat_cache_path = cache_root / cs.STAT_CACHE_FILENAME
        old_hashes = _load_hash_cache(cache_path) if not force else {}
        # Stat cache is the fast pre-filter sitting in front of the SHA cache.
        # When `force=True` we deliberately skip loading it — caller wants a
        # full re-hash. See BUC-1612.
        old_stats = _load_stat_cache(stat_cache_path) if not force else {}
        if force:
            logger.info(ls.INCREMENTAL_FORCE)

        eligible_files = self._collect_eligible_files()
        # Emit discovery completion with total file count.
        self._emit_progress({"phase": "discovering", "files_total": len(eligible_files)})

        new_hashes: FileHashCache = {}
        new_stats: FileStatCache = {}
        skipped_count = 0
        changed_count = 0
        stat_hit_count = 0  # files where stat-cache let us skip SHA entirely

        current_file_keys: set[str] = set()

        processed_since_flush = 0
        _total_files = len(eligible_files)
        _files_scanned = 0  # all files seen (including skipped)
        _last_cb_time = _time.monotonic()
        _files_since_cb = 0

        for filepath in eligible_files:
            file_key = str(filepath.relative_to(self.repo_path))
            current_file_keys.add(file_key)
            _files_scanned += 1

            # Stage 1 — stat() the file. Cheap (one syscall) compared to
            # reading every byte for SHA-256.
            try:
                st = filepath.stat()
            except OSError as exc:
                # File vanished between scan and stat; treat as if it never
                # existed in this run. Will be removed from cache naturally
                # because we don't add it to new_hashes/new_stats.
                logger.warning(
                    "File disappeared before stat during scan: %s (%s)",
                    file_key,
                    exc,
                )
                current_file_keys.discard(file_key)
                continue

            mtime_ns = st.st_mtime_ns
            size = st.st_size

            cached_stat = old_stats.get(file_key) if not force else None
            current_hash: str | None = None

            if cached_stat is not None and _stat_matches(cached_stat, mtime_ns, size):
                # Fast path — stat fingerprint unchanged. Trust the cached SHA;
                # do NOT read the file. This is the whole point of the layer.
                current_hash = cached_stat.sha
                stat_hit_count += 1
            else:
                # Slow path — stat-cache miss or mismatch. Compute SHA.
                # Edge case (`git clone --depth`): mtime regresses to checkout
                # time, so the cached_stat won't match. We pay the SHA cost
                # once; the new stat entry persists and subsequent runs hit
                # the fast path again.
                try:
                    current_hash = _hash_file(filepath)
                except OSError as exc:
                    # stat() succeeded but open() failed — e.g. an unreadable
                    # file (EACCES) inside leftover container-storage overlays
                    # in the working tree. One bad file must not fail the
                    # whole index job; skip it like a vanished file.
                    logger.warning(
                        "Skipping unreadable file during scan: %s (%s)",
                        file_key,
                        exc,
                    )
                    current_file_keys.discard(file_key)
                    continue

            new_hashes[file_key] = current_hash
            new_stats[file_key] = StatEntry(mtime_ns=mtime_ns, size=size, sha=current_hash)

            if not force and file_key in old_hashes and old_hashes[file_key] == current_hash:
                logger.debug(ls.FILE_HASH_UNCHANGED, path=file_key)
                skipped_count += 1
                _files_since_cb += 1
                # Still emit progress so the bar moves even on skipped files.
                _now = _time.monotonic()
                if _files_since_cb >= 10 or (_now - _last_cb_time) >= 0.5:
                    self._emit_progress(
                        {
                            "phase": "parsing",
                            "files_done": _files_scanned,
                            "current_file": file_key,
                        }
                    )
                    _last_cb_time = _now
                    _files_since_cb = 0
                continue

            if file_key in old_hashes:
                logger.debug(ls.FILE_HASH_CHANGED, path=file_key)
                self.remove_file_from_state(filepath)
            else:
                logger.debug(ls.FILE_HASH_NEW, path=file_key)

            changed_count += 1
            try:
                self._process_single_file(filepath)
            except OSError as exc:
                # Same doctrine as the hash guard above: a file that turns
                # unreadable between hash and parse skips, not crashes. Drop
                # its cache entries so the next run retries it.
                logger.warning("Skipping unreadable file during parse: %s (%s)", file_key, exc)
                current_file_keys.discard(file_key)
                new_hashes.pop(file_key, None)
                new_stats.pop(file_key, None)
                changed_count -= 1
                continue
            _files_since_cb += 1

            _now = _time.monotonic()
            if _files_since_cb >= 10 or (_now - _last_cb_time) >= 0.5:
                self._emit_progress(
                    {
                        "phase": "parsing",
                        "files_done": _files_scanned,
                        "current_file": file_key,
                    }
                )
                _last_cb_time = _now
                _files_since_cb = 0

            processed_since_flush += 1
            if processed_since_flush >= self._config.file_flush_interval:
                logger.info(ls.PERIODIC_FLUSH.format(count=processed_since_flush))
                self.ingestor.flush_all()
                processed_since_flush = 0

        deleted_keys = set(old_hashes.keys()) - current_file_keys
        if deleted_keys:
            logger.info(ls.INCREMENTAL_DELETED, count=len(deleted_keys))
            for deleted_key in deleted_keys:
                deleted_path = self.repo_path / deleted_key
                self.remove_file_from_state(deleted_path)

        if skipped_count > 0:
            logger.info(ls.INCREMENTAL_SKIPPED, count=skipped_count)
        if changed_count > 0:
            logger.info(ls.INCREMENTAL_CHANGED, count=changed_count)
        if _total_files > 0:
            logger.info(ls.STAT_CACHE_HITS, hits=stat_hit_count, total=_total_files)

        _save_hash_cache(cache_path, new_hashes)
        _save_stat_cache(stat_cache_path, new_stats)

    def _process_single_file(self, filepath: Path) -> None:
        lang_config = get_language_spec(filepath.suffix)
        if (
            lang_config
            and isinstance(lang_config.language, cs.SupportedLanguage)
            and lang_config.language in self.parsers
        ):
            result = self.factory.definition_processor.process_file(
                filepath,
                lang_config.language,
                self.queries,
                self.factory.structure_processor.structural_elements,
            )
            if result:
                root_node, language = result
                self.ast_cache[filepath] = (root_node, language)
        elif self._is_dependency_file(filepath.name, filepath):
            self.factory.definition_processor.process_dependencies(filepath)

        self.factory.structure_processor.process_generic_file(filepath, filepath.name)

    def _process_function_calls(self) -> None:
        # BUC-1614: optionally parallelise Pass 3 (call resolution).
        # Profiling showed Pass 3 dominates Pass 2 by 35–55 % on Python
        # repos, so this is where threading buys the most wall-clock.
        # PARSE_PARALLELISM=1 keeps the legacy serial path verbatim;
        # any value >= 2 routes through the worker-pool implementation
        # in ``parsers.parallel_calls``.
        from .parsers.parallel_calls import (
            get_parse_parallelism,
            process_calls_parallel,
        )

        ast_cache_items = list(self.ast_cache.items())
        workers = get_parse_parallelism()

        if workers <= 1 or len(ast_cache_items) < 2:
            for file_path, (root_node, language) in ast_cache_items:
                self.factory.call_processor.process_calls_in_file(
                    file_path, root_node, language, self.queries
                )
            return

        logger.info(
            "Pass 3 parallel: {n} files across {w} workers (PARSE_PARALLELISM)",
            n=len(ast_cache_items),
            w=workers,
        )
        process_calls_parallel(
            factory=self.factory,
            ast_cache_items=ast_cache_items,
            queries=self.queries,
            target_ingestor=self.ingestor,
            workers=workers,
        )

    def _discover_method_rebindings(self) -> None:
        """BUC-1611: scan every Python AST for module-level monkey-patches.

        Populates ``ProcessorFactory.rebind_processor.registry`` so the
        call resolver can consult it during the subsequent CALLS pass,
        then emits one REBINDS edge per rebinding to the ingestor.

        Non-Python ASTs are skipped (the processor itself is a no-op for
        them, but we short-circuit here to avoid unnecessary iteration).
        Cross-module ordering is the AST cache's iteration order — which
        matches the file-discovery order recorded in pass 2.
        """
        rebind_processor = self.factory.rebind_processor
        for file_path, (root_node, language) in self.ast_cache.items():
            if language != cs.SupportedLanguage.PYTHON:
                continue
            rebind_processor.process_file(file_path, root_node, language)

        rebind_processor.emit_rebind_edges()
