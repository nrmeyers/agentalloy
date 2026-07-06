"""Shape tests for the facade DTOs and language surface."""

import dataclasses

import pytest

from agentalloy.code_index.facade import (
    CollectingIngestor,
    ParsedEdge,
    ParsedSymbol,
    ParseResult,
    supported_languages,
)


def test_supported_languages_includes_python() -> None:
    langs = supported_languages()
    assert langs, "at least one grammar must be installed"
    assert "python" in langs
    assert "typescript" in langs
    assert langs == sorted(langs)


def test_parsed_symbol_fields() -> None:
    names = [f.name for f in dataclasses.fields(ParsedSymbol)]
    assert names == [
        "qualified_name",
        "kind",
        "name",
        "file_path",
        "start_line",
        "end_line",
        "docstring",
        "decorators",
        "is_exported",
        "is_async",
        "is_generator",
        "source_code",
    ]


def test_parsed_edge_fields() -> None:
    names = [f.name for f in dataclasses.fields(ParsedEdge)]
    assert names == [
        "src",
        "dst",
        "kind",
        "file_path",
        "line_start",
        "col_start",
        "resolved_via",
        "confidence",
        "new_target",
    ]


def test_dataclasses_are_frozen() -> None:
    symbol = ParsedSymbol(
        qualified_name="p.m.f",
        kind="Function",
        name="f",
        file_path="m.py",
        start_line=1,
        end_line=2,
        docstring=None,
        decorators=(),
        is_exported=False,
        is_async=False,
        is_generator=False,
        source_code=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        symbol.name = "g"  # type: ignore[misc]


def test_collecting_ingestor_satisfies_engine_protocol() -> None:
    from agentalloy.code_index.engine.services import IngestorProtocol

    assert isinstance(CollectingIngestor(), IngestorProtocol)


def test_parse_result_shape() -> None:
    result = ParseResult(symbols=[], edges=[])
    assert result.symbols == []
    assert result.edges == []


def test_symbol_collisions_resolve_deterministically() -> None:
    """FQN collisions must produce the same winner regardless of emission order.

    Found live: nested closures collapsing to one qualified name re-embedded
    ~15 symbols on every incremental index of an unchanged tree because the
    last-write winner depended on parse order.
    """
    from agentalloy.code_index.facade import ParsedSymbol, _dedupe_symbols

    def sym(src: str, line: int) -> ParsedSymbol:
        return ParsedSymbol(
            qualified_name="pkg.mod.hook.mutationFn",
            kind="Function",
            name="mutationFn",
            file_path="pkg/mod.ts",
            start_line=line,
            end_line=line + 3,
            docstring=None,
            decorators=(),
            is_exported=False,
            is_async=False,
            is_generator=False,
            source_code=src,
        )

    a, b = sym("() => alpha()", 10), sym("() => beta()", 40)
    forward = _dedupe_symbols([a, b])
    backward = _dedupe_symbols([b, a])
    assert len(forward) == len(backward) == 1
    assert forward[0] == backward[0], "winner must not depend on emission order"
