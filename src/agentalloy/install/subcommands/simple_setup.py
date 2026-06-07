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
import shutil
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

    # Container runtime fields (used when deployment="container")
    runtime_binary: str = ""  # resolved path to container runtime (podman/docker)
    image_tag: str = "agentalloy:local"  # container image tag
    container_name: str = "agentalloy"  # base name for containers
    data_volume: str = "agentalloy-data"  # named volume for persistent data

    # Upstream LLM (proxy target)
    upstream_url: str = ""
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


def _prompt_upstream(cfg: SetupConfig) -> None:
    """Interactive prompts to capture upstream LLM configuration."""
    _print("\n  [bold]Upstream LLM (proxy target)[/bold]")
    _print("  [dim]The AgentAlloy proxy forwards requests to this LLM.[/dim]")

    cfg.upstream_url = _prompt_context(
        "  Upstream URL",
        "  Base URL of the upstream LLM (e.g. http://localhost:11434/v1 for Ollama, https://api.openai.com/v1 for OpenAI)",
        default=cfg.upstream_url or "",
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
    url = (cfg.upstream_url or "").rstrip("/")
    if not url:
        _print("  [yellow]No upstream URL set — skipping validation.[/yellow]")
        return False

    models_url = f"{url}/models"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.upstream_api_key:
        headers["Authorization"] = f"Bearer {cfg.upstream_api_key}"

    req = urllib.request.Request(models_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
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

    # Capture original .env content for backup/restore (only on first call)
    original_content = None
    if env_fp.exists():
        original_content = env_fp.read_text()

    # Persist to state if this is the first backup
    if original_content is not None:
        st = install_state.load_state()
        if st.get("env_original_content") is None:
            st["env_original_content"] = original_content
            install_state.save_state(st)

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


def _inspect_ollama_project(binary_path: str) -> tuple[str, str]:
    """Return (compose_project, network_name) inferred from the running
    agentalloy-ollama container.

    podman-compose names the default network ``{project}_default`` where
    ``project`` defaults to the compose-file dir basename or
    ``COMPOSE_PROJECT_NAME``. Hardcoding ``agentalloy_default`` breaks when
    the user clones into a differently-named dir. Instead, ask podman for
    the truth: ollama is already up by the time we hit step 9, and its
    labels + network attachments carry the actual project name.

    Falls back to ``("agentalloy", "agentalloy_default")`` if inspection
    fails — matches the previous hardcoded behavior so we never block setup
    on a missing field, just degrade gracefully.
    """
    fallback = ("agentalloy", "agentalloy_default")
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, binary_path from shutil.which
            [
                binary_path,
                "inspect",
                "agentalloy-ollama",
                "--format",
                '{{ index .Config.Labels "com.docker.compose.project" }}'
                "\t"
                "{{ range $k, $_ := .NetworkSettings.Networks }}{{ $k }}\n{{ end }}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return fallback
    if result.returncode != 0:
        return fallback
    raw = result.stdout.strip()
    if not raw or "\t" not in raw:
        return fallback
    project, _, networks_blob = raw.partition("\t")
    project = project.strip() or fallback[0]
    networks = [n.strip() for n in networks_blob.splitlines() if n.strip()]
    # Prefer the default network for this project; fall back to first attached.
    target = f"{project}_default"
    network = target if target in networks else (networks[0] if networks else fallback[1])
    return (project, network)


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


def _run_container_flow(cfg: SetupConfig, t0: float) -> int:
    """Execute the container deployment flow.

    Skips native prompts (runner, model, hardware, port, mode, packs).
    Validates container prerequisites, runs compose up, and validates.
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
    from agentalloy.install.subcommands.container_runtime import (  # noqa: PLC0415
        _build_image,
        _cleanup_temp_entrypoint,
        _detect_runtime_binary,
        _ensure_ollama_dir,
        _ensure_volume,
        _generate_entrypoint,
        _run_container,
        _wait_for_readiness,
    )

    label = _detect_runtime_binary()
    if label is None:
        _print(
            "  [red]Neither `podman` nor `docker` found on PATH.[/red]\n"
            "  Install Podman (recommended) or Docker:\n"
            "    Linux:   sudo apt install podman\n"
            "    macOS:   brew install podman\n"
            "  Verify:  podman --version"
        )
        return 1
    binary_path = shutil.which(label)
    assert binary_path is not None, (
        f"{label} not found on PATH despite _detect_runtime_binary returning it"
    )
    cfg.runtime_binary = label
    _print(f"  Runtime binary: {label} at {binary_path}")

    # 2b. Container = CPU-only, on every host. GPU passthrough is intentionally
    # out of scope: nvidia needs nvidia-container-toolkit + deploy.resources,
    # AMD needs ROCm device mounts + a ROCm Ollama image, and Docker Desktop
    # on macOS cannot pass Metal through at all. Users who want GPU should
    # choose the native install. The bundled Ollama sidecar handles inference
    # on CPU using the qwen3-embedding:0.6b model — functional for embeddings
    # but slower than GPU.
    _print(
        "\n  [yellow]Note — container deployment is CPU-only on every host.[/yellow]\n"
        "  GPU acceleration (NVIDIA/AMD/Apple Metal) only works with a native\n"
        "  install. The bundled Ollama runs on CPU; for a 600M embedding model\n"
        "  on short text this is functional but noticeably slower than GPU.\n"
        "  If you want GPU acceleration, cancel and re-run setup choosing the\n"
        "  native deployment."
    )
    if not cfg.non_interactive:
        ans = input("  Continue with container (CPU-only)? [Y/n]: ").strip().lower()
        if ans in ("n", "no"):
            _print("[yellow]Setup cancelled.[/yellow]")
            return 1

    # 3. Select compose file
    # The Containerfile build context needs the full repo (pyproject.toml,
    # uv.lock, src/, data/), so container deployment requires a checkout on
    # disk. Search order:
    #   1. cwd (user ran setup from inside the clone)
    #   2. parents[4] of __file__ (editable install — points at repo root)
    #   3. fall back to cloning into ~/.cache/agentalloy/repo so users who
    #      installed via `uv tool install agentalloy` don't have to clone
    #      manually. Pinned to `main` for now; revisit when we tag releases.
    default_compose = "compose.yaml"

    def _has_assets(d: Path) -> bool:
        # Match _check_image_build_deps in preflight.py: Containerfile OR Dockerfile.
        has_build_file = (d / "Containerfile").exists() or (d / "Dockerfile").exists()
        return (d / default_compose).exists() and has_build_file

    def _resolve_user_path(raw: str) -> Path:
        """Accept either a directory (append default_compose) or a compose file path."""
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            return p / default_compose
        return p

    def _ensure_cached_repo() -> Path | None:
        """Clone (or refresh) the agentalloy repo into ~/.cache/agentalloy/repo.

        Returns the cache dir on success, None on failure. Uses --depth=1 so the
        clone is fast (~few MB). On refresh, hard-resets to origin/main so any
        local edits or stale state in the cache don't break the build context.
        """
        cache_dir = Path.home() / ".cache" / "agentalloy" / "repo"
        if shutil.which("git") is None:
            _print(
                "  [red]git not found on PATH — cannot clone the agentalloy repo "
                "for the build context.[/red]"
            )
            return None
        repo_url = "https://github.com/nrmeyers/agentalloy.git"
        # If the cache dir exists but isn't a valid git checkout (no .git/
        # — possibly a partial clone, leftover files, or a manually-placed
        # directory), `git clone <url> <dest>` would fail with "destination
        # path already exists and is not an empty directory". Nuke it so
        # the clone branch below can recreate cleanly.
        if cache_dir.exists() and not (cache_dir / ".git").exists():
            _print(
                f"  [yellow]-> Cache dir {cache_dir} exists but isn't a git "
                "checkout; recreating.[/yellow]"
            )
            try:
                shutil.rmtree(cache_dir)
            except OSError as exc:
                _print(f"  [red]Could not remove stale cache dir: {exc}[/red]")
                return None
        try:
            if (cache_dir / ".git").exists():
                _print(f"  [dim]-> Refreshing cached repo at {cache_dir}[/dim]")
                subprocess.run(
                    ["git", "-C", str(cache_dir), "fetch", "--depth=1", "origin", "main"],
                    check=True,
                    timeout=120,
                )
                subprocess.run(
                    ["git", "-C", str(cache_dir), "reset", "--hard", "origin/main"],
                    check=True,
                    timeout=60,
                )
            else:
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                _print(f"  [dim]-> Cloning {repo_url} into {cache_dir}[/dim]")
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "--depth=1",
                        "--branch=main",
                        repo_url,
                        str(cache_dir),
                    ],
                    check=True,
                    timeout=180,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            _print(f"  [red]git clone/fetch failed: {exc}[/red]")
            return None
        if not _has_assets(cache_dir):
            _print(
                f"  [red]Cached repo at {cache_dir} is missing {default_compose} "
                "or Containerfile after clone.[/red]"
            )
            return None
        return cache_dir

    candidates = [Path.cwd(), Path(__file__).resolve().parents[4]]
    compose_path: Path | None = None
    for cand in candidates:
        if _has_assets(cand):
            compose_path = cand / default_compose
            break

    if compose_path is None:
        cached = _ensure_cached_repo()
        if cached is not None:
            compose_path = cached / default_compose

    if compose_path is None:
        if cfg.non_interactive:
            _print(
                "  [red]Could not locate or fetch the agentalloy repo.[/red]\n"
                f"  Looked for {default_compose} + (Containerfile or Dockerfile) in:\n"
                + "\n".join(f"    - {c}" for c in candidates)
                + "\n  Auto-clone fallback also failed (see error above).\n"
                "  Container deployment requires a checkout (the build context\n"
                "  needs pyproject.toml, src/, data/). Either:\n"
                "    a) cd into your agentalloy clone and re-run setup, or\n"
                "    b) install editably: `git clone … && cd agentalloy && \n"
                "       uv tool install --editable .`"
            )
            return 1
        _print(
            "  [yellow]Could not auto-locate or fetch the agentalloy repo.[/yellow] "
            "Enter the\n"
            "  path to your agentalloy clone (or directly to a compose YAML):"
        )
        custom = input("  ").strip()
        compose_path = _resolve_user_path(custom)
    elif not cfg.non_interactive:
        _print(f"\n  Detected compose file: {compose_path} — correct? [Y/n]")
        ans = input("  ").strip().lower()
        if ans in ("n", "no"):
            custom = input("  Enter compose file path (or repo dir): ").strip()
            compose_path = _resolve_user_path(custom)
    cfg.image_tag = "agentalloy:local"

    # 4. Run container preflight
    _print("  [dim]-> Preflight (container)[/dim]")
    container_preflight = preflight.run_preflight(
        phase="container",
        build_context=str(compose_path.parent),
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
    cfg.runner = "ollama"
    cfg.port = 47950
    cfg.mode = "manual"
    cfg.harness = "manual"
    cfg.deployment = "container"

    # 5b. Skill pack selection. Mirrors the native flow at step 6
    # (simple_setup.py:1696). Done here — after all preflight, before
    # any compose work — so a Ctrl-C costs nothing and the chosen packs
    # show up in the review summary below. Selection is threaded into
    # the one-shot install-packs container at step 8b via --packs.
    if not cfg.non_interactive and not cfg.packs:
        cfg.packs = _prompt_for_packs()
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

    # 6. Show summary
    _print("\n[dim]" + "─" * 40)
    _print("\n[bold]Review your container setup:[/bold]")
    _print("  Deployment:   container")
    _print(f"  Runtime:      {cfg.runtime_binary}")
    _print(f"  Image:        {cfg.image_tag}")
    _print(f"  Port:         {cfg.port}")
    _print(f"  Packs:        {cfg.packs or '(always-on only)'}")

    if not cfg.non_interactive:
        confirm = input("  Confirm and continue? [Y/n]: ").strip().lower()
        if confirm not in ("", "y", "yes"):
            _print("[yellow]Setup cancelled.[/yellow]")
            return 1
    _print()

    # 6.5. Check for stale containers from a prior project run. podman-compose
    # papers over "name already in use" errors by silently `podman start`ing
    # the existing container — which "succeeds" but the container immediately
    # re-exits with its old exit code, and the wizard then bails with a
    # confusing "init exited 1" message. Surface this up front and let the
    # user remove them. We filter by the project label so we don't touch
    # other agentalloy:local containers (e.g. from a parallel checkout).
    existing = _list_project_containers(binary_path)
    if existing:
        _print("[bold]Existing AgentAlloy containers detected:[/bold]")
        for name, status in existing:
            _print(f"  - {name}  [dim]({status})[/dim]")
        _print(
            f"  [dim]{cfg.runtime_binary} will misbehave if these stay around "
            "(name collisions, stale exit codes, dangling dependency graphs).[/dim]"
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
    #   2. Start Ollama and wait for it to be healthy
    #   3. Pull the embedding model  (ollama-pull equivalent)
    #   4. Install skill packs       (if cfg.packs is non-empty)
    #   5. Start uvicorn
    #
    # This replaces the old multi-container compose model
    # (agentalloy-init, ollama, ollama-pull, agentalloy) with a single
    # container.  The entrypoint script's sequential flow (set -e) ensures
    # no race conditions between steps — migrations finish before ollama
    # starts, ollama is healthy before the model is pulled, the model is
    # cached before packs are installed, and uvicorn only starts after all
    # bootstrap steps succeed.
    log_path = _container_setup_log_path()
    _print("[bold]Running container setup...[/bold]")
    _print(f"  [dim]Full setup log: {log_path}[/dim]")

    # 7a. Build the agentalloy image (or use existing).
    _print("  [dim]-> Building container image (1-2 min)...[/dim]")
    build_ctx = compose_path.parent  # build context is the compose file's dir
    build_rc = _build_image(binary_path, build_ctx)
    if build_rc != 0:
        return build_rc

    # 7b. Ensure the agentalloy-data volume exists.
    _ensure_volume(binary_path)

    # 7c. Ensure ~/.ollama exists on the host (bind-mounted into the container).
    _ensure_ollama_dir()

    # 7d. Generate the entrypoint script and start the container.
    _print("  [dim]-> Starting agentalloy container (1-3 min)...[/dim]")
    entrypoint = _generate_entrypoint(cfg.packs)
    rc = _run_container(binary_path, entrypoint, cfg.packs)
    if rc != 0:
        return rc
    _cleanup_temp_entrypoint(entrypoint)
    _print("  [green]  Done.[/green]")

    # 10. Wait for container readiness (fast-start uvicorn serves /readiness
    # while pack ingest runs in the background). All-packs gets 1800s; any
    # explicit pack subset gets 300s — see the design doc for rationale.
    all_packs = (cfg.packs or "").strip() == "" or "," in (cfg.packs or "")
    # The "all-packs" expansion happens upstream; here we approximate by
    # counting comma-separated entries. >=8 packs triggers the long timeout.
    pack_count = len([p for p in (cfg.packs or "").split(",") if p.strip()])
    readiness_timeout = 1800 if (all_packs and pack_count >= 8) else 300
    _print(
        f"  [dim]-> Waiting for container readiness "
        f"(timeout {readiness_timeout}s, ~30s per progress update)...[/dim]"
    )

    last_pack: str | None = None

    def _on_progress(evt: dict[str, Any]) -> None:
        nonlocal last_pack
        progress = evt.get("progress") or {}
        extra = evt.get("extra") or {}
        # Prefer the in-container progress file; fall back to whatever
        # /readiness echoed.
        current = extra.get("current_pack") or progress.get("current_pack")
        ingested = extra.get("packs_ingested", progress.get("packs_ingested"))
        total = extra.get("packs_total", progress.get("packs_total"))
        elapsed = int(evt.get("elapsed") or 0)
        # Only print on change (pack rolled over) or every ~minute on the
        # same pack so the user sees liveness without log spam.
        if current and current != last_pack:
            last_pack = current
            suffix = f" ({ingested}/{total})" if ingested is not None and total else ""
            _print(f"     [dim]bootstrap: {current}{suffix}  elapsed={elapsed}s[/dim]")
        elif evt.get("status") == "warming_up" and elapsed and elapsed % 60 < 30:
            # Heartbeat for slow packs — show every 60s window.
            _print(f"     [dim]bootstrap: still warming up  elapsed={elapsed}s[/dim]")

    healthy = _wait_for_readiness(
        cfg.port,
        timeout=readiness_timeout,
        runtime=binary_path,
        container_name=cfg.container_name or "agentalloy",
        poll_interval=30.0,
        on_progress=_on_progress,
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

    # 11. Run verify
    _print("  [dim]-> Verifying installation[/dim]")
    rc = verify.run(_build_namespace(cfg))
    if rc not in (0, 4):
        _print("  [red]Validation failed.[/red]")
        _report_verify_failures()
        return rc
    _print("  [green]  All checks passed.[/green]")

    # 12. Wire harness
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
    _print(f"  Runtime:  {cfg.runtime_binary}")
    _print(f"  Image:    {cfg.image_tag}")
    _print(f"  Container: {cfg.container_name}")
    _print(f"  Volume:   {cfg.data_volume}")

    _print(f"\n  [bold]Logs:[/bold] {cfg.runtime_binary} logs {cfg.container_name}")
    _print(f"\n  [bold]Stop:[/bold] {cfg.runtime_binary} stop {cfg.container_name}")
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
        _report_verify_failures()
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
