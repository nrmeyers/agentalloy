# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportArgumentType=false
"""``wire-harness`` subcommand.

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

Manual / MCP:
  manual                       → prints snippet to stdout
  --mcp-fallback (with claude-code, cursor, continue-{closed,local})
                               → writes the strict-tools MCP server config
                                 instead of the markdown-injection variant
                                 (see harness-catalog.md § "MCP fallback")

The legacy ``--harness mcp-only`` is no longer accepted; use the
``--mcp-fallback`` flag with a real harness instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state

SCHEMA_VERSION = 1
STEP_NAME = "wire-harness"

# Track whether we've emitted the deprecation warning this session.
_deprecation_warned = False

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
}

VALID_HARNESSES = frozenset(_HARNESS_REGISTRY.keys())


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
    """
    nl = _detect_line_ending(existing) if existing else "\n"

    full_block = f"{SENTINEL_BEGIN}{nl}{block}{nl}{SENTINEL_END}"

    # Reject duplicate sentinel pairs. Multiple BEGIN/END pairs in a single
    # file would mean modifying the first leaves a stranded second pair
    # that uninstall can never clean up. Force the user to manually
    # consolidate before we touch the file.
    begin_count = existing.count(SENTINEL_BEGIN)
    end_count = existing.count(SENTINEL_END)
    if begin_count > 1 or end_count > 1:
        print(
            f"ERROR: target file contains {begin_count} BEGIN and {end_count} END "
            f"agentalloy sentinels (expected at most 1 of each). Refusing to write.",
            file=sys.stderr,
        )
        print(
            "FIX:   Remove duplicate sentinel blocks manually, leaving at most one pair.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if SENTINEL_BEGIN in existing and SENTINEL_END in existing:
        # Replace existing block
        begin_idx = existing.index(SENTINEL_BEGIN)
        end_idx = existing.index(SENTINEL_END) + len(SENTINEL_END)
        # Consume trailing newline if present
        if end_idx < len(existing) and existing[end_idx] in ("\n", "\r"):
            if existing[end_idx : end_idx + 2] == "\r\n":
                end_idx += 2
            else:
                end_idx += 1
        return existing[:begin_idx] + full_block + nl + existing[end_idx:]

    # Append at end
    if existing and not existing.endswith(nl):
        existing += nl
    if existing:
        existing += nl  # blank line separator
    return existing + full_block + nl


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
        }
    ]


def wire_harness(
    harness: str,
    port: int = 47950,
    root: Path | None = None,
    force: bool = False,
    mcp_fallback: bool = False,
    proxy: bool = False,
    scope: str = "user",
) -> dict[str, Any]:
    """Wire the specified harness. Returns contract-shaped result.

    If the target file already has a sentinel block and the inner content's
    sha256 differs from what install-state.json recorded (i.e., the user
    edited inside the sentinels), refuse to clobber unless ``force=True``.

    If ``mcp_fallback=True``, writes the strict-tools MCP server config for
    the chosen harness instead of the markdown-injection variant. Supported
    harnesses for MCP fallback: claude-code, cursor, continue-closed,
    continue-local. Other harnesses raise SystemExit(1).

    If ``proxy=True``, wires the harness to use the AgentAlloy proxy at
    ``http://localhost:{port}/v1`` as its API base URL. For harnesses that
    don't support custom API endpoints, writes a proxy instruction block
    instead.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    if scope not in ("user", "repo"):
        print(f"ERROR: --scope must be 'user' or 'repo', got '{scope}'", file=sys.stderr)
        raise SystemExit(1)

    if root is None:
        root = Path.home() if scope == "user" else _repo_root()

    if harness not in _HARNESS_REGISTRY:
        print(f"ERROR: Unknown harness: '{harness}'", file=sys.stderr)
        print(f"FIX:   Use one of: {', '.join(sorted(VALID_HARNESSES))}", file=sys.stderr)
        raise SystemExit(1)

    reg = _HARNESS_REGISTRY[harness]

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

    # MCP fallback path: write the harness-specific MCP server config
    # instead of the default markdown-injection content.
    if mcp_fallback:
        files_written = _wire_mcp_fallback(harness, port, root, force)
        return _build_result(harness, "mcp_server_config", files_written, root)

    # Proxy path: wire the harness to use the AgentAlloy proxy URL.
    if proxy:
        files_written = _wire_proxy(harness, port, root, force, scope)
        return _build_result(harness, "proxy", files_written, root)

    # Markdown-injection / system-prompt-snippet wiring is deprecated in
    # favor of the proxy model. Warn once per session.
    global _deprecation_warned
    if not _deprecation_warned:
        _deprecation_warned = True
        print(
            "DEPRECATION: markdown-injection wiring is deprecated. "
            "The proxy model (agentalloy wire --proxy) is the recommended "
            "approach. See docs for migration.",
            file=sys.stderr,
        )

    # Handle Continue.dev specially
    if harness in ("continue-closed", "continue-local"):
        variant = "closed" if harness == "continue-closed" else "local"
        files_written = _wire_continue(root, port, variant)
        return _build_result(harness, reg["vector"], files_written, root)

    # Handle manual: emit the sentinel block on stderr (so stdout stays
    # parseable as the JSON result the runbook reads).
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

    # Tamper detection: if a prior wire-harness run recorded this path and
    # the current inner content's sha256 differs, the user edited inside
    # the sentinels. Refuse to clobber without --force.
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
        # We own the entire file
        install_state._atomic_write(target_path, rendered)  # pyright: ignore[reportPrivateUsage]
        action = "wrote_new_file"
        # sha256 of the rendered file content for drift detection
        content_sha256 = _sha256(rendered.strip())
    else:
        # Sentinel-bounded injection
        existing = target_path.read_text() if target_path.exists() else ""
        result_content = _inject_sentinel_block(existing, rendered)
        install_state._atomic_write(target_path, result_content)  # pyright: ignore[reportPrivateUsage]
        action = "injected_block"
        # sha256 of the inner content (matches what uninstall extracts via
        # _extract_sentinel_content, which strips surrounding whitespace).
        content_sha256 = _sha256(rendered.strip())

    files_written = [
        {
            "path": str(target_path),
            "action": action,
            "sentinel_begin": SENTINEL_BEGIN if not dedicated else None,
            "sentinel_end": SENTINEL_END if not dedicated else None,
            "content_sha256": content_sha256,
        }
    ]

    # For aider, also wire .aider.conf.yml
    if harness == "aider":
        files_written.extend(_wire_aider_conf(root))

    # For Tier 3 harnesses, write watcher config and print guidance
    _tier3_harnesses = frozenset(
        {"cursor", "windsurf", "github-copilot", "cline", "gemini-cli", "aider"}
    )
    if harness in _tier3_harnesses:
        _wire_tier3_watcher_config(harness, root)

    # For claude-code, additionally wire signal-layer hooks
    if harness == "claude-code":
        files_written.extend(_wire_claude_code_hooks(root))

    # Probe for code-indexer and persist result to state.json
    _probe_code_indexer(root)

    return _build_result(harness, reg["vector"], files_written, root)


def _wire_aider_conf(root: Path) -> list[dict[str, Any]]:
    """Add our instructions file to .aider.conf.yml's read list."""
    conf_path = root / ".aider.conf.yml"
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
        }
    ]


# ---------------------------------------------------------------------------
# MCP fallback wiring
# ---------------------------------------------------------------------------

_AGENTALLOY_HOOKS_MARKER = "agentalloy-signal"

# Hook command template — AGENTALLOY_HOOK_PATH is replaced at wire time.
_HOOK_COMMAND_TEMPLATE = (
    "AGENTALLOY_HOOK_EVENT={event} AGENTALLOY_TOOL_NAME=$CLAUDE_TOOL_NAME "
    "AGENTALLOY_TOOL_PATH=$CLAUDE_TOOL_PATH bash {hook_path}"
)
_HOOK_COMMAND_UPS = "AGENTALLOY_HOOK_EVENT=UserPromptSubmit bash {hook_path}"


def _resolve_hook_path() -> str:
    """Return the installed path to agentalloy-signal.sh, copying it to ~/.agentalloy/hooks/ first."""
    import shutil

    hooks_dir = Path.home() / ".agentalloy" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "agentalloy-signal.sh"

    # Try to find the script relative to this package
    import agentalloy

    candidates = [
        Path(agentalloy.__file__).resolve().parent.parent.parent.parent
        / "tools"
        / "agentalloy-signal.sh",
        Path(__file__).resolve().parent.parent.parent.parent.parent
        / "tools"
        / "agentalloy-signal.sh",
    ]
    for src in candidates:
        if src.exists():
            shutil.copy2(str(src), str(dest))
            dest.chmod(0o755)
            return str(dest)

    # Fallback: write a minimal stub if not found
    if not dest.exists():
        dest.write_text(
            "#!/usr/bin/env bash\nagentalloy signal evaluate-phase 2>/dev/null || true\nexit 0\n"
        )
        dest.chmod(0o755)
    return str(dest)


def _wire_claude_code_hooks(root: Path) -> list[dict[str, Any]]:
    """Write AgentAlloy signal-layer hooks to .claude/settings.json (merging with existing)."""
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    config: dict[str, Any] = {}
    if settings_path.exists():
        try:
            config = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            config = {}

    hook_path = _resolve_hook_path()
    hooks = config.setdefault("hooks", {})

    # UserPromptSubmit
    ups_cmd = f"AGENTALLOY_HOOK_EVENT=UserPromptSubmit bash {hook_path}"
    ups_hooks = hooks.setdefault("UserPromptSubmit", [])
    # Remove any existing agentalloy entry before re-adding
    ups_hooks[:] = [h for h in ups_hooks if _AGENTALLOY_HOOKS_MARKER not in str(h)]
    ups_hooks.append(
        {
            "matcher": "",
            "_agentalloy": _AGENTALLOY_HOOKS_MARKER,
            "hooks": [{"type": "command", "command": ups_cmd}],
        }
    )

    # PostToolUse
    ptu_cmd = (
        f"AGENTALLOY_HOOK_EVENT=PostToolUse "
        f"AGENTALLOY_TOOL_NAME=$TOOL_NAME "
        f"AGENTALLOY_TOOL_PATH=$TOOL_INPUT_PATH bash {hook_path}"
    )
    ptu_hooks = hooks.setdefault("PostToolUse", [])
    ptu_hooks[:] = [h for h in ptu_hooks if _AGENTALLOY_HOOKS_MARKER not in str(h)]
    ptu_hooks.append(
        {
            "matcher": "Edit|Write|MultiEdit",
            "_agentalloy": _AGENTALLOY_HOOKS_MARKER,
            "hooks": [{"type": "command", "command": ptu_cmd}],
        }
    )

    # PreToolUse
    pretool_cmd = (
        f"AGENTALLOY_HOOK_EVENT=PreToolUse AGENTALLOY_TOOL_NAME=$TOOL_NAME bash {hook_path}"
    )
    pretool_hooks = hooks.setdefault("PreToolUse", [])
    pretool_hooks[:] = [h for h in pretool_hooks if _AGENTALLOY_HOOKS_MARKER not in str(h)]
    pretool_hooks.append(
        {
            "matcher": ".*",
            "_agentalloy": _AGENTALLOY_HOOKS_MARKER,
            "hooks": [{"type": "command", "command": pretool_cmd}],
        }
    )

    serialized = json.dumps(config, indent=2) + "\n"
    install_state._atomic_write(settings_path, serialized)  # pyright: ignore[reportPrivateUsage]

    return [
        {
            "path": str(settings_path),
            "action": "merged_hooks",
            "marker_key": _AGENTALLOY_HOOKS_MARKER,
            "content_sha256": _sha256(serialized),
        }
    ]


def _wire_tier3_watcher_config(harness: str, root: Path) -> None:
    """Write watcher config and print Tier 3 guidance. Soft-fail."""
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
        f"\n[AgentAlloy — Tier 3 wiring]\n"
        f"You selected: {harness}\n\n"
        "Tier 3 harnesses do not support per-turn hooks. To get phase- and\n"
        "contract-driven context updates, run the watcher sidecar:\n\n"
        f"    agentalloy watch start --harness {harness}\n\n"
        "Run under tmux, systemd, or launchd for persistence. Without the\n"
        "watcher, you'll only get the initial workflow skill context. System\n"
        "skills (commit-safety, etc.) are advisory-only on Tier 3.\n\n"
        "See docs/tier3-experience.md for the full picture.\n",
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


def _unwire_claude_code_hooks(root: Path) -> list[dict[str, Any]]:
    """Remove AgentAlloy signal-layer hooks from .claude/settings.json."""
    settings_path = root / ".claude" / "settings.json"
    if not settings_path.exists():
        return []
    try:
        config = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    hooks = config.get("hooks", {})
    changed = False
    for event in ("UserPromptSubmit", "PostToolUse", "PreToolUse"):
        original = hooks.get(event, [])
        filtered = [h for h in original if _AGENTALLOY_HOOKS_MARKER not in str(h)]
        if len(filtered) != len(original):
            hooks[event] = filtered
            changed = True

    if not changed:
        return []

    serialized = json.dumps(config, indent=2) + "\n"
    install_state._atomic_write(settings_path, serialized)  # pyright: ignore[reportPrivateUsage]
    return [{"path": str(settings_path), "action": "removed_hooks"}]


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
        }
    ]


def _wire_mcp_cursor(port: int, root: Path) -> list[dict[str, Any]]:
    """Write the agentalloy MCP entry to <repo>/.cursor/mcp.json."""
    config_path = root / ".cursor" / "mcp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {}
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
        }
    ]


def _wire_mcp_continue(port: int, root: Path, variant: str) -> list[dict[str, Any]]:
    """Write the agentalloy MCP entry into .continuerc.json."""
    config_path = root / ".continuerc.json"
    config: dict[str, Any] = {}
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
        }
    ]


# ---------------------------------------------------------------------------
# Proxy wiring
# ---------------------------------------------------------------------------

_PROXY_SUPPORTED_API = frozenset({"continue-closed", "continue-local"})


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
    if harness in _PROXY_SUPPORTED_API:
        return _wire_proxy_continue(harness, port, root)

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
            "of the default markdown-injection variant. Supported on: claude-code, "
            "cursor, continue-closed, continue-local. The MCP server module lives at "
            "agentalloy.install.mcp_server."
        ),
    )
    p.add_argument(
        "--proxy",
        action="store_true",
        help=(
            "Wire the harness to use the AgentAlloy proxy (recommended). "
            "For harnesses that support custom API endpoints (Continue), "
            "configures the API base URL. For others, writes a proxy "
            "instruction block."
        ),
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    st = install_state.load_state()
    port = install_state.validate_port(
        args.port if args.port is not None else st.get("port", 47950)
    )
    result = wire_harness(
        args.harness,
        port=port,
        force=args.force,
        mcp_fallback=args.mcp_fallback,
        proxy=getattr(args, "proxy", False),
        scope=args.scope,
    )
    if not getattr(args, "quiet", False):
        print(json.dumps(result, indent=2))
    return 0


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers (e.g. simple_setup)."""
    return _run(args)
