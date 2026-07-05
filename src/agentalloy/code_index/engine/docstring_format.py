"""Structured docstring formatting for embedding text.

Plan G of the Context Intelligence stack: parse Python/Java docstrings using
``docstring-parser`` and emit a normalized, high-information form. The
embedding model (CodeRankEmbed) sees clean ``Description / Args / Returns /
Raises`` sections rather than free-form text, which improves semantic recall
on symbols where docstrings exist.

The :func:`format_docstring` helper is intentionally defensive: any exception
raised by the parser falls back to the original string, so a malformed
docstring never aborts an indexing run.
"""

from __future__ import annotations

__all__ = ["format_docstring"]


def format_docstring(raw: str | None) -> str:
    """Return a structured form of ``raw`` suitable for embedding.

    The output collapses Google / NumPy / RST styles into a single shape::

        Description: <short description>
        <long description if present>
        Args:
          name: <description>
        Returns: <description>
        Raises: <description>

    Empty sections are stripped so the result stays tight. If parsing fails
    for any reason, the raw input (stripped) is returned instead of raising.

    Args:
        raw: Source docstring, possibly ``None`` or empty/whitespace-only.

    Returns:
        Structured multi-line string, or ``""`` when there is nothing to
        embed.
    """
    if raw is None:
        return ""
    stripped = raw.strip()
    if not stripped:
        return ""

    try:
        import docstring_parser

        parsed = docstring_parser.parse(stripped, style=docstring_parser.DocstringStyle.AUTO)
    except Exception:
        # Defensive: never let a bad docstring kill an indexing run.
        return stripped

    sections: list[str] = []

    short = (parsed.short_description or "").strip()
    long_desc = (parsed.long_description or "").strip()
    if short:
        sections.append(f"Description: {short}")
    if long_desc:
        sections.append(long_desc)

    arg_lines: list[str] = []
    for param in parsed.params or []:
        name = (param.arg_name or "").strip()
        desc = (param.description or "").strip().replace("\n", " ")
        if not name and not desc:
            continue
        if name and desc:
            arg_lines.append(f"  {name}: {desc}")
        elif name:
            arg_lines.append(f"  {name}:")
        else:
            arg_lines.append(f"  {desc}")
    if arg_lines:
        sections.append("Args:\n" + "\n".join(arg_lines))

    if parsed.returns is not None:
        ret_desc = (parsed.returns.description or "").strip().replace("\n", " ")
        if ret_desc:
            sections.append(f"Returns: {ret_desc}")

    raise_lines: list[str] = []
    for exc in parsed.raises or []:
        type_name = (exc.type_name or "").strip()
        desc = (exc.description or "").strip().replace("\n", " ")
        if not type_name and not desc:
            continue
        if type_name and desc:
            raise_lines.append(f"  {type_name}: {desc}")
        elif type_name:
            raise_lines.append(f"  {type_name}")
        else:
            raise_lines.append(f"  {desc}")
    if raise_lines:
        sections.append("Raises:\n" + "\n".join(raise_lines))

    if not sections:
        # Parser succeeded but extracted nothing structured — keep raw text
        # rather than dropping potentially useful signal.
        return stripped

    return "\n".join(sections)
