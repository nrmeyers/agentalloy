"""Release-update check — the single place that talks to the GitHub releases API.

The running service is otherwise offline by design (it makes no outbound calls).
This module is the one exception, and it is deliberately ringfenced:

* **One throttled producer** — :func:`refresh` hits the GitHub releases API at
  most once per :data:`CHECK_INTERVAL_SECONDS` and writes a tiny JSON cache.
* **Cheap read-only consumers** — :func:`notice` (statusline badge, server-start
  line, status row) read that cache and **never** touch the network.
* **Fail-silent** — nothing here raises; a failed fetch or unwritable cache
  degrades to "no update shown", never to a traceback on a hot path.
* **Opt-out** — ``AGENTALLOY_RELEASE_CHECK=0`` (also ``off``/``false``/``no``)
  disables the check. The effective state is persisted into the cache so
  cache-only CLI consumers honour it even when the ``.env`` that carries the
  toggle is sourced only into the long-lived service process.

The version helpers (:func:`parse_semver`, :func:`current_version`,
:func:`fetch_latest_tag`, :func:`fetch_release_info`) live here too so that all
GitHub-API code sits in one module; ``upgrade.py`` imports them.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state

REPO = "nrmeyers/agentalloy"
_RELEASES_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
_RELEASES_BY_TAG = f"https://api.github.com/repos/{REPO}/releases/tags/"
_USER_AGENT = "agentalloy-release-check"

# Check the network at most this often. A new-release nudge is not time-critical,
# so a day between checks keeps us well clear of GitHub's 60 req/hr/IP limit on
# the unauthenticated API and adds no per-turn cost (consumers read the cache).
CHECK_INTERVAL_SECONDS = 24 * 3600
# The background task waits this long before its first fetch so a briefly-lived
# app (e.g. a TestClient/integration run) cancels it before it touches the wire.
INITIAL_DELAY_SECONDS = 5.0

_CACHE_NAME = "release-check.json"
_DISABLED_VALUES = {"0", "off", "false", "no"}


# ---------------------------------------------------------------------------
# Toggle + cache location
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """True unless ``AGENTALLOY_RELEASE_CHECK`` is set to an off-ish value."""
    return os.environ.get("AGENTALLOY_RELEASE_CHECK", "1").strip().lower() not in _DISABLED_VALUES


def cache_path() -> Path:
    """Path to the small JSON cache under the user data dir."""
    return install_state.user_data_dir() / _CACHE_NAME


# ---------------------------------------------------------------------------
# Version helpers (single home; upgrade.py imports these)
# ---------------------------------------------------------------------------


def parse_semver(value: str) -> tuple[int, int, int]:
    """Parse ``vX.Y.Z`` / ``X.Y.Z`` into a comparable tuple (extras ignored)."""
    core = value.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    nums: list[int] = []
    for part in core.split(".")[:3]:
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def current_version() -> str:
    from agentalloy import __version__

    return __version__


def bump_type(current: str, latest: str) -> str:
    """Classify ``current`` -> ``latest`` as major/minor/patch (or "" if not ahead)."""
    cur = parse_semver(current)
    new = parse_semver(latest)
    if new <= cur:
        return ""
    if new[0] != cur[0]:
        return "major"
    if new[1] != cur[1]:
        return "minor"
    return "patch"


# ---------------------------------------------------------------------------
# GitHub releases API (the only outbound network egress)
# ---------------------------------------------------------------------------


def _get_json(url: str, timeout: float) -> dict[str, Any] | None:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
            payload: Any = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def fetch_latest_tag(timeout: float = 10.0) -> str | None:
    """Return the newest release tag (e.g. ``v3.7.0``), or ``None`` if unreachable."""
    payload = _get_json(_RELEASES_LATEST, timeout)
    if payload is None:
        return None
    tag = payload.get("tag_name")
    return tag if isinstance(tag, str) and tag else None


def fetch_release_info(ref: str | None = None, timeout: float = 10.0) -> dict[str, Any] | None:
    """Return ``{tag,name,html_url,body,published_at}`` for a release, or ``None``.

    ``ref`` pins a specific tag (``/releases/tags/<ref>``); the default resolves
    the latest release. Used by the ``upgrade`` preflight card — the only place
    that wants the full notes/URL, fetched fresh at the moment of intent.
    """
    url = _RELEASES_BY_TAG + ref if ref else _RELEASES_LATEST
    payload = _get_json(url, timeout)
    if payload is None:
        return None
    tag = payload.get("tag_name")
    if not (isinstance(tag, str) and tag):
        return None

    def _str(key: str) -> str:
        v = payload.get(key)
        return v if isinstance(v, str) else ""

    return {
        "tag": tag,
        "name": _str("name"),
        "html_url": _str("html_url"),
        "body": _str("body"),
        "published_at": _str("published_at"),
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def read_cache() -> dict[str, Any]:
    """Read the JSON cache, or ``{}`` when absent/corrupt. Never raises."""
    try:
        data: Any = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        install_state._atomic_write(cache_path(), json.dumps(data, indent=2) + "\n")


def refresh(*, force: bool = False) -> dict[str, Any]:
    """Throttled producer: fetch the latest tag and persist it. Never raises.

    Returns the (possibly unchanged) cache dict. When disabled, records
    ``enabled=False`` so cache-only consumers stay quiet without the env var.
    A fetch is skipped while the previous one is younger than
    :data:`CHECK_INTERVAL_SECONDS` (unless ``force``). ``checked_at`` is stamped
    even on a failed fetch so a transient outage doesn't busy-loop the network.
    """
    cache = read_cache()

    if not is_enabled():
        if cache.get("enabled") is not False:
            cache["enabled"] = False
            _write_cache(cache)
        return cache

    now = time.time()
    last = cache.get("checked_at")
    fresh = isinstance(last, int | float) and (now - last) < CHECK_INTERVAL_SECONDS
    if not force and fresh and cache.get("enabled") is not False:
        return cache

    tag = fetch_latest_tag()
    cache["enabled"] = True
    cache["checked_at"] = now
    cache.setdefault("dismissed_version", None)
    cache["current_version"] = current_version()
    if tag is not None:
        cache["latest_tag"] = tag
    else:
        cache.setdefault("latest_tag", None)
    _write_cache(cache)
    return cache


def notice() -> dict[str, Any] | None:
    """``{current,latest,bump_type}`` when an upgrade is worth showing, else None.

    Cache-only (no network). Returns ``None`` when disabled, when the running
    version is unknown (source checkout) or already current/ahead, or when the
    latest tag has been dismissed. Compares the *live* running version against
    the cached tag, so it self-clears the moment the user upgrades.
    """
    cache = read_cache()
    if cache.get("enabled") is False:
        return None
    latest = cache.get("latest_tag")
    if not isinstance(latest, str) or not latest:
        return None
    current = current_version()
    if current == "0.0.0+unknown":
        return None
    if cache.get("dismissed_version") == latest:
        return None
    bump = bump_type(current, latest)
    if not bump:
        return None
    return {"current": current, "latest": latest, "bump_type": bump}


def dismiss(version: str) -> None:
    """Silence the badge for ``version`` until a newer tag lands. Never raises."""
    cache = read_cache()
    cache["dismissed_version"] = version
    _write_cache(cache)


def take_optout_notice() -> bool:
    """Return True exactly once (then persist), so a consumer can print the
    one-time "release checks are on; disable with AGENTALLOY_RELEASE_CHECK=0"
    line. False when disabled or already shown.
    """
    if not is_enabled():
        return False
    cache = read_cache()
    if cache.get("optout_notified"):
        return False
    cache["optout_notified"] = True
    _write_cache(cache)
    return True
