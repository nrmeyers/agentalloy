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
    from agentalloy.signals.skill_loader import LIFECYCLE_MODES

    p.add_argument(
        "--lifecycle-mode",
        choices=LIFECYCLE_MODES,
        default=None,
        help=(
            "How AgentAlloy behaves in this repo. 'full' (default): intake + "
            "phase lifecycle. 'off': wire but inject nothing. When omitted and "
            "the repo already defines its own agents/commands, you're prompted "
            "(TTY only); non-interactive runs default to 'full'."
        ),
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


def resolve_port(port_override: int | None) -> int:
    """Resolve the service port: explicit override, else user state, else 47950."""
    if port_override is not None:
        return install_state.validate_port(port_override)
    st = install_state.load_state()
    return install_state.validate_port(st.get("port", 47950))


def adopt_and_wire(
    harness: str,
    root: Path,
    *,
    port: int,
    upstream_url: str | None = None,
    upstream_model: str | None = None,
    key_env: str | None = None,
    lifecycle_mode: str | None = None,
) -> tuple[Upstream | None, dict[str, Any], str | None]:
    """Adopt *harness*'s upstream and wire interception at *root* (repo scope).

    The reusable core shared by ``add`` (root = cwd) and ``worktree`` (root = a
    freshly created worktree): capture upstream → wire the harness through the
    proxy → record the lifecycle mode → seed the entry phase (``full`` only) →
    git-exclude ``.agentalloy/``. Returns ``(upstream, wire_result,
    phase_seeded)`` for the caller to render. Callers are responsible for
    validating *harness* against ``REGISTRY`` first.

    ``lifecycle_mode`` follows ``wire``'s precedence: an explicit value wins;
    ``None`` prompts when the repo defines its own agent workflow (TTY only)
    and otherwise defaults to ``full``.
    """
    spec = REGISTRY.get(harness)
    upstream = capture_upstream(
        harness,
        root,
        upstream_url=upstream_url,
        upstream_model=upstream_model,
        key_env=key_env,
    )
    # A harness that advertises an extractor but yielded nothing is a soft miss:
    # wire interception anyway (the proxy falls back to the global UPSTREAM), but
    # tell the user so they can pass --upstream-url. Harnesses with no extractor
    # (claude-code) intentionally adopt nothing — stay quiet.
    if upstream is None and spec is not None and spec.upstream_extractor is not None:
        print(
            f"WARN:  No upstream found in {harness}'s config. Wiring interception only; "
            "the proxy will fall back to the global UPSTREAM. Pass --upstream-url to adopt one.",
            file=sys.stderr,
        )

    # Wire the harness through the proxy (per-repo) and activate the repo.
    from agentalloy.install.subcommands.wire import (
        _detect_custom_workflow,  # pyright: ignore[reportPrivateUsage]
        _git_exclude_agentalloy,  # pyright: ignore[reportPrivateUsage]
        _prompt_lifecycle_mode,  # pyright: ignore[reportPrivateUsage]
        _seed_entry_phase,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.install.subcommands.wire_harness import (
        _wire_harness_core,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.skill_loader import (
        _write_lifecycle_mode,  # pyright: ignore[reportPrivateUsage]
    )

    # Lifecycle mode is repo-global (one workflow machine per repo, however
    # many harnesses point at the proxy). Same precedence as `wire`.
    mode = lifecycle_mode
    if mode is None:
        detected = _detect_custom_workflow(root)
        mode = _prompt_lifecycle_mode(detected) if detected and sys.stdin.isatty() else "full"
    _write_lifecycle_mode(root, mode)

    result = _wire_harness_core(harness, port=port, root=root, scope="repo")
    result["lifecycle_mode"] = mode
    if mode == "full":
        phase_seeded = _seed_entry_phase(root)
    else:
        # off must NOT seed a phase (a seeded `intake` re-arms the front door),
        # and a stale phase from a prior `full` wiring would silently suppress
        # composition while looking active — clear it.
        phase_seeded = None
        phase_file = root / ".agentalloy" / "phase"
        if phase_file.exists():
            phase_file.unlink()
            result["stale_phase_cleared"] = True
    _git_exclude_agentalloy(root)  # ensure .agentalloy/ (upstream + phase) stays uncommitted

    # Code-index harness block (second sentinel pair) — written only when the
    # service reports the module enabled; cleans up stale/legacy blocks otherwise.
    from agentalloy.install import code_index_wiring

    code_index_wiring.maybe_wire(root, port)
    return upstream, result, phase_seeded


def _run(args: argparse.Namespace) -> int:
    harness: str = args.harness
    if REGISTRY.get(harness) is None:
        print(f"ERROR: Unknown harness: {harness}.", file=sys.stderr)
        print(f"FIX:   Choices: {', '.join(sorted(REGISTRY))}.", file=sys.stderr)
        return 1

    cwd = Path.cwd().resolve()
    port = resolve_port(args.port)

    upstream, result, phase_seeded = adopt_and_wire(
        harness,
        cwd,
        port=port,
        upstream_url=args.upstream_url,
        upstream_model=args.upstream_model,
        key_env=args.key_env,
        lifecycle_mode=getattr(args, "lifecycle_mode", None),
    )

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
    mode = result.get("lifecycle_mode")
    if mode and mode != "full":
        print(f"  lifecycle: {mode} (proxy wired; no workflow injected)")
    if phase_seeded:
        print(f"  phase: {phase_seeded} (repo activated; composes next prompt)")
