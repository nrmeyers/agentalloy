"""Shared sentinel-bounded block helpers.

Provides ``replace_marked_block()`` for injecting/replacing sentinel-bounded
blocks in existing file content, and ``remove_sentinel_block()`` for
removing such blocks.  All provider install modules and uninstall
subcommands delegate to these shared helpers so that validation and
line-ending logic lives in exactly one place.
"""

from __future__ import annotations


def detect_line_ending(content: str) -> str:
    """Detect whether content uses CRLF or LF.

    Returns ``\\r\\n`` if the content contains CRLF sequences, otherwise
    ``\\n``.
    """
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def replace_marked_block(
    existing: str,
    block: str,
    sentinel_begin: str,
    sentinel_end: str,
) -> str:
    """Insert or replace a sentinel-bounded block in existing content.

    If both *sentinel_begin* and *sentinel_end* markers already exist in
    *existing*, the content between them is replaced with *block* wrapped
    in the sentinels.  If the markers are absent, the full block is
    appended at the end of the content.

    Validation:

    - Raises ``ValueError`` if *sentinel_end* appears before
      *sentinel_begin* in the content (inverted order would corrupt the
      file).
    - Raises ``ValueError`` if either marker appears more than once
      (duplicate pairs are ambiguous).

    Args:
        existing: The original file content.
        block: The inner content to place between the sentinels (without
            the sentinel markers themselves).
        sentinel_begin: The begin marker string.
        sentinel_end: The end marker string.

    Returns:
        The modified content with the sentinel-bounded block.
    """
    nl = detect_line_ending(existing) if existing else "\n"

    full_block = f"{sentinel_begin}{nl}{block}{nl}{sentinel_end}"

    begin_count = existing.count(sentinel_begin)
    end_count = existing.count(sentinel_end)
    if begin_count > 1 or end_count > 1:
        raise ValueError(
            f"target file contains {begin_count} BEGIN and {end_count} END "
            f"agentalloy sentinels (expected at most 1 of each). Refusing to write."
        )

    if sentinel_begin in existing and sentinel_end in existing:
        begin_idx = existing.index(sentinel_begin)
        end_idx = existing.index(sentinel_end) + len(sentinel_end)

        # Validate order: END must not appear before BEGIN
        end_marker_start = existing.index(sentinel_end)
        if end_marker_start < begin_idx:
            raise ValueError(
                "sentinel END marker appears before BEGIN marker in target file. Refusing to write."
            )

        # Consume trailing newline if present
        if end_idx < len(existing) and existing[end_idx] in ("\n", "\r"):
            if existing[end_idx : end_idx + 2] == "\r\n":
                end_idx += 2
            else:
                end_idx += 1
        return existing[:begin_idx] + full_block + nl + existing[end_idx:]

    # Append at end
    if existing and not existing.endswith(nl):
        existing += nl
    if existing:
        existing += nl  # blank line separator
    return existing + full_block + nl


def remove_sentinel_block(
    content: str,
    sentinel_begin: str,
    sentinel_end: str,
) -> str:
    """Remove a sentinel-bounded block from content.

    Handles both raw HTML-style sentinels (``<!-- BEGIN ... -->``) and
    commented-out variants (``# <!-- BEGIN ... -->``) used by YAML/shell
    files.  Operates on whole lines so leading ``#`` fragments are not
    left behind.

    Raises ``ValueError`` if *sentinel_end* appears before
    *sentinel_begin* in the content.

    Returns the original content unchanged if no sentinels are found.
    """
    lines = content.split("\n")
    result: list[str] = []
    skip = False
    found_sentinel = False

    sentinel_begin_raw = sentinel_begin
    sentinel_end_raw = sentinel_end
    # A commented marker ("# " + raw) is a superstring of the raw marker, so the
    # raw substring test already matches both raw and commented lines.

    i = 0
    while i < len(lines):
        line = lines[i]
        # Check for begin sentinel (raw or commented)
        if sentinel_begin_raw in line:
            skip = True
            found_sentinel = True
            i += 1
            continue
        # Check for end sentinel (raw or commented)
        if skip and sentinel_end_raw in line:
            skip = False
            i += 1
            # Skip trailing blank line after end sentinel
            if i < len(lines) and lines[i].strip() == "":
                i += 1
            continue
        if not skip:
            result.append(line)
        i += 1

    # Only clean up blank lines if we actually removed a sentinel block
    if not found_sentinel:
        return content

    cleaned: list[str] = []
    blank_count = 0
    for line in result:
        if line.strip() == "":
            blank_count += 1
            if blank_count < 3:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)

    return "\n".join(cleaned)


def remove_sentinel_block_simple(
    text: str,
    begin: str,
    end: str,
) -> str:
    """Remove a sentinel block (inclusive) from text -- simple variant.

    This is a simpler version of ``remove_sentinel_block`` that does not
    handle commented variants.  Used by uninstall.py for its own
    sentinel-bounded removal logic.

    Raises ``ValueError`` if *end* appears before *begin* in *text*.
    """
    if begin not in text or end not in text:
        return text

    b = text.index(begin)
    e = text.index(end) + len(end)

    # Validate order: end must not appear before begin
    end_marker_start = text.index(end)
    if end_marker_start < b:
        raise ValueError(
            "sentinel END marker appears before BEGIN marker in target file. Refusing to remove."
        )

    # Consume trailing newline
    if e < len(text) and text[e] == "\n":
        e += 1
    elif e + 1 < len(text) and text[e : e + 2] == "\r\n":
        e += 2
    # Consume blank line before block if present
    if b > 0 and text[b - 1] == "\n":
        b -= 1
        if b > 0 and text[b - 1] == "\n":
            b -= 1
    result = text[:b] + text[e:]
    # Clean up double blank lines
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result
