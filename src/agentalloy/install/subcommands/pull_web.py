"""Download the prebuilt web UI bundle from GitHub release assets.

Installs are source builds (``uv tool install git+...``) on machines without
node/pnpm, so the SPA can't be built locally. CI builds ``frontend/dist`` and
attaches it to each release as ``web-dist.tar.gz`` (see the ``release`` job in
.github/workflows/container-build.yml); this subcommand downloads the asset
matching the *installed* version into
``~/.local/share/agentalloy/web-dist/<version>/`` where ``web/spa.py`` picks it
up. Version-matched on purpose: the SPA calls the service API, so a newer UI
against an older server (or vice versa) is a support trap.

Failure is always non-fatal to callers (setup/upgrade warn and continue) — the
API surface works without the UI, and ``spa.py`` serves a 501 hint pointing
back here.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.release_check import REPO, current_version
from agentalloy.install.subcommands.pull_models import (
    _DOWNLOAD_USER_AGENT,
    _download_with_retry,
    _extract_archive,
)

logger = logging.getLogger(__name__)

WEB_DIST_ASSET = "web-dist.tar.gz"


def web_dist_root() -> Path:
    return install_state.user_data_dir() / "web-dist"


def web_dist_dir(version: str) -> Path:
    return web_dist_root() / version


def _asset_url(version: str) -> str:
    return f"https://github.com/{REPO}/releases/download/v{version}/{WEB_DIST_ASSET}"


def _asset_available(url: str) -> bool:
    """HEAD probe so a missing asset fails fast.

    A 404 means no bundle was published for this version (releases <= v5.1.3
    predate bundle assets) — that's permanent, and letting it ride the
    transient-error retry loop stalls setup for minutes. Anything else
    (transient error, other status) defers to the retry downloader.
    """
    request = urllib.request.Request(  # noqa: S310 — https only
        url, method="HEAD", headers={"User-Agent": _DOWNLOAD_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310 — https only
            return True
    except urllib.error.HTTPError as exc:
        return exc.code != 404
    except OSError:
        return True


def _prune_other_versions(root: Path, keep: str) -> list[str]:
    """Remove bundles for other versions; only the installed version is served."""
    removed: list[str] = []
    if not root.is_dir():
        return removed
    for entry in root.iterdir():
        if entry.is_dir() and entry.name != keep:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def pull_web_dist(version: str | None = None, *, force: bool = False) -> dict[str, Any]:
    """Fetch + install the web UI bundle for ``version`` (default: installed).

    Returns ``{success, skipped, version, dest, error}``. Idempotent: an
    already-present bundle for the version is a no-op unless ``force``.
    """
    ver = version or current_version()
    dest = web_dist_dir(ver)
    if not force and (dest / "index.html").is_file():
        return {"success": True, "skipped": True, "version": ver, "dest": str(dest), "error": None}

    root = web_dist_root()
    root.mkdir(parents=True, exist_ok=True)
    url = _asset_url(ver)
    if not _asset_available(url):
        return {
            "success": False,
            "skipped": False,
            "version": ver,
            "dest": str(dest),
            "error": f"no web UI bundle published for v{ver} ({url})",
        }

    with tempfile.TemporaryDirectory(dir=root, prefix=".pull-web-") as tmp:
        archive = Path(tmp) / WEB_DIST_ASSET
        result = _download_with_retry(url, archive, label="web-dist")
        if not result["success"]:
            return {
                "success": False,
                "skipped": False,
                "version": ver,
                "dest": str(dest),
                "error": f"download failed: {result['error']} ({url})",
            }
        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        try:
            _extract_archive(archive, extract_dir)
        except (OSError, ValueError) as exc:
            return {
                "success": False,
                "skipped": False,
                "version": ver,
                "dest": str(dest),
                "error": f"extract failed: {exc}",
            }
        if not (extract_dir / "index.html").is_file():
            return {
                "success": False,
                "skipped": False,
                "version": ver,
                "dest": str(dest),
                "error": "bundle has no index.html at its root — refusing to install it",
            }
        # Swap into place: build fully under a tmp sibling, then rename. The
        # rename is atomic on the same filesystem, so spa.py never sees a
        # half-extracted dir.
        if dest.exists():
            shutil.rmtree(dest)
        extract_dir.rename(dest)

    _prune_other_versions(root, keep=ver)
    return {"success": True, "skipped": False, "version": ver, "dest": str(dest), "error": None}


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    p = subparsers.add_parser(
        "pull-web",
        help="Download the prebuilt web UI bundle for the installed version",
    )
    p.add_argument("--version", default=None, help="Release version to fetch (default: installed)")
    p.add_argument("--force", action="store_true", help="Re-download even if already present")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    result = pull_web_dist(getattr(args, "version", None), force=getattr(args, "force", False))
    if result["success"]:
        verb = "already present" if result["skipped"] else "installed"
        print(f"web UI bundle {verb}: {result['dest']}")
        return 0
    print(f"web UI bundle download failed: {result['error']}")
    print("The API is unaffected; retry later with `agentalloy pull-web`.")
    return 1
