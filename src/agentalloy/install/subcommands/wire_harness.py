# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportArgumentType=false
"""``wire-harness`` subcommand.

.. deprecated::
    This module is deprecated.  All harness wiring is now handled through
    the provider registry in ``agentalloy.providers.REGISTRY``.  Each
    provider package registers a ``HarnessSpec.install_writer`` callable
    that performs the same wiring logic.  New code should import from
    ``agentalloy.providers`` instead of this module.

Emit harness-specific integration files with sentinel markers for
clean removal by ``uninstall``.

Closed harnesses (markdown injection):
  claude-code     → CLAUDE.md
  gemini-cli      → GEMINI.md
  cursor          → .cursor/rules/agentalloy.mdc   (or .cursorrules fallback)
  windsurf        → .windsurf/rules/agentalloy.md  (or .windsurfrules fallback)
  github-copilot  → .github/copilot-instructions.md
  hermes-agent    → ~/.hermes/SOUL.md (user scope) or AGENTS.md (repo scope)

Open harnesses (system-prompt snippet):
  opencode     → .opencode/system-prompt.md
  aider        → .agentalloy-aider-instructions.md  (+.aider.conf.yml entry)
  cline        → .clinerules

Continue.dev:
  continue-closed → .continuerc.json (system message + custom command)
  continue-local  → .continuerc.json (custom command only)

# Manual / MCP:
  manual                       → prints snippet to stdout
  --mcp-fallback (with claude-code, cursor, continue-{closed,local})
                               → writes the strict-tools MCP server config
                                 instead of the markdown-injection variant
                                 (see harness-catalog.md § "MCP fallback")
  codex                        → ~/.codex/config.toml with apiBaseUrl sentinel
  openclaw                     → ~/.openclaw/plugins.json with agentalloy plugin entry

The legacy ``--harness mcp-only`` is no longer accepted; use the
``--mcp-fallback`` flag with a real harness instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import warnings
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.sentinel_utils import replace_marked_block
from agentalloy.providers import REGISTRY

SCHEMA_VERSION = 1
STEP_NAME = "wire-harness"

SENTINEL_BEGIN = "<!-- BEGIN agentalloy install -->"
SENTINEL_END = "<!-- END agentalloy install -->"

# Templates live alongside this file's parent
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "harness_templates"

# Map harness name → (target_file_relative_path, template_filename, is_dedicated_file)
# is_dedicated_file: if True, the entire file is ours (no sentinels needed in file)
_HARNESS_REGISTRY: dict[str, dict[str, Any]] = {
    "claude-code": {
        "target": "CLAUDE.md",
        "template": "claude-code.md",
        "dedicated": False,
        "vector": "markdown_injection",
    },
    "gemini-cli": {
        "target": "GEMINI.md",
        "template": "gemini-cli.md",
        "dedicated": False,
        "vector": "markdown_injection",
    },
    "cursor": {
        # Resolved at runtime: .cursor/rules/agentalloy.mdc or .cursorrules
        "target": None,
        "template": "cursor.mdc",
        "dedicated": None,  # depends on path chosen
        "vector": "markdown_injection",
    },
    "windsurf": {
        # Resolved at runtime: .windsurf/rules/agentalloy.md or .windsurfrules
        "target": None,
        "template": "windsurf.md",
        "dedicated": None,  # depends on path chosen
        "vector": "markdown_injection",
    },
    "github-copilot": {
        "target": ".github/copilot-instructions.md",
        "template": "github-copilot.md",
        "dedicated": False,
        "vector": "markdown_injection",
    },
    "hermes-agent": {
        # Resolved at runtime by scope:
        #   user → .hermes/SOUL.md (under $HOME)
        #   repo → AGENTS.md       (under repo root)
        "target": None,
        "template": "hermes-agent.md",
        "dedicated": False,  # both targets are shared files → sentinel-bounded
        "vector": "markdown_injection",
    },
    "opencode": {
        "target": ".opencode/system-prompt.md",
        "template": "opencode.md",
        "dedicated": False,
        "vector": "system_prompt_snippet",
    },
    "aider": {
        "target": ".agentalloy-aider-instructions.md",
        "template": "aider.md",
        "dedicated": True,
        "vector": "system_prompt_snippet",
    },
    "cline": {
        "target": ".clinerules",
        "template": "cline.md",
        "dedicated": False,
        "vector": "system_prompt_snippet",
    },
    "continue-closed": {
        "target": ".continuerc.json",
        "template": None,  # handled specially
        "dedicated": False,
        "vector": "markdown_injection",
    },
    "continue-local": {
        "target": ".continuerc.json",
        "template": None,  # handled specially
        "dedicated": False,
        "vector": "system_prompt_snippet",
    },
    "manual": {
        "target": None,
        "template": "claude-code.md",  # generic template for stdout
        "dedicated": False,
        "vector": "manual",
    },
    "mcp-only": {
        # MCP fallback variant — the actual MCP server module + per-harness
        # MCP config writers are scoped to install spec step 11 (deferred).
        # The registry entry exists so `--harness mcp-only` is accepted by
        # the CLI parser; invoking it surfaces a clear "step 11" message.
        "target": None,
        "template": None,
        "dedicated": False,
        "vector": "mcp_server_config",
    },
    "codex": {
        # Codex CLI — writes ~/.codex/config.toml with apiBaseUrl sentinel.
        "target": None,
        "template": None,
        "dedicated": False,
        "vector": "proxy",
    },
    "openclaw": {
        # Openclaw plugin harness — writes ~/.openclaw/plugins.json with
        # agentalloy plugin entry pointing to the AgentAlloy proxy.
        "target": None,
        "template": None,
        "dedicated": False,
        "vector": "proxy",
    },
}

VALID_HARNESSES: frozenset[str] = frozenset(REGISTRY.keys())


def _load_template(name: str) -> str:
    """Load a harness template file."""
    path = _TEMPLATES_DIR / name
    if not path.exists():
        print(f"ERROR: Template not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    return path.read_text()


def _render_template(template: str, port: int) -> str:
    """Substitute {port} in template content."""
    return template.replace("{port}", str(port))


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _capture_original(path: Path) -> str | None:
    """Read and return the file's content if it exists, else None."""
    if path.exists():
        return path.read_text()
    return None


def _detect_line_ending(content: str) -> str:
    """Detect whether file uses CRLF or LF."""
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def _inject_sentinel_block(
    existing: str,
    block: str,
) -> str:
    """Insert or replace a sentinel-bounded block in existing content.

    If sentinels already exist, replaces the content between them.
    If not, appends the full sentinel block at the end.

    Delegates to the shared ``replace_marked_block`` helper which
    validates BEGIN-before-END ordering and duplicate counts.
    """
    return replace_marked_block(existing, block, SENTINEL_BEGIN, SENTINEL_END)


def _resolve_cursor_path(root: Path) -> tuple[str, bool]:
    """Resolve Cursor target path.

    Returns (relative_path, is_dedicated_file).
    Modern: .cursor/rules/agentalloy.mdc (dedicated file, we own it)
    Legacy: .cursorrules (shared, sentinel-bounded)
    """
    if (root / ".cursor").is_dir():
        return ".cursor/rules/agentalloy.mdc", True
    return ".cursorrules", False


def _resolve_windsurf_path(root: Path) -> tuple[str, bool]:
    """Resolve Windsurf target path.

    Returns (relative_path, is_dedicated_file).
    Modern: .windsurf/rules/agentalloy.md (dedicated per-rule file)
    Legacy: .windsurfrules (shared, sentinel-bounded)
    """
    if (root / ".windsurf").is_dir():
        return ".windsurf/rules/agentalloy.md", True
    return ".windsurfrules", False


def _resolve_hermes_path(scope: str) -> tuple[str, bool]:
    """Resolve Hermes Agent target path.

    Returns (relative_path, is_dedicated_file).
      user scope → .hermes/SOUL.md   (resolved against $HOME)
      repo scope → AGENTS.md         (resolved against repo root)
    Both files are shared with user content → sentinel-bounded.
    """
    if scope == "user":
        return ".hermes/SOUL.md", False
    return "AGENTS.md", False


def _wire_continue(
    root: Path,
    port: int,
    variant: str,
) -> list[dict[str, Any]]:
    """Wire Continue.dev (.continuerc.json).

    variant: 'closed' or 'local'
    """
    config_path = root / ".continuerc.json"
    config: dict[str, Any] = {}

    # Capture original for backup/restore
    original_content = _capture_original(config_path)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as err:
            print(f"ERROR: {config_path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from err

    # Custom command (both variants)
    custom_commands = config.get("customCommands", [])
    # Remove existing agentalloy command if present
    custom_commands = [c for c in custom_commands if c.get("name") != "skill"]
    custom_commands.append(
        {
            "name": "skill",
            "description": "Query the local agentalloy for guidance on a coding task",
            "prompt": (
                f"Run: curl -s -X POST http://localhost:{port}/compose/text "
                f"-H 'Content-Type: application/json' "
                '-d \'{"task":"{input}","phase":"build"}\' '
                "and read the plain text response as your skill context."
            ),
        }
    )
    config["customCommands"] = custom_commands

    # System message (closed variant only)
    if variant == "closed":
        sys_msg = config.get("systemMessage", "")
        injection = (
            f"A local agentalloy service runs at http://localhost:{port}. "
            "Before starting any task (spec, design, build, test, debug), invoke the `/skill` "
            "custom command with a one-sentence task description to fetch plain text skill context. "
            "Read the response before generating code or a plan."
        )
        sentinel_block = f"<!-- agentalloy:begin -->\n{injection}\n<!-- agentalloy:end -->"

        if "<!-- agentalloy:begin -->" in sys_msg:
            begin = sys_msg.index("<!-- agentalloy:begin -->")
            end = sys_msg.index("<!-- agentalloy:end -->") + len("<!-- agentalloy:end -->")
            sys_msg = sys_msg[:begin] + sentinel_block + sys_msg[end:]
        else:
            if sys_msg:
                sys_msg += "\n\n"
            sys_msg += sentinel_block

        config["systemMessage"] = sys_msg

    # Marker for uninstall
    added_paths = ["customCommands.agentalloy"]
    if variant == "closed":
        added_paths.append("systemMessage.agentalloy_block")
    config["_agentalloy_install_marker"] = {
        "managed_by": "agentalloy install",
        "added_paths": added_paths,
    }

    content = json.dumps(config, indent=2) + "\n"
    install_state._atomic_write(config_path, content)  # pyright: ignore[reportPrivateUsage]

    return [
        {
            "path": str(config_path),
            "action": "injected_block",
            "sentinel_begin": "<!-- agentalloy:begin -->"
            if variant == "closed"
            else "_agentalloy_install_marker",
            "sentinel_end": "<!-- agentalloy:end -->"
            if variant == "closed"
            else "_agentalloy_install_marker",
            "content_sha256": _sha256(content),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def wire_harness(
    harness: str,
    port: int = 47950,
    root: Path | None = None,
    force: bool = False,
    mcp_fallback: bool = False,
    legacy: bool = False,
    scope: str = "user",
) -> dict[str, Any]:
    """Wire the specified harness. Returns contract-shaped result.

    .. deprecated::
        This function is deprecated.  Use
        ``agentalloy.providers.REGISTRY[harness].install_writer`` instead.

    If the target file already has a sentinel block and the inner content's
    sha256 differs from what install-state.json recorded (i.e., the user
    edited inside the sentinels), refuse to clobber unless ``force=True``.

    If ``mcp_fallback=True``, writes the strict-tools MCP server config for
    the chosen harness instead of the default proxy wiring. Supported
    harnesses for MCP fallback: claude-code, cursor, continue-closed,
    continue-local. Other harnesses raise SystemExit(1).

    If ``legacy=True``, uses the old markdown-injection wiring path instead
    of the default proxy model. Orthogonal to ``--mcp-fallback``.
    """
    warnings.warn(
        "wire_harness() is deprecated; use agentalloy.providers.REGISTRY "
        "instead. This module will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    if scope not in ("user", "repo"):
        print(f"ERROR: --scope must be 'user' or 'repo', got '{scope}'", file=sys.stderr)
        raise SystemExit(1)

    if root is None:
        root = Path.home() if scope == "user" else _repo_root()

    if harness not in REGISTRY:
        print(f"ERROR: Unknown harness: '{harness}'", file=sys.stderr)
        print(f"FIX:   Use one of: {', '.join(sorted(VALID_HARNESSES))}", file=sys.stderr)
        raise SystemExit(1)

    # Handle the legacy `mcp-only` harness name: it pre-dates the
    # `--mcp-fallback` flag. Surface a clear migration message.
    if harness == "mcp-only":
        print(
            "ERROR: --harness mcp-only is no longer a standalone harness.",
            file=sys.stderr,
        )
        print(
            "FIX:   Pick a real harness and add --mcp-fallback. Example:",
            file=sys.stderr,
        )
        print(
            "       python -m agentalloy.install wire-harness --harness claude-code --mcp-fallback",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # MCP fallback path: write the harness-specific MCP server config.
    if mcp_fallback:
        files_written = _wire_mcp_fallback(harness, port, root, force)
        return _build_result(harness, "mcp_server_config", files_written, root)

    # Legacy path: old markdown-injection wiring (--legacy flag).
    if legacy:
        return _wire_legacy(harness, port, root, force, scope)

    # Default: proxy wiring.
    files_written = _wire_proxy(harness, port, root, force, scope)
    return _build_result(harness, "proxy", files_written, root)


def _wire_legacy(
    harness: str,
    port: int,
    root: Path,
    force: bool = False,
    scope: str = "user",
) -> dict[str, Any]:
    """Legacy markdown-injection wiring path.

    This is the OLD behavior — used only when ``--legacy`` is passed.
    Extracted from the inline legacy path in ``wire_harness()``.
    """
    # _HARNESS_REGISTRY is the legacy subset and may not include every harness
    # that the modern REGISTRY does. Fail with a clear error instead of letting
    # the dict lookup raise KeyError into the caller.
    if harness not in _HARNESS_REGISTRY:
        legacy_supported = ", ".join(sorted(_HARNESS_REGISTRY))
        raise SystemExit(
            f"wire-harness --legacy does not support harness '{harness}'. "
            f"Legacy-supported harnesses: {legacy_supported}. "
            f"Re-run without --legacy to use the modern provider registry."
        )
    reg = _HARNESS_REGISTRY[harness]
    files_written: list[dict[str, Any]] = []

    # Claude Code hook wiring (legacy path): installs the hook script
    # and writes the hooks config file.
    # Check for duplicate sentinels in CLAUDE.md before proceeding.
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        existing_content = claude_md.read_text()
        begin_count = existing_content.count(SENTINEL_BEGIN)
        end_count = existing_content.count(SENTINEL_END)
        if begin_count > 1 or end_count > 1:
            raise ValueError(
                f"target file contains {begin_count} BEGIN and {end_count} END "
                f"agentalloy sentinels (expected at most 1 of each). Refusing to write."
            )
    if harness == "claude-code":
        # Route the legacy claude-code branch through the modern provider
        # hook_writer (installs the hook script + merges ~/.claude/settings.json
        # in Claude Code's current format) rather than the retired
        # claude-code-hooks.json writer. Hook is the default claude-code wiring.
        from agentalloy.providers.claude_code.hooks import hook_writer

        for rec in hook_writer(port, root):
            files_written.append(rec.to_dict())
        return _build_result(harness, "claude_code_hooks", files_written, root)

    # continue special case (already has proxy, skip)
    if harness in ("continue-closed", "continue-local"):
        variant = "closed" if harness == "continue-closed" else "local"
        files_written = _wire_continue(root, port, variant)
        return _build_result(harness, reg["vector"], files_written, root)

    # Handle manual: emit the sentinel block on stderr
    if harness == "manual":
        template = _load_template(reg["template"])
        rendered = _render_template(template, port)
        block = f"{SENTINEL_BEGIN}\n{rendered}\n{SENTINEL_END}"
        print(block, file=sys.stderr)
        return {
            "schema_version": SCHEMA_VERSION,
            "harness": harness,
            "integration_vector": "manual",
            "files_written": [],
            "manual_block": block,
        }

    # Resolve target path
    if harness == "cursor":
        rel_path, dedicated = _resolve_cursor_path(root)
    elif harness == "windsurf":
        rel_path, dedicated = _resolve_windsurf_path(root)
    elif harness == "hermes-agent":
        rel_path, dedicated = _resolve_hermes_path(scope)
    else:
        rel_path = reg["target"]
        dedicated = reg["dedicated"]

    target_path = root / rel_path
    template = _load_template(reg["template"])
    rendered = _render_template(template, port)

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Capture original for backup/restore
    original_content = _capture_original(target_path)

    # Tamper detection
    if not force and not dedicated and target_path.exists():
        st = install_state.load_state(root)
        prior = next(
            (e for e in st.get("harness_files_written", []) if e.get("path") == str(target_path)),
            None,
        )
        if prior:
            existing_content = target_path.read_text()
            if SENTINEL_BEGIN in existing_content and SENTINEL_END in existing_content:
                begin = existing_content.index(SENTINEL_BEGIN) + len(SENTINEL_BEGIN)
                end = existing_content.index(SENTINEL_END)
                current_inner = existing_content[begin:end].strip()
                stored_sha = prior.get("content_sha256", "")
                expected = (
                    stored_sha[len("sha256:") :] if stored_sha.startswith("sha256:") else stored_sha
                )
                if expected and _sha256(current_inner) != expected:
                    print(
                        f"ERROR: Sentinel block in {target_path} has been edited since the last "
                        "wire-harness run (sha256 mismatch).",
                        file=sys.stderr,
                    )
                    print(
                        "CAUSE: User content inside <!-- BEGIN/END agentalloy install --> markers "
                        "has changed.",
                        file=sys.stderr,
                    )
                    print(
                        "FIX:   Either move your edits outside the sentinels, or re-run with "
                        "--force to overwrite them.",
                        file=sys.stderr,
                    )
                    raise SystemExit(1)

    if dedicated:
        install_state._atomic_write(target_path, rendered)  # pyright: ignore[reportPrivateUsage]
        action = "wrote_new_file"
        content_sha256 = _sha256(rendered.strip())
    else:
        existing = target_path.read_text() if target_path.exists() else ""
        result_content = _inject_sentinel_block(existing, rendered)
        install_state._atomic_write(target_path, result_content)  # pyright: ignore[reportPrivateUsage]
        action = "injected_block"
        content_sha256 = _sha256(rendered.strip())

    files_written.append(
        {
            "path": str(target_path),
            "action": action,
            "sentinel_begin": SENTINEL_BEGIN if not dedicated else None,
            "sentinel_end": SENTINEL_END if not dedicated else None,
            "content_sha256": content_sha256,
            **({"original_content": original_content} if original_content is not None else {}),
        }
    )

    # For aider, also wire .aider.conf.yml
    if harness == "aider":
        files_written.extend(_wire_aider_conf(root))

    # For sidecar harnesses (can't be proxy-wired), write watcher config and print guidance
    from agentalloy.install import PROXY_UNABLE_HARNESSES

    if harness in PROXY_UNABLE_HARNESSES:
        _wire_sidecar_watcher_config(harness, root)

    # Probe for code-indexer and persist result to state.json
    _probe_code_indexer(root)

    return _build_result(harness, reg["vector"], files_written, root)


def _wire_aider_conf(root: Path) -> list[dict[str, Any]]:
    """Add our instructions file to .aider.conf.yml's read list."""
    conf_path = root / ".aider.conf.yml"
    original_content = _capture_original(conf_path)
    sentinel_line_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_line_end = "# <!-- END agentalloy install -->"
    entry = "  - .agentalloy-aider-instructions.md"
    block = f"{sentinel_line_begin}\nread:\n{entry}\n{sentinel_line_end}"

    if conf_path.exists():
        content = conf_path.read_text()
        if sentinel_line_begin in content:
            # Replace existing block
            begin = content.index(sentinel_line_begin)
            end = content.index(sentinel_line_end) + len(sentinel_line_end)
            if end < len(content) and content[end] == "\n":
                end += 1
            content = content[:begin] + block + "\n" + content[end:]
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += block + "\n"
    else:
        content = block + "\n"

    install_state._atomic_write(conf_path, content)  # pyright: ignore[reportPrivateUsage]
    return [
        {
            "path": str(conf_path),
            "action": "injected_block",
            "sentinel_begin": sentinel_line_begin,
            "sentinel_end": sentinel_line_end,
            "content_sha256": _sha256(block),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


# ---------------------------------------------------------------------------
# MCP fallback wiring
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sidecar watcher wiring (harnesses that can't be proxy-wired)
# ---------------------------------------------------------------------------


def _wire_sidecar_watcher_config(harness: str, root: Path) -> None:
    """Write watcher config and print sidecar guidance. Soft-fail."""
    try:
        import yaml as _yaml

        watch_dir = Path.home() / ".agentalloy" / "watch"
        watch_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "project_root": str(root),
            "profile_name": "default",
            "harness": harness,
            "poll_interval_s": 1.0,
            "debounce_ms": 500,
        }
        (watch_dir / "default.yaml").write_text(_yaml.dump(config))
    except Exception:
        pass

    print(
        f"\n[AgentAlloy — sidecar wiring]\n"
        f"You selected: {harness}\n\n"
        f"{harness} cannot be proxy-wired (it does not honor base-URL overrides\n"
        "for the AgentAlloy proxy). To get phase- and contract-driven context\n"
        "updates, run the watcher sidecar:\n\n"
        f"    agentalloy watch start --harness {harness}\n\n"
        "Run under tmux, systemd, or launchd for persistence. Without the\n"
        "watcher, you'll only get the initial workflow skill context. System\n"
        "skills (commit-safety, etc.) are advisory-only for sidecar harnesses.\n\n"
        "See docs/sidecar-experience.md for the full picture.\n",
        file=sys.stderr,
    )


def _probe_code_indexer(root: Path) -> None:
    """Probe code-indexer health and persist reachability to state.json. Soft-fail."""
    import time
    import urllib.request

    from agentalloy.config import get_settings

    ci_url = get_settings().code_indexer_url
    reachable = False
    try:
        req = urllib.request.urlopen(f"{ci_url}/health", timeout=2)
        reachable = req.status == 200
    except Exception:
        pass

    st = install_state.load_state(root)
    st["code_indexer"] = {
        "reachable": reachable,
        "url": ci_url,
        "last_health_at": int(time.time()),
    }
    install_state.save_state(st, root)


# Harnesses we know how to wire MCP for. Others (gemini-cli, opencode,
# aider, cline) get a clear "not yet supported" error.
_MCP_SUPPORTED = frozenset({"claude-code", "cursor", "continue-closed", "continue-local"})


def _mcp_server_entry(port: int) -> dict[str, Any]:
    """The agentalloy MCP server config block (per harness-catalog.md).

    Uses ``sys.executable`` rather than bare ``python`` so the harness
    invokes the same interpreter that wrote this config — avoids
    "command not found" on systems where only ``python3`` is on PATH,
    and avoids cross-venv breakage.
    """
    return {
        "command": sys.executable,
        "args": ["-m", "agentalloy.install.mcp_server", "--port", str(port)],
    }


def _normalize_mcp_servers_dict(config: dict[str, Any], path: Path) -> dict[str, Any]:
    """Return ``config["mcpServers"]`` as a dict, raising on incompatible types.

    A user with ``"mcpServers": []`` or any non-dict shape would otherwise
    cause a ``TypeError`` when we try to add our entry — surface it explicitly.
    """
    raw = config.get("mcpServers")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    print(
        f"ERROR: {path} has 'mcpServers' that is not a JSON object: got {type(raw).__name__}",
        file=sys.stderr,
    )
    print(
        "FIX:   Repair or remove the malformed 'mcpServers' field, then re-run wire-harness.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _wire_mcp_claude_code(port: int) -> list[dict[str, Any]]:
    """Write the agentalloy MCP entry to ~/.claude/mcp_servers.json."""
    config_path = Path.home() / ".claude" / "mcp_servers.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {}
    original_content = _capture_original(config_path)
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR: {config_path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc
    servers = _normalize_mcp_servers_dict(config, config_path)
    servers["agentalloy"] = _mcp_server_entry(port)
    config["mcpServers"] = servers
    serialized = json.dumps(config, indent=2) + "\n"
    install_state._atomic_write(config_path, serialized)  # pyright: ignore[reportPrivateUsage]
    return [
        {
            "path": str(config_path),
            "action": "wrote_user_dotfile",
            "marker_key": "mcpServers.agentalloy",
            "content_sha256": _sha256(serialized),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_mcp_cursor(port: int, root: Path) -> list[dict[str, Any]]:
    """Write the agentalloy MCP entry to <repo>/.cursor/mcp.json."""
    config_path = root / ".cursor" / "mcp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {}
    original_content = _capture_original(config_path)
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR: {config_path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc
    servers = _normalize_mcp_servers_dict(config, config_path)
    servers["agentalloy"] = _mcp_server_entry(port)
    config["mcpServers"] = servers
    serialized = json.dumps(config, indent=2) + "\n"
    install_state._atomic_write(config_path, serialized)  # pyright: ignore[reportPrivateUsage]
    return [
        {
            "path": str(config_path),
            "action": "injected_block",
            "marker_key": "mcpServers.agentalloy",
            "content_sha256": _sha256(serialized),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_mcp_continue(port: int, root: Path, variant: str) -> list[dict[str, Any]]:
    """Write the agentalloy MCP entry into .continuerc.json."""
    config_path = root / ".continuerc.json"
    config: dict[str, Any] = {}
    original_content = _capture_original(config_path)
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR: {config_path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc

    servers = _normalize_mcp_servers_dict(config, config_path)
    servers["agentalloy"] = _mcp_server_entry(port)
    config["mcpServers"] = servers

    # Marker for clean removal by uninstall
    marker = config.get("_agentalloy_install_marker") or {}
    marker["managed_by"] = "agentalloy install"
    added = set(marker.get("added_paths") or [])
    added.add("mcpServers.agentalloy")
    marker["added_paths"] = sorted(added)
    marker["variant"] = f"mcp-{variant}"
    config["_agentalloy_install_marker"] = marker

    serialized = json.dumps(config, indent=2) + "\n"
    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
        config_path, serialized
    )
    return [
        {
            "path": str(config_path),
            "action": "injected_block",
            "marker_key": "mcpServers.agentalloy",
            "content_sha256": _sha256(serialized),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


# ---------------------------------------------------------------------------
# Proxy wiring
# ---------------------------------------------------------------------------

_PROXY_SUPPORTED_API = frozenset(
    {
        "continue-closed",
        "continue-local",
        "aider",
        "hermes-agent",
        "opencode",
        "claude-code",
        "cline",
    }
)


def _wire_proxy(
    harness: str,
    port: int,
    root: Path,
    _force: bool,
    scope: str,
) -> list[dict[str, Any]]:
    """Wire the harness to use the AgentAlloy proxy.

    For harnesses that support custom API endpoints (Continue), configures
    the API base URL. For all others, writes a proxy instruction block using
    sentinel markers.
    """
    # Handle manual: emit the proxy instruction on stderr
    if harness == "manual":
        template = _load_template("proxy-instruction.md")
        rendered = _render_template(template, port)
        block = f"{SENTINEL_BEGIN}\n{rendered}\n{SENTINEL_END}"
        print(block, file=sys.stderr)
        return []

    # Harnesses that support custom API endpoints
    if harness in ("continue-closed", "continue-local"):
        return _wire_proxy_continue(harness, port, root)

    if harness == "aider":
        return _wire_proxy_aider(port, root)

    if harness == "hermes-agent":
        return _wire_proxy_hermes_agent(port, root, scope)

    if harness == "opencode":
        return _wire_proxy_opencode(port, root)

    if harness == "claude-code":
        return _wire_proxy_claude_code(port, root)

    if harness == "cline":
        return _wire_proxy_cline(port, root)

    if harness == "codex":
        return _wire_proxy_codex(port, root)

    # All other harnesses: write a proxy instruction block
    return _wire_proxy_instruction(harness, port, root, scope)


def _wire_proxy_continue(
    harness: str,
    port: int,
    root: Path,
) -> list[dict[str, Any]]:
    """Wire Continue.dev to use the proxy as its API base."""
    variant = "closed" if harness == "continue-closed" else "local"
    config_path = root / ".continuerc.json"
    config: dict[str, Any] = {}
    original_content = _capture_original(config_path)
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as err:
            print(f"ERROR: {config_path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from err

    proxy_url = f"http://localhost:{port}/v1"

    # Add custom model pointing to the proxy
    models = config.get("models", [])
    # Remove any existing agentalloy proxy model
    models = [m for m in models if m.get("agentalloy_proxy") is not True]
    models.append(
        {
            "name": "agentalloy-proxy",
            "apiBase": proxy_url,
            "agentalloy_proxy": True,
            "provider": "openai",
        }
    )
    config["models"] = models

    # Marker for clean removal
    marker = config.get("_agentalloy_install_marker") or {}
    marker["managed_by"] = "agentalloy install"
    added = set(marker.get("added_paths") or [])
    added.add("models.agentalloy-proxy")
    marker["added_paths"] = sorted(added)
    marker["variant"] = f"proxy-{variant}"
    config["_agentalloy_install_marker"] = marker

    serialized = json.dumps(config, indent=2) + "\n"
    install_state._atomic_write(config_path, serialized)  # pyright: ignore[reportPrivateUsage]

    return [
        {
            "path": str(config_path),
            "action": "injected_block",
            "marker_key": "models.agentalloy-proxy",
            "content_sha256": _sha256(serialized),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_proxy_aider(port: int, root: Path) -> list[dict[str, Any]]:
    """Wire aider to use the AgentAlloy proxy via .aider.conf.yml.

    Writes a sentinel-bounded YAML block that configures aider's
    ``openai-api-base``, ``openai-api-key``, and ``model`` fields to point
    at the proxy.
    """
    conf_path = root / ".aider.conf.yml"
    original_content = _capture_original(conf_path)
    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"

    proxy_url = f"http://localhost:{port}/v1"
    block_lines = [
        sentinel_begin,
        f"openai-api-base: {proxy_url}",
        "openai-api-key: agentalloy",
        "model: agentalloy-proxy",
        sentinel_end,
    ]
    block = "\n".join(block_lines)

    if conf_path.exists():
        content = conf_path.read_text()
        if sentinel_begin in content and sentinel_end in content:
            # Replace existing block
            begin_idx = content.index(sentinel_begin)
            end_idx = content.index(sentinel_end) + len(sentinel_end)
            if end_idx < len(content) and content[end_idx] == "\n":
                end_idx += 1
            content = content[:begin_idx] + block + "\n" + content[end_idx:]
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += block + "\n"
    else:
        content = block + "\n"

    install_state._atomic_write(conf_path, content)  # pyright: ignore[reportPrivateUsage]
    return [
        {
            "path": str(conf_path),
            "action": "injected_block",
            "sentinel_begin": sentinel_begin,
            "sentinel_end": sentinel_end,
            "content_sha256": _sha256(block),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_proxy_hermes_agent(port: int, root: Path, scope: str) -> list[dict[str, Any]]:
    """Wire Hermes Agent to use the AgentAlloy proxy.

    User scope: writes a ``custom_providers`` entry to ~/.hermes/config.yaml
    so the Hermes agent can pick up the proxy as a named provider.

    Repo scope: writes a compact sentinel-bounded proxy-mode instruction to
    AGENTS.md so agents reading that file know to use the proxy.
    """
    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"

    if scope == "user":
        config_path = Path.home() / ".hermes" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        proxy_block_lines = [
            sentinel_begin,
            "custom_providers:",
            "  agentalloy:",
            f"    base_url: http://localhost:{port}/v1",
            "    api_key: agentalloy",
            sentinel_end,
        ]
        block = "\n".join(proxy_block_lines)
        original_content = _capture_original(config_path)

        if config_path.exists():
            content = config_path.read_text()
            if sentinel_begin in content and sentinel_end in content:
                begin_idx = content.index(sentinel_begin)
                end_idx = content.index(sentinel_end) + len(sentinel_end)
                if end_idx < len(content) and content[end_idx] == "\n":
                    end_idx += 1
                content = content[:begin_idx] + block + "\n" + content[end_idx:]
            else:
                if content and not content.endswith("\n"):
                    content += "\n"
                content += block + "\n"
        else:
            content = block + "\n"

        install_state._atomic_write(config_path, content)  # pyright: ignore[reportPrivateUsage]
        return [
            {
                "path": str(config_path),
                "action": "injected_block",
                "sentinel_begin": sentinel_begin,
                "sentinel_end": sentinel_end,
                "content_sha256": _sha256(block),
                **({"original_content": original_content} if original_content is not None else {}),
            }
        ]

    # Repo scope: write a proxy instruction block to AGENTS.md
    agents_md = root / "AGENTS.md"
    original_content = _capture_original(agents_md)
    instruction = (
        f"## AgentAlloy proxy\n\n"
        f"An AgentAlloy proxy is running at `http://localhost:{port}/v1`.\n"
        "Configure your agent to use it as its API base.\n"
    )
    existing = agents_md.read_text() if agents_md.exists() else ""
    result_content = _inject_sentinel_block(existing, instruction)
    install_state._atomic_write(agents_md, result_content)  # pyright: ignore[reportPrivateUsage]
    return [
        {
            "path": str(agents_md),
            "action": "injected_block",
            "content_sha256": _sha256(instruction.strip()),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_proxy_opencode(port: int, root: Path) -> list[dict[str, Any]]:
    """Wire OpenCode to use the AgentAlloy proxy.

    Writes two files:
    - ``.opencode/.agentalloy-env``: shell script exporting OPENAI_API_BASE and
      OPENAI_API_KEY, which the user sources before launching OpenCode.
    - ``.opencode/system-prompt.md``: brief proxy-mode instruction appended with
      sentinel markers.

    Prints a one-line activation reminder to stderr.
    """
    opencode_dir = root / ".opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)

    # Write env file (always overwrites — it's a generated file we own fully)
    env_path = opencode_dir / ".agentalloy-env"
    env_content = (
        f"export OPENAI_API_BASE=http://localhost:{port}/v1\nexport OPENAI_API_KEY=agentalloy\n"
    )
    install_state._atomic_write(env_path, env_content)  # pyright: ignore[reportPrivateUsage]

    # Write / update system-prompt.md with sentinel block
    prompt_path = opencode_dir / "system-prompt.md"
    original_content = _capture_original(prompt_path)
    instruction = (
        "## AgentAlloy proxy\n\n"
        f"An AgentAlloy proxy is active at `http://localhost:{port}/v1`.\n"
        "It intercepts requests to inject skill context before forwarding to your LLM.\n"
    )
    existing = prompt_path.read_text() if prompt_path.exists() else ""
    result_content = _inject_sentinel_block(existing, instruction)
    install_state._atomic_write(prompt_path, result_content)  # pyright: ignore[reportPrivateUsage]

    print(
        "[AgentAlloy] Activate proxy: source .opencode/.agentalloy-env",
        file=sys.stderr,
    )

    return [
        {
            "path": str(env_path),
            "action": "wrote_new_file",
            "content_sha256": _sha256(env_content),
        },
        {
            "path": str(prompt_path),
            "action": "injected_block",
            "content_sha256": _sha256(instruction.strip()),
            **({"original_content": original_content} if original_content is not None else {}),
        },
    ]


def _wire_proxy_claude_code(port: int, root: Path) -> list[dict[str, Any]]:
    """Wire Claude Code to use the AgentAlloy proxy.

    Writes ``~/.agentalloy/claude-code-env.sh`` with sentinel-bounded
    ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_API_KEY`` exports. Claude Code
    reads these environment variables at startup to route requests through
    the proxy.
    """
    agentalloy_dir = Path.home() / ".agentalloy"
    agentalloy_dir.mkdir(parents=True, exist_ok=True)

    env_path = agentalloy_dir / "claude-code-env.sh"
    original_content = _capture_original(env_path)
    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"

    # No /v1 suffix: the Anthropic SDK appends /v1/messages to the base URL,
    # so a /v1 here produces /v1/v1/messages (404 against the proxy).
    proxy_url = f"http://localhost:{port}"
    block_lines = [
        sentinel_begin,
        f"export ANTHROPIC_BASE_URL={proxy_url}",
        "export ANTHROPIC_API_KEY=agentalloy",
        sentinel_end,
    ]
    block = "\n".join(block_lines)

    if env_path.exists():
        content = env_path.read_text()
        if sentinel_begin in content and sentinel_end in content:
            # Replace existing block
            begin_idx = content.index(sentinel_begin)
            end_idx = content.index(sentinel_end) + len(sentinel_end)
            if end_idx < len(content) and content[end_idx] == "\n":
                end_idx += 1
            content = content[:begin_idx] + block + "\n" + content[end_idx:]
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += block + "\n"
    else:
        content = block + "\n"

    install_state._atomic_write(env_path, content)  # pyright: ignore[reportPrivateUsage]

    return [
        {
            "path": str(env_path),
            # "replaced_file" when the file pre-existed: uninstall's
            # restore-original branch handles those; "wrote_new_file" is the
            # delete-on-uninstall signal and must mean the file is ours.
            "action": "wrote_new_file" if original_content is None else "replaced_file",
            "content_sha256": _sha256(block),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_proxy_cline(port: int, root: Path) -> list[dict[str, Any]]:
    """Wire Cline to use the AgentAlloy proxy.

    Writes ``.cline/settings.json`` with proxy fields (``apiProvider``,
    ``apiBaseUrl``, ``apiKey``, ``model``).  Overwrites those four keys;
    preserves all other keys in the file.
    """
    settings_path = root / ".cline" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    original_content = _capture_original(settings_path)
    proxy_url = f"http://localhost:{port}/v1"
    proxy_fields = {
        "apiProvider": "openai",
        "apiBaseUrl": proxy_url,
        "apiKey": "agentalloy",
        "model": "agentalloy-proxy",
    }

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR: {settings_path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc
    else:
        settings = {}

    settings.update(proxy_fields)
    serialized = json.dumps(settings, indent=2) + "\n"
    install_state._atomic_write(settings_path, serialized)  # pyright: ignore[reportPrivateUsage]

    # Record as "injected_block" so uninstall knows to merge-remove proxy keys
    # rather than delete the file outright (users may have their own settings).
    return [
        {
            "path": str(settings_path),
            "action": "injected_block",
            "content_sha256": _sha256(serialized),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_proxy_codex(port: int, root: Path) -> list[dict[str, Any]]:
    """Wire Codex to use the AgentAlloy proxy.

    Writes ``~/.codex/config.toml`` with a sentinel-bounded TOML block
    containing ``apiBaseUrl`` and ``apiKey`` pointing to the proxy.
    """
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    proxy_url = f"http://localhost:{port}/v1"
    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"

    block_lines = [
        sentinel_begin,
        "[codex]",
        f'apiBaseUrl = "{proxy_url}"',
        'apiKey = "agentalloy"',
        sentinel_end,
    ]
    block = "\n".join(block_lines)

    original_content = _capture_original(config_path)

    if config_path.exists():
        content = config_path.read_text()
        if sentinel_begin in content and sentinel_end in content:
            # Replace existing block
            begin_idx = content.index(sentinel_begin)
            end_idx = content.index(sentinel_end) + len(sentinel_end)
            if end_idx < len(content) and content[end_idx] == "\n":
                end_idx += 1
            content = content[:begin_idx] + block + "\n" + content[end_idx:]
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += block + "\n"
    else:
        content = block + "\n"

    install_state._atomic_write(config_path, content)  # pyright: ignore[reportPrivateUsage]

    return [
        {
            "path": str(config_path),
            "action": "wrote_new_file" if original_content is None else "injected_block",
            "content_sha256": _sha256(block),
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_proxy_instruction(
    harness: str,
    port: int,
    root: Path,
    scope: str,
) -> list[dict[str, Any]]:
    """Write a proxy instruction block for the harness.

    For harnesses that don't support custom API endpoints, this writes a
    sentinel-bounded instruction block explaining that the proxy is active.
    """
    # Resolve target path
    if harness == "cursor":
        rel_path, dedicated = _resolve_cursor_path(root)
    elif harness == "windsurf":
        rel_path, dedicated = _resolve_windsurf_path(root)
    elif harness == "hermes-agent":
        rel_path, dedicated = _resolve_hermes_path(scope)
    else:
        reg = _HARNESS_REGISTRY[harness]
        rel_path = reg["target"]
        dedicated = reg["dedicated"]

    target_path = root / rel_path
    original_content = _capture_original(target_path)
    template = _load_template("proxy-instruction.md")
    rendered = _render_template(template, port)

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if dedicated:
        install_state._atomic_write(target_path, rendered)  # pyright: ignore[reportPrivateUsage]
        action = "wrote_new_file"
        content_sha256 = _sha256(rendered.strip())
    else:
        existing = target_path.read_text() if target_path.exists() else ""
        result_content = _inject_sentinel_block(existing, rendered)
        install_state._atomic_write(target_path, result_content)  # pyright: ignore[reportPrivateUsage]
        action = "injected_block"
        content_sha256 = _sha256(rendered.strip())

    return [
        {
            "path": str(target_path),
            "action": action,
            "content_sha256": content_sha256,
            **({"original_content": original_content} if original_content is not None else {}),
        }
    ]


def _wire_mcp_fallback(
    harness: str,
    port: int,
    root: Path,
    _force: bool,
) -> list[dict[str, Any]]:
    """Dispatch to the per-harness MCP config writer."""
    if harness not in _MCP_SUPPORTED:
        print(
            f"ERROR: --mcp-fallback is not yet supported for harness '{harness}'.",
            file=sys.stderr,
        )
        print(
            f"FIX:   Use --mcp-fallback only with: {', '.join(sorted(_MCP_SUPPORTED))}. "
            f"For other harnesses, use the default markdown-injection variant.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if harness == "claude-code":
        return _wire_mcp_claude_code(port)
    if harness == "cursor":
        return _wire_mcp_cursor(port, root)
    if harness in ("continue-closed", "continue-local"):
        variant = "closed" if harness == "continue-closed" else "local"
        return _wire_mcp_continue(port, root, variant)
    # _MCP_SUPPORTED guard above makes this unreachable
    raise RuntimeError(f"unreachable: {harness}")


# ---------------------------------------------------------------------------
# Result + state recording
# ---------------------------------------------------------------------------


def _build_result(
    harness: str,
    vector: str,
    files_written: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    """Build result dict and record state."""
    output: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "harness": harness,
        "integration_vector": vector,
        "files_written": files_written,
    }

    # Stamp each file entry with its repo root and the harness that wrote
    # it. State is now user-scoped (one install-state.json across all of
    # the user's repos), so the source of truth for "which harness was
    # wired in repo X" is each entry, not a single top-level `harness`
    # field. uninstall walks every entry to clean up sentinel blocks.
    repo_root_str = str(root)
    for entry in files_written:
        entry.setdefault("harness", harness)
        entry.setdefault("repo_root", repo_root_str)

    st = install_state.load_state(root)
    prior = st.get("harness_files_written") or []
    new_paths = {f.get("path") for f in files_written}
    # Preserve original_content from prior entries on re-wire: the new entry
    # captures the post-first-write state, but we need the true original.
    prior_by_path = {e.get("path"): e for e in prior}
    for new_entry in files_written:
        prior_entry = prior_by_path.get(new_entry.get("path"))
        if prior_entry and "original_content" in prior_entry:
            new_entry.setdefault("original_content", prior_entry["original_content"])
    merged = [e for e in prior if e.get("path") not in new_paths] + files_written
    st["harness_files_written"] = merged
    st = install_state.record_step(st, STEP_NAME, extra={"output": output})
    install_state.save_state(st, root)

    return output


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Add the wire-harness subparser to an argparse parser.

    .. deprecated::
        This function is deprecated.  The wire-harness subcommand module
        is deprecated; use the provider registry instead.

    Registration itself stays silent: ``__main__`` calls this on EVERY CLI
    invocation, so warning here printed a DeprecationWarning for unrelated
    commands like ``agentalloy status``. The warning now fires only when
    the wire-harness subcommand actually runs (``wire_harness()``).
    """
    p: argparse.ArgumentParser = subparsers.add_parser(
        "wire-harness",
        help="Emit harness-specific integration with sentinel markers.",
    )
    p.add_argument(
        "--harness",
        required=True,
        choices=sorted(VALID_HARNESSES),
        help="Which coding agent harness to integrate with.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="AgentAlloy service port (default: read from user state, fallback 47950).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing sentinel block even if its inner content has been "
            "edited since the last wire-harness run (sha256 mismatch). Without this "
            "flag, edited blocks are preserved and the command exits with an error."
        ),
    )
    p.add_argument(
        "--scope",
        choices=("user", "repo"),
        default="user",
        help=(
            "Install scope. 'user' (default) wires at $HOME so every repo's "
            "harness session picks up AgentAlloy. 'repo' wires inside the "
            "current repo only. Harnesses whose config path is inherently "
            "user-scoped (e.g. claude-code at ~/.claude) ignore this flag."
        ),
    )
    p.add_argument(
        "--mcp-fallback",
        action="store_true",
        help=(
            "Write the strict-tools MCP server config for the chosen harness instead "
            "of the default proxy wiring. Supported on: claude-code, cursor, "
            "continue-closed, continue-local. Orthogonal to --legacy. The MCP server "
            "module lives at agentalloy.install.mcp_server."
        ),
    )
    p.add_argument(
        "--legacy",
        action="store_true",
        help=(
            "Use the legacy markdown-injection wiring method instead of the proxy model. "
            "Writes harness-specific instruction blocks into config files. "
            "Orthogonal to --mcp-fallback."
        ),
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    """Execute the wire-harness subcommand.

    .. deprecated::
        This function is deprecated.  The wire-harness subcommand module
        is deprecated; use the provider registry instead.
    """
    warnings.warn(
        "wire_harness._run() is deprecated; use "
        "agentalloy.providers.REGISTRY instead. This module will be "
        "removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    st = install_state.load_state()
    port = install_state.validate_port(
        args.port if args.port is not None else st.get("port", 47950)
    )
    result = wire_harness(
        args.harness,
        port=port,
        force=args.force,
        mcp_fallback=args.mcp_fallback,
        legacy=getattr(args, "legacy", False),
        scope=args.scope,
    )
    if not getattr(args, "quiet", False):
        # Strip restore-only original_content (may hold secrets) from stdout; it's
        # already persisted to install-state.json for unwire.
        safe = dict(result)
        for key in ("files_written", "files_modified"):
            if isinstance(safe.get(key), list):
                safe[key] = [
                    {k: v for k, v in r.items() if k != "original_content"} for r in safe[key]
                ]
        print(json.dumps(safe, indent=2))
    return 0


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers (e.g. simple_setup).

    .. deprecated::
        This function is deprecated.  The wire-harness subcommand module
        is deprecated; use the provider registry instead.
    """
    warnings.warn(
        "wire_harness.run() is deprecated; use "
        "agentalloy.providers.REGISTRY instead. This module will be "
        "removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _run(args)
