"""Where a corpus write goes (T2).

Every host-side corpus write — ``lessons promote``, ``install-pack``,
``install-packs`` — asks :func:`decide_corpus_write_route` where to send the
work, then either writes the host corpus directly (today's path, service down)
or pushes the pack to the running service's ``/corpus/ingest-pack`` endpoint
(service up, native or container). When neither is possible it returns the
honest #391 block reason instead of writing a corpus nothing reads.

This replaces the #391 block/allow oracle (``lessons._corpus_write_blocker``,
still used here to phrase the blocked reason) with a three-way decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from agentalloy.install.server_proc import DEFAULT_HOST

RouteMode = Literal["write_host", "via_service", "blocked"]

# The pack payload is small (a handful of YAMLs); a generous read timeout covers
# the server-side reembed pass on a cold embed server.
_PUSH_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class CorpusWriteRoute:
    mode: RouteMode
    port: int | None = None
    reason: str | None = None


def decide_corpus_write_route(
    *,
    blocker_fn: Callable[[], str | None] | None = None,
    reachable_fn: Callable[[int], bool] | None = None,
) -> CorpusWriteRoute:
    """Resolve the write route from live deployment reality.

    - **Service reachable** (native *or* container) → ``via_service``: the
      running service owns the writer, so hand it the pack.
    - **Not reachable, host corpus writable** → ``write_host``: today's direct
      install (AC-8, unregressed).
    - **Not reachable, host corpus unwritable** → ``blocked`` with the #391
      reason (a foreign lock, or a configured-but-stopped container whose
      in-volume corpus a host write can't reach) (AC-6).
    """
    from agentalloy.install.server_proc import port_reachable, resolve_deployment

    target = resolve_deployment()
    reachable = (reachable_fn or port_reachable)(target.port)
    if reachable:
        return CorpusWriteRoute("via_service", port=target.port)

    # A configured-but-stopped container is NOT a host-writable situation: the
    # live corpus lives in the container's data volume, so a host write lands in
    # a different file the container never reads. Refuse honestly rather than
    # silently writing a corpus nothing serves — the only correct write here is
    # through the service, which is down.
    if target.deployment == "container":
        return CorpusWriteRoute(
            "blocked",
            reason=(
                "the AgentAlloy container isn't running — start it so the install can "
                "route through the service; a host-side write can't reach the corpus "
                "inside the container's data volume."
            ),
        )

    if blocker_fn is None:
        from agentalloy.install.subcommands.lessons import (
            _corpus_write_blocker,  # pyright: ignore[reportPrivateUsage]
        )

        blocker_fn = _corpus_write_blocker
    reason = blocker_fn()
    if reason:
        return CorpusWriteRoute("blocked", reason=reason)
    return CorpusWriteRoute("write_host")


def install_or_route(
    pack_dir: Path,
    *,
    root: Path | None = None,
    strict: bool = True,
    allow_duplicates: bool = False,
    reembed: bool = True,
    route_fn: Callable[[], CorpusWriteRoute] | None = None,
    push_fn: Callable[..., dict[str, Any]] | None = None,
    install_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Install a ready pack dir via the route the deployment dictates.

    The single chokepoint the CLI corpus-write callers share:
    ``via_service`` → push to the endpoint; ``write_host`` → today's direct
    ``install_local_pack``; ``blocked`` → an honest install_blocked result. The
    ``install_local_pack`` the endpoint runs server-side is *not* routed — this
    helper lives only in the CLI callers, so there is no recursion.
    """
    route = (route_fn or decide_corpus_write_route)()
    if route.mode == "blocked":
        return {
            "action": "install_blocked",
            "pack_dir": str(pack_dir),
            "error": f"pack NOT installed: {route.reason}",
            "remediation": (
                "once the corpus is writable (stop the service, or bring it up so the "
                "install can route through it), re-run the install."
            ),
        }
    if route.mode == "via_service":
        return (push_fn or push_pack_to_service)(
            pack_dir,
            route=route,
            allow_duplicates=allow_duplicates,
            reembed=reembed,
            strict=strict,
        )
    if install_fn is None:
        from agentalloy.install.subcommands.install_pack import install_local_pack

        install_fn = install_local_pack
    return install_fn(
        pack_dir,
        root=root,
        strict=strict,
        allow_duplicates=allow_duplicates,
        run_reembed=reembed,
    )


def _read_pack_files(pack_dir: Path) -> dict[str, str]:
    """Pack contents as ``{relative-path: text}`` for the bytes protocol.

    Text-only (pack.yaml + skill YAMLs); the corpus packs carry no binaries.
    """
    pack: dict[str, str] = {}
    for path in sorted(pack_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(pack_dir).as_posix()
        pack[rel] = path.read_text(encoding="utf-8")
    return pack


def push_pack_to_service(
    pack_dir: Path,
    *,
    route: CorpusWriteRoute,
    allow_duplicates: bool = False,
    reembed: bool = True,
    strict: bool = True,
    post_fn: Callable[..., httpx.Response] | None = None,
) -> dict[str, Any]:
    """POST a generated pack to ``/corpus/ingest-pack``; return the result dict.

    The CLI mints the ingest secret if absent (the host is source of truth) and
    sends it as the guard header. HTTP/transport failures map to an
    ``install_failed`` result so the caller renders it through the existing
    result-surface (AC-10) — a raw HTTP error never reaches the user.
    """
    from agentalloy.install.ingest_secret import resolve_ingest_secret

    secret = resolve_ingest_secret(mint=True)
    port = route.port
    url = f"http://{DEFAULT_HOST}:{port}/corpus/ingest-pack"
    payload = {
        "pack": _read_pack_files(pack_dir),
        "allow_duplicates": allow_duplicates,
        "reembed": reembed,
        "strict": strict,
    }
    headers = {"X-AgentAlloy-Ingest-Token": secret or ""}
    post = post_fn or httpx.post
    try:
        resp = post(url, json=payload, headers=headers, timeout=_PUSH_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return {
            "action": "install_failed",
            "error": f"could not reach the ingest service at {url}: {exc}",
        }
    if resp.status_code != 200:
        detail = resp.text[:300] if hasattr(resp, "text") else ""
        return {
            "action": "install_failed",
            "error": f"ingest service returned HTTP {resp.status_code}: {detail}",
        }
    return resp.json()
