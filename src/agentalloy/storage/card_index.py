"""Stage 0 — skill-card indexing helpers (deterministic document expansion).

Both retrieval legs index only fragment *body* text: ``frag.content`` is what
gets embedded into DuckDB and what the BM25 index searches. A skill's
``canonical_name``, ``domain_tags`` and self-``description`` never reach the
index, so the corpus can know "React is for websites" while retrieval is never
told. This module builds the two deterministic document-expansion shapes the
re-embed pass can apply (controlled by ``--card-index``):

- **prefix** — a one-line header prepended to each fragment's *indexed*
  representation (the embedded text and the BM25 ``prose`` column). The stored
  fragment ``content`` returned by ``/compose`` is never touched.
- **cards** — one synthetic "card" document per skill (name + tags +
  description) added to both legs. Cards influence fusion *skill ranking* only;
  they are excluded from ``/compose`` assembly by ``retrieval.domain`` because
  no skill-store fragment row hydrates them.

``off`` applies neither and must reproduce the pre-Stage-0 index byte-for-byte.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

# fragment_type stamped on synthetic card rows. Distinct from the authored
# 6-type taxonomy so cards are trivially identifiable (and excludable) in both
# the DuckDB rows and the fused candidate list.
CARD_FRAGMENT_TYPE = "card"

# fragment_id namespace for cards. ``card::<skill_id>`` round-trips to the
# skill so a card hit in fusion can boost its skill's rank.
CARD_ID_PREFIX = "card::"

# corpus_meta key recording which mode the corpus index was built with.
META_KEY_CARD_INDEX = "card_index"

# corpus_meta key recording the corpus schema version. Stamped on every embed
# pass so `update`/`seed-corpus` read an explicit marker instead of assuming
# "implicit v1".
META_KEY_SCHEMA_VERSION = "schema_version"

# Current corpus schema version, stamped into corpus_meta at build time. Bump
# this when a corpus migration lands (and add the matching entry to
# install/subcommands/update.py MIGRATIONS).
CORPUS_SCHEMA_VERSION = 1


class CardIndexMode(StrEnum):
    """``--card-index`` values. ``OFF`` is the regression-guaranteed default."""

    OFF = "off"
    PREFIX = "prefix"
    CARDS = "cards"
    BOTH = "both"

    @property
    def with_prefix(self) -> bool:
        return self in (CardIndexMode.PREFIX, CardIndexMode.BOTH)

    @property
    def with_cards(self) -> bool:
        return self in (CardIndexMode.CARDS, CardIndexMode.BOTH)


def card_fragment_id(skill_id: str) -> str:
    """The synthetic card fragment_id for a skill."""
    return f"{CARD_ID_PREFIX}{skill_id}"


def is_card_id(fragment_id: str) -> bool:
    return fragment_id.startswith(CARD_ID_PREFIX)


def skill_id_from_card_id(fragment_id: str) -> str:
    """Recover the skill_id encoded in a card fragment_id."""
    return fragment_id[len(CARD_ID_PREFIX) :]


def _tags_str(domain_tags: Iterable[str] | None) -> str:
    return ", ".join(t for t in (domain_tags or []) if t)


def build_card_text(
    canonical_name: str,
    domain_tags: Iterable[str] | None,
    description: str | None,
) -> str:
    """One-line skill card: ``skill: <name> — tags: <tags> — <description>``.

    Used both as the header prepended in ``prefix`` mode and as the body of the
    synthetic document in ``cards`` mode. Omits empty segments so a tagless or
    description-less skill still yields a clean line. Deterministic — identical
    inputs always produce identical output (no ordering or formatting drift).
    """
    parts = [f"skill: {canonical_name.strip()}"]
    tags = _tags_str(domain_tags)
    if tags:
        parts.append(f"tags: {tags}")
    desc = (description or "").strip()
    if desc:
        parts.append(desc)
    return " — ".join(parts)


def apply_prefix(card_text: str, content: str) -> str:
    """Prepend ``card_text`` as a header line to a fragment's indexed text."""
    return f"{card_text}\n\n{content}"
