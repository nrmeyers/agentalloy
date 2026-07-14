# pyright: reportPrivateUsage=false
"""Pack-level validation gates for ingest.

Provides three gates that fire *before* the per-skill ingest subprocess loop:

1. **Schema + vocabulary gate** — every skill YAML in the pack must parse
   cleanly and pass ``agentalloy.ingest._validate``.  The same validation
   the bundled-corpus tests enforce is now enforced at ingest time for any
   pack (bundled or third-party).

2. **Version gate** — if a pack with the same name is already recorded in
   ``installed_packs`` state and its content hash differs from the incoming
   pack, the incoming pack's ``pack.yaml`` version must differ too.  Identical
   content + same version → silent skip (``already_installed``).  Changed
   content + same version → hard error.

3. **Semantic review gate (Gate 1.5)** — every skill must carry a fresh,
   approving ``review.yaml`` verdict authored upstream by the operator's coding
   agent.  Pure/deterministic: validates the artifact only, never calls an LLM.

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


def expected_active_skill_ids(pack_dir: Path, skills_entries: list[dict[str, Any]]) -> list[str]:
    """skill_ids the corpus should hold with an ACTIVE version once the pack
    is installed: every manifest entry except those whose YAML carries
    ``deprecated: true`` (ingest processes those as deprecation tombstones,
    never as active skills).

    Shared by the corpus-aware skip in ``install_local_pack`` and verify's
    registry-derived skill-count check. Unreadable YAMLs stay included —
    ingest surfaces the real error, and verify counting one too many beats
    silently expecting one too few.
    """
    import yaml

    ids: list[str] = []
    for entry in skills_entries:
        sid = entry.get("skill_id")
        fname = str(entry.get("file", ""))
        deprecated = False
        path = pack_dir / fname if fname else None
        if path is not None and path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    deprecated = bool(data.get("deprecated", False))
                    sid = sid or data.get("skill_id")
            except Exception:  # noqa: BLE001 — ingest surfaces the real error
                pass
        if sid and not deprecated:
            ids.append(str(sid))
    return ids


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


# ---------------------------------------------------------------------------
# Semantic review gate (Gate 1.5)
# ---------------------------------------------------------------------------
#
# Enforces an agent-authored ``review.yaml`` verdict per skill. The verdict is
# produced UPSTREAM by the operator's own coding agent (whatever LLM they run);
# this gate is pure and deterministic — it only validates the artifact, and MUST
# NOT call an LLM or the network. It returns the same PackValidationResult shape
# as the schema gate so failures aggregate into one report.

REVIEW_FILENAME = "review.yaml"


@dataclass
class ReviewVerdict:
    """One skill's parsed review verdict from ``review.yaml``.

    Tolerant of missing optional keys — a malformed entry simply fails the
    predicate checks in :func:`_verdict_errors` rather than raising.
    """

    skill_id: str
    target_hash: str
    verdict: str
    blocking_issues: list[str]
    checks: dict[str, str]
    reviewer_mode: str
    reviewer_model: str
    reviewer_harness: str
    source_refs: list[str]
    created_at: str

    @classmethod
    def from_entry(cls, data: dict[str, Any]) -> ReviewVerdict:
        reviewer_raw = data.get("reviewer")
        reviewer = reviewer_raw if isinstance(reviewer_raw, dict) else {}
        checks_raw = data.get("checks")
        checks = (
            {str(k): str(v) for k, v in cast("dict[Any, Any]", checks_raw).items()}
            if isinstance(checks_raw, dict)
            else {}
        )
        return cls(
            skill_id=str(data.get("skill_id", "")),
            target_hash=str(data.get("target_hash", "")),
            verdict=str(data.get("verdict", "")).lower().strip(),
            blocking_issues=[str(x) for x in cast("list[Any]", data.get("blocking_issues") or [])],
            checks=checks,
            reviewer_mode=str(cast("dict[str, Any]", reviewer).get("mode", "")).lower().strip(),
            reviewer_model=str(cast("dict[str, Any]", reviewer).get("model", "")),
            reviewer_harness=str(cast("dict[str, Any]", reviewer).get("harness", "")),
            source_refs=[str(x) for x in cast("list[Any]", data.get("source_refs") or [])],
            created_at=str(data.get("created_at", "")),
        )


def skill_file_sha256(pack_dir: Path, fname: str) -> str:
    """``sha256:`` over the exact on-disk bytes of a skill YAML — the DK2 hash
    the review verdict binds to (same bytes Gate 1 reads)."""
    return "sha256:" + hashlib.sha256((pack_dir / fname).read_bytes()).hexdigest()


def _verdict_errors(
    v: ReviewVerdict, expected_hash: str, *, require_independent: bool
) -> list[str]:
    errs: list[str] = []
    if v.target_hash != expected_hash:
        errs.append(
            f"stale review: target_hash {v.target_hash or '(missing)'} does not match the "
            f"current skill bytes ({expected_hash}) — re-review the edited skill"
        )
    if v.verdict != "approve":
        errs.append(f"review verdict is {v.verdict or '(missing)'!r}, not 'approve'")
    if v.blocking_issues:
        joined = "; ".join(v.blocking_issues[:5])
        errs.append(f"review has {len(v.blocking_issues)} blocking issue(s): {joined}")
    if not v.checks:
        errs.append("review has no 'checks' — cannot confirm the review actually ran")
    else:
        failed = sorted(k for k, status in v.checks.items() if status.lower().strip() == "fail")
        if failed:
            errs.append(f"review checks failed: {', '.join(failed)}")
    if require_independent and v.reviewer_mode != "independent":
        errs.append(
            f"reviewer.mode is {v.reviewer_mode or '(missing)'!r}, but an independent review "
            f"is required (AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW=1)"
        )
    return errs


def validate_review_verdicts(
    pack_dir: Path,
    skills_entries: list[dict[str, Any]],
    *,
    require_independent: bool = False,
) -> PackValidationResult:
    """Gate 1.5 — require a fresh, approving review verdict for every skill.

    Loads ``pack_dir/review.yaml`` and, for each manifest skill, checks: a
    matching ``reviews`` entry exists, its ``target_hash`` equals the skill's
    current on-disk ``sha256:`` (freshness), ``verdict == "approve"`` with no
    blocking issues, and a non-empty ``checks`` map with no ``fail``. When
    *require_independent* is set, ``reviewer.mode`` must be ``"independent"``.

    Pure and deterministic — no LLM, no network. Returns the same
    :class:`PackValidationResult` shape as :func:`validate_pack_skills` so Gate 1
    and Gate 1.5 failures render in one aggregated report.
    """
    import yaml  # noqa: PLC0415

    review_path = pack_dir / REVIEW_FILENAME
    verdicts_by_id: dict[str, ReviewVerdict] = {}
    load_error: str | None = None

    if not review_path.is_file():
        load_error = (
            f"no review verdict: {REVIEW_FILENAME} is missing from the pack — a semantic "
            f"review verdict is required (or pass --allow-unreviewed)"
        )
    else:
        try:
            data: Any = yaml.safe_load(review_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — surface any YAML error as a gate failure
            data = None
            load_error = f"{REVIEW_FILENAME} is not valid YAML: {exc}"
        if load_error is None:
            if not isinstance(data, dict):
                load_error = f"{REVIEW_FILENAME} must be a YAML mapping with a 'reviews' list"
            else:
                reviews: Any = cast("dict[str, Any]", data).get("reviews")
                if not isinstance(reviews, list):
                    load_error = f"{REVIEW_FILENAME} must contain a 'reviews' list"
                else:
                    for item in cast("list[Any]", reviews):
                        if isinstance(item, dict):
                            v = ReviewVerdict.from_entry(cast("dict[str, Any]", item))
                            if v.skill_id:
                                verdicts_by_id[v.skill_id] = v

    skill_errors: list[SkillValidationError] = []
    for entry in skills_entries:
        fname = str(entry.get("file", ""))
        skill_id = str(entry.get("skill_id", fname))
        yaml_path = pack_dir / fname
        if not yaml_path.is_file():
            # Absent file is Gate 1 / manifest reader's error, not ours.
            continue
        errs: list[str] = []
        if load_error is not None:
            errs.append(load_error)
        else:
            verdict = verdicts_by_id.get(skill_id)
            if verdict is None:
                errs.append(f"no review verdict for skill '{skill_id}' in {REVIEW_FILENAME}")
            else:
                errs.extend(
                    _verdict_errors(
                        verdict,
                        skill_file_sha256(pack_dir, fname),
                        require_independent=require_independent,
                    )
                )
        if errs:
            skill_errors.append(SkillValidationError(skill_id=skill_id, file=fname, errors=errs))

    return PackValidationResult(ok=len(skill_errors) == 0, errors=skill_errors)


def review_modes(pack_dir: Path) -> list[str]:
    """Distinct ``reviewer.mode`` values declared in ``review.yaml`` — provenance
    for the result contract (surfaced to the human approver / telemetry).

    Best-effort and side-effect-free: returns ``[]`` on a missing or malformed
    file rather than raising (the gate itself has already judged validity)."""
    import yaml  # noqa: PLC0415

    path = pack_dir / REVIEW_FILENAME
    if not path.is_file():
        return []
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — provenance only; never fail the caller
        return []
    if not isinstance(data, dict):
        return []
    reviews: Any = cast("dict[str, Any]", data).get("reviews")
    if not isinstance(reviews, list):
        return []
    modes: set[str] = set()
    for item in cast("list[Any]", reviews):
        if isinstance(item, dict):
            mode = ReviewVerdict.from_entry(cast("dict[str, Any]", item)).reviewer_mode
            if mode:
                modes.add(mode)
    return sorted(modes)
