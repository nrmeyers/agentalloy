"""Instance env.sh access — the file that starts the service and proxy.

The proxy's `POST /upstream` rewrites the AGENTALLOY_UPSTREAM_URL / AGENTALLOY_MODEL /
AGENTALLOY_UPSTREAM_KEY lines here so a runtime upstream change survives restarts.
env.sh lives next to state.duck in the instance directory (both come from
the same AGENTALLOY_* env vars at bring-up).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^export (?P<key>[A-Z0-9_]+)=(?P<value>.*)$")


def instance_env_path(state_duck: str) -> Path:
    """env.sh path derived from the state store location."""
    return Path(state_duck).parent / "env.sh"


def read_env_vars(path: Path) -> dict[str, str]:
    """Parse `export KEY=VALUE` lines into a dict (missing file -> {})."""
    vars_: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return vars_
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            vars_[m.group("key")] = m.group("value")
    return vars_


def update_env_vars(path: Path, updates: dict[str, str]) -> bool:
    """Rewrite the `export KEY=VALUE` lines for the given keys.

    Keys already present are replaced in place; missing keys are appended.
    Returns False when the file is absent (nothing to persist into).
    """
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _LINE.match(line)
        if m and m.group("key") in remaining:
            out.append(f"export {m.group('key')}={remaining.pop(m.group('key'))}")
        else:
            out.append(line)
    out.extend(f"export {key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out) + "\n")
    # The file can carry AGENTALLOY_UPSTREAM_KEY — owner-only, not world-readable.
    os.chmod(path, 0o600)
    return True
