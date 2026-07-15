"""Auto-wire a freshly created git worktree via a ``post-checkout`` hook.

Wiring is currently a one-shot, per-directory step: ``agentalloy add``/``wire``
(or the dedicated ``agentalloy worktree``) must be run again for every new
linked worktree of an already-wired repo, or that worktree silently composes
nothing (the proxy just passes requests through unchanged when
``.agentalloy/`` is absent). This module closes that gap for the common case —
a worktree created any way (``agentalloy worktree``, plain ``git worktree
add``, an IDE's worktree picker) — by installing a small ``post-checkout`` git
hook the first time a repo is wired. Git fires ``post-checkout`` for
``git worktree add`` with the new worktree as cwd and the previous-HEAD arg set
to the all-zero SHA (confirmed empirically — this is the same "no prior HEAD"
signal git itself uses to distinguish a fresh working tree from an ordinary
branch switch, which fires ``post-checkout`` with a real previous SHA). Hooks
resolve via the repo's *shared* common git dir (``git rev-parse --git-path``),
not per-worktree, so installing once from any checkout covers every worktree,
present and future — mirrors ``wire._resolve_git_exclude``'s approach to the
same shared-vs-per-worktree resolution problem.

The hook itself does no wiring logic — it just shells out to
``agentalloy auto-wire-worktree`` (see
``agentalloy.install.subcommands.auto_wire_worktree``), so the actual
decision-making lives in testable Python, not shell.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

_BEGIN = "# >>> agentalloy post-checkout >>>"
_END = "# <<< agentalloy post-checkout <<<"
_GIT_TIMEOUT_S = 5.0

_BLOCK_BODY = (
    "# Auto-wires a freshly created git worktree so it composes without a\n"
    "# manual `agentalloy worktree`/`add` run — see agentalloy.install.git_hooks.\n"
    '# $1=previous HEAD  $2=new HEAD  $3="1" for a branch checkout, "0" for a file checkout.\n'
    'if [ "$1" = "0000000000000000000000000000000000000000" ] && [ "$3" = "1" ]; then\n'
    "  if command -v agentalloy >/dev/null 2>&1; then\n"
    "    agentalloy auto-wire-worktree >/dev/null 2>&1 || true\n"
    "  fi\n"
    "fi\n"
)


def _hook_path(root: Path) -> Path | None:
    """Resolve *root*'s shared ``post-checkout`` hook file, or ``None``.

    Uses ``git -C <root> rev-parse --git-path hooks/post-checkout`` — this
    resolves to the SAME file for the main checkout and every linked worktree
    (hooks live in the common git dir, never per-worktree), so installing once
    covers all of them. ``None`` when *root* isn't a git work tree or git isn't
    available.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks/post-checkout"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rel = out.stdout.strip()
    if out.returncode != 0 or not rel:
        return None
    return (root / rel) if not Path(rel).is_absolute() else Path(rel)


def install_post_checkout_hook(root: Path) -> Path | None:
    """Ensure *root*'s shared ``post-checkout`` hook contains our auto-wire block.

    Idempotent and additive: a pre-existing hook (the user's own, or a
    different tool's) is preserved — our sentinel-bounded block is appended,
    never overwriting existing content, mirroring ``wire._git_exclude_agentalloy``'s
    append-don't-clobber convention for shared repo-local files. A no-op when
    already installed. Best-effort: wiring must never fail over this — returns
    ``None`` on any error instead of raising.
    """
    hook = _hook_path(root)
    if hook is None:
        return None
    try:
        existing = hook.read_text(encoding="utf-8") if hook.exists() else ""
        if _BEGIN in existing:
            return hook
        prefix = "" if existing.strip() else "#!/bin/sh\n"
        body = existing if (not existing or existing.endswith("\n")) else existing + "\n"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(f"{prefix}{body}{_BEGIN}\n{_BLOCK_BODY}{_END}\n", encoding="utf-8")
        mode = hook.stat().st_mode
        hook.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        return None
    return hook


def uninstall_post_checkout_hook(root: Path) -> None:
    """Remove ONLY our sentinel block from *root*'s ``post-checkout`` hook.

    Leaves any surrounding hook content (the user's own, or a different
    tool's) untouched. Deletes the hook file entirely if our block was the
    only content. No-op when unresolvable or our block isn't present. Never
    raises.
    """
    hook = _hook_path(root)
    if hook is None or not hook.exists():
        return
    try:
        text = hook.read_text(encoding="utf-8")
        if _BEGIN not in text or _END not in text:
            return
        start = text.index(_BEGIN)
        end = text.index(_END) + len(_END)
        if text[end : end + 1] == "\n":
            end += 1
        remaining = text[:start] + text[end:]
        if remaining.strip() in ("", "#!/bin/sh"):
            hook.unlink()
        else:
            hook.write_text(remaining, encoding="utf-8")
    except (OSError, ValueError):
        pass
