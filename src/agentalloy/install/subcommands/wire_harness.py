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
  antigravity     → GEMINI.md  (Antigravity CLI, formerly Gemini CLI)
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
import os
import shutil
import subprocess
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
    "antigravity": {
        # Antigravity CLI (formerly Gemini CLI) still reads GEMINI.md.
        "target": "GEMINI.md",
        "template": "antigravity.md",
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

# Deprecated alias — Antigravity CLI was formerly Gemini CLI. Same config dict.
_HARNESS_REGISTRY["gemini-cli"] = _HARNESS_REGISTRY["antigravity"]

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
    """Deprecated public alias for :func:`_wire_harness_core`.

    .. deprecated::
        This function is deprecated.  Use
        ``agentalloy.providers.REGISTRY[harness].install_writer`` instead.

    Internal product callers (``add``, ``wire``, ``wrap``) call
    :func:`_wire_harness_core` directly so the canonical ``add`` verb does not
    trip this warning; this shim remains only for external/legacy callers.
    """
    warnings.warn(
        "wire_harness() is deprecated; use agentalloy.providers.REGISTRY "
        "instead. This module will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _wire_harness_core(
        harness,
        port=port,
        root=root,
        force=force,
        mcp_fallback=mcp_fallback,
        legacy=legacy,
        scope=scope,
    )


def _wire_harness_core(
    harness: str,
    port: int = 47950,
    root: Path | None = None,
    force: bool = False,
    mcp_fallback: bool = False,
    legacy: bool = False,
    scope: str = "user",
) -> dict[str, Any]:
    """Wire the specified harness. Returns contract-shaped result.

    The actual wiring mechanism, shared by ``add``/``wire``/``wrap``. (The
    public :func:`wire_harness` is a deprecated warning shim over this.)

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


# Harnesses we know how to wire MCP for. Others (antigravity, opencode,
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

    if harness == "openclaw":
        # openclaw wires ~/.openclaw/plugins.json, not a repo instruction file —
        # its legacy registry `target` is None, so the instruction fallback below
        # crashed on `root / None`. Delegate to the provider registry's working
        # install_writer instead.
        writer = REGISTRY["openclaw"].install_writer
        assert writer is not None, "openclaw registers an install_writer"
        records = writer(port, root, _force)
        return [r.to_dict() for r in records]

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


def _restart_hermes_gateway(root: Path) -> bool:
    """Restart (or start) the repo-scoped Hermes gateway under ``<root>/.hermes``.

    The Hermes gateway is a long-lived daemon that captures ``HERMES_HOME`` at
    process start, so the repo-local proxy config only takes effect once a
    gateway is (re)started under the repo home. Gateways are home-scoped (pid
    file + flock under ``HERMES_HOME``, no control port), so this per-repo
    gateway coexists with the user's global one.

    Returns True when the restart succeeded, False otherwise — a failure never
    fails the wiring itself; the manual steps are printed instead.
    """
    manual_hint = (
        "[AgentAlloy] Start the repo gateway manually before using hermes here:\n"
        "    source .hermes/.agentalloy-env && hermes gateway restart\n"
        "then open a new hermes session in this repo."
    )

    hermes = shutil.which("hermes")
    if hermes is None:
        print(
            "[AgentAlloy] `hermes` not found on PATH — could not start the repo gateway.",
            file=sys.stderr,
        )
        print(manual_hint, file=sys.stderr)
        return False

    env = {**os.environ, "HERMES_HOME": str(root / ".hermes")}
    try:
        proc = subprocess.run(  # noqa: S603
            [hermes, "gateway", "restart"],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[AgentAlloy] hermes gateway restart failed: {e}", file=sys.stderr)
        print(manual_hint, file=sys.stderr)
        return False

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        print(
            f"[AgentAlloy] hermes gateway restart exited {proc.returncode}: " + " | ".join(tail),
            file=sys.stderr,
        )
        print(manual_hint, file=sys.stderr)
        return False

    print(
        "[AgentAlloy] Repo-scoped hermes gateway (re)started under .hermes/ — "
        "new sessions in this repo route through the proxy.",
        file=sys.stderr,
    )
    return True


def _activation_managers() -> set[str]:
    """Which per-directory env managers are installed (``direnv``, ``mise``).

    Detected at wire time from PATH — the activation carriers below are only
    written for managers that can actually load them, so ``agentalloy add``
    never litters a repo with files nothing will read.
    """
    return {name for name in ("direnv", "mise") if shutil.which(name)}


def _write_hermes_mise_env(root: Path, records: list[dict[str, Any]]) -> bool:
    """Add ``HERMES_HOME`` to the repo's mise config ``[env]`` table.

    Targets an existing ``mise.toml`` / ``.mise.toml`` (in that order) or
    creates ``mise.toml``. The insertion is sentinel-bounded for uninstall and
    idempotent for re-wiring. Two shapes:

    - no ``[env]`` table: append a sentinel-bounded block declaring one at EOF
      (a trailing table never collides with earlier tables);
    - existing ``[env]`` table: insert only the sentinel-bounded key line right
      after the header — TOML forbids a second ``[env]`` table.

    The result is validated with ``tomllib``; on a parse failure the original
    content is restored and False is returned so the caller can fall back to
    the manual hint. Returns True when the carrier landed.
    """
    import tomllib

    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"
    key_line = 'HERMES_HOME = "{{config_root}}/.hermes"'

    mise_path = next(
        (p for name in ("mise.toml", ".mise.toml") if (p := root / name).exists()),
        root / "mise.toml",
    )
    original = _capture_original(mise_path)
    content = original if original is not None else ""

    if sentinel_begin in content and sentinel_end in content:
        # Idempotent replace of the prior block (key line or full [env] block).
        begin_idx = content.index(sentinel_begin)
        end_idx = content.index(sentinel_end) + len(sentinel_end)
        had_env_outside = "[env]" in (content[:begin_idx] + content[end_idx:])
        block = (
            f"{sentinel_begin}\n{key_line}\n{sentinel_end}"
            if had_env_outside
            else f"{sentinel_begin}\n[env]\n{key_line}\n{sentinel_end}"
        )
        new_content = content[:begin_idx] + block + content[end_idx:]
    elif "[env]" in content:
        header_end = content.index("[env]") + len("[env]")
        insertion = f"\n{sentinel_begin}\n{key_line}\n{sentinel_end}"
        new_content = content[:header_end] + insertion + content[header_end:]
    else:
        block = f"{sentinel_begin}\n[env]\n{key_line}\n{sentinel_end}\n"
        if content and not content.endswith("\n"):
            content += "\n"
        new_content = content + block

    try:
        tomllib.loads(new_content)
    except tomllib.TOMLDecodeError as e:
        print(
            f"[AgentAlloy] {mise_path.name} edit would produce invalid TOML ({e}) — "
            "skipped the mise carrier.",
            file=sys.stderr,
        )
        return False

    install_state._atomic_write(mise_path, new_content)  # pyright: ignore[reportPrivateUsage]
    records.append(
        {
            "path": str(mise_path),
            "action": "wrote_new_file" if original is None else "replaced_file",
            "content_sha256": _sha256(new_content),
            **({"original_content": original} if original is not None else {}),
        }
    )
    return True


def _wire_proxy_hermes_agent(port: int, root: Path, scope: str) -> list[dict[str, Any]]:
    """Wire Hermes Agent to use the AgentAlloy proxy (per-repo interception).

    Hermes config is home-scoped (``~/.hermes/config.yaml``) with no per-repo
    form, but the proxy needs a per-repo ``/proj/<token>`` discriminator. So we
    isolate per repo via ``HERMES_HOME``: a repo-local ``.hermes/`` activated by
    ``.hermes/.agentalloy-env`` (sourced directly, or automatically on ``cd``
    via the activation carriers written below — ``.envrc`` for direnv users,
    a ``mise.toml`` ``[env]`` entry for mise users, chosen by PATH detection).

    The repo-local ``config.yaml`` is a copy of the user's global one with only
    the ``model`` block redirected at the proxy, so their other tuning survives.
    Where the proxy then *forwards* (upstream adoption) is handled separately by
    ``agentalloy add`` writing ``.agentalloy/upstream``.

    Because the hermes gateway daemon captures ``HERMES_HOME`` at startup, the
    wiring finishes by (re)starting a repo-scoped gateway via
    :func:`_restart_hermes_gateway` — without it the env file alone changes
    nothing for gateway-routed sessions.

    ``scope`` is ignored: hermes is inherently per-repo (like claude-code), so it
    always wires the repo-local carrier at *root*.
    """
    import yaml

    from agentalloy.api.proxy_context import encode_proj_token

    _ = scope
    token = encode_proj_token(root)
    proxy_base = f"http://localhost:{port}/proj/{token}/v1"

    # Start from the user's global config so their non-endpoint settings survive
    # under HERMES_HOME; fall back to a minimal config if there is none.
    global_config = Path.home() / ".hermes" / "config.yaml"
    config: dict[str, Any] = {}
    if global_config.exists():
        try:
            loaded = yaml.safe_load(global_config.read_text())
            if isinstance(loaded, dict):
                config = loaded
        except yaml.YAMLError:
            config = {}

    model = config.get("model")
    if not isinstance(model, dict):
        model = {}
    model["provider"] = "custom"
    model["base_url"] = proxy_base
    model["default"] = "agentalloy-proxy"
    config["model"] = model

    records: list[dict[str, Any]] = []

    repo_config = root / ".hermes" / "config.yaml"
    repo_config.parent.mkdir(parents=True, exist_ok=True)
    original_config = _capture_original(repo_config)
    config_text = yaml.safe_dump(config, sort_keys=False)
    install_state._atomic_write(repo_config, config_text)  # pyright: ignore[reportPrivateUsage]
    records.append(
        {
            "path": str(repo_config),
            "action": "wrote_new_file" if original_config is None else "replaced_file",
            "content_sha256": _sha256(config_text),
            **({"original_content": original_config} if original_config is not None else {}),
        }
    )

    env_path = root / ".hermes" / ".agentalloy-env"
    original_env = _capture_original(env_path)
    env_text = 'export HERMES_HOME="$PWD/.hermes"\n'
    install_state._atomic_write(env_path, env_text)  # pyright: ignore[reportPrivateUsage]
    records.append(
        {
            "path": str(env_path),
            "action": "wrote_new_file" if original_env is None else "replaced_file",
            "content_sha256": _sha256(env_text),
            **({"original_content": original_env} if original_env is not None else {}),
        }
    )

    # Activation carriers: auto-set HERMES_HOME on cd, but only via managers
    # actually installed (PATH detection) — hermes has no native settings-file
    # carrier, so the env var is the only activation path, and a carrier no
    # manager reads is just litter. Sentinel style matches the claude-code
    # wiring so uninstall's record walk handles it.
    managers = _activation_managers()
    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"
    rel_env = env_path.relative_to(root).as_posix()
    activated: list[str] = []

    envrc_path = root / ".envrc"
    if "direnv" in managers or envrc_path.exists():
        envrc_original = _capture_original(envrc_path)
        envrc_block = f"{sentinel_begin}\nsource_env {rel_env}\n{sentinel_end}"
        if envrc_path.exists():
            envrc_content = envrc_path.read_text()
            if sentinel_begin in envrc_content and sentinel_end in envrc_content:
                begin_idx = envrc_content.index(sentinel_begin)
                end_idx = envrc_content.index(sentinel_end) + len(sentinel_end)
                if end_idx < len(envrc_content) and envrc_content[end_idx] == "\n":
                    end_idx += 1
                envrc_content = (
                    envrc_content[:begin_idx] + envrc_block + "\n" + envrc_content[end_idx:]
                )
            else:
                if envrc_content and not envrc_content.endswith("\n"):
                    envrc_content += "\n"
                envrc_content += envrc_block + "\n"
        else:
            envrc_content = envrc_block + "\n"
        install_state._atomic_write(envrc_path, envrc_content)  # pyright: ignore[reportPrivateUsage]
        records.append(
            {
                "path": str(envrc_path),
                "action": "wrote_new_file" if envrc_original is None else "replaced_file",
                "content_sha256": _sha256(envrc_block),
                **({"original_content": envrc_original} if envrc_original is not None else {}),
            }
        )
        activated.append(".envrc (direnv: run `direnv allow` once)")

    has_mise_config = (root / "mise.toml").exists() or (root / ".mise.toml").exists()
    if ("mise" in managers or has_mise_config) and _write_hermes_mise_env(root, records):
        activated.append("mise.toml [env] (run `mise trust` once; loads on cd)")

    if activated:
        print(
            f"[AgentAlloy] HERMES_HOME auto-activation wired: {'; '.join(activated)}.",
            file=sys.stderr,
        )
    else:
        print(
            f"[AgentAlloy] No direnv/mise detected — activate manually per shell: "
            f"`source {rel_env}` before running hermes in this repo.",
            file=sys.stderr,
        )

    # The gateway daemon captured HERMES_HOME at startup — without a restart
    # under the repo home, gateway-routed sessions keep the old endpoint.
    _restart_hermes_gateway(root)

    return records


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
    """Wire Claude Code to use the AgentAlloy proxy (auth-transparent passthrough).

    Writes a per-repo carrier at ``<root>/.agentalloy/claude-code-env.sh``
    holding a sentinel-bounded ``ANTHROPIC_BASE_URL`` export. The URL embeds the
    repo's ``/proj/<token>`` discriminator so the proxy resolves this repo from
    the URL alone (see :func:`agentalloy.api.proxy_context.encode_proj_token`).

    Auth transparency: we set **only** ``ANTHROPIC_BASE_URL`` and never
    ``ANTHROPIC_API_KEY``. Setting any API key forces Claude Code into API-key
    mode, which breaks account/OAuth auth for Pro/Max/Team users who have no API
    key. The proxy forwards the caller's own credential upstream untouched.

    Carriers, in order of preference:

    1. ``<root>/.claude/settings.local.json`` ``env`` block — Claude Code reads it
       natively, so the proxy auto-loads with no shell step. Gitignored by
       convention, so the machine-specific URL stays out of git. Common-case path;
       prints a one-line confirmation (no must-source wart).
    2. ``<root>/.envrc`` — if one already exists, a sentinel-bounded ``source_env``
       line is appended (idempotently) so direnv loads the shell env file on ``cd``.
    3. Fallback hint — only when (1) is unavailable (malformed settings file) AND
       there is no ``.envrc``: tell the user to source the env file themselves.
    """
    from agentalloy.api.proxy_context import encode_proj_token
    from agentalloy.providers.claude_code.install import apply_claude_settings_env

    agentalloy_dir = root / ".agentalloy"
    agentalloy_dir.mkdir(parents=True, exist_ok=True)

    env_path = agentalloy_dir / "claude-code-env.sh"
    original_content = _capture_original(env_path)
    sentinel_begin = "# <!-- BEGIN agentalloy install -->"
    sentinel_end = "# <!-- END agentalloy install -->"

    # The /proj/<token> discriminator lets the stateless proxy resolve this repo
    # from the URL path alone, independent of its own cwd. No /v1 suffix: the
    # Anthropic SDK appends /v1/messages to the base URL, so a /v1 here would
    # produce /v1/v1/messages (404 against the proxy).
    token = encode_proj_token(root)
    proxy_url = f"http://localhost:{port}/proj/{token}"
    block_lines = [
        sentinel_begin,
        f"export ANTHROPIC_BASE_URL={proxy_url}",
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

    records: list[dict[str, Any]] = [
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

    # Primary carrier: Claude Code natively reads the `env` map from
    # .claude/settings.local.json, so writing ANTHROPIC_BASE_URL there auto-loads
    # the proxy with no shell/direnv step (this kills the "you must source this"
    # wart for the common case). settings.local.json is gitignored by Claude Code
    # convention, so the machine-specific /proj/<token> URL stays out of git.
    # Cleanup is surgical via uninstall_proxy._unwire_proxy_claude_code_settings,
    # so this is deliberately NOT added to `records` (a record-driven restore
    # would clobber unrelated user edits to settings.local.json on unwire).
    settings_wired = False
    settings_rel = ""
    try:
        settings_path, _ = apply_claude_settings_env(root, proxy_url)
        settings_wired = True
        settings_rel = settings_path.relative_to(root).as_posix()
    except json.JSONDecodeError:
        print(
            f"[AgentAlloy] {root / '.claude' / 'settings.local.json'} is not valid JSON — "
            "skipped the auto-load carrier; the env file below still works.",
            file=sys.stderr,
        )

    # Secondary carrier: prefer direnv when an .envrc already exists; otherwise a
    # one-line hint — but only when settings.local.json did NOT auto-wire (else
    # the var already loads and the hint would be noise).
    envrc_path = root / ".envrc"
    rel_env = env_path.relative_to(root).as_posix()
    if envrc_path.exists():
        envrc_original = _capture_original(envrc_path)
        envrc_block = f"{sentinel_begin}\nsource_env {rel_env}\n{sentinel_end}"
        envrc_content = envrc_path.read_text()
        if sentinel_begin in envrc_content and sentinel_end in envrc_content:
            begin_idx = envrc_content.index(sentinel_begin)
            end_idx = envrc_content.index(sentinel_end) + len(sentinel_end)
            if end_idx < len(envrc_content) and envrc_content[end_idx] == "\n":
                end_idx += 1
            envrc_content = envrc_content[:begin_idx] + envrc_block + "\n" + envrc_content[end_idx:]
        else:
            if envrc_content and not envrc_content.endswith("\n"):
                envrc_content += "\n"
            envrc_content += envrc_block + "\n"
        install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
            envrc_path, envrc_content
        )
        records.append(
            {
                "path": str(envrc_path),
                "action": "wrote_new_file" if envrc_original is None else "replaced_file",
                "content_sha256": _sha256(envrc_block),
                **({"original_content": envrc_original} if envrc_original is not None else {}),
            }
        )
    elif not settings_wired:
        # No .envrc AND settings.local.json carrier unavailable: the var won't
        # auto-load, so emit the must-source hint and attach it to the env-file
        # record (rather than a pathless record uninstall would warn about) so the
        # returned result still carries the guidance.
        hint = (
            f"AgentAlloy wrote {env_path}. Load it before running Claude Code: "
            f"`source {rel_env}` in your shell, or add `source_env {rel_env}` to a "
            "direnv `.envrc` in this repo."
        )
        print(f"[AgentAlloy] {hint}", file=sys.stderr)
        records[0]["carrier_hint"] = hint

    if settings_wired:
        print(
            f"[AgentAlloy] Wired Claude Code → AgentAlloy proxy via {settings_rel} — "
            "Claude Code loads it automatically, no shell setup needed. "
            f"(Shell/direnv users can also `source {rel_env}`.)",
            file=sys.stderr,
        )

    return records


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
