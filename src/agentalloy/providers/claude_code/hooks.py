"""Claude Code hook wiring — the DEFAULT claude-code integration.

The hook is the default (over proxy) because of a failure-mode asymmetry:
proxy wiring rewrites ``ANTHROPIC_BASE_URL`` to the local proxy, so a down
AgentAlloy service breaks Claude Code entirely. Hook wiring degrades
gracefully — when the hook endpoint is unreachable the hook script exits 0
with no output (see ``agentalloy-hook-claude-code.sh``), leaving Claude Code
behaving like vanilla Claude. The hook is also the signal layer's native
carrier (it wakes on prompt submit and tool fires).

What ``hook_writer`` does:

1. Installs the packaged hook script to a stable, user-scoped location
   (``~/.agentalloy/hooks/agentalloy-hook-claude-code.sh``, chmod 0o755).
2. Merges hook entries into ``~/.claude/settings.json`` in Claude Code's
   current (2026) format, preserving ALL existing user content. Idempotent:
   wiring twice does not duplicate entries (matched on our script path).

Settings.json merge/restore design:

- The original file content is captured BEFORE modification and returned in
  the WireRecord's ``original_content`` so uninstall's restore-original
  branch can put the file back byte-identically. When the file did not
  exist, ``original_content`` is None and the record's action is
  ``wrote_new_file``, so uninstall deletes the file we created.
- A malformed/unparseable existing settings.json is refused with an
  actionable error — we never clobber a file we cannot safely round-trip.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from agentalloy.providers.base import WireRecord

# Hook events we wire. Rationale:
# - SessionStart: intake is the session front door — every session opens with
#   the intake workflow skill (resume-or-new check, then routing). No matcher.
# - UserPromptSubmit: the signal layer's primary wake — evaluate phase exit
#   gates on every prompt and inject the composed workflow block.
# - PreToolUse: lets system skills (commit-safety, etc.) fire before a tool
#   runs; matcher "*" so every tool is observed.
# - PostToolUse: contract validation when .agentalloy/contracts/ files are
#   written; matcher "*" so the router can filter by tool/path itself.
_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")

# Events that take a Claude Code tool "matcher"; the prompt/session events don't.
_MATCHER_EVENTS = ("PreToolUse", "PostToolUse")

# Per-event endpoint env var + URL path. The script reads these to know where
# to POST for each event type.
_EVENT_ENDPOINTS: dict[str, tuple[str, str]] = {
    "SessionStart": ("AGENTALLOY_HOOK_URL_SESSION", "/v1/hook/session-start"),
    "UserPromptSubmit": ("AGENTALLOY_HOOK_URL", "/v1/hook/user-prompt-submit"),
    "PreToolUse": ("AGENTALLOY_HOOK_URL_PRE", "/v1/hook/pre-tool-use"),
    "PostToolUse": ("AGENTALLOY_HOOK_URL_POST", "/v1/hook/post-tool-use"),
}

# Claude Code's per-hook timeout (seconds). Generous relative to the script's
# own 1s fail-open cap so the script — not the harness — owns the deadline.
_HOOK_TIMEOUT_S = 5


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _packaged_script_path() -> Path:
    """Path to the hook script that ships inside the wheel."""
    return (
        Path(__file__).resolve().parent.parent.parent / "install" / "agentalloy-hook-claude-code.sh"
    )


def installed_script_path() -> Path:
    """Stable, user-scoped install location for the hook script."""
    return Path.home() / ".agentalloy" / "hooks" / "agentalloy-hook-claude-code.sh"


def settings_json_path() -> Path:
    """Path to the user-scope Claude Code settings file."""
    return Path.home() / ".claude" / "settings.json"


def _install_script() -> WireRecord:
    """Copy the packaged hook script to its stable location (chmod 0o755).

    Returns a WireRecord. The script lives in a dedicated ``~/.agentalloy/hooks``
    directory we own outright, so the action is always ``wrote_new_file`` —
    uninstall deletes it. Re-running overwrites in place (idempotent).
    """
    dest = installed_script_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = _packaged_script_path()
    content = src.read_text(encoding="utf-8")
    shutil.copyfile(src, dest)
    dest.chmod(0o755)
    return WireRecord(
        path=str(dest),
        action="wrote_new_file",
        content_sha256=_sha256(content),
        original_content=None,
        marker_key="claude-code.hook-script",
    )


def _command_for(script_abs: str, env_var: str, url: str) -> str:
    """Build the shell command string for a hook entry.

    Claude Code runs the ``command`` string in a shell, so we prefix the
    per-event endpoint env var. Baking the URL in here (rather than relying on
    ambient env) keeps the wiring self-contained and re-readable for idempotency.
    """
    return f"{env_var}={url} {script_abs}"


def _build_hook_entries(port: int, script_abs: str) -> dict[str, list[dict[str, Any]]]:
    """Build the settings.json ``hooks`` sub-object for our events.

    Shape (Claude Code 2026):
        {
          "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "...", "timeout": N}]}
          ],
          "PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", ...}]}
          ],
          ...
        }
    """
    entries: dict[str, list[dict[str, Any]]] = {}
    for event in _HOOK_EVENTS:
        env_var, path = _EVENT_ENDPOINTS[event]
        url = f"http://localhost:{port}{path}"
        hook_obj: dict[str, Any] = {
            "type": "command",
            "command": _command_for(script_abs, env_var, url),
            "timeout": _HOOK_TIMEOUT_S,
        }
        group: dict[str, Any] = {"hooks": [hook_obj]}
        # Tool events take a matcher; SessionStart / UserPromptSubmit do not.
        if event in _MATCHER_EVENTS:
            group["matcher"] = "*"
        entries[event] = [group]
    return entries


def _entry_targets_our_script(group: dict[str, Any], script_abs: str) -> bool:
    """True if a settings.json event-group already references our script."""
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    for h in hooks:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(h, dict):
            cmd = h.get("command", "")
            if isinstance(cmd, str) and script_abs in cmd:
                return True
    return False


class MalformedSettingsError(RuntimeError):
    """Raised when ~/.claude/settings.json exists but is not parseable JSON."""


def _merge_into_settings(port: int, script_abs: str) -> WireRecord:
    """Merge our hook entries into ~/.claude/settings.json, preserving content.

    Captures the original content BEFORE modifying so uninstall can restore it.
    Idempotent: removes any prior group that targets our script before adding
    the fresh one, so re-wiring (e.g. on a port change) never duplicates.
    """
    settings_path = settings_json_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        original_content: str | None = settings_path.read_text(encoding="utf-8")
        try:
            data: dict[str, Any] = json.loads(original_content)
        except json.JSONDecodeError as exc:
            raise MalformedSettingsError(
                f"{settings_path} is not valid JSON ({exc}). Refusing to modify it. "
                f"FIX: repair the JSON syntax (or move the file aside) and re-run wiring."
            ) from exc
        if not isinstance(data, dict):
            raise MalformedSettingsError(
                f"{settings_path} does not contain a JSON object at the top level. "
                f"Refusing to modify it. FIX: repair the file and re-run wiring."
            )
    else:
        original_content = None
        data = {}

    hooks_section = data.get("hooks")
    if hooks_section is None:
        hooks_section = {}
    elif not isinstance(hooks_section, dict):
        raise MalformedSettingsError(
            f"{settings_path} has a 'hooks' field that is not a JSON object "
            f"(got {type(hooks_section).__name__}). Refusing to modify it."
        )

    our_entries = _build_hook_entries(port, script_abs)
    for event, groups in our_entries.items():
        existing = hooks_section.get(event)
        existing_list: list[dict[str, Any]] = list(existing) if isinstance(existing, list) else []
        # Drop any prior group that targets our script (idempotent re-wire).
        kept = [g for g in existing_list if not _entry_targets_our_script(g, script_abs)]
        hooks_section[event] = kept + groups

    data["hooks"] = hooks_section
    serialized = json.dumps(data, indent=2) + "\n"
    settings_path.write_text(serialized, encoding="utf-8")

    return WireRecord(
        path=str(settings_path),
        # wrote_new_file → uninstall deletes the file we created.
        # injected_block → uninstall's restore-original branch (original_content)
        #   puts the prior file back byte-identically.
        action="wrote_new_file" if original_content is None else "injected_block",
        content_sha256=_sha256(serialized),
        original_content=original_content,
        marker_key="claude-code.settings-hooks",
    )


def hook_writer(port: int, root: Path) -> list[WireRecord]:
    """Wire Claude Code hooks (the default claude-code integration).

    Installs the hook script and merges hook entries into the user-scope
    settings.json. Returns WireRecords for both, with ``original_content``
    captured so uninstall can fully reverse the change.
    """
    records: list[WireRecord] = []
    records.append(_install_script())
    records.append(_merge_into_settings(port, str(installed_script_path())))
    return records
