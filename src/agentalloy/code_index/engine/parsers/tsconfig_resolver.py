"""TypeScript ``tsconfig.json`` path alias resolver.

Resolves bare-specifier imports that depend on a project's
``compilerOptions.paths`` configuration -- for example
``"@/components/Button"`` -> ``<repo>/src/components/Button.ts``.

Only the standard library is used (jsonc tolerance is implemented locally with
``re``). The parser tolerates ``//`` line comments, ``/* ... */`` block
comments, and trailing commas, all of which are common in ``tsconfig.json``
files. ``extends`` chains are followed so that child configs inherit
``baseUrl`` and ``paths`` from their parents, with TypeScript's
override-or-inherit semantics.

Public surface (kept narrow on purpose):

* :class:`TsconfigResolver` -- repo-scoped resolver. Caches each parsed
  ``tsconfig.json`` so repeated ``resolve_alias`` calls do not re-read disk.
* :meth:`TsconfigResolver.resolve_alias` -- given an import specifier and the
  source file performing the import, return the resolved on-disk
  :class:`~pathlib.Path` (with a real extension when probing succeeds) or
  ``None`` when no alias matches.

A return of ``None`` means *no opinion*; the caller should fall back to the
existing relative / ``node_modules`` resolution path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .._logging import logger

__all__ = ["TsconfigResolver", "parse_jsonc", "strip_jsonc_comments"]


# Match ``//`` line comments and ``/* ... */`` block comments while ignoring
# anything that appears inside a JSON string literal. The string-literal arm of
# the alternation is captured so that ``re.sub`` can put it back unchanged.
_COMMENT_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'  # double-quoted string (with escapes) -- preserved
    r"|//[^\n]*"  # line comment -- stripped
    r"|/\*.*?\*/",  # block comment -- stripped
    re.DOTALL,
)

# Match trailing commas before ``}`` or ``]`` -- legal in jsonc, illegal in
# strict json. Applied after comment stripping.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def strip_jsonc_comments(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments and trailing commas from a jsonc blob.

    String literals are preserved verbatim so that ``//`` characters embedded
    inside strings (for example URLs) are not mangled.
    """

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        # Strings are preserved verbatim -- they start with a double quote.
        if token.startswith('"'):
            return token
        return ""

    stripped = _COMMENT_RE.sub(_replace, text)
    return _TRAILING_COMMA_RE.sub(r"\1", stripped)


def parse_jsonc(text: str) -> dict:
    """Parse a jsonc string into a Python ``dict``.

    Raises ``json.JSONDecodeError`` on malformed input after comment stripping
    or when the top-level value is not an object.
    """

    cleaned = strip_jsonc_comments(text)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        # tsconfig.json must be an object at the top level.
        raise json.JSONDecodeError("tsconfig.json root must be an object", cleaned, 0)
    return parsed


# Recognised extensions for resolved files, in priority order. ``.d.ts`` is
# included last so that a real source file always wins over an ambient
# declaration.
_RESOLUTION_EXTENSIONS: tuple[str, ...] = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mts",
    ".cts",
    ".mjs",
    ".cjs",
    ".d.ts",
)

# File names treated as the entry point of a directory import.
_INDEX_BASENAMES: tuple[str, ...] = (
    "index.ts",
    "index.tsx",
    "index.js",
    "index.jsx",
    "index.mts",
    "index.cts",
    "index.mjs",
    "index.cjs",
)


class _ParsedTsconfig:
    """Materialised view of a single ``tsconfig.json`` after ``extends`` is
    flattened.

    ``base_dir`` is the absolute directory the alias targets are resolved
    against. ``patterns`` maps an alias pattern (e.g. ``"@/*"``) to the list of
    target templates declared for it (e.g. ``["src/*"]``). An empty mapping is
    a no-op.
    """

    __slots__ = ("base_dir", "config_path", "patterns")

    def __init__(
        self,
        config_path: Path,
        base_dir: Path,
        patterns: dict[str, list[str]],
    ) -> None:
        self.config_path = config_path
        self.base_dir = base_dir
        self.patterns = patterns


class TsconfigResolver:
    """Resolve TypeScript path aliases for files inside a single repository.

    Instances are cheap to construct; expensive work (reading and parsing
    ``tsconfig.json`` files) happens lazily on first ``resolve_alias`` and is
    cached for the life of the resolver. Construct one resolver per repo and
    share it across all import-resolution calls.
    """

    __slots__ = ("_config_cache", "_nearest_cache", "repo_path")

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path.resolve()
        # tsconfig path -> flattened parsed view (None if unreadable / invalid).
        self._config_cache: dict[Path, _ParsedTsconfig | None] = {}
        # source-file-dir -> nearest tsconfig path (or None when none exists).
        self._nearest_cache: dict[Path, Path | None] = {}

    # ------------------------------------------------------------------
    # Public API

    def resolve_alias(self, specifier: str, source_file: Path) -> Path | None:
        """Resolve ``specifier`` imported from ``source_file`` via tsconfig paths.

        Returns the resolved absolute :class:`~pathlib.Path` (with the real
        on-disk extension appended by probing) or ``None`` if no alias pattern
        matched or none of the matched targets pointed at an existing file.
        """

        if not specifier or _looks_relative(specifier):
            # Relative specifiers are handled by the existing resolver -- never
            # consult tsconfig paths for them.
            return None

        try:
            source_file = source_file.resolve()
        except OSError:
            return None

        config_path = self._find_nearest_tsconfig(source_file)
        if config_path is None:
            return None

        parsed = self._load_config(config_path)
        if parsed is None or not parsed.patterns:
            return None

        # Longest-prefix-wins ordering: TypeScript picks the most specific
        # pattern when multiple match. ``len(pattern)`` is a stable proxy
        # because patterns share at most one ``*``.
        ordered = sorted(parsed.patterns.items(), key=lambda kv: len(kv[0]), reverse=True)
        for pattern, targets in ordered:
            substitution = _match_pattern(pattern, specifier)
            if substitution is None:
                continue
            for target in targets:
                candidate = _apply_target(target, substitution)
                resolved = (parsed.base_dir / candidate).resolve()
                probed = _probe_path_for_file(resolved)
                if probed is not None:
                    logger.debug(
                        "tsconfig: resolved alias {specifier!r} -> {path}",
                        specifier=specifier,
                        path=probed,
                    )
                    return probed

        logger.debug(
            "tsconfig: no alias match for {specifier!r} (source={source})",
            specifier=specifier,
            source=source_file,
        )
        return None

    # ------------------------------------------------------------------
    # Internals -- exposed as plain methods (no name-mangling) for testability.

    def _find_nearest_tsconfig(self, source_file: Path) -> Path | None:
        """Walk up from ``source_file`` looking for a ``tsconfig.json``.

        Stops at ``self.repo_path``. Result is cached per starting directory.
        """

        start_dir = source_file.parent if source_file.is_file() else source_file
        cached = self._nearest_cache.get(start_dir)
        if cached is not None or start_dir in self._nearest_cache:
            return cached

        current = start_dir
        result: Path | None = None
        repo = self.repo_path
        while True:
            candidate = current / "tsconfig.json"
            if candidate.is_file():
                result = candidate.resolve()
                break
            if current in (repo, current.parent):
                break
            try:
                current.relative_to(repo)
            except ValueError:
                # Walked above the repo root -- stop.
                break
            current = current.parent

        self._nearest_cache[start_dir] = result
        return result

    def _load_config(self, config_path: Path) -> _ParsedTsconfig | None:
        if config_path in self._config_cache:
            return self._config_cache[config_path]

        parsed = self._read_and_flatten(config_path, seen=set())
        self._config_cache[config_path] = parsed
        return parsed

    def _read_and_flatten(self, config_path: Path, seen: set[Path]) -> _ParsedTsconfig | None:
        """Read ``config_path``, follow ``extends``, and produce a flattened view.

        ``seen`` is used to break circular ``extends`` chains. The returned
        ``patterns`` map is the child's paths if the child declares any; if it
        does not, the nearest parent's paths are inherited. ``baseUrl`` follows
        the same override-or-inherit rule and is resolved against the directory
        of whichever tsconfig actually declared it.
        """

        try:
            resolved = config_path.resolve()
        except OSError:
            return None
        if resolved in seen:
            logger.debug("tsconfig: cycle detected at {path}", path=resolved)
            return None
        seen.add(resolved)

        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("tsconfig: failed to read {path}: {err}", path=resolved, err=exc)
            return None

        try:
            raw = parse_jsonc(text)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("tsconfig: malformed JSON at {path}: {err}", path=resolved, err=exc)
            return None

        parent: _ParsedTsconfig | None = None
        extends_field = raw.get("extends")
        if isinstance(extends_field, str) and extends_field:
            parent_path = _resolve_extends(resolved.parent, extends_field)
            if parent_path is not None and parent_path.is_file():
                parent = self._read_and_flatten(parent_path, seen)

        compiler_options = raw.get("compilerOptions")
        if not isinstance(compiler_options, dict):
            compiler_options = {}

        # ``baseUrl`` is resolved against the directory of *this* tsconfig --
        # even if it was declared in a parent, the spec says the value is
        # relative to the config that introduced it.
        own_base_url = compiler_options.get("baseUrl")
        if isinstance(own_base_url, str) and own_base_url:
            base_dir = (resolved.parent / own_base_url).resolve()
        elif parent is not None:
            base_dir = parent.base_dir
        else:
            base_dir = resolved.parent

        # ``paths`` override semantics: a child's ``paths`` block (even an
        # empty one) replaces the parent's. Only fall back to the parent when
        # the child does not declare the key at all.
        own_paths = compiler_options.get("paths")
        if isinstance(own_paths, dict):
            patterns = _normalise_paths(own_paths)
        elif parent is not None:
            patterns = parent.patterns
        else:
            patterns = {}

        return _ParsedTsconfig(resolved, base_dir, patterns)


# ----------------------------------------------------------------------
# Helpers (module-private but stable for unit tests).


def _looks_relative(specifier: str) -> bool:
    return specifier.startswith(("./", "../")) or specifier in {".", ".."}


def _normalise_paths(paths: dict) -> dict[str, list[str]]:
    """Filter the raw ``paths`` mapping down to well-formed entries."""

    normalised: dict[str, list[str]] = {}
    for pattern, targets in paths.items():
        if not isinstance(pattern, str) or not isinstance(targets, list):
            continue
        cleaned = [t for t in targets if isinstance(t, str) and t]
        if cleaned:
            normalised[pattern] = cleaned
    return normalised


def _resolve_extends(config_dir: Path, extends: str) -> Path | None:
    """Resolve an ``extends`` value to a tsconfig path.

    Supports two of the three TypeScript-accepted forms:

    * Relative (``./base`` or ``../base``) -- resolved against ``config_dir``;
      ``.json`` is appended when missing.
    * Path-like absolute (``/abs/path``) -- used as-is.

    The third form -- bare module specifiers like ``"@tsconfig/strictest"`` --
    requires ``node_modules`` resolution and is intentionally out of scope.
    Such ``extends`` values are ignored (return ``None``).
    """

    if not extends:
        return None
    if extends.startswith("/"):
        candidate = Path(extends)
    elif _looks_relative(extends):
        candidate = config_dir / extends
    else:
        # Bare specifier -- would require node_modules walk. Skip.
        return None
    if candidate.suffix != ".json":
        candidate = candidate.with_suffix(".json")
    return candidate


def _match_pattern(pattern: str, specifier: str) -> str | None:
    """Return the wildcard substitution if ``specifier`` matches ``pattern``.

    For wildcard patterns (containing exactly one ``*``), returns the captured
    fragment. For exact patterns, returns ``""`` on match. Returns ``None`` on
    no match. Patterns with multiple ``*`` are skipped -- they are not standard
    TypeScript syntax.
    """

    star_count = pattern.count("*")
    if star_count == 0:
        return "" if specifier == pattern else None
    if star_count != 1:
        return None
    prefix, suffix = pattern.split("*", 1)
    if not specifier.startswith(prefix) or not specifier.endswith(suffix):
        return None
    end = len(specifier) - len(suffix) if suffix else len(specifier)
    return specifier[len(prefix) : end]


def _apply_target(target: str, substitution: str) -> str:
    """Substitute the captured wildcard fragment into a target template."""

    if "*" in target:
        return target.replace("*", substitution, 1)
    return target


def _probe_path_for_file(candidate: Path) -> Path | None:
    """Resolve ``candidate`` (a path with no extension yet) to an on-disk file.

    Tries, in order:

    1. ``candidate`` exactly (if it already points at a file).
    2. ``candidate`` with each recognised TypeScript / JavaScript extension.
    3. ``candidate/index.<ext>`` for each recognised index file name.

    Returns the matched :class:`~pathlib.Path` or ``None``.
    """

    if candidate.is_file():
        return candidate
    for ext in _RESOLUTION_EXTENSIONS:
        with_ext = candidate.parent / (candidate.name + ext)
        if with_ext.is_file():
            return with_ext
    if candidate.is_dir():
        for basename in _INDEX_BASENAMES:
            index_path = candidate / basename
            if index_path.is_file():
                return index_path
    return None
