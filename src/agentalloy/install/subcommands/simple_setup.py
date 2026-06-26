"""``agentalloy setup`` — interactive one-shot install wizard.

    pipx install git+https://github.com/nrmeyers/agentalloy.git
    agentalloy setup          # interactive: questions -> execution -> validation

The command:
1. **Asks questions** -- prompts the user for model, port, service mode, packs, harness
   (llama-server is the sole inference runner, so there is no runner prompt)
2. **Executes** -- runs all install steps with the gathered config
3. **Validates** -- confirms embedder is listening, corpus is healthy, harness is wired

After setup, per-repo commands still work:

    cd ~/my-project && agentalloy wire
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.subcommands import (
    detect,
    enable_service,
    install_packs,
    preflight,
    pull_models,
    seed_corpus,
    start_embed_server,
    start_rerank_server,
    verify,
    write_env,
)

try:
    from rich.console import Console  # type: ignore[import-untyped]

    console: Console | None = Console(force_terminal=True, soft_wrap=True)  # type: ignore[assignment]
except ImportError:
    console = None  # type: ignore[assignment]

# Import container_runtime helpers at module level so tests can mock them.
# These are re-exported for test mocking via the module scope (not inside run_setup).
from agentalloy.install.subcommands.container_runtime import (  # noqa: PLC0415, F401
    _check_container_running,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    _detect_functional_runtimes,  # noqa: F401
    _detect_runtime_binary,  # noqa: F401
    _ensure_volume,  # noqa: F401
    _list_conflicting_containers,  # noqa: F401
    _pull_image,  # noqa: F401
    _run_container,  # noqa: F401
    _tail_container_logs,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    _wait_for_readiness,  # noqa: F401
)


def _print(*args: Any, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
    """Print with Rich if available, plain stdout otherwise."""
    if console is not None:
        console.print(*args, **kwargs)  # type: ignore[union-attr, arg-type]
    else:
        print(*args, **kwargs)


def _image_variant_label(image_ref: str) -> str:
    """Return a human-readable label for the image variant.

    Handles both full image refs (e.g. 'ghcr.io/nrmeyers/agentalloy:latest')
    and bare tag suffixes (e.g. 'latest').

    Examples:
        'ghcr.io/nrmeyers/agentalloy:latest' -> 'latest (~300 MB, no model)'
        'ghcr.io/nrmeyers/agentalloy:full'   -> 'full (~975 MB, pre-pulled model)'
        'latest'                              -> 'latest (~300 MB, no model)'
        'full'                                -> 'full (~975 MB, pre-pulled model)'
    """
    # Extract the tag from the image ref (everything after the last ':')
    tag = image_ref.rsplit(":", 1)[-1] if ":" in image_ref else image_ref
    if tag == "latest":
        return "latest (~300 MB, no model)"
    elif tag == "full":
        return "full (~975 MB, pre-pulled model)"
    return f"custom ({image_ref})"


def _check_network_speed() -> tuple[str, int]:
    """Measure network speed to huggingface.co and return (warning_message, adjusted_timeout).

    Performs a quick download of the huggingface.co landing page (~50 KB) and
    estimates the effective bandwidth. huggingface.co is the host the GGUF
    models are actually pulled from, so it is the representative probe target.
    Used to warn users on slow networks and to adjust the readiness timeout
    for first-run model downloads.

    Returns
    -------
    tuple[str, int]
        (warning_message, adjusted_timeout_seconds).
        Returns an empty message and 1800s timeout on any failure.
    """
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen("https://huggingface.co/", timeout=5) as resp:
            body = resp.read()
        elapsed = time.monotonic() - t0
        # bits / seconds / 1e6 = Mbps. (Was /1024 → kbit/s mislabeled as Mbps,
        # which made the <1 / <5 Mbps thresholds and the warning unreachable.)
        speed_mbps = (len(body) * 8) / (elapsed * 1_000_000)

        if speed_mbps < 1:
            est_minutes = 600 / (speed_mbps * 60 / 8)
            return (
                f"  [yellow]\u26a0 Slow network ({speed_mbps:.1f} Mbps) \u2014 "
                f"model download may take ~{est_minutes:.0f} minutes.[/yellow]",
                int(est_minutes * 60) + 600,
            )
        elif speed_mbps < 5:
            est_minutes = 600 / (speed_mbps * 60 / 8)
            return (
                f"  [yellow]\u26a0 Slow network ({speed_mbps:.1f} Mbps) \u2014 "
                f"model download may take ~{est_minutes:.0f} minutes.[/yellow]",
                int(est_minutes * 60) + 300,
            )
        return ("", 1800)
    except Exception:
        return ("", 1800)


def _get_readiness_timeout(
    cfg: SetupConfig,
    first_run: bool,
    pack_count: int,
) -> int:
    """Compute the base readiness timeout (without network-speed adjustment).

    Dynamic timeout based on install scenario:
      - Re-install (no packs, models cached, not force): 300s
      - First-run with always-on packs (models need download): 1800s
      - First-run with 8+ packs (models need download): 2400s
      - First-run with 1-7 packs (models need download): 1500s
      - Fresh install with always-on packs (models cached): 600s
      - Fresh install with 8+ packs: 1200s
      - Fresh install with 1-7 packs: 300s
      - User override via SetupConfig.readiness_timeout takes precedence.
    """
    # User explicitly set --timeout.
    user_timeout = getattr(cfg, "readiness_timeout", None)
    if user_timeout is not None:
        return user_timeout

    # Determine whether this is a re-install (bootstrap already complete).
    # A re-install has no packs to install and the data volume likely
    # already contains .bootstrap-complete.
    is_reinstall = pack_count == 0 and not cfg.force

    if is_reinstall:
        # Re-install: bootstrap already done, uvicorn starts immediately.
        return 300
    elif first_run:
        # Fresh install — model download dominates first-run time (5-15 min).
        # Add 1200s (20 min) on top of the base timeout to account for this.
        if pack_count == 0:
            return 600 + 1200  # always-on packs + model download
        elif pack_count >= 8:
            return 1200 + 1200  # many packs + model download
        else:
            return 300 + 1200  # few packs + model download
    elif pack_count == 0:
        # Fresh install with always-on packs (core, documentation, engineering,
        # performance). Covers 99% of cases within 10 minutes.
        return 600
    elif pack_count >= 8:
        # Many explicit packs — generous timeout.
        return 1200
    else:
        # Few explicit packs (1–7).
        return 300


@dataclass
class SetupConfig:
    """User-facing configuration gathered during the interactive wizard."""

    runner: str | None = None
    model: str = ""
    port: int = 47950
    mode: str = "persistent"  # "persistent" or "manual"
    packs: str = ""  # comma-separated, empty = always-on only
    harness: str = "manual"
    preset: str = ""  # filled by auto-detect: "cpu", "nvidia", etc.
    non_interactive: bool = False
    force: bool = False
    acknowledge_sidecar: bool = False
    hardware_target: str = ""  # explicit user choice: "nvidia", "radeon", "apple-silicon", "cpu"

    # Deployment type: "native" (default) or "container"
    deployment: str = ""

    # Container runtime fields (used when deployment="container")
    runtime_binary: str = ""  # resolved path to container runtime (podman/docker)
    image_tag: str = "ghcr.io/nrmeyers/agentalloy:latest"  # full container image reference (GHCR)
    container_name: str = "agentalloy"  # base name for containers
    data_volume: str = "agentalloy-data"  # named volume for persistent data
    readiness_timeout: int | None = None  # user override for readiness timeout (seconds)

    # Upstream LLM (proxy target)
    upstream_url: str = ""
    upstream_model: str = ""
    upstream_api_key: str = ""

    # Resolved during execution -- not user-facing.
    detected_runner: str | None = None  # from detect.json (e.g. "llama-server")
    recommended_host: str | None = None  # from recommend-host-targets.json
    models_output: dict[str, Any] = field(default_factory=dict)  # type: ignore[type-arg]


# llama-server (llama.cpp) is the sole inference runner. The embed GGUF is the
# only model the wizard prompts for; the reranker GGUF is fixed.
_DEFAULT_EMBED_MODEL = "nomic-embed-text-v1.5.Q8_0.gguf"
_RERANK_MODEL = "Qwen3-Reranker-0.6B-Q8_0.gguf"

# Human-readable labels for hardware targets
_HW_LABELS: dict[str, str] = {
    "cpu": "CPU (RAM-only)",
    "nvidia": "NVIDIA GPU (CUDA)",
    "radeon": "AMD GPU (Vulkan/ROCm)",
    "apple-silicon": "Apple Silicon (Metal)",
}

# Preset name == hardware target (presets are named by hardware only; the
# hardware difference is handled via -ngl at server start, not preset env).
_VALID_PRESETS = frozenset(_HW_LABELS)

# Internal sentinel returned by _run_container_flow when the user, faced with a
# missing/unusable container runtime, opts to fall back to a native install.
# run_setup intercepts it and continues the native flow; it is never returned
# to the OS as an exit code.
_SWITCH_TO_NATIVE = -99


def _resolve_preset(cfg: SetupConfig) -> str:
    """Resolve the write-env preset from the hardware target.

    Preset names are hardware targets (cpu / nvidia / radeon / apple-silicon).
    Uses the user's explicit hardware_target if set, otherwise the auto-detected
    recommended_host. Falls back to "cpu" if the target is unknown.
    """
    hw = cfg.hardware_target or cfg.recommended_host or "cpu"
    if hw not in _VALID_PRESETS:
        _print(f"  [dim]Warning: unknown hardware target '{hw}', falling back to cpu.[/dim]")
        hw = "cpu"
    cfg.preset = hw
    return hw


def _report_verify_failures() -> None:
    """Surface failing verify checks from the saved verify.json.

    The wizard invokes verify with quiet=True (to suppress JSON spam in the
    success path), which also swallows the human checklist on failure. When
    verify returns non-zero, re-load the saved output and print each failing
    check's error + remediation so the user knows what to fix.
    """
    verify_fp = install_state.outputs_dir() / "verify.json"
    if not verify_fp.exists():
        _print(f"  [dim](no verify output found at {verify_fp})[/dim]")
        return
    try:
        result = json.loads(verify_fp.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _print(f"  [dim](could not read {verify_fp}: {exc})[/dim]")
        return
    failures = [c for c in result.get("checks", []) if not c.get("passed", False)]
    if not failures:
        return
    for c in failures:
        _print(f"    - {c['name']}: {c.get('error', 'unknown')}")
        if c.get("remediation"):
            _print(f"      FIX: {c['remediation']}")
    _print(f"  [dim]Full report: {verify_fp}[/dim]")


def _build_namespace(cfg: SetupConfig, **overrides: Any) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    """Build an argparse.Namespace from SetupConfig for subcommand dispatch.

    Each subcommand's .run() expects an argparse.Namespace with specific
    attributes. This function bridges the gap between our typed config and
    the argparse contract.
    """
    attrs: dict[str, Any] = {
        "port": cfg.port,
        "preset": cfg.preset,
        "runner": cfg.runner,
        "non_interactive": cfg.non_interactive,
        "packs": cfg.packs,
        "mode": "native" if cfg.mode == "persistent" else "manual",
        "harness": cfg.harness,
        "phase": "early",
        "models": None,
        "force": False,
        "ignore_unknown": False,
        "list": False,
        "runtime": None,
        "hardware": cfg.hardware_target,
        "host": None,
        "timeout": 120.0,  # start_embed_server timeout
        "overrides": None,  # write_env overrides
        "scope": "user",  # wire_harness scope
        "mcp_fallback": False,  # wire_harness mcp_fallback
        "legacy": False,  # wire_harness legacy mode
        "quiet": True,  # suppress JSON stdout when called from wizard
        "json": False,  # human-readable output (not raw JSON)
    }
    attrs.update(overrides)  # type: ignore[arg-type]
    return argparse.Namespace(**attrs)


def _prompt(text: str, default: Any = None) -> str:
    """Interactive prompt with default. Returns default if non-TTY."""
    if not sys.stdin.isatty():
        return str(default) if default is not None else ""
    return input(f"{text} [{default}]: ") or (str(default) if default is not None else "")


def _prompt_context(text: str, context: str, default: Any = None) -> str:
    """Interactive prompt with a context description and default. Returns default if non-TTY."""
    _print(f"  [dim]{context}[/dim]")
    return _prompt(text, default=default)


# ---------------------------------------------------------------------------
# Numbered-menu helpers (N1–N4)
# ---------------------------------------------------------------------------


def _prompt_numbered(
    title: str,
    options: list[tuple[str, str]],
    default_index: int,
) -> str:
    """Render a numbered menu and return the chosen option's value.

    options: list of (value, label) pairs in display order.
    default_index: 1-based index of the default option.
    Non-TTY: returns the default's value without prompting.
    """
    if not sys.stdin.isatty():
        return options[default_index - 1][0]

    _print(f"\n  [bold]{title}[/bold]")
    for i, (_value, label) in enumerate(options, start=1):
        _print(f"    {i}. {label}")
    while True:
        raw = input(f"  Enter number [{default_index}]: ").strip()
        if not raw:
            return options[default_index - 1][0]
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        _print(f"  [yellow]Please enter a number between 1 and {len(options)}.[/yellow]")


def _prompt_mode() -> str:
    return _prompt_numbered(
        "Service mode:",
        [
            ("persistent", "systemd  — runs as a background service (recommended)"),
            ("manual", "manual   — you manage the service lifecycle yourself"),
        ],
        default_index=1,
    )


def _prompt_hardware(default: str) -> str:
    options = [
        ("cpu", _HW_LABELS["cpu"]),
        ("nvidia", _HW_LABELS["nvidia"]),
        ("radeon", _HW_LABELS["radeon"]),
        ("apple-silicon", _HW_LABELS["apple-silicon"]),
    ]
    # 1-based index of the detected default; fall back to CPU (option 1).
    default_index = 1
    for i, (value, _label) in enumerate(options, start=1):
        if value == default:
            default_index = i
            break
    return _prompt_numbered(
        "Select hardware target:",
        options,
        default_index=default_index,
    )


def _prompt_deployment() -> str:
    """Prompt for deployment type: container or native.

    Container is listed first (option 1) and is the default, as it is the
    recommended option for new installs.
    """
    return _prompt_numbered(
        "Select deployment type:",
        [
            (
                "container",
                "Container — single container pulled from GHCR (recommended for new installs)",
            ),
            ("native", "Native  — runs directly on this host (systemd or manual)"),
        ],
        default_index=1,
    )


def _offer_switch_to_native(cfg: SetupConfig) -> bool:
    """Ask whether to fall back to a native install when no container runtime is
    usable. Interactive only — non-interactive runs keep the fail-fast behavior.

    Returns True if the user opts to switch to native.
    """
    # Match the other prompt helpers: never block on input when there's no TTY,
    # even if --non-interactive wasn't passed (piped stdin / CI). A bare input()
    # there would raise EOFError and abort setup with a traceback.
    if cfg.non_interactive or not sys.stdin.isatty():
        return False
    try:
        ans = (
            input(
                "  Install a container runtime (Docker or Podman) and re-run, or switch "
                "to a native install now? [switch to native: y / N]: "
            )
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _discover_packs() -> dict[str, dict[str, Any]]:
    """Discover available packs from the _packs directory."""
    try:
        import yaml as _yaml

        import agentalloy

        packs_root = Path(agentalloy.__file__).resolve().parent / "_packs"
    except (ImportError, AttributeError):
        return {}

    out: dict[str, dict[str, Any]] = {}
    if not packs_root.is_dir():
        return out
    for pack_dir in sorted(packs_root.iterdir()):
        if not pack_dir.is_dir():
            continue
        manifest_path = pack_dir / "pack.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifest: dict[str, Any] = (
                _yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            )
        except Exception:
            continue
        name = str(manifest.get("name") or pack_dir.name)
        out[name] = manifest
    return out


def _prompt_for_packs() -> str:
    """Interactive pack selection. Returns comma-separated pack names or empty string."""
    available = _discover_packs()
    if not available:
        _print("  [yellow]No packs found. Skipping pack selection.[/yellow]")
        return ""

    # Group by tier
    tiers: dict[str, list[tuple[str, int, bool]]] = {}
    always_on: list[str] = []
    for name, m in available.items():
        tier = m.get("tier", "other")
        always = m.get("always_install", False)
        skills = len(m.get("skills", []))
        tiers.setdefault(tier, []).append((name, skills, always))
        if always:
            always_on.append(name)

    # Tier display order
    tier_order = [
        "foundation",
        "language",
        "framework",
        "tooling",
        "protocol",
        "store",
        "platform",
        "domain",
        "workflow",
        "other",
    ]
    tier_labels = {
        "foundation": "Foundation",
        "language": "Languages",
        "framework": "Frameworks",
        "tooling": "Tooling",
        "protocol": "Protocols",
        "store": "Data Stores",
        "platform": "Platforms",
        "domain": "Domain",
        "workflow": "Workflows",
        "other": "Other",
    }
    # Reverse map: display label (lowercased) -> internal tier key
    _label_to_tier = {v.lower(): k for k, v in tier_labels.items()}

    # Build numbered list for reference
    _print("\n  [bold]Available skill packs[/bold]\n")
    pack_index: list[str] = []  # flat list for numeric selection
    for tier in tier_order:
        packs = tiers.get(tier)
        if not packs:
            continue
        label = tier_labels.get(tier, tier.title())
        _print(f"  [{label}]")
        for name, skills, always in sorted(packs, key=lambda x: x[0]):
            marker = " (always-on)" if always else ""
            _print(f"    - {name:22} {skills:2} skills{marker}")
            pack_index.append(name)
        _print()

    _print(f"  Always-on (auto-installed): {', '.join(sorted(always_on)) or '(none)'}")
    _print("\n  Tip: You can also use tiers (comma-separated):")
    _print(f"    {', '.join(tier_labels.get(t, t) for t in tier_order if t in tiers)}")
    _print("\n  Enter pack/tier names (comma-separated), 'all', or blank for always-on only.")

    try:
        raw = input("  Skill packs: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""

    if not raw or raw.lower() == "defaults":
        return ""
    if raw.lower() == "all":
        return ",".join(pack_index)

    chosen: list[str] = []
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        # Tier-based selection: match internal key or display label (case-insensitive)
        tier_key = None
        if t in tiers:
            tier_key = t
        elif t.lower() in _label_to_tier:
            tier_key = _label_to_tier[t.lower()]
        if tier_key is not None and tier_key in tiers:
            chosen.extend(name for name, _, _ in tiers[tier_key])
        elif t in available:
            chosen.append(t)
        elif t.isdigit() and 1 <= int(t) <= len(pack_index):
            chosen.append(pack_index[int(t) - 1])
        else:
            _print(f"  [yellow]Ignoring unknown: {t}[/yellow]")

    # Deduplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for name in chosen:
        if name not in seen:
            seen.add(name)
            deduped.append(name)

    return ",".join(deduped) if deduped else ""


def _derive_host_target(detect_data: dict[str, Any]) -> str:
    """Derive a hardware target string from detect.json output.

    Priority (regardless of list order):
      1. NVIDIA discrete GPU       →  "nvidia"
      2. AMD discrete GPU          →  "radeon"
      3. AMD integrated (APU)      →  "radeon"
      4. Apple integrated GPU      →  "apple-silicon"
      5. Fallback                  →  "cpu"
    """
    gpu = detect_data.get("gpu", {})
    discrete = gpu.get("discrete", [])
    integrated = gpu.get("integrated", [])

    # NVIDIA takes priority over AMD
    for card in discrete:
        if str(card.get("vendor") or "").lower() == "nvidia":
            return "nvidia"
    for card in discrete:
        if str(card.get("vendor") or "").lower() == "amd":
            return "radeon"
    # AMD integrated (APU: Strix Point, Phoenix, Hawk Point, etc.)
    for card in integrated:
        if str(card.get("vendor") or "").lower() == "amd":
            return "radeon"
    # Apple Silicon (integrated on Mac)
    for card in integrated:
        if str(card.get("vendor") or "").lower() == "apple":
            return "apple-silicon"
    return "cpu"


def _write_upstream_env(cfg: SetupConfig) -> None:
    """Persist the optional global upstream LLM vars to .env (idempotent).

    Harness selection + upstream adoption now live in the per-repo ``agentalloy
    add`` command (it writes ``.agentalloy/upstream``, which the proxy prefers).
    The global ``UPSTREAM_*`` is only the proxy's last-resort fallback, so setup
    writes it solely when one was passed explicitly via ``--upstream-*`` /
    ``$UPSTREAM_*``.
    """
    env_fp = install_state.env_path()

    original_content = env_fp.read_text() if env_fp.exists() else None
    if original_content is not None:
        st = install_state.load_state()
        if st.get("env_original_content") is None:
            st["env_original_content"] = original_content
            install_state.save_state(st)

    existing = env_fp.read_text(encoding="utf-8") if env_fp.exists() else ""
    filtered_lines = [
        line
        for line in existing.splitlines()
        if not line.startswith(("UPSTREAM_URL=", "UPSTREAM_MODEL=", "UPSTREAM_API_KEY="))
    ]
    filtered_lines.append(f"UPSTREAM_URL={cfg.upstream_url}")
    filtered_lines.append(f"UPSTREAM_MODEL={cfg.upstream_model}")
    filtered_lines.append(f"UPSTREAM_API_KEY={cfg.upstream_api_key}")
    filtered_lines.append("")

    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
        env_fp, "\n".join(filtered_lines)
    )


def _test_embed_endpoint(cfg: SetupConfig) -> None:
    """Smoke test: send a real embedding request and show the curl equivalent."""
    # Read .env values for the embed endpoint
    env_path = install_state.env_path()
    embed_url = None
    embed_model = None
    proxy_port = None
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("RUNTIME_EMBED_BASE_URL="):
                embed_url = line.split("=", 1)[1].strip()
            elif line.startswith("RUNTIME_EMBEDDING_MODEL="):
                embed_model = line.split("=", 1)[1].strip()
            elif line.startswith("RUNTIME_PORT="):
                proxy_port = line.split("=", 1)[1].strip()

    if not embed_url or not embed_model:
        _print("  [yellow]Could not read embed URL/model from .env -- skipping test.[/yellow]")
        return

    test_text = "test embedding for setup verification"
    payload = json.dumps({"model": embed_model, "input": test_text}).encode()
    req = urllib.request.Request(
        f"{embed_url}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            dim = len(data["data"][0]["embedding"])
            _print(f"  Embedding test: [green]OK[/green] -- {dim}-dim vector returned")
    except Exception as exc:
        _print(f"  [yellow]Embedding test failed: {exc}[/yellow]")
        _print(
            f"  [dim]The embed server may still start up; "
            f"check {install_state.user_data_dir() / 'logs' / 'embed-server.log'}[/dim]"
        )
        return

    # Second test: end-to-end skill query via the proxy
    if proxy_port:
        proxy_url = f"http://localhost:{proxy_port}"
        # Use the synthetic proxy model name (agentalloy-proxy) which the proxy
        # resolves to UPSTREAM_MODEL — exercises the proxy's full resolution path.
        query_payload = json.dumps(
            {
                "model": "agentalloy-proxy",
                "messages": [{"role": "user", "content": "add a pytest for the CLI"}],
            }
        ).encode()
        req2 = urllib.request.Request(
            f"{proxy_url}/v1/chat/completions",
            data=query_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                result = json.loads(resp2.read())
                completion = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                _print(f"  Skill query test: [green]OK[/green] -- {len(completion)} chars returned")
        except Exception as exc:
            _print(f"  [yellow]Skill query test: {exc}[/yellow]")
            _print(
                f"  [dim]The proxy may not be running yet; "
                f"check {install_state.user_data_dir() / 'logs' / 'agentalloy.log'}[/dim]"
            )


def _wait_for_one_shot(binary_path: str, container_name: str, *, timeout: int) -> int | None:
    """Block until a one-shot container exits, then return its exit code.

    Uses ``podman wait`` / ``docker wait`` (both behave identically: stdout
    is the exit code as a decimal, the wait call itself returns 0). Returns
    ``None`` if the wait call fails or times out so the caller can decide
    whether to bail or continue.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, binary_path from shutil.which
            [binary_path, "wait", container_name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip().splitlines()
    if not out:
        return None
    try:
        return int(out[-1].strip())
    except ValueError:
        return None


def _container_setup_log_path() -> Path:
    """Where we tee captured subprocess output during container setup."""
    log_dir = install_state.user_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "container-setup.log"


def _run_quiet(
    cmd: list[str],
    *,
    label: str,
    timeout: int,
    log_file: Path,
) -> int:
    """Run ``cmd`` with captured output appended to ``log_file``.

    Returns the process exit code on completion, or 1 (EXIT_USER) on
    timeout / OSError — staying inside the install CLI exit-code contract
    (0–4, see __main__.py). On non-zero exit, prints the last 30 captured
    lines to stderr so the user can diagnose without scrolling through
    every podman-compose debug line. The full output is always available
    in ``log_file``.

    Replaces the previous ``stdout=sys.stdout, stderr=sys.stderr``
    streaming pattern, which dumped all of podman-compose's internal
    debug chatter (``['podman', '--version', '']`` etc.) inline.

    Log file is opened in binary mode because ``subprocess.run`` writes
    raw child-process bytes to the stdout fd; a text-mode handle would
    risk encoding/buffering mismatches (per the subprocess docs).
    """
    with log_file.open("ab") as fh:
        fh.write(f"\n----- {label} -----\n$ {' '.join(cmd)}\n".encode())
        fh.flush()
        try:
            result = subprocess.run(  # noqa: S603 — argv list from caller
                cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            fh.write(f"[TIMEOUT after {timeout}s]\n".encode())
            _print(f"  [red]  {label} timed out after {timeout}s.[/red]")
            _print(f"  [dim]  Full output: {log_file}[/dim]")
            return 1
        except OSError as exc:
            fh.write(f"[OSError: {exc}]\n".encode())
            _print(f"  [red]  {label} failed to start: {exc}[/red]")
            _print(f"  [dim]  Full output: {log_file}[/dim]")
            return 1
    if result.returncode != 0:
        try:
            tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        except OSError:
            tail = []
        _print(f"  [red]  {label} failed (exit {result.returncode}). Last 30 lines:[/red]")
        for line in tail:
            _print(f"  [dim]  | {line}[/dim]")
        _print(f"  [dim]  Full output: {log_file}[/dim]")
    return result.returncode


# Fixed container names declared in compose.yaml via `container_name:`.
# Used as a fallback when the project-label query doesn't return them —
# e.g. when the user has overridden COMPOSE_PROJECT_NAME, so labels read
# `com.docker.compose.project=<other>` and the label filter misses them.
# The container_names themselves are hard-coded in compose.yaml so they
# WILL collide regardless of project name; we must clean them up.
_FIXED_CONTAINER_NAMES: tuple[str, ...] = (
    "agentalloy",
    "agentalloy-init",
    "agentalloy-ollama",
    "agentalloy-ollama-pull",
)


def _list_project_containers(binary_path: str) -> list[tuple[str, str]]:
    """Return [(name, status), ...] for containers belonging to this project.

    Two-pass detection:
      1. Filter by compose project label (covers the common case where the
         compose project name defaults to ``agentalloy`` from the repo dir).
      2. Look up the fixed container_names from compose.yaml by name. This
         catches installs where the user set ``COMPOSE_PROJECT_NAME`` to
         something else (so the label is wrong) but the ``container_name:``
         directives still collide on a fresh setup.
    """
    out: list[tuple[str, str]] = []

    def _record(line: str) -> None:
        # Require the tab delimiter from our --format string so we don't
        # accidentally parse unrelated single-token output (e.g. mocked
        # subprocess returns in tests).
        if "\t" not in line:
            return
        name, _, status = line.partition("\t")
        name = name.strip()
        if name and not any(n == name for n, _ in out):
            out.append((name, status.strip() or "unknown"))

    # Pass 1: label-based filter (covers the default project name).
    for label in (
        "io.podman.compose.project=agentalloy",
        "com.docker.compose.project=agentalloy",
    ):
        try:
            result = subprocess.run(  # noqa: S603 — fixed argv, binary_path from shutil.which
                [
                    binary_path,
                    "ps",
                    "-a",
                    "--filter",
                    f"label={label}",
                    "--format",
                    "{{.Names}}\t{{.Status}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            _record(line)

    # Pass 2: by fixed container_name — catches projects renamed via
    # COMPOSE_PROJECT_NAME where the label filter misses them.
    for fixed_name in _FIXED_CONTAINER_NAMES:
        if any(n == fixed_name for n, _ in out):
            continue
        try:
            result = subprocess.run(  # noqa: S603 — fixed argv, binary_path from shutil.which
                [
                    binary_path,
                    "ps",
                    "-a",
                    "--filter",
                    f"name=^{fixed_name}$",
                    "--format",
                    "{{.Names}}\t{{.Status}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            _record(line)

    return out


def _remove_containers(binary_path: str, names: list[str]) -> bool:
    """Force-remove the given containers. Retries once after a short sleep
    to handle podman's dependency-graph race when sibling containers in
    the same project reference each other via --requires.

    Returns True if all names are gone after the operation.
    """
    if not names:
        return True

    def _try_rm(targets: list[str]) -> None:
        try:
            subprocess.run(  # noqa: S603 — fixed argv, binary_path from shutil.which
                [binary_path, "rm", "-f", *targets],
                stdout=sys.stdout,
                stderr=sys.stderr,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            # Don't crash the wizard — fall through and let the post-rm
            # listing decide whether the cleanup succeeded. The caller
            # prints a remediation hint when the return value is False.
            _print(f"  [yellow]  rm -f failed: {exc}; will re-check state.[/yellow]")

    # First pass — best effort, errors expected for containers with
    # dependents that haven't been removed yet.
    _try_rm(names)
    survivors = [n for n, _ in _list_project_containers(binary_path) if n in names]
    if not survivors:
        return True
    # Retry the survivors after a brief pause so podman can settle its
    # dependency cache.
    time.sleep(2)
    _try_rm(survivors)
    final = [n for n, _ in _list_project_containers(binary_path) if n in names]
    return not final


def _reconcile_native_port_holder(cfg: SetupConfig) -> int:
    """Detect + reconcile a NATIVE process holding the container's host port.

    A native ``uvicorn agentalloy.app`` (a prior native install, or a stale one left
    by ``uv tool install --force``) bound to ``127.0.0.1:<port>`` shadows the
    container: the container publishes ``0.0.0.0:<port>`` but loopback traffic still
    reaches the native process — which has no in-container embed/reranker backend, so
    composition silently degrades. The container sweep above only removes our
    *containers*; this catches the *native* holder before ``podman run`` so the bind
    doesn't fail cryptically (or, worse, succeed-but-shadowed).

    Reclaims only a holder whose cmdline matches our own signature; never kills a
    foreign process or podman's own forwarder. Returns 0 to proceed, 1 to abort.
    """
    from agentalloy.install import server_proc
    from agentalloy.install.runtime_artifacts import RUNTIME_PORTS, SERVICE_PORT

    pid, cmdline = server_proc.port_holder_cmdline(cfg.port)
    if pid is None:
        return 0  # port is free — nothing to reconcile

    # Our native-service signature (uvicorn + agentalloy.app) for the service port.
    signature = next((list(m) for p, m in RUNTIME_PORTS if p == SERVICE_PORT), [])
    is_ours = bool(cmdline) and bool(signature) and all(s in cmdline for s in signature)

    if not is_ours:
        # podman's rootlessport forwarder for a container we already cleared above
        # frees the port momentarily; a genuinely foreign holder does not. Never kill
        # either — but refuse to proceed into a doomed `podman run` against a foreign
        # holder, with an actionable message instead of "address already in use".
        if "rootlessport" in cmdline:
            return 0
        _print(f"[yellow]Port {cfg.port} is held by an unrelated process (pid {pid}):[/yellow]")
        _print(f"  [dim]{cmdline[:100]}[/dim]")
        _print(
            f"  [yellow]The container must publish port {cfg.port}. Free it or pick "
            "another port, then re-run setup.[/yellow]"
        )
        return 1

    _print(f"[bold]A native AgentAlloy service is holding port {cfg.port}:[/bold]")
    _print(f"  - pid {pid}  [dim]({cmdline[:100]})[/dim]")
    _print(
        "  [dim]The container can't own the port while this native process is bound — "
        "loopback traffic would reach it instead of the container, which has no embed/"
        "reranker backend (those run inside the container).[/dim]"
    )
    if cfg.non_interactive:
        _print("  [dim]non-interactive: reclaiming automatically[/dim]")
        confirm = "y"
    else:
        confirm = _prompt("  Stop it and continue?", default="Y").strip().lower()
    if confirm not in ("", "y", "yes"):
        _print(
            "[yellow]Setup cancelled. Stop the native service first "
            "(`agentalloy disable`) or keep the native install instead.[/yellow]"
        )
        return 1

    reclaimed = server_proc.reclaim_stale_port(cfg.port, signature)
    if reclaimed is None:
        _print(
            f"  [red]Could not stop pid {pid} on port {cfg.port} (it may have exited, or the "
            f"signal was denied). Stop it manually (e.g. `kill {pid}`) or run "
            "`agentalloy disable`, then re-run setup.[/red]"
        )
        return 1
    _print("  [green]  Reclaimed.[/green]\n")
    return 0


def _run_container_flow(cfg: SetupConfig, t0: float) -> int:
    """Execute the container deployment flow.

    Skips native prompts (runner, model, hardware, port, mode, packs).
    Validates container prerequisites, pulls the pre-built image from GHCR,
    and runs a single self-contained container.
    """
    # 1. Run early preflight
    _print("  [dim]-> Preflight (early)[/dim]")
    preflight_result = preflight.run_preflight(phase="early", port=47950)
    fatal = [
        c["name"]
        for c in preflight_result.get("checks", [])
        if not c["passed"] and c.get("severity") == "fatal"
    ]
    if fatal:
        _print("  [red]Preflight failed:[/red]")
        for name in fatal:
            check = next(c for c in preflight_result["checks"] if c["name"] == name)
            _print(f"    - {name}: {check.get('error', 'unknown')}")
            if check.get("remediation"):
                _print(f"      FIX: {check['remediation']}")
        _print("  [red]Fix the issues above and re-run setup.[/red]")
        return 1
    _print("  [green]  Preflight (early) passed.[/green]")

    # 2. Detect container runtime (standalone, before image selection)
    # NOTE: container_runtime helpers are already imported at module level
    # (lines 54-67) so tests can mock them — no need to re-import here.

    requested = cfg.runtime_binary  # explicit --runtime flag, or "" for auto
    functional = _detect_functional_runtimes()
    if requested:
        # An explicit choice is strict: don't silently substitute another runtime.
        if requested in functional:
            label = requested
        elif shutil.which(requested) is None:
            _print(
                f"  [red]--runtime {requested} requested but `{requested}` is not on "
                f"PATH.[/red]\n  Install it, or drop --runtime to auto-detect."
            )
            return _SWITCH_TO_NATIVE if _offer_switch_to_native(cfg) else 1
        else:
            _print(
                f"  [red]--runtime {requested} requested but `{requested}` is not "
                f"responding.[/red]\n  Start its daemon/machine and re-run setup:\n"
                "    Podman:  podman machine start\n"
                "    Docker:  start Docker Desktop (macOS) or "
                "`sudo systemctl start docker` (Linux)"
            )
            return _SWITCH_TO_NATIVE if _offer_switch_to_native(cfg) else 1
    elif not functional:
        present = _detect_runtime_binary()  # present-but-non-functional, or None
        if present is None:
            _print(
                "  [red]Container deployment needs a container runtime, but neither "
                "`podman` nor `docker` was found on PATH.[/red]\n"
                "  Install one (then re-run setup):\n"
                "    Podman:  brew install podman  (macOS) / sudo apt install podman (Linux)\n"
                "    Docker:  https://docs.docker.com/get-docker/"
            )
        else:
            _print(
                f"  [red]`{present}` is installed but not responding.[/red]\n"
                "  Start its daemon/machine, then re-run setup:\n"
                "    Podman:  podman machine start\n"
                "    Docker:  start Docker Desktop (macOS) or "
                "`sudo systemctl start docker` (Linux)"
            )
        return _SWITCH_TO_NATIVE if _offer_switch_to_native(cfg) else 1
    elif len(functional) == 1:
        label = functional[0]
    else:
        # More than one runtime works — let the user pick. On a non-TTY this
        # returns the default (podman, the preferred runtime) without prompting.
        label = _prompt_numbered(
            "Multiple container runtimes detected — choose one:",
            [(rt, {"podman": "Podman", "docker": "Docker"}[rt]) for rt in functional],
            default_index=1,
        )
    binary_path = shutil.which(label)
    assert binary_path is not None, (
        f"{label} not found on PATH despite functional-runtime detection returning it"
    )
    cfg.runtime_binary = label
    _print(f"  Runtime binary: {label} at {binary_path}")

    # 2b. Container = CPU-only, on every host. GPU passthrough is intentionally
    # out of scope: nvidia needs nvidia-container-toolkit + deploy.resources,
    # AMD needs ROCm device mounts + a ROCm llama-server image, and Docker Desktop
    # on macOS cannot pass Metal through at all. Users who want GPU should
    # choose the native install. The bundled llama-server handles inference
    # on CPU using the nomic-embed-text-v1.5 model — functional for embeddings
    # but slower than GPU.
    _print(
        "\n  [yellow]Note — container deployment is CPU-only on every host.[/yellow]\n"
        "  GPU acceleration (NVIDIA/AMD/Apple Metal) only works with a native\n"
        "  install. The bundled llama-server runs on CPU; for a 600M embedding model\n"
        "  on short text this is functional but noticeably slower than GPU.\n"
        "  If you want GPU acceleration, cancel and re-run setup choosing the\n"
        "  native deployment."
    )
    if not cfg.non_interactive:
        ans = input("  Continue with container (CPU-only)? [Y/n]: ").strip().lower()
        if ans in ("n", "no"):
            _print("[yellow]Setup cancelled.[/yellow]")
            return 1

    # 3. (Removed) Compose-file / build-context discovery. The single-container
    # GHCR model pulls a self-contained image (_pull_image below), so no repo
    # checkout, compose.yaml, or Containerfile build context is needed at
    # install time.

    # 4. Run container preflight
    _print("  [dim]-> Preflight (container)[/dim]")
    container_preflight = preflight.run_preflight(
        phase="container",
        runtime=cfg.runtime_binary,
    )
    container_fatal = [
        c["name"]
        for c in container_preflight.get("checks", [])
        if not c["passed"] and c.get("severity") == "fatal"
    ]
    if container_fatal:
        _print("  [red]Container preflight failed:[/red]")
        for name in container_fatal:
            check = next(c for c in container_preflight["checks"] if c["name"] == name)
            _print(f"    - {name}: {check.get('error', 'unknown')}")
            if check.get("remediation"):
                _print(f"      FIX: {check['remediation']}")
        _print("  [red]Fix the issues above and re-run setup.[/red]")
        return 1
    _print("  [green]  Preflight (container) passed.[/green]")

    # 5. Set fixed values (container mode overrides)
    cfg.runner = "llama-server"
    cfg.port = 47950
    cfg.mode = "manual"
    cfg.deployment = "container"

    # 5b. Skill pack selection. The published GHCR image ships a prebuilt
    # all-packs corpus (seeded at first run), so the pack picker is dead
    # weight on the container path — skip it and tell the user. Native
    # installs still get the interactive picker (handled further below).
    if cfg.packs:
        pass  # caller pre-set packs (e.g. --packs flag)
    else:
        _print("  [dim]Published image ships all packs preloaded — selection skipped.[/dim]")
    # Expand 'all' keyword to the full pack list before validation.
    # Without this, 'all' is treated as an unknown pack name and silently
    # stripped — the user gets "always-on only" instead of all packs.
    if cfg.packs and cfg.packs.strip().lower() == "all":
        _all_packs = _discover_packs()
        cfg.packs = ",".join(sorted(_all_packs.keys()))
        _print(f"  [dim]-> Resolved packs: {len(_all_packs)} packs[/dim]")
    # Strip names that don't resolve against the host's seeds/packs dir.
    # Host and image are built from the same tree, so this is a reliable
    # pre-check that turns a typo into an immediate warning instead of a
    # five-minute wait followed by install-packs exit 1.
    if cfg.packs:
        _available_packs = _discover_packs()
        _requested = [p.strip() for p in cfg.packs.split(",") if p.strip()]
        _unknown = [p for p in _requested if p not in _available_packs]
        _valid = [p for p in _requested if p in _available_packs]
        if _unknown:
            _print(f"  [yellow]Unknown pack(s) skipped: {sorted(_unknown)}[/yellow]")
        cfg.packs = ",".join(_valid)

    # 5c. Engine-only setup. Harness wiring moved to the per-repo `agentalloy add
    # <harness>` command (it adopts the harness's own upstream), so setup no longer
    # prompts for a harness.

    # 6. Show summary
    _print("\n[dim]" + "─" * 40)
    _print("\n[bold]Review your container setup:[/bold]")
    _print("  Deployment:   container")
    _print(f"  Runtime:      {cfg.runtime_binary}")
    _print(f"  Image:        {cfg.image_tag} ({_image_variant_label(cfg.image_tag)})")
    _print(f"  Port:         {cfg.port}")
    _print(f"  Packs:        {cfg.packs or '(always-on only)'}")

    if not cfg.non_interactive:
        confirm = input("  Confirm and continue? [Y/n]: ").strip().lower()
        if confirm not in ("", "y", "yes"):
            _print("[yellow]Setup cancelled.[/yellow]")
            return 1
    _print()

    # 6.4. A native AgentAlloy service (or a stale one from `uv tool install --force`)
    # bound to the host port shadows the container: it publishes 0.0.0.0:<port> but
    # loopback traffic still reaches the native process (which has no in-container
    # embed/reranker). Reconcile it FIRST — before the container sweep below — so an
    # abort here never tears down existing containers and leaves a half-installed state.
    native_rc = _reconcile_native_port_holder(cfg)
    if native_rc != 0:
        return native_rc

    # 6.5. Check for stale containers from a prior project run.
    # Two sweeps are merged:
    #   a) _list_project_containers: label-based + fixed compose names (old compose installs).
    #   b) _list_conflicting_containers: name-exact + port-match (single-container GHCR
    #      installs — no compose label). An Exited container from a crashed bootstrap
    #      holds podman's rootlessport reservation for port 47950 even though nothing
    #      listens, causing `podman run` to fail with "address already in use".
    _label_containers = _list_project_containers(binary_path)
    _conflict_containers = _list_conflicting_containers(
        binary_path,
        container_name=cfg.container_name or "agentalloy",
        port=cfg.port,
    )
    # Dedup: _conflict_containers may overlap with _label_containers.
    _seen_names = {n for n, _ in _label_containers}
    existing = _label_containers + [(n, s) for n, s in _conflict_containers if n not in _seen_names]
    if existing:
        _print("[bold]Existing AgentAlloy containers detected:[/bold]")
        for name, status in existing:
            _print(f"  - {name}  [dim]({status})[/dim]")
        _print(
            f"  [dim]{cfg.runtime_binary} will misbehave if these stay around "
            "(name collisions, stale exit codes, port-reservation conflicts — "
            f"an Exited container can still hold port {cfg.port}).[/dim]"
        )
        if cfg.non_interactive:
            _print("  [dim]non-interactive: removing automatically[/dim]")
            confirm_rm = "y"
        else:
            # Use _prompt() so non-TTY stdin (CI pipes, redirected input)
            # falls back to the default ("Y") instead of EOFError'ing on
            # raw input(). Defaulting to "yes" matches the [Y/n] UX shown.
            confirm_rm = _prompt("  Remove them and continue?", default="Y").strip().lower()
        if confirm_rm not in ("", "y", "yes"):
            _print(
                "[yellow]Setup cancelled. Remove the containers manually "
                "or re-run setup and accept removal.[/yellow]"
            )
            return 1
        if not _remove_containers(binary_path, [name for name, _ in existing]):
            _print(
                "  [red]Failed to remove one or more containers; see errors above. Aborting.[/red]"
            )
            return 1
        _print("  [green]  Removed.[/green]\n")

    # 7. Build the image, start the single agentalloy container, and wait
    # for it to become healthy.  The entrypoint script handles the full
    # bootstrap sequence internally (in order):
    #
    #   1. Run DB schema migrations  (agentalloy-init equivalent)
    #   2. Start the llama-servers and wait for them to be healthy
    #   3. Download the GGUF models   (if missing)
    #   4. Install skill packs       (if cfg.packs is non-empty)
    #   5. Start uvicorn
    #
    # This replaces the old multi-container compose model with a single
    # container.  The entrypoint script's sequential flow (set -e) ensures
    # no race conditions between steps — migrations finish before the
    # llama-servers start, they are healthy before the models are downloaded,
    # the models are cached before packs are installed, and uvicorn only
    # starts after all bootstrap steps succeed.
    log_path = _container_setup_log_path()
    _print("[bold]Running container setup...[/bold]")
    _print(f"  [dim]Full setup log: {log_path}[/dim]")

    # 7a. Pull the agentalloy image from GHCR.
    _print("  [dim]-> Pulling container image from GHCR...[/dim]")
    # Pull the agentalloy image from GHCR
    build_rc = _pull_image(binary_path)
    if build_rc != 0:
        return 1

    # 7b. Ensure the agentalloy-data volume exists. The GGUF models persist
    # under /app/data/models inside this volume across restarts.
    _ensure_volume(binary_path)

    # 7c. Start the container. It runs the image's baked /app/entrypoint.sh and
    # picks up the pack list from the AGENTALLOY_PACKS env var — we no longer
    # bind-mount a host-generated entrypoint (that temp file was deleted after
    # install, which broke `start`/reboot since the mount source vanished).
    # The GGUF models live in the agentalloy-data volume (not the host home),
    # so we can't cheaply tell first-run from a restart here — surface the
    # first-run timing once; restarts skip the download (entrypoint checks the
    # volume) and reach readiness in 30-60s.
    _print(
        "  [dim]-> Starting agentalloy container "
        "(first run: ~5-10 min to download the embed + reranker GGUF models; "
        "published images ship a prebuilt skill corpus, locally built images "
        "also build the corpus, adding 20+ min on CPU; restarts: 30-60s)...[/dim]"
    )
    rc = _run_container(binary_path, cfg.packs)
    if rc != 0:
        return rc
    _print("  [green]  Done.[/green]")

    # 10. Wait for container readiness (fast-start uvicorn serves /readiness
    # while pack ingest runs in the background).
    #
    # Dynamic timeout based on install scenario:
    #   - Re-install (no packs, models cached): 300s
    #   - Fresh install, always-on packs (models cached): 600s
    #   - Fresh install, always-on packs (models downloaded): 1800s
    #   - Fresh install, 8+ packs (models downloaded): 2400s
    #   - Fresh install, 1-7 packs (models downloaded): 1800s
    #   - Re-install with explicit packs: 600s
    #   - User override via --timeout takes precedence.
    #
    # Network speed check: warn on slow connections and adjust timeout.
    network_msg, network_timeout = _check_network_speed()
    if network_msg:
        _print(network_msg)

    # First-run detection: the GGUF models now live in the agentalloy-data
    # volume (not the host home), so the host can't cheaply tell a fresh
    # install from a re-install. Assume first-run and budget the full
    # model-download time — the conservative choice (a re-install that skips
    # the download simply reaches readiness well inside the larger window).
    is_first_run = True

    # Count valid pack names (filter out empty strings from splitting).
    pack_count = len([p for p in (cfg.packs or "").split(",") if p.strip()])

    # Compute base timeout (without network-speed adjustment).
    readiness_timeout = _get_readiness_timeout(cfg, is_first_run, pack_count)

    # Apply network-speed-based timeout adjustment (if slow network detected).
    if network_timeout > readiness_timeout:
        readiness_timeout = network_timeout

    _print(
        f"  [dim]-> Waiting for container readiness "
        f"(timeout {readiness_timeout}s, ~30s per progress update)...[/dim]"
    )

    last_pack: str | None = None
    _last_heartbeat: int = 0  # last elapsed time we printed a heartbeat
    _HEARTBEAT_INTERVAL = 60  # seconds between heartbeats

    def _on_progress(evt: dict[str, Any]) -> None:
        nonlocal last_pack, _last_heartbeat
        progress = evt.get("progress") or {}
        extra = evt.get("extra") or {}
        # Prefer the in-container progress file; fall back to whatever
        # /readiness echoed.
        current = extra.get("current_pack") or progress.get("current_pack")
        ingested = extra.get("packs_ingested", progress.get("packs_ingested"))
        total = extra.get("packs_total", progress.get("packs_total"))
        elapsed = int(evt.get("elapsed") or 0)
        # Model download phase — show as a distinct status.
        # The entrypoint writes {"phase": "model_pull", "model": "...", ...}
        # to .bootstrap-progress before downloading the GGUF model.
        phase = extra.get("phase") or progress.get("phase")
        if phase == "model_pull":
            model = current or progress.get("model", "")
            if model and model != last_pack:
                last_pack = model
                _print(f"     [dim]bootstrap: downloading {model}  elapsed={elapsed}s[/dim]")
            elif elapsed and (elapsed - _last_heartbeat) >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = elapsed
                _print(f"     [dim]bootstrap: downloading {model}  elapsed={elapsed}s[/dim]")
            return

        # Prebuilt-corpus seed — published images skip pack ingest entirely;
        # tell the user instead of leaving silence where ingest updates were.
        if phase == "corpus_seeded" and last_pack != "__corpus_seeded__":
            last_pack = "__corpus_seeded__"
            _print(
                "     [dim]bootstrap: prebuilt corpus seeded from image — "
                f"skipping skill ingest  elapsed={elapsed}s[/dim]"
            )
            return

        # Pack ingestion phase — only print on change (pack rolled over) or
        # every ~minute on the same pack so the user sees liveness without
        # log spam.
        if current and current != last_pack:
            last_pack = current
            suffix = f" ({ingested}/{total})" if ingested is not None and total else ""
            _print(f"     [dim]bootstrap: {current}{suffix}  elapsed={elapsed}s[/dim]")
            _last_heartbeat = elapsed
        elif (
            evt.get("status") == "warming_up"
            and elapsed
            and (elapsed - _last_heartbeat) >= _HEARTBEAT_INTERVAL
        ):
            # Heartbeat for slow packs — show every ~60s since last update.
            _last_heartbeat = elapsed
            _print(f"     [dim]bootstrap: still warming up  elapsed={elapsed}s[/dim]")

    healthy = _wait_for_readiness(
        cfg.port,
        timeout=readiness_timeout,
        runtime=binary_path,
        container_name=cfg.container_name or "agentalloy",
        poll_interval=30.0,
        on_progress=_on_progress,
        stream_logs=True,
    )
    if not healthy:
        _print(
            f"  [yellow]  Service not ready after {readiness_timeout}s — "
            "check container logs.[/yellow]"
        )
    else:
        _print("  [green]  Service ready.[/green]")

    # 10. Record state + write .env (before verify so it reads fresh values)
    st = install_state.load_state()
    st["deployment"] = "container"
    st["runtime_binary"] = cfg.runtime_binary
    st["image_tag"] = cfg.image_tag
    st["container_name"] = cfg.container_name
    st["data_volume"] = cfg.data_volume
    st["port"] = cfg.port
    # Persist bootstrap timing for diagnostics (only meaningful when readiness
    # actually returned ready; otherwise leave the completed_at unset).
    from datetime import UTC  # noqa: PLC0415
    from datetime import datetime as _dt

    if not st.get("bootstrap_started_at"):
        st["bootstrap_started_at"] = _dt.now(UTC).isoformat()
    if healthy:
        st["bootstrap_completed_at"] = _dt.now(UTC).isoformat()
        st["bootstrap_packs_ingested"] = [
            p.strip() for p in (cfg.packs or "").split(",") if p.strip()
        ]
    install_state.save_state(st)

    # Host .env for container deployments only needs the API port. The
    # embedder lives entirely inside the container (compose internal network)
    # and is not reachable from the host. Host-side verify reads embed status
    # through agentalloy's /diagnostics/runtime endpoint instead of probing
    # the embedder URL directly.
    env_dir = install_state.user_config_dir()
    env_dir.mkdir(parents=True, exist_ok=True)
    env_fp = install_state.env_path()

    # Capture original .env content for backup/restore (only on first write)
    if env_fp.exists() and st.get("env_original_content") is None:
        st["env_original_content"] = env_fp.read_text()
        install_state.save_state(st)

    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
        env_fp, f"RUNTIME_PORT={cfg.port}\n"
    )

    # 11. Harness wiring is per-repo: run `agentalloy add <harness>` in each repo
    # (it adopts the harness's own upstream). Setup installs the engine only.

    # 12. Run verify
    _print("  [dim]-> Verifying installation[/dim]")
    rc = verify.run(_build_namespace(cfg))
    if rc not in (0, 4):
        _print("  [red]Validation failed.[/red]")
        _report_verify_failures()
        return rc
    _print("  [green]  All checks passed.[/green]")

    # -- Done --
    _print(
        f"\n[green]  Container setup complete in {int((time.monotonic() - t0) * 1000)}ms[/green]\n"
    )
    _print(f"  URL:      http://localhost:{cfg.port}")
    _print(f"  Runtime:  {cfg.runtime_binary}")
    _print(f"  Image:    {cfg.image_tag} ({_image_variant_label(cfg.image_tag)})")
    _print(f"  Container: {cfg.container_name}")
    _print(f"  Volume:   {cfg.data_volume}")

    _print(f"\n  [bold]Logs:[/bold] {cfg.runtime_binary} logs {cfg.container_name}")
    _print(f"\n  [bold]Stop:[/bold] {cfg.runtime_binary} stop {cfg.container_name}")
    return 0


def _offer_provision_runner(cfg: SetupConfig, preset: str) -> bool:
    """Offer to provision llama-server when it's missing at the runner gate.

    The runner binary is normally downloaded by the later 'Pulling models' step,
    so a missing llama-server at preflight is recoverable, not fatal: download a
    prebuilt now (for the chosen hardware), then let the caller re-check. Returns
    True if a binary is now on PATH. Non-interactive installs provision
    automatically (matching pull-models' own headless behavior).
    """
    _print(
        "  [yellow]llama-server is not on PATH yet[/yellow] — it's normally "
        "downloaded during 'Pulling models'."
    )
    if not cfg.non_interactive:
        try:
            ans = input("  Download a prebuilt llama-server now? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no"):
            _print("  [dim]Skipped — install llama-server manually, then re-run setup.[/dim]")
            return False
    else:
        _print("  [dim]non-interactive: provisioning a prebuilt automatically.[/dim]")

    result = pull_models.ensure_runner_binary(interactive=not cfg.non_interactive, preset=preset)
    if result.get("success"):
        _print(f"  [green]  llama-server ready at {result.get('binary_path', '?')}.[/green]")
        if result.get("warning"):
            _print(f"  [yellow]  {result['warning']}[/yellow]")
        return True
    _print(f"  [red]  Could not provision llama-server: {result.get('error', 'unknown')}[/red]")
    if result.get("hint"):
        _print(f"  [dim]  {result['hint']}[/dim]")
    return False


def _corpus_skill_count() -> int:
    """Embedded-skill count in the user corpus; 0 if absent/empty/unreadable.

    Kept as a module-level seam (tests stub this) — delegates to the shared
    :func:`seed_corpus.corpus_skill_count` used by both setup and upgrade.
    """
    return seed_corpus.corpus_skill_count()


def _teardown_prior_deployment(prior_state: dict[str, Any], prior_deployment: str) -> int:
    """Stop and remove a prior install's RUNTIME (servers/containers/services).

    Leaves the installed CLI package and the user's code/repos untouched — this
    is the in-place overwrite/switch path, not a full uninstall. Foreign
    processes are never killed (the reap/teardown primitives spare them).
    """
    if prior_deployment == "container":
        from agentalloy.install.subcommands import uninstall

        _print("  [dim]-> Removing the previous container[/dim]")
        warnings: list[str] = []
        uninstall._stop_container_stack(prior_state, warnings)  # pyright: ignore[reportPrivateUsage]
        for w in warnings:
            _print(f"  [yellow]  {w}[/yellow]")
        _print("  [green]  Done.[/green]")
    else:  # native
        from agentalloy.install import runtime_artifacts

        _print("  [dim]-> Stopping the previous native runtime[/dim]")
        actions = runtime_artifacts.reap(scope="all")
        for a in actions:
            if a.op == "warn_foreign":
                _print(f"  [yellow]  {a.summary}[/yellow]")
        _print("  [green]  Done.[/green]")
    return 0


def _reconcile_prior_install(
    cfg: SetupConfig,
    prior_state: dict[str, Any],
    prior_deployment: Any,
    datastore_initialized: bool,
) -> int:
    """Detect a prior install and offer to overwrite / switch deployment in place.

    Returns 0 to proceed with setup, or EXIT_NOOP when the user (or a
    non-interactive run without ``--force``) declines — leaving the prior install
    untouched. On accept, tears down the prior runtime and sets ``cfg.force`` so
    the downstream steps overwrite rather than no-op.
    """
    from agentalloy.install.__main__ import EXIT_NOOP

    prior_is_deployment = prior_deployment in ("native", "container")
    if not prior_is_deployment and not datastore_initialized:
        return 0  # nothing to reconcile — fresh host

    new_deployment = cfg.deployment or "native"
    switching = prior_is_deployment and prior_deployment != new_deployment

    if switching:
        _print(
            f"\n[yellow]Found an existing [bold]{prior_deployment}[/bold] install; "
            f"you chose [bold]{new_deployment}[/bold]. Setup can switch it in "
            f"place.[/yellow]"
        )
    elif prior_is_deployment:
        _print(
            f"\n[yellow]Found an existing [bold]{prior_deployment}[/bold] install. "
            f"Setup can overwrite it in place.[/yellow]"
        )
    else:
        _print(
            "\n[yellow]AgentAlloy is already initialized for this profile. Setup "
            "can overwrite it in place.[/yellow]"
        )
    _print(
        "  [dim]This stops and removes the previous runtime "
        "(servers/containers/services) and re-runs setup — no separate "
        "`uninstall` needed. Your code and repos are untouched.[/dim]"
    )

    if cfg.force:
        proceed = True
    elif cfg.non_interactive:
        _print(
            "  [yellow]Existing install present — re-run with `--force` to "
            "overwrite non-interactively. Leaving it untouched.[/yellow]"
        )
        return EXIT_NOOP
    else:
        verb = "Switch" if switching else "Overwrite"
        ans = _prompt(f"  {verb} the existing install and continue?", default="y")
        proceed = ans.strip().lower() in ("", "y", "yes")

    if not proceed:
        _print("  [yellow]Setup cancelled — existing install left untouched.[/yellow]")
        return EXIT_NOOP

    if prior_is_deployment:
        rc = _teardown_prior_deployment(prior_state, str(prior_deployment))
        if rc != 0:
            return rc

    # Overwrite is now authoritative: let downstream steps re-run rather than
    # short-circuit on the already-initialized datastore / cached step results.
    cfg.force = True
    return 0


def run_setup(cfg: SetupConfig) -> int:
    """Execute the simple interactive setup flow.

    Three phases:
    1. Detect hardware
    2. Gather user config with context descriptions
    3. Show summary for confirmation
    4. Execute install steps
    5. Validate
    """
    t0 = time.monotonic()

    # -- Profile detection + prior-install discovery --
    # We no longer hard-refuse when a prior install exists; instead we discover
    # it here and — once the deployment choice is known — offer to overwrite it
    # or switch deployment modes in place (see _reconcile_prior_install), so the
    # user doesn't have to uninstall + force-reinstall + re-run setup by hand.
    datastore_initialized = False
    try:
        from agentalloy.profiles import (
            _ensure_profile_dir,  # pyright: ignore[reportPrivateUsage]
            detect_profile,
        )

        _ensure_profile_dir("default")  # pyright: ignore[reportPrivateUsage]
        active_profile = detect_profile()
        ds_path = active_profile.datastore_path

        if ds_path.exists():
            try:
                import duckdb

                con = duckdb.connect(str(ds_path), read_only=True)
                datastore_initialized = (
                    con.execute("SELECT 1 FROM profile_skills LIMIT 1").fetchone() is not None
                )
                con.close()
            except Exception:
                datastore_initialized = False
    except ImportError:
        active_profile = None  # type: ignore[assignment]

    prior_state = install_state.load_state()
    prior_deployment = prior_state.get("deployment")

    # -- Phase 0: Auto-detect hardware --

    _print("\n[dim]Detecting hardware...[/dim]")

    detect_result = detect.run(_build_namespace(cfg))

    if detect_result not in (0, 4):
        _print("  [red]Hardware detection failed. Continuing with defaults.[/red]")

    # Read detect output to determine host target
    detect_fp = install_state.outputs_dir() / "detect.json"
    if detect_fp.exists():
        detect_data = json.loads(detect_fp.read_text())
        cfg.detected_runner = detect_data.get("runner")
        cfg.recommended_host = _derive_host_target(detect_data)
        # Print a concise summary instead of raw JSON
        gpu_info = detect_data.get("gpu", {})
        discrete = gpu_info.get("discrete", [])
        integrated = gpu_info.get("integrated", [])
        if discrete:
            gpus = ", ".join(f"{c.get('vendor', '')} {c.get('model', '')}" for c in discrete)
            _print(f"  GPUs: {gpus}")
        if integrated:
            igpus = ", ".join(f"{c.get('vendor', '')} {c.get('model', '')}" for c in integrated)
            _print(f"  Integrated: {igpus}")
    else:
        cfg.recommended_host = "cpu"

    # -- Deployment type prompt --

    if not cfg.non_interactive:
        cfg.deployment = _prompt_deployment()
    elif cfg.deployment:
        pass  # from CLI flag
    else:
        cfg.deployment = "native"  # non-interactive default

    # -- Reconcile a prior install (overwrite / native↔container switch) --
    rc = _reconcile_prior_install(cfg, prior_state, prior_deployment, datastore_initialized)
    if rc != 0:
        return rc

    if cfg.deployment == "container":
        rc = _run_container_flow(cfg, t0)
        if rc != _SWITCH_TO_NATIVE:
            return rc
        # No usable container runtime — user chose to fall back to native.
        # cfg is untouched at this point (the switch happens before any
        # container-specific overrides), so continue into the native flow.
        cfg.deployment = "native"
        cfg.runtime_binary = ""
        _print("\n  [yellow]Switching to a native install.[/yellow]")

    # -- Phase 1: Gather config --

    _print("\n[bold]agentalloy setup[/bold]\n")

    # 1. Runner — llama-server (llama.cpp) is the sole inference runner. Any
    # value passed via --runner is rejected unless it is "llama-server".
    if cfg.runner is not None and cfg.runner.strip().lower() not in ("", "llama-server"):
        _print(
            f"  [red]Invalid runner: {cfg.runner}. llama-server is the only supported runner.[/red]"
        )
        return 1
    cfg.runner = "llama-server"
    _print(f"  Runner: {cfg.runner}")

    # 2. Hardware target
    detected = cfg.recommended_host or "cpu"
    if not cfg.non_interactive:
        _print(f"\n  Detected: {_HW_LABELS.get(detected, detected)}")
        cfg.hardware_target = _prompt_hardware(default=detected)
    else:
        if cfg.hardware_target:
            cfg.hardware_target = cfg.hardware_target.strip().lower()
            if cfg.hardware_target not in _HW_LABELS:
                _print(f"  [red]Invalid hardware: {cfg.hardware_target}.[/red]")
                return 1
        else:
            cfg.hardware_target = detected
    _print(f"  Hardware: {_HW_LABELS.get(cfg.hardware_target, cfg.hardware_target)}")

    # 3. Model (embed GGUF; reranker GGUF is fixed)
    default_model = _DEFAULT_EMBED_MODEL
    if not cfg.non_interactive:
        chosen = _prompt_context(
            "  Model",
            "  Which embedding model to use. We recommend the default for your hardware.",
            default=default_model,
        )
        cfg.model = chosen or default_model
    else:
        cfg.model = cfg.model or default_model
    _print(f"  Model: {cfg.model}")

    # 4. Port
    if not cfg.non_interactive:
        port_str = _prompt_context(
            "  Service port",
            "  Port the agentalloy FastAPI service will listen on (default: 47950)",
            default=47950,
        )
        try:
            cfg.port = int(port_str)
        except ValueError:
            _print(f"  [red]Invalid port: {port_str}[/red]")
            return 1
    _print(f"  Port: {cfg.port}")

    # 5. Service mode
    if not cfg.non_interactive:
        cfg.mode = _prompt_mode()
    if cfg.mode not in ("persistent", "manual"):
        _print(f"  [red]Invalid mode: {cfg.mode}. Use persistent or manual.[/red]")
        return 1
    _print(f"  Mode: {cfg.mode}")

    # 6. Packs
    if not cfg.non_interactive:
        cfg.packs = _prompt_for_packs()
    _print(f"  Packs: {cfg.packs or '(always-on only)'}")

    # Persist the user's choice so install-packs picks it up without
    # re-prompting. A standalone re-run of install-packs later (no pending
    # selection on disk) will fall back to its own interactive flow.
    # Best-effort: a state-write failure must not block setup.
    try:
        _st = install_state.load_state()
        pack_list: list[str] = []
        if cfg.packs:
            pack_list = [p.strip() for p in cfg.packs.split(",") if p.strip()]
        install_state.set_pending_pack_selection(_st, pack_list)
        install_state.save_state(_st)
    except Exception as exc:  # noqa: BLE001 — best-effort
        _print(f"  [yellow]  warning: could not persist pack selection ({exc}).[/yellow]")

    # 7. Engine-only setup. Harness selection + upstream adoption moved to the
    # per-repo `agentalloy add <harness>` command, which discovers each harness's
    # upstream from its own config — so setup no longer prompts for a harness or an
    # upstream LLM. ``cfg.upstream_*`` stays as an optional global fallback (via
    # --upstream-* / $UPSTREAM_*) for the proxy's last-resort upstream.

    # Resolve preset from explicit choices (after all user input)
    preset = _resolve_preset(cfg)
    # Preset is an internal write-env detail; not shown to the user.

    # -- Phase 2: Summary confirmation --

    _print("\n[dim]" + "─" * 40)
    _print("\n[bold]Review your choices:[/bold]")
    _print(f"  Runner:     {cfg.runner}")
    _print(f"  Model:      {cfg.model}")
    _print(f"  Port:       {cfg.port}")
    _print(f"  Mode:       {cfg.mode}")
    _print(f"  Packs:      {cfg.packs or '(always-on only)'}")

    hw_label = _HW_LABELS.get(cfg.hardware_target, cfg.hardware_target)
    detected = cfg.recommended_host or "cpu"
    if cfg.hardware_target == detected:
        _print(f"  Hardware:   {hw_label}")
    else:
        detected_label = _HW_LABELS.get(detected, detected)
        _print(f"  Hardware:   {hw_label}  (detected: {detected_label})")

    if not cfg.non_interactive:
        confirm = _prompt("  Confirm and continue? (y/n)", default="y")
        if confirm.lower() not in ("y", "yes"):
            _print("[yellow]Setup cancelled.[/yellow]")
            return 1
    _print()

    # -- Phase 3: Execute install steps --

    _print("[bold]Running setup steps...[/bold]")

    # Step a: Preflight (early)
    _print("  [dim]-> Preflight (early)[/dim]")
    preflight_result = preflight.run_preflight(phase="early", port=cfg.port)
    fatal = [
        c["name"]
        for c in preflight_result.get("checks", [])
        if not c["passed"] and c.get("severity") == "fatal"
    ]
    if fatal:
        _print("  [red]Preflight failed:[/red]")
        for name in fatal:
            check = next(c for c in preflight_result["checks"] if c["name"] == name)
            _print(f"    - {name}: {check.get('error', 'unknown')}")
            if check.get("remediation"):
                _print(f"      FIX: {check['remediation']}")
        _print("  [red]Fix the issues above and re-run setup.[/red]")
        return 1
    _print("  [green]  Preflight (early) passed.[/green]")

    # Step a2: Pre-clean stale native state from a prior broken install.
    # A crashed `uv tool install --force` or a half-finished setup can leave
    # dead systemd/launchd units and a dangling llama-server shim behind. Reap
    # ONLY the stale ones (live, healthy units/shims are left untouched) so a
    # fresh install doesn't trip over them when it pulls models and binds ports.
    # Best-effort: reap() never raises, and a failure here must not block setup.
    # Native-only — the container flow already returned above.
    from agentalloy.install import runtime_artifacts

    stale = runtime_artifacts.reap("services", stale_only=True) + runtime_artifacts.reap(
        "shim", stale_only=True
    )
    if stale:
        _print("  [dim]-> Clearing stale state from a prior install[/dim]")
        for a in stale:
            _print(f"  [dim]  {a.summary}[/dim]")

    # Step b: Preflight (runner)
    _print("  [dim]-> Preflight (runner)[/dim]")
    runner_preflight = preflight.run_preflight(phase="runner", runner=cfg.runner, port=cfg.port)
    runner_fatal = [
        c["name"]
        for c in runner_preflight.get("checks", [])
        if not c["passed"] and c.get("severity") == "fatal"
    ]

    # A missing llama-server here is recoverable: the binary is provisioned by
    # the later 'Pulling models' step, so rather than dead-end a fresh host that
    # has no runner on PATH yet, offer to download a prebuilt now and re-check.
    if (
        runner_fatal == ["llama_server_present"]
        and cfg.runner == "llama-server"
        and _offer_provision_runner(cfg, preset)
    ):
        runner_preflight = preflight.run_preflight(phase="runner", runner=cfg.runner, port=cfg.port)
        runner_fatal = [
            c["name"]
            for c in runner_preflight.get("checks", [])
            if not c["passed"] and c.get("severity") == "fatal"
        ]

    if runner_fatal:
        _print("  [red]Runner preflight failed:[/red]")
        for name in runner_fatal:
            check = next(c for c in runner_preflight["checks"] if c["name"] == name)
            _print(f"    - {name}: {check.get('error', 'unknown')}")
        _print("  [red]Install/start the runner and re-run setup.[/red]")
        return 1
    _print("  [green]  Preflight (runner) passed.[/green]")

    # Step c: Write .env
    _print("  [dim]-> Writing .env[/dim]")
    ns = _build_namespace(cfg, preset=preset, port=cfg.port, overrides=None, force=False)
    rc = write_env.run(ns)
    if rc not in (0, 4):
        _print(f"  [red]  write-env failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Optional global upstream fallback: persist only when one was passed
    # explicitly (per-repo `agentalloy add` adoption is the primary path).
    if cfg.upstream_url:
        _print("  [dim]-> Writing optional global upstream fallback[/dim]")
        _write_upstream_env(cfg)
        _print("  [green]  Done.[/green]")

    # Step d: Pull models (embed + reranker GGUFs)
    _print("  [dim]-> Pulling models[/dim]")
    # Build a minimal recommend-models.json for pull_models to consume.
    # pull_models.pull_models() reads models_json["options"], where each entry
    # carries the embed pair plus the reranker pair so BOTH GGUFs are pulled.
    models_json = {
        "schema_version": 1,
        "preset": preset,
        "selected_runner": cfg.runner,
        "options": [
            {
                "default": True,
                "embed_model": cfg.model,
                "embed_runner": cfg.runner,
                "rerank_model": _RERANK_MODEL,
                "rerank_runner": cfg.runner,
            }
        ],
    }
    models_fp = install_state.outputs_dir() / "recommend-models.json"
    models_fp.write_text(json.dumps(models_json))
    rc = pull_models.run(_build_namespace(cfg, models=str(models_fp), runner=cfg.runner))
    if rc == 4:
        _print("  [dim]  Model already present, skipping.[/dim]")
    elif rc != 0:
        _print(f"  [red]  pull-models failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Step e: Seed corpus
    _print("  [dim]-> Seeding corpus[/dim]")
    rc = seed_corpus.run(_build_namespace(cfg))
    if rc not in (0, 4):  # 4 = EXIT_NOOP
        _print(f"  [red]  seed-corpus failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Step f: Start embed server (llama-server, port 47951)
    _print("  [dim]-> Starting embed server[/dim]")
    rc = start_embed_server.run(_build_namespace(cfg, models=str(models_fp), timeout=120.0))
    if rc not in (0, 4):
        _print(f"  [red]  start-embed-server failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Step f2: Start reranker server (second llama-server, port 47952)
    _print("  [dim]-> Starting reranker server[/dim]")
    rc = start_rerank_server.run(
        _build_namespace(
            cfg,
            models=str(models_fp),
            hardware_target=cfg.hardware_target or cfg.recommended_host or "cpu",
            timeout=120.0,
        )
    )
    if rc not in (0, 4):
        _print(f"  [red]  start-rerank-server failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Step g: Install packs
    _print("  [dim]-> Installing packs[/dim]")
    rc = install_packs.run(
        _build_namespace(
            cfg,
            packs=cfg.packs,
            non_interactive=cfg.non_interactive,
            ignore_unknown=False,
            list=False,
        )
    )
    if rc not in (0, 4):
        _print(f"  [red]  install-packs failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Guard: install-packs reported success — verify the corpus actually
    # populated. A silent half-install (embed step skipped/NOOP, or written to a
    # different data dir) would otherwise leave an absent/empty corpus that only
    # surfaces later in `doctor` as "corpus DB absent". Fail loudly here instead.
    corpus_skill_count = _corpus_skill_count()
    if corpus_skill_count < seed_corpus.MIN_SKILL_COUNT:
        _print(
            f"  [red]  install-packs reported success but the corpus at "
            f"{install_state.corpus_dir()} is missing or empty "
            f"({corpus_skill_count} skills embedded, expected "
            f">= {seed_corpus.MIN_SKILL_COUNT}) — the install is incomplete.[/red]"
        )
        _print(
            "  [red]  Re-run `agentalloy install-packs`; if it persists, run "
            "`agentalloy doctor` and share the output.[/red]"
        )
        return 1

    # Step h: Enable service
    _print("  [dim]-> Enabling service[/dim]")
    mode_flag = "native" if cfg.mode == "persistent" else "manual"
    rc = enable_service.run(_build_namespace(cfg, mode=mode_flag, runtime=None, port=cfg.port))
    if rc not in (0, 4):
        _print(f"  [red]  enable-service failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Harness wiring is per-repo: run `agentalloy add <harness>` in each repo.
    # It points the harness at the proxy and adopts the harness's own upstream,
    # so setup installs the engine only.

    # -- Phase 4: Validate --

    _print("\n[bold]Validating installation...[/bold]")
    rc = verify.run(_build_namespace(cfg))
    if rc not in (0, 4):
        _print("  [red]Validation failed.[/red]")
        _report_verify_failures()
        return rc
    _print("  [green]All checks passed.[/green]")

    # Embedding endpoint smoke test
    _print("\n[dim]Testing embed endpoint...[/dim]")
    _test_embed_endpoint(cfg)

    # -- Done --

    # Record native deployment in state
    st = install_state.load_state()
    st["deployment"] = "native"
    install_state.save_state(st)

    _print(f"\n[green]  Setup complete in {int((time.monotonic() - t0) * 1000)}ms[/green]\n")
    _print(f"  Service: {cfg.mode}")
    _print(f"  URL:     http://localhost:{cfg.port}")
    _print(f"  Config:  {install_state.user_config_dir()}")
    _print(f"  Data:    {install_state.user_data_dir()}")

    # Reranker status — it's the primary phase-transition trigger (v2.4.0); make
    # plain whether the install gets the sharp intent path or the cosine floor.
    try:
        _backend, _rerank_url = install_state.resolve_intent_reranker(
            install_state.parse_env_file()
        )
        if _backend == "cosine":
            _print("  Reranker: cosine backend (embedder-based intent; no reranker server)")
        elif install_state.rerank_reachable(_rerank_url):
            _print(
                f"  Reranker: [green]live[/green] at {_rerank_url} (intent-based phase detection)"
            )
        else:
            _print(
                f"  Reranker: [yellow]not reachable[/yellow] at {_rerank_url} — phase detection "
                "uses the cosine floor; run [bold]agentalloy enable-service[/bold] to start it"
            )
    except Exception:
        pass

    # Profile-aware completion message
    try:
        from agentalloy.profiles import detect_profile  # noqa: PLC0415

        _profile = detect_profile()
        _print(f"\n  [bold]Profile:[/bold]  {_profile.name}")
        _print(f"  Datastore: {_profile.datastore_path}")
        _print(
            "  Customize skills: [bold]agentalloy customize list[/bold] "
            "to see available system+workflow skills."
        )
    except Exception:
        pass

    _print("\n  [bold]Next:[/bold] cd to your project repo and run [bold]agentalloy wire[/bold]")
    return 0


def add_parser(
    subparsers: Any,  # type: ignore[type-arg]
) -> None:  # type: ignore[no-untyped-def]
    """Register 'setup' as a subcommand in the existing argparse dispatcher."""
    p: argparse.ArgumentParser = subparsers.add_parser(
        "setup",
        help="Interactive setup wizard: detect, configure, install, validate.",
    )
    p.add_argument(
        "--non-interactive",
        "-n",
        action="store_true",
        help="Accept all defaults without prompting.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Bypass the already-initialized check and overwrite existing state without prompting (dangerous).",
    )
    p.add_argument(
        "--runner",
        choices=["llama-server"],
        default=None,
        help="Inference runner. llama-server (llama.cpp) is the only choice.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Embedding model name.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Service port (default: 47950).",
    )
    p.add_argument(
        "--mode",
        choices=["persistent", "manual"],
        default=None,
        help="Service mode (default: persistent).",
    )
    p.add_argument(
        "--packs",
        default=None,
        help="Comma-separated pack names, 'all', or blank for always-on.",
    )
    p.add_argument(
        "--harness",
        default=None,
        help="IDE harness to wire (default: manual).",
    )
    p.add_argument(
        "--hardware",
        choices=["nvidia", "radeon", "apple-silicon", "cpu"],
        default=None,
        help="Hardware target for embedding (default: auto-detected).",
    )
    p.add_argument(
        "--acknowledge-sidecar",
        action="store_true",
        default=False,
        dest="acknowledge_sidecar",
        help="Acknowledge sidecar harness limitations (required for non-interactive setup of cursor/windsurf/github-copilot/gemini-cli).",
    )
    # Deprecated alias; preserved for backward compatibility. Sets the same dest.
    p.add_argument(
        "--acknowledge-tier3",
        action="store_true",
        default=False,
        dest="acknowledge_sidecar",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--deployment",
        choices=["native", "container"],
        default=None,
        help="Deployment type (default: native for non-interactive, prompted interactively).",
    )
    p.add_argument(
        "--runtime",
        choices=["podman", "docker"],
        default=None,
        help=(
            "Container runtime to use (default: auto-detect; podman preferred when both work). "
            "Use this to choose docker non-interactively when both are installed."
        ),
    )
    p.add_argument(
        "--image-tag",
        choices=["latest", "full"],
        default="latest",
        help="Container image variant: 'latest' (~300 MB, no pre-pulled model) or 'full' (~975 MB, model pre-pulled for air-gapped/enterprise). Default: latest.",
    )
    # Proxy upstream LLM (only needed for proxy-wired harnesses). Interactive
    # setup prompts for these; these flags / env vars make them reachable in
    # non-interactive mode.
    p.add_argument(
        "--upstream-url",
        default=None,
        help="Proxy upstream LLM base URL (non-interactive; falls back to $UPSTREAM_URL).",
    )
    p.add_argument(
        "--upstream-model",
        default=None,
        help="Proxy upstream model name (non-interactive; falls back to $UPSTREAM_MODEL).",
    )
    p.add_argument(
        "--upstream-api-key",
        default=None,
        help=(
            "Proxy upstream API key (non-interactive; falls back to $UPSTREAM_API_KEY). "
            "Prefer the env var — a CLI value is visible in process args and shell history."
        ),
    )
    p.set_defaults(func=_run_from_args)


def _run_from_args(args: argparse.Namespace) -> int:
    """Bridge from argparse.Namespace to SetupConfig -> run_setup()."""
    # Build the full image reference from the CLI-provided tag suffix.
    image_tag_suffix = getattr(args, "image_tag", "latest")
    image_ref = f"ghcr.io/nrmeyers/agentalloy:{image_tag_suffix}"
    cfg = SetupConfig(
        runner=args.runner,  # may be None; resolved inside run_setup
        model=args.model or "",
        port=args.port or 47950,
        mode=args.mode or "persistent",
        packs=args.packs or "",
        harness=args.harness or "manual",
        hardware_target=getattr(args, "hardware", None) or "",
        deployment=getattr(args, "deployment", None) or "",
        runtime_binary=getattr(args, "runtime", None) or "",
        non_interactive=args.non_interactive,
        force=getattr(args, "force", False),
        acknowledge_sidecar=getattr(args, "acknowledge_sidecar", False),
        image_tag=image_ref,
        # Proxy upstream: explicit flag wins, else env var (interactive setup
        # overwrites these via its own prompts, using them as the defaults).
        upstream_url=getattr(args, "upstream_url", None) or os.environ.get("UPSTREAM_URL", ""),
        upstream_model=getattr(args, "upstream_model", None)
        or os.environ.get("UPSTREAM_MODEL", ""),
        upstream_api_key=getattr(args, "upstream_api_key", None)
        or os.environ.get("UPSTREAM_API_KEY", ""),
    )
    # Model default is resolved inside run_setup() after cfg.runner is finalized.
    return run_setup(cfg)
