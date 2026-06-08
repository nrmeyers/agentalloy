# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""``install-pack`` subcommand — pull a published skill pack into the corpus.

Operator-tier. The skill-pack registry shape is **TBD for v1**; we ship a
minimal mechanism that:

  1. Resolves a pack name to a manifest URL using a hardcoded pattern.
  2. Downloads + parses the manifest (JSON: ``{tarball_url, sha256, ...}``).
  3. Downloads the tarball, validates its sha256 against the manifest.
  4. Extracts YAML draft files into ``skill-source/pending-review/``.
  5. Calls the existing ``agentalloy.ingest`` pipeline on each YAML.
  6. Records the pack name and ingested skill IDs in install state.

A real registry (org-scoped, signed manifests, dependency resolution) is
deferred — flagged in the install spec's open questions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml as _yaml

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1
STEP_NAME = "install-pack"

# Hardcoded URL pattern. The placeholder org is ``navistone``; this lands
# in the manifest URL ``…/skill-pack-{name}/releases/latest/download/manifest.json``.
# When a real registry exists, this becomes a registry lookup instead.
_DEFAULT_MANIFEST_URL_PATTERN = (
    "https://github.com/navistone/skill-pack-{name}/releases/latest/download/manifest.json"
)

# Allowed URL schemes for both manifest and tarball. Refusing file:// / ftp://
# blocks SSRF + local-file disclosure via a malicious manifest.
_ALLOWED_SCHEMES = frozenset({"https", "http"})

# Pack name pattern — letters, digits, hyphens, underscores. Disallows path
# traversal (`..`, `/`) or scheme injection in the URL substitution.
_PACK_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# Per-fetch size caps. Manifest is small JSON; tarball can be larger.
_MAX_MANIFEST_BYTES = 1 << 20  # 1 MiB
_MAX_TARBALL_BYTES = 100 << 20  # 100 MiB


def _validate_url(url: str, kind: str) -> None:
    """Raise SystemExit(1) if ``url`` scheme is not in the allowlist."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        print(
            f"ERROR: {kind} URL has disallowed scheme '{parsed.scheme}': {url}",
            file=sys.stderr,
        )
        print(
            f"FIX:   Use one of: {', '.join(sorted(_ALLOWED_SCHEMES))}.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _download(url: str, dest: Path, max_bytes: int, timeout: int = 60) -> None:
    """Download a URL to a local file with a size cap.

    Raises on HTTP/network errors and on payloads exceeding ``max_bytes``
    (avoids tempdir DoS via attacker-controlled redirect targets).
    """
    _validate_url(url, "download")
    req = urllib.request.Request(url, headers={"User-Agent": "agentalloy-install/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — scheme allowlisted
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        bytes_read = 0
        chunk = 64 * 1024
        with dest.open("wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                bytes_read += len(buf)
                if bytes_read > max_bytes:
                    raise RuntimeError(f"Download exceeded {max_bytes} bytes from {url}; aborting")
                f.write(buf)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_manifest_url(pack_name: str, override: str | None) -> str:
    if override:
        _validate_url(override, "manifest")
        return override
    if not _PACK_NAME_RE.match(pack_name):
        print(
            f"ERROR: Pack name '{pack_name}' contains disallowed characters.",
            file=sys.stderr,
        )
        print(
            "FIX:   Pack names must match [a-zA-Z0-9][a-zA-Z0-9_-]{0,63} "
            "(no slashes, dots, or scheme prefixes).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return _DEFAULT_MANIFEST_URL_PATTERN.format(name=pack_name)


def _is_deprecated(yaml_path: Path) -> tuple[bool, str, str]:
    """Check if a skill YAML is deprecated. Returns (is_deprecated, skill_id, superseded_by).

    Reads the YAML without full validation — just checks the deprecated flag.
    Uses the same boolean parsing semantics as ``ingest._load_yaml()`` so that
    quoted ``"false"``/``"no"``/``"0"`` values are treated as false, not true.
    """
    try:
        data = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError:
        return False, "", ""

    if not isinstance(data, dict):
        return False, "", ""

    skill_id = str(data.get("skill_id", ""))

    def _parse_bool(key: str, default: bool = False) -> bool:
        v: Any = data.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("true", "yes", "1")
        return bool(v)

    deprecated = _parse_bool("deprecated", False)
    superseded_by = str(data.get("superseded_by", ""))
    return deprecated, skill_id, superseded_by


def _ingest_yaml(
    yaml_path: Path,
    repo_root: Path,
    *,
    no_restart: bool = False,
) -> dict[str, Any]:
    """Run the existing ingest pipeline on one YAML. Returns parsed result.

    Distinguishes four outcomes:
      - exit_code 0           → ingested fresh
      - exit_code 4 (DUPLICATE) → skill_id or canonical_name already in corpus;
                                 treated as a benign skip, not a failure.
      - outcome "deprecated"  → skill is marked deprecated; skipped with warning.
      - other non-zero        → real failure (parse, validation, DB error).

    ``no_restart`` is passed as ``--no-restart`` to the ingest subprocess when
    True. Defense-in-depth alongside the AGENTALLOY_DB_LOCK_HELD sentinel:
    if a future caller adds ``env={}`` to subprocess.run(), the flag still fires.
    """
    assert isinstance(no_restart, bool), "no_restart must be bool"  # P10-R5
    # --- check for deprecated before calling ingest ---
    is_dep, skill_id, superseded_by = _is_deprecated(yaml_path)
    if is_dep:
        return {
            "yaml": yaml_path.name,
            "exit_code": 0,
            "outcome": "deprecated",
            "stdout_tail": f"skipped deprecated skill '{skill_id}'",
            "stderr_tail": f"superseded by '{superseded_by}'",
        }

    # T1: build cmd list; append --no-restart when caller owns stop/restart lifecycle.
    cmd = [sys.executable, "-m", "agentalloy.ingest", str(yaml_path), "--yes"]
    if no_restart:
        cmd.append("--no-restart")

    result = subprocess.run(  # noqa: S603 — fixed args, no shell
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    rc = result.returncode
    return {
        "yaml": yaml_path.name,
        "exit_code": rc,
        "outcome": ("ingested" if rc == 0 else "duplicate" if rc == 4 else "failed"),
        "stdout_tail": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "",
        "stderr_tail": result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "",
    }


_REQUIRED_MANIFEST_FIELDS = ("name", "version", "embed_model", "embedding_dim", "skills")

# Pack tier — drives the install picker grouping, retirement policy, and
# retrieval scoping. See docs/PACK-AUTHORING.md §"Pack tier".
_VALID_PACK_TIERS = frozenset(
    {
        "foundation",  # always-installed process & generic engineering (core, engineering)
        "language",  # standalone programming languages (nodejs, python, rust, go, typescript)
        "framework",  # depends on a language (nestjs, react, fastify, vue, nextjs, fastapi)
        "store",  # data stores & runtimes (postgres, mongodb, redis, s3, temporal)
        "cross-cutting",  # capability domains usable from any stack (auth, security, observability)
        "platform",  # infra/orchestration (containers, iac, cicd, monorepo)
        "tooling",  # dev-loop tooling (testing, linting, vite, mocha-chai)
        "domain",  # application-layer domains (agents, ui-design, data-engineering)
        "protocol",  # wire-format / integration (graphql, webhooks, websockets)
        "workflow",  # SDD pipeline workflows (spec → design → plan → testgen → build → verify → deliver)
    }
)


def _read_pack_manifest(pack_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load and validate a local pack.yaml. Returns (manifest, errors)."""
    manifest_path = pack_dir / "pack.yaml"
    if not manifest_path.is_file():
        return None, [f"missing pack.yaml in {pack_dir}"]
    try:
        manifest = _yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError as exc:
        return None, [f"pack.yaml parse error: {exc}"]

    errors: list[str] = []
    for f in _REQUIRED_MANIFEST_FIELDS:
        if f not in manifest:
            errors.append(f"pack.yaml missing required field: {f}")

    tier = manifest.get("tier")
    if tier is None:
        errors.append(
            f"pack.yaml missing required field: tier (must be one of {sorted(_VALID_PACK_TIERS)})"
        )
    elif tier not in _VALID_PACK_TIERS:
        errors.append(
            f"pack.yaml 'tier' value '{tier}' is not valid "
            f"(must be one of {sorted(_VALID_PACK_TIERS)})"
        )

    skills = manifest.get("skills") or []
    if not isinstance(skills, list):
        errors.append("pack.yaml 'skills' must be a list")
        skills = []

    for i, entry in enumerate(skills):
        if not isinstance(entry, dict):
            errors.append(f"skills[{i}] must be a mapping")
            continue
        for f in ("skill_id", "file"):
            if f not in entry:
                errors.append(f"skills[{i}] missing required field: {f}")
        fname = entry.get("file")
        skill_path = pack_dir / fname if fname else None
        if not skill_path or not skill_path.is_file():
            errors.append(f"skills[{i}] file not found on disk: {fname}")
            continue

        # Validate that the YAML's actual fragment count + skill_id match
        # the manifest's claim. A stale manifest indicates the pack was
        # edited without re-running the migration script — surface the
        # drift instead of letting it ingest with wrong inventory.
        claimed_count = entry.get("fragment_count")
        claimed_id = entry.get("skill_id")
        try:
            data = _yaml.safe_load(skill_path.read_text(encoding="utf-8")) or {}
        except _yaml.YAMLError as exc:
            errors.append(f"skills[{i}] {fname}: yaml parse error: {exc}")
            continue
        actual_id = data.get("skill_id")
        if claimed_id and actual_id and str(claimed_id) != str(actual_id):
            errors.append(
                f"skills[{i}] skill_id drift: manifest says '{claimed_id}', "
                f"file '{fname}' has '{actual_id}'"
            )
        if isinstance(claimed_count, int):
            actual_count = len(data.get("fragments") or [])
            if actual_count != claimed_count:
                errors.append(
                    f"skills[{i}] fragment_count drift: manifest says "
                    f"{claimed_count}, file '{fname}' has {actual_count}"
                )

    return manifest, errors


def _check_embedding_dim(manifest: dict[str, Any], root: Path) -> str | None:
    """Hard-block on dim mismatch with the running corpus. Returns error str or None.

    Also soft-warns to stderr on `embed_model` name mismatch when dims agree
    — the pack will likely work but retrieval quality could degrade if the
    two models embed different things into the same dimension.
    """
    _ = root  # reserved
    pack_dim = manifest.get("embedding_dim")
    pack_model = manifest.get("embed_model")
    if not isinstance(pack_dim, int):
        return None  # nothing to check against; let ingest decide
    try:
        from agentalloy.config import get_settings
        from agentalloy.storage.vector_store import open_or_create

        settings = get_settings()
        with open_or_create(settings.duckdb_path) as vs:
            current_dim = vs.embedding_dim()
            if current_dim is None:
                return None  # corpus is empty; pack defines the dim
            if current_dim != pack_dim:
                return (
                    f"embedding dimension mismatch: pack expects {pack_dim}-dim "
                    f"but corpus is {current_dim}-dim. Re-embed with a matching "
                    f"model or pick a pack with embedding_dim={current_dim}."
                )
            # Dims match. Soft-warn on model-name mismatch.
            current_model = settings.runtime_embedding_model
            if pack_model and current_model and pack_model != current_model:
                print(
                    f"WARN: pack was authored with embed_model='{pack_model}' "
                    f"but the running corpus uses '{current_model}'. The pack "
                    f"will install (dimensions match), but vector retrieval "
                    f"quality may be reduced for these skills.",
                    file=sys.stderr,
                )
    except Exception:  # noqa: BLE001 — best-effort; let downstream surface real failures
        return None
    return None


def install_local_pack(
    pack_dir: Path,
    *,
    root: Path,
    no_restart: bool = False,
) -> dict[str, Any]:
    """Install a pack from a local directory (containing pack.yaml + YAMLs).

    No tarball download, no sha256 check. Trusts the local filesystem.

    ``no_restart`` is forwarded to each ``_ingest_yaml()`` call so that
    the container stop/restart lifecycle is owned by the outermost caller
    (e.g. ``_run_container_guard()`` in install-packs) rather than each
    individual ingest subprocess.
    """
    assert isinstance(no_restart, bool), "no_restart must be bool"  # P10-R5
    t0 = time.monotonic()
    pack_dir = pack_dir.resolve()

    manifest, errors = _read_pack_manifest(pack_dir)
    if manifest is None or errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "manifest_invalid",
            "pack_dir": str(pack_dir),
            "errors": errors,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    name = str(manifest["name"])

    dim_err = _check_embedding_dim(manifest, root)
    if dim_err:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "embedding_dim_mismatch",
            "pack": name,
            "pack_dir": str(pack_dir),
            "error": dim_err,
            "remediation": (
                "Either re-embed the corpus with a model matching the pack, "
                "or install only packs with the same embedding_dim as the existing corpus."
            ),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    skills_entries = manifest.get("skills") or []
    ingest_results: list[dict[str, Any]] = []
    for entry in skills_entries:
        yaml_path = pack_dir / str(entry["file"])
        # T1: pass no_restart so ingest subprocess suppresses its own stop/restart.
        ingest_results.append(_ingest_yaml(yaml_path, root, no_restart=no_restart))

    new_count = sum(1 for r in ingest_results if r["outcome"] == "ingested")
    duplicate_count = sum(1 for r in ingest_results if r["outcome"] == "duplicate")
    deprecated_count = sum(1 for r in ingest_results if r["outcome"] == "deprecated")
    failed = [r for r in ingest_results if r["outcome"] == "failed"]

    state = install_state.load_state(root)
    packs = state.get("installed_packs") or []
    packs.append(
        {
            "name": name,
            "source": f"local:{pack_dir}",
            "version": str(manifest.get("version", "")),
            "embed_model": str(manifest.get("embed_model", "")),
            "embedding_dim": int(manifest.get("embedding_dim", 0)),
            "yaml_files": [str(e["file"]) for e in skills_entries],
            "skill_count": len(skills_entries),
            "skills_ingested": new_count,
            "skills_already_present": duplicate_count,
            "skills_deprecated": deprecated_count,
            "ingest_failures": len(failed),
            "installed_at": int(time.time()),
        }
    )
    state["installed_packs"] = packs
    install_state.record_step(state, STEP_NAME, extra={"pack": name, "source": "local"})
    install_state.save_state(state, root)

    if failed:
        action = "ingested_with_errors"
    elif new_count == 0 and duplicate_count > 0 and deprecated_count == 0:
        action = "already_installed"
    else:
        action = "ingested"

    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "pack": name,
        "source": f"local:{pack_dir}",
        "version": manifest.get("version"),
        "skill_count": len(skills_entries),
        "skills_ingested": new_count,
        "skills_already_present": duplicate_count,
        "skills_deprecated": deprecated_count,
        "ingest_results": ingest_results,
        "ingest_failures": len(failed),
        "remediation": (
            "Some YAMLs failed to ingest; inspect ingest_results.stderr_tail and "
            "re-run `python -m agentalloy.ingest <yaml>` manually."
            if failed
            else None
        ),
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def install_pack(
    name_or_path: str,
    manifest_url: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Install a skill pack. Returns contract-shaped result.

    Three input shapes:
      1. A path to a local pack directory containing pack.yaml → local install.
      2. A pack name (resolved via manifest URL pattern) → remote tarball install.
      3. A pack name + --manifest-url override → remote tarball install.
    """
    from agentalloy.install.state import pack_source_dir

    root = root or pack_source_dir()
    root.mkdir(parents=True, exist_ok=True)

    # Branch: local directory? (Path-like and exists as a dir on disk.)
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "pack.yaml").is_file():
        return install_local_pack(candidate, root=root)

    # Otherwise: remote pack-by-name flow.
    name = name_or_path
    t0 = time.monotonic()
    url = _resolve_manifest_url(name, manifest_url)

    # 1. Fetch manifest
    with tempfile.TemporaryDirectory(prefix="agentalloy-pack-") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        manifest_path = tmpdir / "manifest.json"
        try:
            _download(url, manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
        except urllib.error.URLError as exc:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "manifest_fetch_failed",
                "pack": name,
                "manifest_url": url,
                "error": str(exc.reason),
                "remediation": (
                    "Verify the pack name is correct and the manifest URL is reachable. "
                    "If the pack is hosted elsewhere, pass --manifest-url to override "
                    "the default pattern."
                ),
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        manifest = json.loads(manifest_path.read_text())
        tarball_url = manifest.get("tarball_url")
        expected_sha = (manifest.get("sha256") or "").lower()
        if not tarball_url or not expected_sha:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "manifest_invalid",
                "pack": name,
                "error": "Manifest is missing required fields tarball_url and/or sha256",
                "remediation": "Contact the pack author to publish a valid manifest.",
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        # 2. Download tarball + validate sha256
        tar_path = tmpdir / "pack.tar.gz"
        try:
            _download(tarball_url, tar_path, max_bytes=_MAX_TARBALL_BYTES)
        except urllib.error.URLError as exc:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "tarball_fetch_failed",
                "pack": name,
                "tarball_url": tarball_url,
                "error": str(exc.reason),
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        actual_sha = _sha256_file(tar_path)
        if actual_sha != expected_sha:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "sha256_mismatch",
                "pack": name,
                "expected_sha256": expected_sha,
                "actual_sha256": actual_sha,
                "error": "Downloaded tarball sha256 does not match manifest",
                "remediation": "Pack may be tampered or manifest stale; abort and contact the author.",
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        # 3. Extract into a staging dir using the stdlib 'data' filter
        # (Python 3.12+). This filter rejects absolute paths, path traversal,
        # symlink/hardlink escapes, device/FIFO members, and stays inside the
        # destination root by design — much safer than the prior name-only
        # check which missed the link-traversal vector.
        extract_dir = tmpdir / "extracted"
        extract_dir.mkdir()
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                tar.extractall(extract_dir, filter="data")
        except tarfile.TarError as exc:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "tarball_unsafe_path",
                "pack": name,
                "error": f"Tarball extraction rejected: {exc}",
                "remediation": "Pack is malformed or hostile; contact the author.",
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        yaml_files = sorted(extract_dir.glob("**/*.yaml")) + sorted(extract_dir.glob("**/*.yml"))
        if not yaml_files:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "no_yaml_in_pack",
                "pack": name,
                "error": "Pack tarball contained no YAML skill drafts",
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        pending_dir = root / "skill-source" / "pending-review"
        pending_dir.mkdir(parents=True, exist_ok=True)
        # Refuse to write through a symlink at pending_dir — a pre-planted
        # symlink there would otherwise redirect copies outside the repo.
        if not install_state.is_inside_root(pending_dir, root):
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "pending_dir_outside_root",
                "pack": name,
                "error": (
                    f"skill-source/pending-review resolves outside repo root "
                    f"({pending_dir.resolve()}). A symlink may have been planted."
                ),
                "remediation": "Remove the symlink and re-run install-pack.",
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        # Two YAMLs at different paths within the tarball can share the
        # same basename (e.g. `engineering/foo.yaml` + `quality/foo.yaml`).
        # The tarfile-data filter doesn't dedupe by basename, so we keep
        # the on-disk names unique by flattening the relative path.
        copied: list[str] = []
        ingest_targets: list[Path] = []
        for yf in yaml_files:
            rel = yf.relative_to(extract_dir)
            safe_name = "_".join(rel.parts)
            target = pending_dir / safe_name
            if target.exists():
                # Defensive — shouldn't happen given the rel-path encoding,
                # but bail rather than silently overwrite.
                return {
                    "schema_version": SCHEMA_VERSION,
                    "action": "yaml_filename_collision",
                    "pack": name,
                    "error": f"Tarball produced colliding pending-review filename: {safe_name}",
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                }
            shutil.copyfile(yf, target)
            copied.append(safe_name)
            ingest_targets.append(target)

        # 4. Ingest each YAML via the existing pipeline
        ingest_results: list[dict[str, Any]] = []
        for target in ingest_targets:
            ingest_results.append(_ingest_yaml(target, root))

    # Same outcome classification as the local-pack flow: only `failed`
    # counts as a real failure; `duplicate` and `deprecated` are benign skips.
    new_count = sum(1 for r in ingest_results if r["outcome"] == "ingested")
    duplicate_count = sum(1 for r in ingest_results if r["outcome"] == "duplicate")
    deprecated_count = sum(1 for r in ingest_results if r["outcome"] == "deprecated")
    failed = [r for r in ingest_results if r["outcome"] == "failed"]

    # 5. Record in install state
    state = install_state.load_state(root)
    packs = state.get("installed_packs") or []
    packs.append(
        {
            "name": name,
            "manifest_url": url,
            "manifest_sha256": expected_sha,
            "yaml_files": copied,
            "skills_ingested": new_count,
            "skills_already_present": duplicate_count,
            "skills_deprecated": deprecated_count,
            "ingest_failures": len(failed),
            "installed_at": int(time.time()),
        }
    )
    state["installed_packs"] = packs
    install_state.record_step(state, STEP_NAME, extra={"pack": name})
    install_state.save_state(state, root)

    if failed:
        action = "ingested_with_errors"
    elif new_count == 0 and duplicate_count > 0 and deprecated_count == 0:
        action = "already_installed"
    else:
        action = "ingested"

    duration_ms = int((time.monotonic() - t0) * 1000)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "pack": name,
        "manifest_url": url,
        "manifest_sha256": expected_sha,
        "yaml_files": copied,
        "skills_ingested": new_count,
        "skills_already_present": duplicate_count,
        "skills_deprecated": deprecated_count,
        "ingest_results": ingest_results,
        "ingest_failures": len(failed),
        "remediation": (
            "Some YAMLs failed to ingest; inspect ingest_results.stderr_tail and "
            "re-run `python -m agentalloy.ingest <yaml>` manually for each failure."
            if failed
            else None
        ),
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "install-pack",
        help="Install a skill pack into the corpus (local directory or remote name).",
    )
    p.add_argument(
        "pack",
        help=(
            "Pack name (resolves to a manifest URL) OR path to a local pack "
            "directory containing pack.yaml."
        ),
    )
    p.add_argument(
        "--manifest-url",
        help=(
            "Override the default manifest URL pattern (remote install only). "
            "Default: https://github.com/navistone/skill-pack-{name}/releases/latest/download/manifest.json"
        ),
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    """Render install pack result in human-readable format."""
    action = result.get("action", "unknown")
    pack_name = result.get("pack", "unknown")
    skills_ingested = result.get("skills_ingested", 0)
    skills_deprecated = result.get("skills_deprecated", 0)
    failures = result.get("ingest_failures", 0)

    print_rich("\n  [bold]Install Pack[/bold]\n")
    print_rich(f"  Pack: {pack_name}")
    print_rich(f"  Status: {action}")
    print_rich(f"  Skills ingested: {skills_ingested}")
    if skills_deprecated:
        print_rich(f"  Skills skipped (deprecated): {skills_deprecated}")
    if failures:
        print_rich(f"  Failures: {failures}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    result = install_pack(args.pack, manifest_url=args.manifest_url)
    write_result(result, args, human_fn=_render_human)
    if result.get("ingest_failures", 0) > 0:
        return 2
    if result.get("action") not in ("ingested", "already_installed"):
        return 1
    return 0
