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
import contextlib
import hashlib
import json
import os
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
from agentalloy.pack_validation import (
    PackValidationResult,
    VersionGateResult,
    check_version_gate,
    content_hash,
    expected_active_skill_ids,
    review_modes,
    validate_pack_skills,
    validate_review_verdicts,
)
from agentalloy.storage.open import open_fragments, open_skills
from agentalloy.storage.skill_store import is_lock_held_error

logger = __import__("logging").getLogger(__name__)

SCHEMA_VERSION = 1
STEP_NAME = "install-pack"

# Shown when the corpus DB is held by another process. The usual holder is the
# running agentalloy service (its read-only handle blocks writers for its whole
# lifetime); a concurrent ingest/reembed is the transient case. Imported by
# install_packs and reembed.
LOCK_HELD_REMEDIATION = (
    "Another process is holding the corpus DB (agentalloy.duck) open. A running "
    "agentalloy service blocks writers for its whole lifetime — stop it first "
    "(`agentalloy server-stop`), re-run this command, then `agentalloy "
    "server-start`. If a concurrent ingest/reembed briefly holds the lock "
    "instead, wait and re-run."
)

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


def _propagate_deprecation(skill_id: str, superseded_by: str) -> str:
    """Back-propagate a deprecation to an already-ingested skill row.

    When a skill YAML is marked ``deprecated: true`` but its ``skill_id`` was
    ingested into the skill store by a prior install, the existing skill row
    still carries ``deprecated = false`` and keeps being served by retrieval /
    ambient injection (active reads filter ``s.deprecated = false``). Setting the
    flag is the only thing needed to retire it.

    Returns one of:
      - "deprecated_updated" — the skill existed and was updated (deprecated=true,
        superseded_by set).
      - "deprecated" — the skill was not in the store; nothing to update (skip).
      - "deprecated" — the DB lock was held (warned to stderr, install continues).

    A lock failure must NOT crash the install; we warn with the FIX hint and
    treat it as a plain skip, mirroring ``_ensure_skill_schema()``.
    """
    if not skill_id:
        return "deprecated"
    from agentalloy.config import get_settings

    store = None
    try:
        settings = get_settings()
        store = open_skills(settings, read_only=False)
        exists = store.scalar(
            "SELECT count(*) FROM skills WHERE skill_id = ?",
            [skill_id],
        )
        if not exists:
            return "deprecated"
        store.execute(
            "UPDATE skills SET deprecated = true, superseded_by = ? WHERE skill_id = ?",
            [superseded_by, skill_id],
        )
        logger.info(
            "deprecation propagated: skill %s marked deprecated (superseded_by=%r)",
            skill_id,
            superseded_by,
        )
        return "deprecated_updated"
    except Exception as exc:  # noqa: BLE001 — best-effort; lock-held must not crash install
        print(
            f"WARN: could not propagate deprecation for skill '{skill_id}': {exc}",
            file=sys.stderr,
        )
        if is_lock_held_error(str(exc)):
            print(f"FIX:   {LOCK_HELD_REMEDIATION}", file=sys.stderr)
        return "deprecated"
    finally:
        if store is not None:
            store.close()


def _ingest_yaml(
    yaml_path: Path,
    repo_root: Path,
    *,
    no_restart: bool = False,
    force: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Run the existing ingest pipeline on one YAML. Returns parsed result.

    Distinguishes four outcomes:
      - exit_code 0           → ingested fresh
      - exit_code 4 (DUPLICATE) → skill_id or canonical_name already in corpus;
                                 treated as a benign skip, not a failure.
      - outcome "deprecated"  → skill is marked deprecated and was NOT previously
                                 ingested; skipped (nothing in the graph to retire).
      - outcome "deprecated_updated" → skill is marked deprecated AND its row
                                 already existed in the skill store; the row was
                                 updated (deprecated=true, superseded_by set) so
                                 retrieval / ambient injection stops serving it.
      - other non-zero        → real failure (parse, validation, DB error).

    ``no_restart`` is passed as ``--no-restart`` to the ingest subprocess when
    True. Defense-in-depth alongside the AGENTALLOY_DB_LOCK_HELD sentinel:
    if a future caller adds ``env={}`` to subprocess.run(), the flag still fires.

    ``force`` passes ``--force`` so ingest overwrites an existing skill_id
    (deletes the old skill/version/fragments, then re-creates) instead of
    returning ``EXIT_DUPLICATE``. Used for version-bump upgrades, where the
    skill already exists but its content changed — without it the rewrite is
    silently skipped and the corpus keeps serving the stale prose.

    ``strict`` passes ``--strict`` so the ingest subprocess's own ``_lint``
    quality-bar warnings (missing rationale/verification fragment, all-execution
    monotony, drifted fragment content, tag lint) are promoted to hard errors.
    Defaults to False so any caller that doesn't explicitly opt in keeps
    today's non-blocking-lint behavior.
    """
    if not isinstance(no_restart, bool):
        raise TypeError(f"no_restart must be bool, got {type(no_restart).__name__}")
    if not isinstance(force, bool):
        raise TypeError(f"force must be bool, got {type(force).__name__}")
    if not isinstance(strict, bool):
        raise TypeError(f"strict must be bool, got {type(strict).__name__}")
    # --- check for deprecated before calling ingest ---
    is_dep, skill_id, superseded_by = _is_deprecated(yaml_path)
    if is_dep:
        outcome = _propagate_deprecation(skill_id, superseded_by)
        stdout_tail = (
            f"marked existing skill '{skill_id}' deprecated"
            if outcome == "deprecated_updated"
            else f"skipped deprecated skill '{skill_id}'"
        )
        return {
            "yaml": yaml_path.name,
            "exit_code": 0,
            "outcome": outcome,
            "stdout_tail": stdout_tail,
            "stderr_tail": f"superseded by '{superseded_by}'",
        }

    # T1: build cmd list; append --no-restart when caller owns stop/restart lifecycle.
    cmd = [sys.executable, "-m", "agentalloy.ingest", str(yaml_path), "--yes"]
    if force:
        cmd.append("--force")
    if no_restart:
        cmd.append("--no-restart")
    if strict:
        cmd.append("--strict")

    try:
        result = subprocess.run(  # noqa: S603 — fixed args, no shell
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # Clean up any leftover processes from the timed-out ingest run.
        import contextlib as _contextlib

        with _contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired, OSError):
            subprocess.run(
                ["pkill", "-f", f"agentalloy.ingest.*{yaml_path.name}"],
                capture_output=True,
                timeout=5,
            )
        logger.error("ingest subprocess timed out after 120s for %s", yaml_path.name)
        return {
            "yaml": yaml_path.name,
            "exit_code": -1,
            "outcome": "failed",
            "stdout_tail": "",
            "stderr_tail": "ingest subprocess timed out after 120s",
        }
    rc = result.returncode
    return {
        "yaml": yaml_path.name,
        "exit_code": rc,
        "outcome": ("ingested" if rc == 0 else "duplicate" if rc == 4 else "failed"),
        "stdout_tail": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "",
        "stderr_tail": result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "",
    }


def _invalidate_pack_vectors(skills_entries: list[dict[str, Any]]) -> int:
    """Delete Lance vectors for a pack's skills so the next reembed re-creates them.

    Needed after a force re-ingest on a version bump: the skill rows are rewritten
    but their fragment_ids are positionally stable, so a non-force reembed would
    skip them as already-embedded. Dropping the rows here makes the reembed treat
    them as missing and re-embed from the refreshed prose.

    Best-effort: any store error is logged and swallowed — the caller's reembed
    and `agentalloy reembed --force` remain the backstop. Returns the number of
    embedding rows deleted.
    """
    from agentalloy.config import get_settings

    deleted = 0
    vs = None
    try:
        vs = open_fragments(get_settings())
        for entry in skills_entries:
            sid = str(entry.get("skill_id", ""))
            if sid:
                deleted += vs.delete_skill(sid)
    except Exception as exc:  # noqa: BLE001 — invalidation is best-effort; reembed is the backstop
        logger.warning(
            "could not invalidate pack vectors (run `agentalloy reembed --force` "
            "if retrieval serves stale prose): %s",
            exc,
        )
        return 0
    finally:
        if vs is not None:
            vs.close()
    if deleted:
        logger.info("invalidated %d stale embedding(s) for version-bumped pack", deleted)
    return deleted


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


# Quantization + container suffix stripped when comparing a pack's declared
# model NAME against the corpus's recorded GGUF FILENAME. The pack records the
# bare model name (`nomic-embed-text-v1.5`) while the runtime records the GGUF
# file it loaded (`nomic-embed-text-v1.5.Q8_0.gguf`) — same model, two strings.
# We collapse only the trailing `.gguf` extension and an immediately-preceding
# quantization tag (`.Q8_0`, `.Q4_K_M`, `.IQ4_XS`, `.f16`, `.BF16`, …) so a
# genuinely different base model still compares unequal and still warns.
_QUANT_SUFFIX_RE = re.compile(
    r"\.(?:I?Q\d[\w]*|f\d+|bf\d+)$",
    re.IGNORECASE,
)


def _canonical_model_name(s: str | None) -> str:
    """Reduce a model name or GGUF filename to a canonical model identity.

    Conservative: strips at most one trailing ``.gguf`` extension and one
    immediately-preceding quantization tag, then lowercases. Does NOT touch the
    base model name, so two different base models still differ.

    Examples (all lowercased):
      - ``nomic-embed-text-v1.5``                 -> ``nomic-embed-text-v1.5``
      - ``nomic-embed-text-v1.5.Q8_0.gguf``       -> ``nomic-embed-text-v1.5``
      - ``nomic-embed-text-v1.5.Q4_K_M.gguf``     -> ``nomic-embed-text-v1.5``
      - ``nomic-embed-text-v1.5.IQ4_XS.gguf``     -> ``nomic-embed-text-v1.5``
      - ``nomic-embed-text-v1.5.f16.gguf``        -> ``nomic-embed-text-v1.5``
      - ``e5-base-v2``                            -> ``e5-base-v2``  (still != nomic)
    """
    if not s:
        return ""
    name = s.strip()
    # Strip one trailing `.gguf` (case-insensitive).
    if name.lower().endswith(".gguf"):
        name = name[: -len(".gguf")]
    # Strip one immediately-preceding quantization tag, if present.
    name = _QUANT_SUFFIX_RE.sub("", name)
    return name.lower()


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
    vs = None
    try:
        from agentalloy.config import get_settings

        settings = get_settings()
        vs = open_fragments(settings)
        current_dim = vs.embedding_dim()
        if current_dim is None:
            return None  # corpus is empty; pack defines the dim
        if current_dim != pack_dim:
            return (
                f"embedding dimension mismatch: pack expects {pack_dim}-dim "
                f"but corpus is {current_dim}-dim. Re-embed with a matching "
                f"model or pick a pack with embedding_dim={current_dim}."
            )
        # Dims match. Soft-warn only on a GENUINE model mismatch. The pack
        # records the bare model name while the runtime records the GGUF
        # filename, so compare on canonical identity (quant + .gguf stripped)
        # to avoid a false positive between e.g. `nomic-embed-text-v1.5` and
        # `nomic-embed-text-v1.5.Q8_0.gguf`.
        current_model = settings.runtime_embedding_model
        if (
            pack_model
            and current_model
            and _canonical_model_name(pack_model) != _canonical_model_name(current_model)
        ):
            print(
                f"WARN: pack was authored with embed_model='{pack_model}' "
                f"but the running corpus uses '{current_model}'. The pack "
                f"will install (dimensions match), but vector retrieval "
                f"quality may be reduced for these skills.",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — best-effort; let downstream surface real failures
        return None
    finally:
        if vs is not None:
            vs.close()
    return None


def _corpus_missing_active(skill_ids: list[str]) -> list[str]:
    """The subset of ``skill_ids`` with no active version in the corpus.

    Confirms a version-gate skip against the store itself: the registry can
    outlive the corpus, and returning ``already_installed`` for skills that
    aren't there leaves the corpus broken with no re-run that can fix it. On
    any read failure everything is reported missing — a force re-ingest is
    idempotent, a wrong skip is not. Deliberately does NOT filter on
    ``skills.deprecated``: a tombstoned skill still counts as present.
    """
    if not skill_ids:
        return []
    try:
        from agentalloy.config import get_settings
        from agentalloy.storage.skill_store import open_skill_store

        settings = get_settings()
        if not Path(settings.duckdb_path).exists():
            return list(skill_ids)
        with open_skill_store(settings.duckdb_path, read_only=True) as store:
            rows = store.execute(
                "SELECT s.skill_id FROM skills s "
                "JOIN skill_versions v ON v.version_id = s.current_version_id "
                "WHERE v.status = 'active' AND list_contains($ids, s.skill_id)",
                {"ids": list(skill_ids)},
            )
        present = {str(r[0]) for r in rows}
        return [sid for sid in skill_ids if sid not in present]
    except Exception:  # noqa: BLE001 — unreadable store → re-ingest, never skip
        return list(skill_ids)


def install_local_pack(
    pack_dir: Path,
    *,
    root: Path,
    no_restart: bool = False,
    strict: bool = True,
    allow_duplicates: bool = False,
    allow_unreviewed: bool = False,
    run_reembed: bool = True,
) -> dict[str, Any]:
    """Install a pack from a local directory (containing pack.yaml + YAMLs).

    No tarball download, no sha256 check. Trusts the local filesystem.

    ``run_reembed=False`` skips this call's own post-ingest reembed/dedup
    pass — used by install-packs' bulk-bootstrap loop, which calls this
    function once per pack but must trigger exactly one reembed pass for
    the whole run (its own, after the loop), not one per pack.

    ``no_restart`` is forwarded to each ``_ingest_yaml()`` call so that
    the container stop/restart lifecycle is owned by the outermost caller
    (e.g. ``_run_container_guard()`` in install-packs) rather than each
    individual ingest subprocess.

    ``strict`` (default True — strict-by-default for the third-party
    install-pack path) promotes ``ingest._lint`` quality-bar warnings to
    hard errors, both in Gate 1's schema validation
    (``validate_pack_skills(..., strict=strict)``) and in the ``--strict``
    flag passed to each ingest subprocess. Callers that need the legacy,
    non-strict behavior (the bundled ``install-packs`` bootstrap, whose
    packs predate this gate) pass ``strict=False`` explicitly.

    ``allow_duplicates`` is forwarded to the post-ingest ``run_bulk_reembed``
    call (see below) — it downgrades a hard cross-pack near-duplicate from
    a failing exit code to a warning; vectors are written either way.

    After a successful ingest loop that ingested at least one new skill,
    this triggers an in-process bulk reembed (``run_bulk_reembed``) so the
    new skills get real vectors and pass through the cross-pack dedup gate
    immediately, instead of silently serving zero vectors until someone
    remembers to run ``agentalloy reembed``.
    """
    if not isinstance(no_restart, bool):
        raise TypeError(f"no_restart must be bool, got {type(no_restart).__name__}")
    if not isinstance(strict, bool):
        raise TypeError(f"strict must be bool, got {type(strict).__name__}")
    if not isinstance(allow_duplicates, bool):
        raise TypeError(f"allow_duplicates must be bool, got {type(allow_duplicates).__name__}")
    if not isinstance(allow_unreviewed, bool):
        raise TypeError(f"allow_unreviewed must be bool, got {type(allow_unreviewed).__name__}")
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

    # --- Gate 1: Schema + vocabulary validation (+ lint, when strict) ---
    schema_result: PackValidationResult = validate_pack_skills(
        pack_dir, skills_entries, strict=strict
    )
    if not schema_result.ok:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "schema_invalid",
            "pack": name,
            "pack_dir": str(pack_dir),
            "errors": [
                {
                    "skill_id": e.skill_id,
                    "file": e.file,
                    "errors": e.errors,
                }
                for e in schema_result.errors
            ],
            "remediation": (
                "Fix the skill YAML errors listed above and re-run "
                "`agentalloy install-pack <path>`.\n" + schema_result.format_errors()
            ),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    # --- Gate 1.5: Semantic review gate ---
    # A fresh, approving review.yaml verdict (authored upstream by the operator's
    # coding agent) is required per skill. The gate is pure/deterministic — it
    # validates the artifact, never calls an LLM.
    #
    # Ships DORMANT: enforced only when AGENTALLOY_INSTALL_REQUIRE_REVIEW=1. The
    # verdict PRODUCER (the skill-review workflow) is a later slice; until it
    # exists, default-on would break every install path that has no review.yaml
    # (the bundled install-packs bootstrap, the web add-skill lane, the service
    # ingest router). Off by default keeps them working; flip the flag on once a
    # producer ships. --allow-unreviewed is the per-invocation override when the
    # flag is on; AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW=1 rejects mode: self.
    gate_1_5: dict[str, Any]
    if os.environ.get("AGENTALLOY_INSTALL_REQUIRE_REVIEW") != "1":
        gate_1_5 = {"status": "disabled", "modes": []}
    elif allow_unreviewed:
        gate_1_5 = {"status": "bypassed", "reason": "--allow-unreviewed", "modes": []}
    else:
        require_independent = os.environ.get("AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW") == "1"
        review_result = validate_review_verdicts(
            pack_dir, skills_entries, require_independent=require_independent
        )
        if not review_result.ok:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "review_failed",
                "pack": name,
                "pack_dir": str(pack_dir),
                "errors": [
                    {"skill_id": e.skill_id, "file": e.file, "errors": e.errors}
                    for e in review_result.errors
                ],
                "remediation": (
                    "Each skill needs a fresh, approving review.yaml verdict. Re-run the "
                    "skill-review workflow to (re)generate it, or pass --allow-unreviewed to "
                    "install without a semantic review.\n" + review_result.format_errors()
                ),
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
        gate_1_5 = {"status": "passed", "modes": review_modes(pack_dir)}

    # --- Gate 2: Version gate ---
    state_for_version = install_state.load_state(root)
    installed_packs_list: list[dict[str, Any]] = state_for_version.get("installed_packs") or []
    version_result: VersionGateResult = check_version_gate(
        pack_name=name,
        pack_version=str(manifest.get("version", "")),
        pack_dir=pack_dir,
        skills_entries=skills_entries,
        installed_packs=installed_packs_list,
    )
    if version_result.skip:
        # The registry can outlive the corpus (engine migration, a wiped or
        # reseeded store): "already installed" is only true if the pack's
        # skills actually sit in the corpus with active versions. When they
        # don't, fall through to a FORCE re-ingest instead of skipping — a
        # skip here is a lie no re-run can fix (issue: sdd-only corpora after
        # the v4→v5 migration).
        missing = _corpus_missing_active(expected_active_skill_ids(pack_dir, skills_entries))
        if missing:
            logger.info(
                "version gate says already-installed, but %d of the pack's skills "
                "are absent from the corpus (e.g. %s) — forcing re-ingest",
                len(missing),
                missing[0],
            )
            version_result = VersionGateResult(ok=True, changed=True)
        else:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "already_installed",
                "pack": name,
                "pack_dir": str(pack_dir),
                "version": manifest.get("version"),
                "skill_count": len(skills_entries),
                "skills_ingested": 0,
                "skills_already_present": len(skills_entries),
                "skills_deprecated": 0,
                "skills_deprecated_updated": 0,
                "ingest_results": [],
                "ingest_failures": 0,
                "remediation": None,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
    if not version_result.ok:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "version_unchanged",
            "pack": name,
            "pack_dir": str(pack_dir),
            "version": manifest.get("version"),
            "error": version_result.error,
            "remediation": version_result.error,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    # Version-bump upgrade: skills already exist in the graph, so force the
    # ingest to overwrite them — a plain re-ingest would skip each as a duplicate
    # and the corpus would keep serving the stale prose.
    force_reingest = version_result.changed
    ingest_results: list[dict[str, Any]] = []
    for entry in skills_entries:
        yaml_path = pack_dir / str(entry["file"])
        # T1: pass no_restart so ingest subprocess suppresses its own stop/restart.
        ingest_results.append(
            _ingest_yaml(
                yaml_path, root, no_restart=no_restart, force=force_reingest, strict=strict
            )
        )

    new_count = sum(1 for r in ingest_results if r["outcome"] == "ingested")
    duplicate_count = sum(1 for r in ingest_results if r["outcome"] == "duplicate")
    deprecated_count = sum(1 for r in ingest_results if r["outcome"] == "deprecated")
    deprecated_updated_count = sum(
        1 for r in ingest_results if r["outcome"] == "deprecated_updated"
    )
    failed = [r for r in ingest_results if r["outcome"] == "failed"]

    if failed:
        # Partial failure — roll back ingested skills and clean up state.
        # Extract skill_ids from the YAML files to identify what to delete.
        failed_yaml_names = {f["yaml"] for f in failed}
        # Collect skill_ids that were successfully ingested
        ingested_skill_ids: list[str] = []
        for r in ingest_results:
            if r["outcome"] == "ingested" and r["yaml"] not in failed_yaml_names:
                # Extract skill_id from the YAML file
                # r["yaml"] is the basename only (set by _ingest_yaml), so compare
                # against the manifest entry's basename — a subpath like
                # "skills/foo.yaml" must still match "foo.yaml" or it won't roll back.
                yaml_entry = next(
                    (e for e in skills_entries if Path(str(e["file"])).name == r["yaml"]),
                    None,
                )
                if yaml_entry:
                    ingested_skill_ids.append(str(yaml_entry.get("skill_id", "")))

        # Roll back ingested skills from DB
        if ingested_skill_ids:
            try:
                settings = __import__("agentalloy.config", fromlist=["get_settings"]).get_settings()
                store = open_skills(settings, read_only=False)
                try:
                    for sid in ingested_skill_ids:
                        if sid:
                            store.delete_skill(sid)
                            logger.info("rollback: deleted ingested skill %s", sid)
                    logger.warning(
                        "install_local_pack: rolled back %d ingested skill(s) due to partial failure",
                        len(ingested_skill_ids),
                    )
                finally:
                    store.close()
            except Exception as exc:
                logger.error("rollback failed for local pack %s: %s", name, exc)

        # Clean up state — don't record this pack as installed
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "ingested_with_errors",
            "pack": name,
            "pack_dir": str(pack_dir),
            "gate_1_5": gate_1_5,
            "skills_ingested": 0,
            "skills_already_present": duplicate_count,
            "skills_deprecated": deprecated_count,
            "skills_deprecated_updated": deprecated_updated_count,
            "ingest_results": ingest_results,
            "ingest_failures": len(failed),
            "remediation": (
                "Batch install failed — rolled back all ingested skills. "
                "Fix the failures and re-run `agentalloy install-pack <path>`."
            ),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    state = install_state.load_state(root)
    packs = state.get("installed_packs") or []

    # Verify corpus files were actually created (Pattern E fix).
    # Must happen BEFORE saving install state so partial installs don't
    # leave the pack recorded as installed.
    # Verify the same path the ingest actually wrote to — settings honor the
    # DUCKDB_PATH env override (e.g. the container points it at /app/data).
    # corpus_dir() is the XDG/profile default and diverges from that override,
    # so it must not be used for verification. ingest writes the skill store
    # (agentalloy.duck); the Lance fragments dataset is built later by reembed.
    from agentalloy.config import get_settings

    _settings = get_settings()
    duck_path = Path(_settings.duckdb_path)
    if not duck_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "corpus_verification_failed",
            "pack": name,
            "pack_dir": str(pack_dir),
            "error": (f"Corpus file missing after ingest: agentalloy.duck={duck_path.exists()}"),
            "remediation": (
                "Re-run `agentalloy seed-corpus` to initialize the corpus, "
                "then re-install the pack."
            ),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    # Version-bump upgrade: the force re-ingest above rewrote each skill's rows,
    # but Lance vectors are keyed by positionally-stable fragment_ids
    # ({skill_id}-v1-f{seq}), so the downstream non-force bulk reembed treats them
    # as already-present and skips them, leaving stale embeddings. Drop the pack's
    # vectors here so the reembed re-creates them from the new prose. (Workflow
    # skills carry no vectors, so this is a harmless no-op for them.)
    if force_reingest:
        _invalidate_pack_vectors(skills_entries)

    packs.append(
        {
            "name": name,
            "source": f"local:{pack_dir}",
            "version": str(manifest.get("version", "")),
            "content_hash": content_hash(pack_dir, skills_entries),
            "embed_model": str(manifest.get("embed_model", "")),
            "embedding_dim": int(manifest.get("embedding_dim", 0)),
            "yaml_files": [str(e["file"]) for e in skills_entries],
            "skill_count": len(skills_entries),
            "skills_ingested": new_count,
            "skills_already_present": duplicate_count,
            "skills_deprecated": deprecated_count,
            "skills_deprecated_updated": deprecated_updated_count,
            "ingest_failures": 0,
            "installed_at": int(time.time()),
        }
    )
    state["installed_packs"] = packs
    install_state.record_step(state, STEP_NAME, extra={"pack": name, "source": "local"})
    install_state.save_state(state, root)

    if new_count == 0 and duplicate_count > 0 and deprecated_count == 0:
        action = "already_installed"
    else:
        action = "ingested"

    # Reembed + cross-pack dedup gate — only fires when this run actually
    # ingested a new skill, mirroring reembed's own "dedup gate — only fires
    # when new fragments were actually embedded" guard. Populates real
    # vectors for the just-ingested skills and runs the hard-duplicate check
    # immediately, instead of leaving them as zero vectors until someone
    # remembers to run `agentalloy reembed`.
    dedup_exit_code: int | None = None
    dedup_hard_matches: list[dict[str, Any]] = []
    dedup_soft_matches: list[dict[str, Any]] = []
    dedup_remediation: str | None = None
    if new_count > 0 and run_reembed:
        from agentalloy.reembed.cli import run_bulk_reembed

        sink: dict[str, Any] = {}
        dedup_exit_code = run_bulk_reembed(
            no_restart=no_restart, allow_duplicates=allow_duplicates, result_sink=sink
        )
        dedup_hard_matches = sink.get("dedup_hard", [])
        dedup_soft_matches = sink.get("dedup_soft", [])
        if dedup_exit_code != 0:
            dedup_remediation = (
                "WARN: bulk reembed exited non-zero (dedup or embedding failure); "
                "run `agentalloy reembed` to retry or inspect stderr for hard-duplicate matches."
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "pack": name,
        "source": f"local:{pack_dir}",
        "version": manifest.get("version"),
        "skill_count": len(skills_entries),
        "gate_1_5": gate_1_5,
        "skills_ingested": new_count,
        "skills_already_present": duplicate_count,
        "skills_deprecated": deprecated_count,
        "skills_deprecated_updated": deprecated_updated_count,
        "ingest_results": ingest_results,
        "ingest_failures": len(failed),
        "dedup_exit_code": dedup_exit_code,
        "dedup_hard_matches": dedup_hard_matches,
        "dedup_soft_matches": dedup_soft_matches,
        "remediation": (
            "Some YAMLs failed to ingest; inspect ingest_results.stderr_tail and "
            "re-run `python -m agentalloy.ingest <yaml>` manually."
            if failed
            else dedup_remediation
        ),
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def install_pack(
    name_or_path: str,
    manifest_url: str | None = None,
    root: Path | None = None,
    *,
    strict: bool = True,
    allow_duplicates: bool = False,
    allow_unreviewed: bool = False,
) -> dict[str, Any]:
    """Install a skill pack. Returns contract-shaped result.

    Three input shapes:
      1. A path to a local pack directory containing pack.yaml → local install.
      2. A pack name (resolved via manifest URL pattern) → remote tarball install.
      3. A pack name + --manifest-url override → remote tarball install.

    ``strict`` (default True) and ``allow_duplicates`` (default False) are
    forwarded to ``install_local_pack`` for shape 1, and drive the same
    ``--strict`` ingest flag + post-ingest ``run_bulk_reembed`` wiring for
    shapes 2/3 below. Both default to the strict, third-party-safe posture;
    pass ``strict=False`` for legacy-style packs (mirrors CLI
    ``--allow-lint-warnings``) or ``allow_duplicates=True`` for a knowingly
    accepted cross-pack overlap (mirrors CLI ``--allow-duplicates``).
    """
    from agentalloy.install.state import pack_source_dir

    root = root or pack_source_dir()
    root.mkdir(parents=True, exist_ok=True)

    # Branch: local directory? (Path-like and exists as a dir on disk.)
    candidate = Path(name_or_path)
    if candidate.is_dir() and (candidate / "pack.yaml").is_file():
        # Route the write: direct host install, or push to the running service
        # (native/container) so an up service no longer blocks the install (#390).
        from agentalloy.install.corpus_write_route import install_or_route

        return install_or_route(
            candidate,
            root=root,
            strict=strict,
            allow_duplicates=allow_duplicates,
            allow_unreviewed=allow_unreviewed,
        )

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
            ingest_results.append(_ingest_yaml(target, root, strict=strict))

    # Same outcome classification as the local-pack flow: only `failed`
    # counts as a real failure; `duplicate` and `deprecated` are benign skips.
    new_count = sum(1 for r in ingest_results if r["outcome"] == "ingested")
    duplicate_count = sum(1 for r in ingest_results if r["outcome"] == "duplicate")
    deprecated_count = sum(1 for r in ingest_results if r["outcome"] == "deprecated")
    deprecated_updated_count = sum(
        1 for r in ingest_results if r["outcome"] == "deprecated_updated"
    )
    failed = [r for r in ingest_results if r["outcome"] == "failed"]

    if failed:
        # Partial failure — roll back ingested skills and clean up copied files.
        ingested_skill_ids: list[str] = []
        for r in ingest_results:
            if r["outcome"] == "ingested":
                # Try to extract skill_id from the YAML file
                try:
                    data = _yaml.safe_load((pending_dir / r["yaml"]).read_text()) or {}
                    sid = str(data.get("skill_id", ""))
                    if sid:
                        ingested_skill_ids.append(sid)
                except Exception:
                    pass

        # Roll back ingested skills from DB
        if ingested_skill_ids:
            try:
                settings = __import__("agentalloy.config", fromlist=["get_settings"]).get_settings()
                store = open_skills(settings, read_only=False)
                try:
                    for sid in ingested_skill_ids:
                        if sid:
                            store.delete_skill(sid)
                            logger.info("rollback: deleted ingested skill %s", sid)
                    logger.warning(
                        "install_pack: rolled back %d ingested skill(s) due to partial failure",
                        len(ingested_skill_ids),
                    )
                finally:
                    store.close()
            except Exception as exc:
                logger.error("rollback failed for pack %s: %s", name, exc)

        # Clean up copied YAML files from pending-review
        for r in ingest_results:
            if r["outcome"] == "ingested":
                yaml_path = pending_dir / r["yaml"]
                with contextlib.suppress(OSError):
                    yaml_path.unlink(missing_ok=True)

        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "ingested_with_errors",
            "pack": name,
            "manifest_url": url,
            "manifest_sha256": expected_sha,
            "yaml_files": copied,
            "skills_ingested": 0,
            "skills_already_present": duplicate_count,
            "skills_deprecated": deprecated_count,
            "skills_deprecated_updated": deprecated_updated_count,
            "ingest_results": ingest_results,
            "ingest_failures": len(failed),
            "remediation": (
                "Batch install failed — rolled back all ingested skills. "
                "Fix the failures and re-run `agentalloy install-pack <name>`."
            ),
            "duration_ms": duration_ms,
        }

    # 5. Verify corpus files were actually created (Pattern E fix).
    # Must happen BEFORE saving install state so partial installs don't
    # leave the pack recorded as installed.
    # Verify the same path the ingest actually wrote to — settings honor the
    # DUCKDB_PATH env override (e.g. the container points it at /app/data).
    # corpus_dir() is the XDG/profile default and diverges from that override,
    # so it must not be used for verification. ingest writes the skill store
    # (agentalloy.duck); the Lance fragments dataset is built later by reembed.
    from agentalloy.config import get_settings

    _settings = get_settings()
    duck_path = Path(_settings.duckdb_path)
    if not duck_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "corpus_verification_failed",
            "pack": name,
            "manifest_url": url,
            "error": (f"Corpus file missing after ingest: agentalloy.duck={duck_path.exists()}"),
            "remediation": (
                "Re-run `agentalloy seed-corpus` to initialize the corpus, "
                "then re-install the pack."
            ),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    # 6. Record in install state (only on full success)
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
            "skills_deprecated_updated": deprecated_updated_count,
            "ingest_failures": 0,
            "installed_at": int(time.time()),
        }
    )
    state["installed_packs"] = packs
    install_state.record_step(state, STEP_NAME, extra={"pack": name})
    install_state.save_state(state, root)

    if new_count == 0 and duplicate_count > 0 and deprecated_count == 0:
        action = "already_installed"
    else:
        action = "ingested"

    # Reembed + cross-pack dedup gate — only fires when this run actually
    # ingested a new skill. See install_local_pack's identical comment for
    # the reasoning.
    dedup_exit_code: int | None = None
    dedup_hard_matches: list[dict[str, Any]] = []
    dedup_soft_matches: list[dict[str, Any]] = []
    dedup_remediation: str | None = None
    if new_count > 0:
        from agentalloy.reembed.cli import run_bulk_reembed

        sink: dict[str, Any] = {}
        dedup_exit_code = run_bulk_reembed(allow_duplicates=allow_duplicates, result_sink=sink)
        dedup_hard_matches = sink.get("dedup_hard", [])
        dedup_soft_matches = sink.get("dedup_soft", [])
        if dedup_exit_code != 0:
            dedup_remediation = (
                "WARN: bulk reembed exited non-zero (dedup or embedding failure); "
                "run `agentalloy reembed` to retry or inspect stderr for hard-duplicate matches."
            )

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
        "skills_deprecated_updated": deprecated_updated_count,
        "ingest_results": ingest_results,
        "ingest_failures": len(failed),
        "dedup_exit_code": dedup_exit_code,
        "dedup_hard_matches": dedup_hard_matches,
        "dedup_soft_matches": dedup_soft_matches,
        "remediation": (
            "Some YAMLs failed to ingest; inspect ingest_results.stderr_tail and "
            "re-run `python -m agentalloy.ingest <yaml>` manually for each failure."
            if failed
            else dedup_remediation
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
    p.add_argument(
        "--allow-lint-warnings",
        action="store_true",
        help=(
            "Downgrade authoring-contract lint warnings (fragment sizes, missing "
            "rationale/verification, tag issues) from errors to warnings for this "
            "install. Off by default — new third-party skills are held to the "
            "strict quality bar."
        ),
    )
    p.add_argument(
        "--allow-duplicates",
        action="store_true",
        help=(
            "Downgrade cross-pack near-duplicate detection from an error to a "
            "warning. Vectors are always written; this only controls the exit code."
        ),
    )
    p.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help=(
            "Bypass the semantic review gate (Gate 1.5) for this install — install "
            "without a review.yaml verdict. Only relevant when "
            "AGENTALLOY_INSTALL_REQUIRE_REVIEW=1; the bypass is recorded in the result."
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
    skills_deprecated_updated = result.get("skills_deprecated_updated", 0)
    failures = result.get("ingest_failures", 0)

    print_rich("\n  [bold]Install Pack[/bold]\n")
    print_rich(f"  Pack: {pack_name}")
    print_rich(f"  Status: {action}")
    print_rich(f"  Skills ingested: {skills_ingested}")
    if skills_deprecated:
        print_rich(f"  Skills skipped (deprecated): {skills_deprecated}")
    if skills_deprecated_updated:
        print_rich(f"  Skills retired (deprecation propagated): {skills_deprecated_updated}")
    dedup_exit_code = result.get("dedup_exit_code")
    if dedup_exit_code is not None:
        reembed_status = "ok" if dedup_exit_code == 0 else f"exit {dedup_exit_code}"
        print_rich(f"  Reembed: {reembed_status}")
        for match in result.get("dedup_hard_matches") or []:
            print_rich(
                f"  HARD duplicate: '{match.get('incoming_skill_id')}' ~ "
                f"'{match.get('existing_skill_id')}' (similarity={match.get('similarity'):.4f})"
            )
        for match in result.get("dedup_soft_matches") or []:
            print_rich(
                f"  soft near-duplicate: '{match.get('incoming_skill_id')}' ~ "
                f"'{match.get('existing_skill_id')}' (similarity={match.get('similarity'):.4f})"
            )
        if dedup_exit_code != 0:
            print_rich(
                "  WARN: bulk reembed exited non-zero (dedup or embedding failure); "
                "run `agentalloy reembed` to retry or inspect stderr for hard-duplicate matches."
            )
    if failures:
        first_fail = next(
            (r for r in result.get("ingest_results") or [] if r.get("outcome") == "failed"),
            None,
        )
        detail = ""
        if first_fail:
            tail = str(first_fail.get("stderr_tail") or "").strip()
            if len(tail) > 120:
                tail = tail[:117] + "..."
            detail = f" (first: {first_fail.get('yaml')}" + (f" — {tail})" if tail else ")")
        print_rich(f"  Failures: {failures}{detail}")
        if first_fail and is_lock_held_error(str(first_fail.get("stderr_tail") or "")):
            print_rich(f"  Remediation: {LOCK_HELD_REMEDIATION}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    result = install_pack(
        args.pack,
        manifest_url=args.manifest_url,
        strict=not args.allow_lint_warnings,
        allow_duplicates=args.allow_duplicates,
        allow_unreviewed=args.allow_unreviewed,
    )
    write_result(result, args, human_fn=_render_human)
    if result.get("ingest_failures", 0) > 0:
        return 2
    if result.get("dedup_exit_code"):
        return 2
    if result.get("action") not in ("ingested", "already_installed"):
        return 1
    return 0
