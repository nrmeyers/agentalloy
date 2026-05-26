"""``agentalloy setup`` — interactive one-shot install wizard.

    pipx install git+https://github.com/nrmeyers/agentalloy.git
    agentalloy setup          # interactive: questions -> execution -> validation

The command:
1. **Asks questions** -- prompts the user for runner, model, port, service mode, packs, harness
2. **Executes** -- runs all install steps with the gathered config
3. **Validates** -- confirms embedder is listening, corpus is healthy, harness is wired

After setup, per-repo commands still work:

    cd ~/my-project && agentalloy wire
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentalloy.install import PROXY_UNABLE_HARNESSES
from agentalloy.install import state as install_state
from agentalloy.install.subcommands import (
    detect,
    enable_service,
    install_packs,
    preflight,
    pull_models,
    seed_corpus,
    start_embed_server,
    verify,
    wire_harness,
    write_env,
)
from agentalloy.install.subcommands.wire_harness import VALID_HARNESSES

try:
    from rich.console import Console  # type: ignore[import-untyped]

    console: Console | None = Console(force_terminal=True, soft_wrap=True)  # type: ignore[assignment]
except ImportError:
    console = None  # type: ignore[assignment]


def _print(*args: Any, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
    """Print with Rich if available, plain stdout otherwise."""
    if console is not None:
        console.print(*args, **kwargs)  # type: ignore[union-attr, arg-type]
    else:
        print(*args, **kwargs)


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
    compose_binary: str = ""  # "podman compose" | "docker compose"
    compose_file: str = ""  # abs path to compose yaml used

    # Upstream LLM (proxy target)
    upstream_url: str = "http://localhost:2099/v1"
    upstream_model: str = ""
    upstream_api_key: str = ""

    # Resolved during execution -- not user-facing.
    detected_runner: str | None = None  # from detect.json (e.g. "ollama", "llama-server")
    recommended_host: str | None = None  # from recommend-host-targets.json
    models_output: dict[str, Any] = field(default_factory=dict)  # type: ignore[type-arg]


_MODEL_DEFAULTS: dict[str, str] = {
    "ollama": "qwen3-embedding:0.6b",
    "lm-studio": "Qwen3-Embedding-0.6B-Q8_0.gguf",
    "llama-server": "Qwen3-Embedding-0.6B-Q8_0.gguf",
}

# Human-readable labels for hardware targets
_HW_LABELS: dict[str, str] = {
    "cpu": "CPU (RAM-only)",
    "nvidia": "NVIDIA GPU (CUDA)",
    "radeon": "AMD GPU (Vulkan/ROCm)",
    "apple-silicon": "Apple Silicon (Metal)",
}


# Map (runner, hardware_target) -> write_env preset name.
_PRESET_MAP: dict[tuple[str, str], str] = {
    ("ollama", "cpu"): "cpu",
    ("ollama", "apple-silicon"): "apple-silicon",
    ("ollama", "nvidia"): "nvidia",
    ("ollama", "radeon"): "radeon",
    ("lm-studio", "cpu"): "cpu-lm-studio",
    ("lm-studio", "apple-silicon"): "apple-silicon-lm-studio",
    ("lm-studio", "nvidia"): "nvidia-lm-studio",
    ("lm-studio", "radeon"): "radeon-lm-studio",
    ("llama-server", "cpu"): "cpu-llama-server",
    ("llama-server", "apple-silicon"): "apple-silicon-llama-server",
    ("llama-server", "nvidia"): "nvidia-llama-server",
    ("llama-server", "radeon"): "radeon-llama-server",
}


def _resolve_preset(cfg: SetupConfig) -> str:
    """Resolve the write-env preset from runner + hardware target.

    Uses the user's explicit hardware_target if set, otherwise falls back to
    the auto-detected recommended_host. Falls back to "cpu" if the combination
    is unknown.
    """
    runner = cfg.runner or "ollama"  # runner should be finalized before this is called
    hw = cfg.hardware_target or cfg.recommended_host or "cpu"
    key = (runner, hw)
    preset = _PRESET_MAP.get(key)
    if preset is None:
        _print(f"  [dim]Warning: no preset for ({runner}, {hw}), falling back to cpu.[/dim]")
        preset = {"ollama": "cpu", "lm-studio": "cpu-lm-studio"}.get(runner, "cpu-llama-server")
    cfg.preset = preset
    return preset


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


def _prompt_runner() -> str:
    return _prompt_numbered(
        "Select inference runner:",
        [
            ("ollama", "Ollama"),
            ("lm-studio", "LM Studio"),
            ("llama-server", "llama-server (llama.cpp)"),
        ],
        default_index=1,
    )


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


_HARNESS_OPTIONS: list[tuple[str, str]] = [
    ("claude-code", "Claude Code CLI (Anthropic)"),
    ("gemini-cli", "Gemini CLI (Google)"),
    ("cursor", "Cursor IDE"),
    ("windsurf", "Windsurf IDE"),
    ("github-copilot", "GitHub Copilot (VS Code)"),
    ("hermes-agent", "Hermes Agent"),
    ("continue-closed", "Continue.dev extension"),
    ("opencode", "OpenCode (with local LLM)"),
    ("aider", "Aider"),
    ("cline", "Cline"),
    ("manual", "manual — skip (configure later)"),
]


def _prompt_harness() -> str:
    # Default is "manual" — the last entry.
    default_index = len(_HARNESS_OPTIONS)
    return _prompt_numbered(
        "Select IDE harness:",
        _HARNESS_OPTIONS,
        default_index=default_index,
    )


def _prompt_deployment() -> str:
    """Prompt for deployment type: native or container.

    Default is "container" (index 2) as it is the recommended option
    for new installs.
    """
    return _prompt_numbered(
        "Select deployment type:",
        [
            ("native", "Native  — runs directly on this host (systemd or manual)"),
            (
                "container",
                "Container — managed by podman/docker compose (recommended for new installs)",
            ),
        ],
        default_index=2,
    )


def _discover_packs() -> dict[str, dict[str, Any]]:
    """Discover available packs from the _packs directory."""
    try:
        from pathlib import Path

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
      1. NVIDIA discrete GPU  →  "nvidia"
      2. AMD discrete GPU     →  "radeon"
      3. Apple integrated GPU →  "apple-silicon"
      4. Fallback             →  "cpu"
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
    # Apple Silicon (integrated on Mac)
    for card in integrated:
        if str(card.get("vendor") or "").lower() == "apple":
            return "apple-silicon"
    return "cpu"


def _prompt_upstream(cfg: SetupConfig) -> None:
    """Interactive prompts to capture upstream LLM configuration."""
    _print("\n  [bold]Upstream LLM (proxy target)[/bold]")
    _print("  [dim]The AgentAlloy proxy forwards requests to this LLM.[/dim]")

    cfg.upstream_url = _prompt_context(
        "  Upstream URL",
        "  Base URL of the upstream LLM (e.g. http://localhost:2099/v1)",
        default=cfg.upstream_url or "http://localhost:2099/v1",
    )
    cfg.upstream_model = _prompt_context(
        "  Upstream model",
        "  Model name to pass to the upstream LLM (e.g. qwen3-14b)",
        default=cfg.upstream_model or "",
    )
    cfg.upstream_api_key = _prompt_context(
        "  Upstream API key",
        "  API key for the upstream LLM (leave blank for local runners)",
        default=cfg.upstream_api_key or "",
    )


def _test_upstream_endpoint(cfg: SetupConfig) -> bool:
    """Validate the upstream LLM connection by hitting /v1/models.

    Returns True if the endpoint responds successfully, False otherwise.
    A missing or empty api key is accepted (local runners may not require one).
    """
    import urllib.request as _urllib_request

    url = (cfg.upstream_url or "").rstrip("/")
    if not url:
        _print("  [yellow]No upstream URL set — skipping validation.[/yellow]")
        return False

    models_url = f"{url}/models"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.upstream_api_key:
        headers["Authorization"] = f"Bearer {cfg.upstream_api_key}"

    req = _urllib_request.Request(models_url, headers=headers, method="GET")
    try:
        with _urllib_request.urlopen(req, timeout=10) as resp:  # noqa: S310
            if resp.status == 200:
                _print(f"  [green]Upstream LLM reachable at {models_url}[/green]")
                return True
            _print(f"  [yellow]Upstream returned HTTP {resp.status} — continuing anyway.[/yellow]")
            return False
    except Exception as exc:
        _print(f"  [yellow]Upstream LLM not reachable ({exc}) — continuing anyway.[/yellow]")
        _print(f"  [dim]Start the upstream LLM and verify: curl {models_url}[/dim]")
        return False


def _write_upstream_env(cfg: SetupConfig) -> None:
    """Append/update upstream LLM vars in the existing .env file.

    Reads the current .env, removes any existing UPSTREAM_* lines, then
    appends the three upstream vars. This is idempotent and safe to call
    multiple times.
    """
    env_fp = install_state.env_path()
    existing = env_fp.read_text(encoding="utf-8") if env_fp.exists() else ""

    # Remove any existing upstream lines
    filtered_lines = [
        line
        for line in existing.splitlines()
        if not line.startswith(("UPSTREAM_URL=", "UPSTREAM_MODEL=", "UPSTREAM_API_KEY="))
    ]

    # Append the three upstream vars
    filtered_lines.append(f"UPSTREAM_URL={cfg.upstream_url}")
    filtered_lines.append(f"UPSTREAM_MODEL={cfg.upstream_model}")
    filtered_lines.append(f"UPSTREAM_API_KEY={cfg.upstream_api_key}")
    filtered_lines.append("")  # trailing newline

    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
        env_fp, "\n".join(filtered_lines)
    )


def _test_embed_endpoint(cfg: SetupConfig) -> None:
    """Smoke test: send a real embedding request and show the curl equivalent."""
    import urllib.request as _urllib_request

    # Read .env values for the embed endpoint
    env_path = install_state.env_path()
    embed_url = None
    embed_model = None
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("RUNTIME_EMBED_BASE_URL="):
                embed_url = line.split("=", 1)[1].strip()
            elif line.startswith("RUNTIME_EMBEDDING_MODEL="):
                embed_model = line.split("=", 1)[1].strip()

    if not embed_url or not embed_model:
        _print("  [yellow]Could not read embed URL/model from .env -- skipping test.[/yellow]")
        return

    test_text = "test embedding for setup verification"
    payload = json.dumps({"model": embed_model, "input": test_text}).encode()
    req = _urllib_request.Request(
        f"{embed_url}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with _urllib_request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            dim = len(data["data"][0]["embedding"])
            _print(f"  Embedding test: [green]OK[/green] -- {dim}-dim vector returned")
            # Show curl command for user reference
            _print("")
            _print("  Verify manually:")
            _print(f"  curl -s {embed_url}/v1/embeddings \\")
            _print("    -H 'Content-Type: application/json' \\")
            _print(f'    -d \'{{"model":"{embed_model}","input":"hello"}}\'')
    except Exception as exc:
        _print(f"  [yellow]Embedding test failed: {exc}[/yellow]")
        _print(
            f"  [dim]The embed server may still start up; "
            f"check {install_state.user_data_dir() / 'logs' / 'embed-server.log'}[/dim]"
        )


def _run_container_flow(cfg: SetupConfig, t0: float) -> int:
    """Execute the container deployment flow.

    Skips native prompts (runner, model, hardware, port, mode, packs).
    Validates container prerequisites, runs compose up, and validates.
    """
    from agentalloy.install.subcommands.preflight import (  # noqa: PLC0415
        _detect_compose_binary,  # pyright: ignore[reportPrivateUsage]
    )

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

    # 2. Detect compose binary (standalone, before compose file selection)
    label, binary_path = _detect_compose_binary()
    if label is None:
        _print("  [red]No compose binary found (podman or docker).[/red]")
        _print(
            "  Install Podman (recommended) or Docker and ensure `compose`\n"
            "  is available:\n"
            "    Linux:  sudo apt install podman\n"
            "    macOS:  brew install podman"
        )
        return 1
    cfg.compose_binary = label
    assert binary_path is not None  # _detect_compose_binary returns both or neither
    _print(f"  Compose binary: {label} at {binary_path}")

    # 3. Select compose file
    default_compose = "compose.radeon.yaml" if cfg.recommended_host == "radeon" else "compose.yaml"

    compose_path = Path.cwd() / default_compose
    if not cfg.non_interactive:
        _print(f"\n  Detected compose file: {default_compose} — correct? [Y/n]")
        ans = input("  ").strip().lower()
        if ans in ("n", "no"):
            custom = input("  Enter compose file path: ").strip()
            compose_path = Path(custom).resolve()
    cfg.compose_file = str(compose_path.resolve())

    # 4. Run container preflight
    _print("  [dim]-> Preflight (container)[/dim]")
    container_preflight = preflight.run_preflight(phase="container", compose_file=cfg.compose_file)
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

    # 5. Set fixed values
    cfg.port = 47950
    cfg.deployment = "container"

    # 6. Show summary
    _print("\n[dim]" + "─" * 40)
    _print("\n[bold]Review your container setup:[/bold]")
    _print("  Deployment:   container")
    _print(f"  Compose file: {cfg.compose_file}")
    _print(f"  Compose binary: {cfg.compose_binary}")
    _print(f"  Port:         {cfg.port}")

    if not cfg.non_interactive:
        confirm = input("  Confirm and continue? [Y/n]: ").strip().lower()
        if confirm not in ("", "y", "yes"):
            _print("[yellow]Setup cancelled.[/yellow]")
            return 1
    _print()

    # 7. Execute: compose up -d --build
    _print("[bold]Running container setup...[/bold]")
    _print("  [dim]-> Starting containers[/dim]")
    cmd = [binary_path, "compose", "-f", cfg.compose_file, "up", "-d", "--build"]
    _print(f"  $ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
            timeout=600,
        )
        if result.returncode != 0:
            _print("  [red]  compose up failed.[/red]")
            return result.returncode
    except subprocess.TimeoutExpired:
        _print("  [red]  compose up timed out (10 min).[/red]")
        return 1
    _print("  [green]  Done.[/green]")

    # 8. Poll health endpoint
    _print("  [dim]-> Waiting for service health...[/dim]")
    healthy = False
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://localhost:{cfg.port}/health", timeout=5
            ) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except Exception:
            pass
        time.sleep(5)
    if not healthy:
        _print("  [yellow]  Service not healthy after 120s — check container logs.[/yellow]")
    else:
        _print("  [green]  Service healthy.[/green]")

    # 9. Record state + write .env (before verify so it reads fresh values)
    st = install_state.load_state()
    st["deployment"] = "container"
    st["compose_file"] = cfg.compose_file
    st["compose_binary"] = cfg.compose_binary
    st["compose_binary_path"] = binary_path
    st["port"] = cfg.port
    install_state.save_state(st)

    # Write minimal .env for container defaults so verify and embed checks work
    env_dir = install_state.user_config_dir()
    env_dir.mkdir(parents=True, exist_ok=True)
    env_fp = install_state.env_path()
    env_lines = [
        f"RUNTIME_EMBED_BASE_URL=http://localhost:{cfg.port}",
        'RUNTIME_EMBEDDING_MODEL=""',
        f"RUNTIME_PORT={cfg.port}",
    ]
    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
        env_fp, "\n".join(env_lines) + "\n"
    )

    # 10. Run verify
    _print("  [dim]-> Verifying installation[/dim]")
    rc = verify.run(_build_namespace(cfg))
    if rc not in (0, 4):
        _print("  [red]Validation failed.[/red]")
        return rc
    _print("  [green]  All checks passed.[/green]")

    # 11. Wire harness
    if not cfg.non_interactive:
        cfg.harness = _prompt_harness()
    else:
        h = (cfg.harness or "manual").strip().lower()
        if h == "continue":
            h = "continue-closed"
        cfg.harness = h

    if cfg.harness and cfg.harness != "manual":
        _print(f"  [dim]-> Wiring harness ({cfg.harness})[/dim]")
        # Sidecar harnesses (Cursor, Windsurf, etc.) can't be proxy-wired —
        # use legacy markdown-injection so we don't write a misleading
        # proxy-instruction.md file that claims traffic flows through the proxy.
        # Uses PROXY_UNABLE_HARNESSES from agentalloy.install
        rc = wire_harness.run(
            _build_namespace(
                cfg,
                harness=cfg.harness,
                force=False,
                legacy=cfg.harness in PROXY_UNABLE_HARNESSES,
            )
        )
        if rc not in (0, 4):
            _print(f"  [red]  wire-harness failed (exit {rc}).[/red]")
            return rc
        _print("  [green]  Done.[/green]")

    # -- Done --
    _print(
        f"\n[green]  Container setup complete in {int((time.monotonic() - t0) * 1000)}ms[/green]\n"
    )
    _print(f"  URL:      http://localhost:{cfg.port}")
    _print(f"  Compose:  {cfg.compose_file}")
    _print(f"  Logs:     {cfg.compose_binary.split()[0]} compose -f {cfg.compose_file} logs -f")

    _print(
        f"\n  [bold]Stop:[/bold] {cfg.compose_binary.split()[0]} compose -f {cfg.compose_file} down"
    )
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
    from agentalloy.install.__main__ import EXIT_NOOP

    t0 = time.monotonic()

    # -- Profile detection and refuse-if-existing check --
    try:
        from agentalloy.profiles import (
            _ensure_profile_dir,  # pyright: ignore[reportPrivateUsage]
            detect_profile,
        )

        _ensure_profile_dir("default")  # pyright: ignore[reportPrivateUsage]
        active_profile = detect_profile()
        ds_path = active_profile.datastore_path

        if ds_path.exists() and not getattr(cfg, "force", False):
            try:
                import duckdb

                con = duckdb.connect(str(ds_path), read_only=True)
                has_skills = (
                    con.execute("SELECT 1 FROM profile_skills LIMIT 1").fetchone() is not None
                )
                con.close()
            except Exception:
                has_skills = False

            if has_skills:
                _print(
                    f"\n[yellow]AgentAlloy is already initialized for profile "
                    f"'{active_profile.name}' (datastore: {ds_path}). "
                    f"Use 'agentalloy update' to refresh defaults or "
                    f"'agentalloy reset' to wipe and reinstall.[/yellow]"
                )
                return EXIT_NOOP
    except ImportError:
        active_profile = None  # type: ignore[assignment]

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

    if cfg.deployment == "container":
        return _run_container_flow(cfg, t0)

    # -- Phase 1: Gather config --

    _print("\n[bold]agentalloy setup[/bold]\n")

    # 1. Runner
    if cfg.runner is None and not cfg.non_interactive:
        cfg.runner = _prompt_runner()
    elif cfg.runner is None:
        cfg.runner = "ollama"
    cfg.runner = cfg.runner.strip().lower()
    if cfg.runner not in ("ollama", "lm-studio", "llama-server"):
        _print(
            f"  [red]Invalid runner: {cfg.runner}. Choose ollama, lm-studio, or llama-server.[/red]"
        )
        return 1
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

    # 3. Model (default varies by runner)
    default_model = _MODEL_DEFAULTS.get(cfg.runner, "qwen3-embedding:0.6b")
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

    # 7. Harness
    if not cfg.non_interactive:
        cfg.harness = _prompt_harness()
    else:
        h = (cfg.harness or "manual").strip().lower()
        if h == "continue":
            h = "continue-closed"
        cfg.harness = h

    if cfg.harness not in VALID_HARNESSES:
        _print(
            f"  [red]Invalid harness: {cfg.harness}. "
            f"Choices: {', '.join(sorted(VALID_HARNESSES))}[/red]"
        )
        return 1
    _print(f"  Harness: {cfg.harness}")

    # Sidecar harness guardrail: these harnesses can't be proxy-wired (they
    # don't honor base-URL overrides), so AgentAlloy falls back to a static
    # rules file kept current by a watcher. System-skill gating degrades to
    # advisory. Non-interactive installs must explicitly acknowledge that with
    # --acknowledge-sidecar; interactive installs get a y/n prompt.
    # Uses PROXY_UNABLE_HARNESSES from agentalloy.install
    if cfg.harness in PROXY_UNABLE_HARNESSES:
        sidecar_msg = (
            f"\n  [yellow]Sidecar harness selected: {cfg.harness}[/yellow]\n"
            "  This harness cannot be proxy-wired (it does not honor OpenAI/Anthropic\n"
            "  base-URL overrides). AgentAlloy falls back to a static rules file kept\n"
            "  current by a file-watching sidecar. System skill enforcement is\n"
            "  advisory-only; phase transitions require the watcher to be running.\n"
            "  See docs/sidecar-experience.md for the full picture."
        )
        if cfg.non_interactive:
            if not cfg.acknowledge_sidecar:
                _print(sidecar_msg)
                _print("  [red]Non-interactive sidecar setup requires --acknowledge-sidecar.[/red]")
                return 1
        else:
            _print(sidecar_msg)
            ans = _prompt_context("  Continue with sidecar harness?", "y/n", default="n")
            if (ans or "n").strip().lower() != "y":
                _print("  [yellow]Setup cancelled.[/yellow]")
                return 0

    # Resolve preset from explicit choices (after all user input)
    preset = _resolve_preset(cfg)
    # Preset is an internal write-env detail; not shown to the user.

    # 8. Upstream LLM
    if not cfg.non_interactive:
        _prompt_upstream(cfg)
    # In non-interactive mode, upstream_url/model/api_key come from SetupConfig defaults
    # (which may be pre-set by the caller). We don't require them to be set — the proxy
    # can be configured later via env vars.
    _print(f"  Upstream URL:   {cfg.upstream_url or '(not set)'}")
    _print(f"  Upstream model: {cfg.upstream_model or '(not set)'}")

    # -- Phase 2: Summary confirmation --

    _print("\n[dim]" + "─" * 40)
    _print("\n[bold]Review your choices:[/bold]")
    _print(f"  Runner:     {cfg.runner}")
    _print(f"  Model:      {cfg.model}")
    _print(f"  Port:       {cfg.port}")
    _print(f"  Mode:       {cfg.mode}")
    _print(f"  Packs:      {cfg.packs or '(always-on only)'}")
    _print(f"  Harness:    {cfg.harness}")

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

    # Step b: Preflight (runner)
    _print("  [dim]-> Preflight (runner)[/dim]")
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

    # Step c2: Write upstream LLM vars to .env
    _print("  [dim]-> Writing upstream LLM config[/dim]")
    _write_upstream_env(cfg)
    _print("  [green]  Done.[/green]")

    # Step d: Pull model
    _print("  [dim]-> Pulling model[/dim]")
    # Build a minimal recommend-models.json for pull_models to consume.
    # pull_models.pull_models() reads models_json["options"], where each entry
    # must have "embed_model" and "embed_runner" keys (not "models"/"name").
    models_json = {
        "schema_version": 1,
        "preset": preset,
        "selected_runner": cfg.runner,
        "options": [
            {
                "default": True,
                "embed_model": cfg.model,
                "embed_runner": cfg.runner,
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

    # Step f: Start embed server
    _print("  [dim]-> Starting embed server[/dim]")
    rc = start_embed_server.run(_build_namespace(cfg, models=str(models_fp), timeout=120.0))
    if rc not in (0, 4):
        _print(f"  [red]  start-embed-server failed (exit {rc}).[/red]")
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

    # Step h: Enable service
    _print("  [dim]-> Enabling service[/dim]")
    mode_flag = "native" if cfg.mode == "persistent" else "manual"
    rc = enable_service.run(_build_namespace(cfg, mode=mode_flag, runtime=None, port=cfg.port))
    if rc not in (0, 4):
        _print(f"  [red]  enable-service failed (exit {rc}).[/red]")
        return rc
    _print("  [green]  Done.[/green]")

    # Step i: Wire harness (if requested)
    if cfg.harness and cfg.harness != "manual":
        _print(f"  [dim]-> Wiring harness ({cfg.harness})[/dim]")
        # Sidecar harnesses (Cursor, Windsurf, etc.) can't be proxy-wired —
        # use legacy markdown-injection so we don't write a misleading
        # proxy-instruction.md file that claims traffic flows through the proxy.
        # Uses PROXY_UNABLE_HARNESSES from agentalloy.install
        rc = wire_harness.run(
            _build_namespace(
                cfg,
                harness=cfg.harness,
                force=False,
                legacy=cfg.harness in PROXY_UNABLE_HARNESSES,
            )
        )
        if rc not in (0, 4):
            _print(f"  [red]  wire-harness failed (exit {rc}).[/red]")
            return rc
        _print("  [green]  Done.[/green]")

    # -- Phase 4: Validate --

    _print("\n[bold]Validating installation...[/bold]")
    rc = verify.run(_build_namespace(cfg))
    if rc not in (0, 4):
        _print("  [red]Validation failed.[/red]")
        return rc
    _print("  [green]All checks passed.[/green]")

    # Embedding endpoint smoke test
    _print("\n[dim]Testing embed endpoint...[/dim]")
    _test_embed_endpoint(cfg)

    # Upstream LLM connectivity check (non-blocking)
    if cfg.upstream_url:
        _print("\n[dim]Testing upstream LLM endpoint...[/dim]")
        _test_upstream_endpoint(cfg)

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
        choices=["ollama", "lm-studio", "llama-server"],
        default=None,
        help="Embedding runner (default: ollama).",
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
    p.set_defaults(func=_run_from_args)


def _run_from_args(args: argparse.Namespace) -> int:
    """Bridge from argparse.Namespace to SetupConfig -> run_setup()."""
    cfg = SetupConfig(
        runner=args.runner,  # may be None; resolved inside run_setup
        model=args.model or "",
        port=args.port or 47950,
        mode=args.mode or "persistent",
        packs=args.packs or "",
        harness=args.harness or "manual",
        hardware_target=getattr(args, "hardware", None) or "",
        deployment=getattr(args, "deployment", None) or "",
        non_interactive=args.non_interactive,
        force=getattr(args, "force", False),
        acknowledge_sidecar=getattr(args, "acknowledge_sidecar", False),
    )
    # Model default is resolved inside run_setup() after cfg.runner is finalized.
    return run_setup(cfg)
