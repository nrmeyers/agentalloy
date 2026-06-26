"""``agentalloy add <harness>`` — adopt a harness's upstream and wire it.

The Tier-1 one-shot: read the harness's own config to recover the upstream LLM
it already points at, record it at ``<repo>/.agentalloy/upstream`` so the proxy
forwards there transparently, then wire the harness to route through the proxy.
No setup wizard, no re-declaring the upstream.

Upstream adoption is per-repo: the proxy decodes the request's ``/proj/<token>``
back to this repo and reads ``.agentalloy/upstream``. So ``add`` always wires the
harness at *repo* scope.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

import yaml

from agentalloy.api.proxy_context import UPSTREAM_FILE, Upstream
from agentalloy.install import state as install_state
from agentalloy.providers import REGISTRY


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Register the ``add`` subcommand."""
    p = subparsers.add_parser(
        "add",
        help="Adopt a harness's upstream and wire it through the proxy (per repo).",
    )
    p.add_argument("harness", help="Harness to add (e.g. hermes-agent).")
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the service port (default: read from user state, fallback 47950).",
    )
    p.add_argument(
        "--upstream-url",
        default=None,
        help="Override the captured upstream base URL (e.g. http://host:8080/v1).",
    )
    p.add_argument(
        "--upstream-model",
        default=None,
        help="Override the captured upstream model name.",
    )
    p.add_argument(
        "--key-env",
        default=None,
        help="Name of the env var holding the upstream API key (a reference, not the secret).",
    )
    p.set_defaults(func=_run)


def capture_upstream(
    harness: str,
    root: Path,
    *,
    upstream_url: str | None = None,
    upstream_model: str | None = None,
    key_env: str | None = None,
) -> Upstream | None:
    """Adopt *harness*'s upstream into ``<root>/.agentalloy/upstream``.

    Reads the harness's own config (its ``HarnessSpec.upstream_extractor``) with
    the optional CLI overrides on top, and records ``{url, model, key_env}`` so the
    proxy forwards there for this repo. Writes nothing and returns ``None`` when no
    upstream can be determined — e.g. claude-code, whose auth-transparent Anthropic
    passthrough forwards the caller's own key and so has nothing to adopt. Shared by
    ``add`` and the deprecated ``wire`` so both are transparent interceptors.
    """
    spec = REGISTRY.get(harness)
    extractor = spec.upstream_extractor if spec else None
    captured = extractor(root) if extractor else None
    url = upstream_url or (captured.url if captured else None)
    model = upstream_model or (captured.model if captured else None)
    kenv = key_env or (captured.key_env if captured else None)
    if not url or not model:
        return None

    upstream = Upstream(url=url.rstrip("/"), model=model, key_env=kenv)
    path = root / UPSTREAM_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {"url": upstream.url, "model": upstream.model}
    if upstream.key_env:
        payload["key_env"] = upstream.key_env
    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
        path, yaml.safe_dump(payload, sort_keys=False)
    )
    return upstream


def _run(args: argparse.Namespace) -> int:
    harness: str = args.harness
    spec = REGISTRY.get(harness)
    if spec is None:
        print(f"ERROR: Unknown harness: {harness}.", file=sys.stderr)
        print(f"FIX:   Choices: {', '.join(sorted(REGISTRY))}.", file=sys.stderr)
        return 1

    cwd = Path.cwd().resolve()

    if args.port is not None:
        port = install_state.validate_port(args.port)
    else:
        st = install_state.load_state()
        port = install_state.validate_port(st.get("port", 47950))

    upstream = capture_upstream(
        harness,
        cwd,
        upstream_url=args.upstream_url,
        upstream_model=args.upstream_model,
        key_env=args.key_env,
    )
    # A harness that advertises an extractor but yielded nothing is a soft miss:
    # wire interception anyway (the proxy falls back to the global UPSTREAM), but
    # tell the user so they can pass --upstream-url. Harnesses with no extractor
    # (claude-code) intentionally adopt nothing — stay quiet.
    if upstream is None and spec.upstream_extractor is not None:
        print(
            f"WARN:  No upstream found in {harness}'s config. Wiring interception only; "
            "the proxy will fall back to the global UPSTREAM. Pass --upstream-url to adopt one.",
            file=sys.stderr,
        )

    # Wire the harness through the proxy (per-repo) and activate the repo.
    from agentalloy.install.subcommands.wire import (
        _git_exclude_agentalloy,  # pyright: ignore[reportPrivateUsage]
        _seed_entry_phase,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.install.subcommands.wire_harness import (
        _wire_harness_core,  # pyright: ignore[reportPrivateUsage]
    )

    result = _wire_harness_core(harness, port=port, root=cwd, scope="repo")
    phase_seeded = _seed_entry_phase(cwd)
    _git_exclude_agentalloy(cwd)  # ensure .agentalloy/ (upstream + phase) stays uncommitted

    _render(harness, upstream, result, phase_seeded)
    return 0


def _render(
    harness: str,
    upstream: Upstream | None,
    result: dict[str, Any],
    phase_seeded: str | None,
) -> None:
    """Human-readable summary of what ``add`` captured and wired."""
    print(f"[AgentAlloy] add {harness}")
    if upstream is not None:
        key_note = f"  key_env={upstream.key_env}" if upstream.key_env else "  (no key)"
        print(f"  upstream: {upstream.url}  model={upstream.model}{key_note}")
    else:
        print("  upstream: (none adopted — auth-transparent or global fallback)")
    touched = cast(
        "list[dict[str, Any]]",
        [*(result.get("files_written") or []), *(result.get("files_modified") or [])],
    )
    for f in touched:
        print(f"  wired: {f.get('path')}")
    if phase_seeded:
        print(f"  phase: {phase_seeded} (repo activated; composes next prompt)")
