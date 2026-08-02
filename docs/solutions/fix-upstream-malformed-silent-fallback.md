# Solution: Refuse malformed `.agentalloy/upstream` instead of silent fallback

## Problem

`read_upstream` returned `Upstream | None`, collapsing "no file" (OK, fall back to global) and "file is broken" (bad, should refuse) into the same `None` path. A malformed per-repo upstream silently routed prompts to the global upstream — a privacy issue where users expecting local/self-hosted routing could accidentally hit `api.openai.com`.

## Approach that worked

**Discriminated result class.** Replace `Upstream | None` with `UpstreamFile(kind="absent"|"valid"|"error")`. Python dataclass instances are discriminable by their `.kind` field, making it impossible for callers to accidentally miss the error case (unlike a bool flag on `None`).

```python
@dataclass(frozen=True)
class UpstreamFile:
    kind: Literal["absent", "valid", "error"]
    upstream: Upstream | None = None
    detail: str | None = None
```

Each caller then explicitly handles the three states:
- Proxy handler: error → 503, absent → global fallback, valid → per-repo.
- Ops API: error → populate `upstream_error` field, absent/valid → url/model or null.

## What didn't work

**Raising exceptions.** The original design rationale was "a per-repo override must never take down the proxy." We kept that invariant — `read_upstream` never raises, even on malformed files. The error is surfaced to callers who decide whether to 503 or report.

**Two separate functions.** `read_upstream_or_raise()` would duplicate the file-reading logic. One function with a discriminated return is cleaner and keeps error handling in one place.

## Key decisions

1. **`kind` field over inheritance.** `isinstance` checks on dataclass variants work, but `.kind` is simpler for `if/elif` chains and requires no inheritance hierarchy.
2. **Error propagation through `_resolve_upstream`.** It returns `tuple | UpstreamFile | None` — the handler checks `isinstance(resolved, UpstreamFile)` first. This keeps the change localized to the handler.
3. **`detail` includes path and specific failure.** Makes the 503 message actionable: user knows exactly what's wrong and where.
