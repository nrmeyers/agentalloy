# pyright: reportPrivateUsage=false
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
import logging
import sys
from pathlib import Path
from typing import Any, cast

import yaml

from agentalloy.api.proxy_context import (
    _PASSTHROUGH_HARNESS_KEYS,
    CHAT_UPSTREAM_HARNESS,
    UPSTREAM_FILE,
    Upstream,
)
from agentalloy.install import state as install_state
from agentalloy.providers import REGISTRY

logger = logging.getLogger(__name__)


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
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
    p.add_argument(
        "--no-index",
        action="store_true",
        default=False,
        help="Wire the harness only; skip code-index block injection and indexing.",
    )
    p.set_defaults(func=_run)


def _load_upstream_map(path: Path) -> dict[str, Any]:
    """Load the per-harness upstream map, migrating a legacy flat file.

    A legacy ``.agentalloy/upstream`` (``url``/``model`` at the top level) is
    folded under :data:`CHAT_UPSTREAM_HARNESS` so it keeps satisfying the chat
    surface while never capturing a passthrough harness. The map is keyed by
    harness: adopting one harness never clobbers another's entry.
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                data = dict(raw)
        except yaml.YAMLError:
            data = {}
        if isinstance(data.get("url"), str):
            flat: dict[str, Any] = {"url": data["url"], "model": data.get("model")}
            if isinstance(data.get("key_env"), str):
                flat["key_env"] = data["key_env"]
            data = {CHAT_UPSTREAM_HARNESS: flat}
    return data


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
    the optional CLI overrides on top, and records ``{url, model, key_env}``
    under *harness*'s own key so each harness carries its own forwarding target.
    Writes nothing and returns ``None`` when no upstream can be determined —
    e.g. claude-code, whose auth-transparent Anthropic passthrough forwards the
    caller's own key to Anthropic and so has nothing to adopt by default (an
    explicit ``--upstream-url``/``--upstream-model`` opts into chaining and is
    recorded under ``claude-code`` only). Shared by ``add`` and the deprecated
    ``wire`` so both are transparent interceptors.
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

    data = _load_upstream_map(path)
    if harness not in _PASSTHROUGH_HARNESS_KEYS:
        # A newly adopted chat harness supersedes any legacy/previous chat-scope
        # entry — a repo keeps exactly one chat forwarding target.
        data.pop(CHAT_UPSTREAM_HARNESS, None)
    entry: dict[str, str] = {"url": upstream.url, "model": upstream.model}
    if upstream.key_env:
        entry["key_env"] = upstream.key_env
    data[harness] = entry
    install_state._atomic_write(path, yaml.safe_dump(data, sort_keys=False))
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
    assume_index: bool = False,
    no_index: bool = False,
) -> tuple[Upstream | None, dict[str, Any]]:
    """Adopt *harness*'s upstream and wire interception at *root* (repo scope).

    The reusable core shared by ``add`` (root = cwd) and ``worktree`` (root = a
    freshly created worktree): capture upstream → wire the harness through the
    proxy → record the lifecycle mode → drop the README and git-exclude
    ``.agentalloy/``. Returns ``(upstream, wire_result)`` for the caller to
    render. Callers are responsible for validating *harness* against
    ``REGISTRY`` first.

    For ``full`` lifecycle repos with no existing phase, the phase is seeded
    to ``intake`` here (via the state store) so the first real prompt triggers
    intake rather than silently passing through. This is a best-effort write:
    a missing or unreachable service is not fatal to wiring.

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
        _detect_custom_workflow,
        _prompt_lifecycle_mode,
        _seed_repo_metadata,
    )
    from agentalloy.install.subcommands.wire_harness import (
        _wire_harness_core,
    )
    from agentalloy.signals.skill_loader import (
        _read_phase,
        _write_lifecycle_mode,
        _write_phase_atomic,
    )

    # Lifecycle mode is repo-global (one workflow machine per repo, however
    # many harnesses point at the proxy). Same precedence as `wire`.
    mode = lifecycle_mode
    if mode is None:
        detected = _detect_custom_workflow(root)
        mode = _prompt_lifecycle_mode(detected) if detected and sys.stdin.isatty() else "full"
    _write_lifecycle_mode(root, mode)

    # Seed phase to intake for new full-lifecycle repos so the first prompt
    # doesn't silently passthrough. Only if no phase is already recorded.
    if mode == "full" and _read_phase(root) is None:
        try:
            _write_phase_atomic(root, "intake")
        except Exception:
            logger.debug(
                "phase seed to intake skipped for %s (store unavailable)",
                root,
                exc_info=True,
            )

    from agentalloy.install import code_index_wiring

    result = _wire_harness_core(harness, port=port, root=root, scope="repo")
    result["lifecycle_mode"] = mode
    _seed_repo_metadata(root)  # README + git-exclude; keeps .agentalloy/ uncommitted

    # Auto-wire future worktrees of this repo (a post-checkout hook, shared
    # across worktrees — installing once from any checkout covers all of
    # them). Best-effort: never blocks wiring on failure.
    from agentalloy.install.git_hooks import install_post_checkout_hook

    install_post_checkout_hook(root)

    # Code-index harness block (second sentinel pair) — written only when the
    # service reports the module enabled; cleans up stale/legacy blocks otherwise.
    if not no_index:
        code_index_wiring.maybe_wire(root, port, assume_yes=assume_index, harness=harness)
    return upstream, result


def _run(args: argparse.Namespace) -> int:
    harness: str = args.harness
    if REGISTRY.get(harness) is None:
        print(f"ERROR: Unknown harness: {harness}.", file=sys.stderr)
        print(f"FIX:   Choices: {', '.join(sorted(REGISTRY))}.", file=sys.stderr)
        return 1

    cwd = Path.cwd().resolve()
    port = resolve_port(args.port)

    upstream, result = adopt_and_wire(
        harness,
        cwd,
        port=port,
        upstream_url=args.upstream_url,
        upstream_model=args.upstream_model,
        key_env=args.key_env,
        lifecycle_mode=getattr(args, "lifecycle_mode", None),
        no_index=getattr(args, "no_index", False),
    )

    _render(harness, upstream, result)
    return 0


def _render(
    harness: str,
    upstream: Upstream | None,
    result: dict[str, Any],
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
