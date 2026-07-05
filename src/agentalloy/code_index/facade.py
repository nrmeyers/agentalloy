"""Typed boundary over the vendored tree-sitter engine.

This is the ONLY agentalloy module allowed to import from
``agentalloy.code_index.engine``. Everything the rest of the codebase needs
from the parser comes through :func:`parse_repo`, which returns frozen
dataclasses instead of the engine's loose property dicts.

The engine ships behind the ``[code-index]`` optional extra — importing this
module requires ``tree_sitter`` + grammar packages to be installed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .engine.constants import SupportedLanguage
from .engine.graph_updater import GraphUpdater
from .engine.parser_loader import load_parsers
from .engine.types_defs import PropertyDict, PropertyValue

# Node labels that describe code symbols (vs. structural nodes such as
# Project / Package / Folder / File / ExternalPackage, which are keyed by
# path or name and carry no symbol payload).
_SYMBOL_LABELS: frozenset[str] = frozenset(
    {"Function", "Method", "Class", "Interface", "Enum", "Type", "Union", "Module"}
)

# The engine emits four structural containment relationship names; the
# storage vocabulary collapses them into a single CONTAINS kind.
_CONTAINS_KINDS: frozenset[str] = frozenset(
    {"CONTAINS_PACKAGE", "CONTAINS_FOLDER", "CONTAINS_FILE", "CONTAINS_MODULE"}
)


@dataclass(frozen=True)
class ParsedSymbol:
    """One code symbol (function / method / class / module / ...)."""

    qualified_name: str
    kind: str
    name: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    docstring: str | None
    decorators: tuple[str, ...]
    is_exported: bool
    is_async: bool
    is_generator: bool
    source_code: str | None


@dataclass(frozen=True)
class ParsedEdge:
    """One relationship between two qualified names."""

    src: str
    dst: str
    kind: str
    file_path: str | None
    line_start: int | None
    col_start: int | None
    resolved_via: str | None
    confidence: float | None
    new_target: str | None


@dataclass(frozen=True)
class ParseResult:
    """Everything :func:`parse_repo` extracted from one repository."""

    symbols: list[ParsedSymbol]
    edges: list[ParsedEdge]


type _RawEdge = tuple[
    tuple[str, str, PropertyValue],
    str,
    tuple[str, str, PropertyValue],
    dict[str, PropertyValue],
]


class CollectingIngestor:
    """IngestorProtocol implementation that accumulates instead of writing.

    The engine batches nodes/relationships through ``ensure_*_batch`` calls
    and expects ``flush_all`` to persist them; here everything stays in
    memory and is translated to typed DTOs by :func:`parse_repo`.
    """

    def __init__(self) -> None:
        self.nodes: list[tuple[str, dict[str, PropertyValue]]] = []
        self.raw_edges: list[_RawEdge] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self.nodes.append((str(label), dict(properties)))

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.raw_edges.append(
            (
                (str(from_spec[0]), from_spec[1], from_spec[2]),
                str(rel_type),
                (str(to_spec[0]), to_spec[1], to_spec[2]),
                dict(properties) if properties else {},
            )
        )

    def flush_all(self) -> None:  # collection is already in memory
        return


def supported_languages() -> list[str]:
    """Language identifiers the installed grammar set can parse."""
    parsers, _queries = load_parsers()
    return sorted(str(lang.value) for lang in parsers)


def parse_repo(
    repo_path: Path,
    *,
    cache_dir: Path | None = None,
    languages: Sequence[str] | None = None,
) -> ParseResult:
    """Parse a repository into symbols and edges.

    ``cache_dir`` relocates the engine's hash/stat sidecar caches out of the
    indexed tree (the storage layer passes a per-slug directory). ``languages``
    restricts parsing to the given language identifiers (see
    :func:`supported_languages`); default is every installed grammar.
    """
    parsers, queries = load_parsers()

    if languages is not None:
        wanted: set[SupportedLanguage] = set()
        for name in languages:
            try:
                wanted.add(SupportedLanguage(name))
            except ValueError as exc:
                raise ValueError(f"unsupported language: {name!r}") from exc
        parsers = {lang: parser for lang, parser in parsers.items() if lang in wanted}
        queries = {lang: query for lang, query in queries.items() if lang in wanted}

    ingestor = CollectingIngestor()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
        cache_dir=cache_dir,
    )
    updater.run()

    return _translate(ingestor, repo_path.resolve() if repo_path.is_dir() else repo_path.parent)


def _as_str(value: PropertyValue) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: PropertyValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: PropertyValue) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else None


def _as_str_tuple(value: PropertyValue) -> tuple[str, ...]:
    return tuple(value) if isinstance(value, list) else ()


def _read_source(
    repo_root: Path,
    file_path: str,
    start_line: int,
    end_line: int,
    file_cache: dict[str, list[str] | None],
) -> str | None:
    if file_path not in file_cache:
        try:
            text = (repo_root / file_path).read_text(encoding="utf-8", errors="replace")
            file_cache[file_path] = text.splitlines()
        except OSError:
            file_cache[file_path] = None
    lines = file_cache[file_path]
    if lines is None or start_line < 1 or end_line < start_line or start_line > len(lines):
        return None
    return "\n".join(lines[start_line - 1 : min(end_line, len(lines))])


def _translate(ingestor: CollectingIngestor, repo_root: Path) -> ParseResult:
    # Last write wins: ensure_node_batch has MERGE semantics upstream, so a
    # re-emitted node replaces the earlier property set.
    latest: dict[tuple[str, str], dict[str, PropertyValue]] = {}
    order: list[tuple[str, str]] = []
    module_paths: dict[str, str] = {}

    for label, props in ingestor.nodes:
        qualified_name = _as_str(props.get("qualified_name"))
        if label == "Module":
            path = _as_str(props.get("path"))
            if qualified_name is not None and path is not None:
                module_paths[qualified_name] = path
        if label not in _SYMBOL_LABELS or qualified_name is None:
            continue
        key = (label, qualified_name)
        if key not in latest:
            order.append(key)
        latest[key] = props

    # Sort module qns longest-first so the first prefix hit is the deepest
    # (most specific) containing module.
    modules_by_depth = sorted(module_paths, key=len, reverse=True)

    def file_for(qualified_name: str) -> str | None:
        for module_qn in modules_by_depth:
            if qualified_name == module_qn or qualified_name.startswith(module_qn + "."):
                return module_paths[module_qn]
        return None

    file_cache: dict[str, list[str] | None] = {}
    symbols: list[ParsedSymbol] = []
    for label, qualified_name in order:
        props = latest[(label, qualified_name)]
        file_path = _as_str(props.get("path")) if label == "Module" else file_for(qualified_name)
        start_line = _as_int(props.get("start_line"))
        end_line = _as_int(props.get("end_line"))
        source_code = None
        if label != "Module" and file_path is not None and start_line and end_line:
            source_code = _read_source(repo_root, file_path, start_line, end_line, file_cache)
        symbols.append(
            ParsedSymbol(
                qualified_name=qualified_name,
                kind=label,
                name=_as_str(props.get("name")) or qualified_name.rsplit(".", 1)[-1],
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                docstring=_as_str(props.get("docstring")),
                decorators=_as_str_tuple(props.get("decorators")),
                is_exported=props.get("is_exported") is True,
                is_async=props.get("is_async") is True,
                is_generator=props.get("is_generator") is True,
                source_code=source_code,
            )
        )

    edges: list[ParsedEdge] = []
    for from_spec, rel_type, to_spec, props in ingestor.raw_edges:
        src = from_spec[2]
        dst = to_spec[2]
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        kind = "CONTAINS" if rel_type in _CONTAINS_KINDS else rel_type
        edges.append(
            ParsedEdge(
                src=src,
                dst=dst,
                kind=kind,
                file_path=_as_str(props.get("file_path")),
                line_start=_as_int(props.get("line_start")),
                col_start=_as_int(props.get("col_start")),
                resolved_via=_as_str(props.get("resolved_via")),
                confidence=_as_float(props.get("confidence")),
                new_target=_as_str(props.get("new_target")),
            )
        )

    return ParseResult(symbols=symbols, edges=edges)
