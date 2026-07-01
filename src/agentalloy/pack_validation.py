# pyright: reportPrivateUsage=false
"""Pack-level validation gates for ingest.

Provides two gates that fire *before* the per-skill ingest subprocess loop:

1. **Schema + vocabulary gate** — every skill YAML in the pack must parse
   cleanly and pass ``agentalloy.ingest._validate``.  The same validation
   the bundled-corpus tests enforce is now enforced at ingest time for any
   pack (bundled or third-party).

2. **Version gate** — if a pack with the same name is already recorded in
   ``installed_packs`` state and its content hash differs from the incoming
   pack, the incoming pack's ``pack.yaml`` version must differ too.  Identical
   content + same version → silent skip (``already_installed``).  Changed
   content + same version → hard error.

These functions are intentionally pure (no DB/state side-effects) so tests
can call them with tmp-path fixtures without a live corpus.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

# ---------------------------------------------------------------------------
# Schema + vocabulary gate
# ---------------------------------------------------------------------------


@dataclass
class SkillValidationError:
    """A single validation failure for a skill inside a pack."""

    skill_id: str
    file: str
    errors: list[str]


@dataclass
class PackValidationResult:
    """Aggregate result of validate_pack_skills()."""

    ok: bool
    errors: list[SkillValidationError] = field(
        default_factory=lambda: cast(list[SkillValidationError], [])
    )

    def format_errors(self) -> str:
        lines: list[str] = []
        for e in self.errors:
            for msg in e.errors:
                lines.append(f"  [{e.file} / {e.skill_id}] {msg}")
        return "\n".join(lines)


def validate_pack_skills(
    pack_dir: Path, skills_entries: list[dict[str, Any]], *, strict: bool = True
) -> PackValidationResult:
    """Validate every skill YAML in *skills_entries* against the ingest schema.

    Imports ``_load_yaml``, ``_validate``, and (when ``strict``) ``_lint`` from
    ``agentalloy.ingest`` so the ingest path and the bundled-corpus tests share
    one implementation.

    When ``strict`` is True (the default — the third-party ``install-pack``
    path), each skill that passes ``_validate`` also runs through ``_lint``;
    any non-empty lint output (missing rationale/verification fragment,
    all-execution monotony, drifted fragment content, mechanical tag issues,
    etc.) is folded into that skill's error list so it fails Gate 1 alongside
    a hard schema error, in one aggregated report, before any ingest
    subprocess or network cost. ``strict=False`` (the legacy bundled-corpus
    path, and ``--allow-lint-warnings``) skips the lint call entirely — lint
    warnings are non-blocking there, same as today.

    Returns a :class:`PackValidationResult` — check ``.ok`` before proceeding
    to the ingest subprocess loop.
    """
    from agentalloy.ingest import IngestError, _lint, _load_yaml, _validate  # noqa: PLC0415

    skill_errors: list[SkillValidationError] = []
    for entry in skills_entries:
        fname = str(entry.get("file", ""))
        skill_id = str(entry.get("skill_id", fname))
        yaml_path = pack_dir / fname
        if not yaml_path.is_file():
            # Missing file is caught by _read_pack_manifest; skip here to avoid
            # a duplicate error message.
            continue
        try:
            record = _load_yaml(yaml_path)
        except IngestError as exc:
            skill_errors.append(
                SkillValidationError(
                    skill_id=skill_id,
                    file=fname,
                    errors=[str(exc)],
                )
            )
            continue

        errs = _validate(record)
        if strict:
            errs = [*errs, *_lint(record, yaml_path)]
        if errs:
            skill_errors.append(
                SkillValidationError(
                    skill_id=skill_id,
                    file=fname,
                    errors=errs,
                )
            )

    return PackValidationResult(ok=len(skill_errors) == 0, errors=skill_errors)


# ---------------------------------------------------------------------------
# Version gate
# ---------------------------------------------------------------------------


def content_hash(pack_dir: Path, skills_entries: list[dict[str, Any]]) -> str:
    """Return a stable SHA-256 of the pack's manifest + all skill YAML bytes.

    Files are sorted by name so the hash is deterministic regardless of
    filesystem ordering.
    """
    h = hashlib.sha256()
    manifest_path = pack_dir / "pack.yaml"
    if manifest_path.is_file():
        h.update(manifest_path.read_bytes())
    for entry in sorted(skills_entries, key=lambda e: str(e.get("file", ""))):
        fname = str(entry.get("file", ""))
        yaml_path = pack_dir / fname
        if yaml_path.is_file():
            h.update(fname.encode())
            h.update(yaml_path.read_bytes())
    return h.hexdigest()


@dataclass
class VersionGateResult:
    """Result of check_version_gate()."""

    ok: bool
    """True → proceed with ingest (or skip when ``skip`` is set); False →
    abort with ``error``."""
    skip: bool = False
    """True when content is identical to the installed snapshot — caller
    should return ``already_installed`` without re-ingesting."""
    error: str = ""
    changed: bool = False
    """True when the pack was previously installed and its content changed under
    a new version string. The skills already exist in the graph, so a plain
    re-ingest skips them as duplicates (``ingest`` returns ``EXIT_DUPLICATE`` on
    skill_id presence). The caller must FORCE re-ingest (overwriting the stale
    graph) and invalidate the pack's vectors so a non-force reembed re-creates
    them — otherwise the bump is recorded in state but never reaches the corpus."""


def check_version_gate(
    pack_name: str,
    pack_version: str,
    pack_dir: Path,
    skills_entries: list[dict[str, Any]],
    installed_packs: list[dict[str, Any]],
) -> VersionGateResult:
    """Gate: changed content requires a version bump.

    Four cases:
    - Pack not previously installed → ok=True, proceed.
    - Previously installed, identical content, same version → skip=True
      (caller emits ``already_installed``).
    - Previously installed, different content, different version → ok=True,
      proceed (legitimate upgrade).
    - Previously installed, different content, SAME version → ok=False,
      error message explaining the version-bump rule.
    """
    # Find the most-recently-installed entry for this pack name.
    prior: dict[str, Any] | None = None
    for entry in installed_packs:
        if str(entry.get("name", "")) == pack_name:
            prior = entry

    if prior is None:
        return VersionGateResult(ok=True)

    prior_version = str(prior.get("version", ""))
    prior_hash = str(prior.get("content_hash", ""))

    # Legacy state: installs recorded before this gate existed carry no
    # content_hash, so "changed vs unchanged" is undecidable. Proceed (the
    # per-skill ingest dedupe makes a same-version re-run a no-op) and let
    # the post-ingest state append record the hash for next time.
    if not prior_hash:
        return VersionGateResult(ok=True)

    incoming_hash = content_hash(pack_dir, skills_entries)

    if incoming_hash == prior_hash:
        # Identical content — always a silent skip regardless of version string.
        return VersionGateResult(ok=True, skip=True)

    # Content differs.
    if pack_version != prior_version:
        # Legitimate upgrade — new version string. The skills already exist in
        # the graph, so signal the caller to force-overwrite and re-embed rather
        # than letting the per-skill ingest skip them as duplicates.
        return VersionGateResult(ok=True, changed=True)

    # Content changed but version string is the same → hard error.
    error = (
        f"Pack '{pack_name}' is already installed at version '{prior_version}' "
        f"but the incoming pack content differs from the installed snapshot. "
        f"Bump the 'version' field in pack.yaml (currently '{pack_version}') "
        f"before re-ingesting. "
        f"Identical content + same version is treated as a no-op; "
        f"changed content requires a version bump to preserve the SkillVersion rollback chain."
    )
    return VersionGateResult(ok=False, error=error)
