"""Per-workstream identifier resolution for issue #548.

Code-index lookups use ``repo_slug`` (worktree-independent). Workflow state
needs per-worktree isolation, so callers that touch the state store must
resolve a ``stream_id`` that distinguishes concurrent worktrees of the same
repo.

Resolution order:

1. **Explicit binding** — a file at ``.agentalloy/.stream`` containing the
   stream identifier, or the ``AGENTALLOY_STREAM_ID`` environment variable.
2. **Worktree path** — SHA-256 of the absolute project root (gives each
   distinct checkout its own identifier).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_STREAM_FILE = ".agentalloy" / ".stream"


def resolve_stream_id(project_root: Path) -> str:
    """Return a per-worktree stream identifier for *project_root*.

    Resolution:

    1. ``.agentalloy/.stream`` file in the project root.
    2. ``AGENTALLOY_STREAM_ID`` environment variable.
    3. SHA-256 of the absolute project root path.

    Returns the empty string when called from a context that cannot resolve
    a stream id (e.g. the project root does not exist).
    """
    # 1 — explicit binding file
    stream_file = project_root / _STREAM_FILE
    if stream_file.is_file():
        try:
            text = stream_file.read_text().strip()
            if text:
                return text
        except OSError:
            pass

    # 2 — env var override
    env_val = os.environ.get("AGENTALLOY_STREAM_ID", "").strip()
    if env_val:
        return env_val

    # 3 — worktree path
    try:
        absolute = project_root.resolve()
        return hashlib.sha256(absolute.as_posix().encode()).hexdigest()[:16]
    except OSError:
        return ""


def bind_stream_id(project_root: Path, stream_id: str) -> None:
    """Write an explicit stream identifier to ``.agentalloy/.stream``.

    This is the programmatic counterpart to ``resolve_stream_id`` — callers
    that *want* a specific stream id (e.g. a harness) can pin it so that
    future sessions in this checkout share the same identifier.
    """
    stream_file = project_root / _STREAM_FILE
    stream_file.parent.mkdir(parents=True, exist_ok=True)
    stream_file.write_text(stream_id + "\n")
