"""Bound the retrieval query so an oversized first user turn can't overflow the
embed/rerank token ceiling.

Context ceiling is real: nomic-embed-text-v1.5 is nomic-bert-2048 (n_ctx_train=2048),
and the embed server runs ``llama-server --embeddings --pooling mean --ubatch-size 2048``.
A non-causal pooled embedding must fit the whole sequence in one ubatch, so anything
over ~2048 tokens 500s and silently falls back to BM25. The cross-encoder reranker has
its own ceiling and will choke on the same raw input.

The first user message in agentic traffic is routinely bloated with injected context
— ``<system-reminder>`` blocks, CLAUDE.md / environment dumps, fenced code, pasted
blobs — none of which carry retrieval intent. Strip that, then head-cap to a small
budget. A focused query is also a *better* dense vector than a transcript: averaging
many intents washes the signal out.

Use the result for the dense leg, the reranker, AND the LM scorer — one extraction,
shared by all three. Leave the phase-gate keyword / predicate path on the full
first-turn text: completion signals may sit past the cap, and the gate query is
already char-bounded upstream.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Injected wrappers the harness folds into user-role content. The whole element
# (tags + body) is noise for retrieval intent — this is the real 6050-token source.
_INJECTED_TAGS = (
    "system-reminder",
    "system_reminder",
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "task-notification",
)
_TAG_BLOCK = re.compile(
    r"<(" + "|".join(_INJECTED_TAGS) + r")\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_LOOSE = re.compile(  # defensive: unmatched / self-closing injected tags
    r"</?(" + "|".join(_INJECTED_TAGS) + r")\b[^>]*>",
    re.IGNORECASE,
)
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INDENTED_BLOB = re.compile(r"(?:^[ \t]{4,}\S.*\n?)+", re.MULTILINE)
_WS = re.compile(r"\s+")

# ~512 tokens. Dense retrieval queries gain nothing past this and 512 << 2048 leaves
# headroom. The char proxy is intentionally conservative; pass ``count_tokens`` (e.g.
# the embed tokenizer) for precision.
DEFAULT_QUERY_TOKEN_BUDGET = 512
_CHARS_PER_TOKEN = 4


def build_retrieval_query(
    task: str | None,
    *,
    token_budget: int = DEFAULT_QUERY_TOKEN_BUDGET,
    count_tokens: Callable[[str], int] | None = None,
) -> str:
    """Strip injected/boilerplate noise from ``task`` and head-cap to a budget.

    strip-then-cap order is load-bearing: cap first and the surviving head is often
    all boilerplate, dropping the real instruction. Returns ``""`` when nothing
    instruction-bearing survives (caller leans on BM25/keyword — acceptable).
    """
    if not task:
        return ""
    text = _TAG_BLOCK.sub(" ", task)
    text = _TAG_LOOSE.sub(" ", text)
    text = _FENCED_CODE.sub(" ", text)
    text = _INDENTED_BLOB.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    if not text:
        return ""

    if count_tokens is not None:
        if count_tokens(text) <= token_budget:
            return text
        # Longest head within budget (binary search — token counts aren't linear).
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count_tokens(text[:mid]) <= token_budget:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip()

    max_chars = token_budget * _CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars)  # prefer a word boundary near the cap
    return text[: cut if cut > max_chars // 2 else max_chars].rstrip()
