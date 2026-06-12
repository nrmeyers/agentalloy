"""Review-gated ingest CLI — loads a review YAML into LadybugDB after human confirmation.

Usage::

    python -m agentalloy.ingest <review.yaml> [--force] [--yes]

The review YAML is produced by the Skill Authoring Agent. It covers both domain
and system skills. No Ollama is required; fragment embeddings are initialised to
zero and can be populated by a separate re-embed pass.

Exit codes
----------
0  success
1  usage error (bad args, file not found)
2  validation error (bad skill data or duplicate)
3  DB error
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from agentalloy.config import get_settings
from agentalloy.install.container_service import (
    is_in_container,
    restart_service_in_container,
    stop_service_in_container,
)
from agentalloy.skill_tier import resolve_skill_tier
from agentalloy.storage.ladybug import LadybugStore

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_VALIDATION = 2
EXIT_DB = 3
# A skill with the same skill_id (or canonical_name) is already in the corpus.
# Distinct exit code so re-running ingest on a populated DB is a benign no-op
# rather than a failure — install-pack uses this to skip the ingest cleanly
# instead of counting it as a real ingest failure.
EXIT_DUPLICATE = 4

_VALID_SYSTEM_CATEGORIES = frozenset(
    {"governance", "operational", "tooling", "safety", "quality", "observability"}
)
_VALID_DOMAIN_CATEGORIES = frozenset(
    {"engineering", "ops", "review", "design", "tooling", "quality"}
)
_VALID_FRAGMENT_TYPES = frozenset(
    {"setup", "execution", "verification", "example", "guardrail", "rationale"}
)
_VALID_PHASES = frozenset({"design", "build", "review"})

# Lint thresholds — derived from fixtures/skill-authoring-guidelines.md (R1–R8)
# and fixtures/skill-authoring-agent.md "Hard fragmentation rules" / "Tag rules".
_FRAG_WORDS_WARN_MIN = 25
_FRAG_WORDS_WARN_MAX = 800
_FRAG_WORDS_HARD_MIN = 5
_FRAG_WORDS_HARD_MAX = 2000
_TAGS_VALIDATE_HARD_CAP = 20

WORKFLOW_POSITION_MARKERS = frozenset(
    {
        # SDD pipeline
        "sdd",
        "phase:spec",
        "phase:design",
        "phase:plan",
        "phase:testgen",
        "phase:build",
        "phase:verify",
        "phase:deliver",
        # General process positions
        "code-review",
        "release",
        "incident",
        "rfc",
    }
)

TAG_POLICY_BY_TIER: dict[str, dict[str, int]] = {
    "foundation": {"soft_ceiling": 12, "rationale_above": 8},
    "language": {"soft_ceiling": 10, "rationale_above": 7},
    "framework": {"soft_ceiling": 10, "rationale_above": 7},
    "store": {"soft_ceiling": 10, "rationale_above": 7},
    "cross-cutting": {"soft_ceiling": 12, "rationale_above": 8},
    "platform": {"soft_ceiling": 10, "rationale_above": 7},
    "tooling": {"soft_ceiling": 8, "rationale_above": 6},
    "domain": {"soft_ceiling": 10, "rationale_above": 7},
    "protocol": {"soft_ceiling": 8, "rationale_above": 6},
    "workflow": {"soft_ceiling": 8, "rationale_above": 6},
}
WORKFLOW_TAG_POLICY = TAG_POLICY_BY_TIER["workflow"]
_HEADING_ONLY_MAX_WORDS = 8


class IngestError(ValueError):
    pass


@dataclass
class FragmentRecord:
    sequence: int
    fragment_type: str
    content: str


@dataclass
class ReviewRecord:
    skill_type: str
    skill_id: str
    canonical_name: str
    category: str
    skill_class: str
    domain_tags: list[str]
    always_apply: bool
    phase_scope: list[str]
    category_scope: list[str]
    author: str
    change_summary: str
    raw_prose: str
    fragments: list[FragmentRecord] = field(default_factory=lambda: cast(list[FragmentRecord], []))
    tier: str | None = None
    deprecated: bool = False
    superseded_by: str = ""
    # Stage 0 (skill-card indexing): optional one-line self-description.
    # Absent in the legacy corpus — tolerated everywhere ("" → NULL on insert).
    description: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentalloy.ingest",
        description=(
            "Load review YAML skill(s) into LadybugDB after human confirmation. "
            "Pass a file path to load a single skill, or a directory to batch-load "
            "all *.yaml files in that directory."
        ),
    )
    parser.add_argument("path", help="Path to a review YAML file or a directory of YAML files")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite if skill_id or canonical_name already exists",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Promote authoring-contract warnings (fragment sizes, missing "
            "rationale/verification, tag count, code-heavy execution fragments) "
            "to errors. Recommended for new authoring; off by default for "
            "compatibility with the legacy imported corpus."
        ),
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help=(
            "Skip stopping and restarting the running uvicorn service when "
            "ingesting inside a container. Useful when you want to load skills "
            "without disrupting the live server."
        ),
    )
    args = parser.parse_args(argv)

    target = Path(args.path)
    if not target.exists():
        print(f"error: path not found: {target}", file=sys.stderr)
        return EXIT_USAGE

    no_restart = args.no_restart

    if target.is_dir():
        return _batch_with_container(
            target, force=args.force, yes=args.yes, strict=args.strict, no_restart=no_restart
        )

    return _single_with_container(
        target, force=args.force, yes=args.yes, strict=args.strict, no_restart=no_restart
    )


def _single_with_container(
    yaml_path: Path, *, force: bool, yes: bool, strict: bool = False, no_restart: bool = False
) -> int:
    """Wrap _single() with container stop/restart logic."""
    container_stopped = False
    if is_in_container() and not no_restart:
        container_stopped = stop_service_in_container(no_restart=no_restart)
    try:
        return _single(yaml_path, force=force, yes=yes, strict=strict)
    finally:
        if is_in_container() and not no_restart and container_stopped:
            restart_service_in_container(no_restart=no_restart)


def _batch_with_container(
    directory: Path, *, force: bool, yes: bool, strict: bool = False, no_restart: bool = False
) -> int:
    """Wrap _batch() with container stop/restart logic."""
    container_stopped = False
    if is_in_container() and not no_restart:
        container_stopped = stop_service_in_container(no_restart=no_restart)
    try:
        return _batch(directory, force=force, yes=yes, strict=strict)
    finally:
        if is_in_container() and not no_restart and container_stopped:
            restart_service_in_container(no_restart=no_restart)


def _single(yaml_path: Path, *, force: bool, yes: bool, strict: bool = False) -> int:
    try:
        record = _load_yaml(yaml_path)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION

    errors = _validate(record)
    if errors:
        for e in errors:
            print(f"validation error: {e}", file=sys.stderr)
        return EXIT_VALIDATION

    warnings = _lint(record, yaml_path=yaml_path)
    if warnings:
        label = "validation error" if strict else "warning"
        for w in warnings:
            print(f"{label}: {w}", file=sys.stderr)
        if strict:
            return EXIT_VALIDATION

    settings = get_settings()
    store = LadybugStore(settings.ladybug_db_path)
    try:
        store.open()
    except Exception as exc:
        print(
            f"error: failed to open LadybugDB at '{settings.ladybug_db_path}': {exc}",
            file=sys.stderr,
        )
        return EXIT_DB

    try:
        existing_name = store.scalar(
            "MATCH (s:Skill {skill_id: $id}) RETURN s.canonical_name",
            {"id": record.skill_id},
        )
        if existing_name is not None and not force:
            print(
                f"skip: skill_id '{record.skill_id}' already exists "
                f"(canonical_name: '{existing_name}'). Use --force to overwrite.",
                file=sys.stderr,
            )
            return EXIT_DUPLICATE

        if existing_name is None:
            existing_id_by_name = store.scalar(
                "MATCH (s:Skill {canonical_name: $name}) RETURN s.skill_id",
                {"name": record.canonical_name},
            )
            if existing_id_by_name is not None and not force:
                print(
                    f"skip: canonical_name '{record.canonical_name}' is already used by "
                    f"skill_id '{existing_id_by_name}'. Use --force to overwrite.",
                    file=sys.stderr,
                )
                return EXIT_DUPLICATE

        _print_summary(record, existing=existing_name is not None)
        if not yes:
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("Aborted.", file=sys.stderr)
                return EXIT_USAGE

        # --- check superseded_by reference (needs DB access) ---
        if record.superseded_by:
            ref_exists = store.scalar(
                "MATCH (s:Skill {skill_id: $id}) RETURN s.skill_id",
                {"id": record.superseded_by},
            )
            if ref_exists is None:
                print(
                    f"validation error: superseded_by '{record.superseded_by}' "
                    f"references a non-existent skill_id",
                    file=sys.stderr,
                )
                return EXIT_VALIDATION

        try:
            _insert(store, record, force=force)
        except Exception as exc:
            print(f"error: DB insert failed: {exc}", file=sys.stderr)
            return EXIT_DB

    finally:
        store.close()

    print(f"ok: loaded '{record.skill_id}' ({record.canonical_name})")
    return EXIT_OK


def _batch(directory: Path, *, force: bool, yes: bool, strict: bool = False) -> int:
    yaml_files = sorted(directory.glob("*.yaml"))
    if not yaml_files:
        print(f"error: no YAML files found in {directory}", file=sys.stderr)
        return EXIT_USAGE

    # --- Phase 1: parse + validate all files without touching the DB ---
    parsed: list[tuple[Path, ReviewRecord]] = []
    invalid: list[tuple[Path, list[str]]] = []
    lint_warnings: list[tuple[Path, list[str]]] = []

    for f in yaml_files:
        try:
            record = _load_yaml(f)
        except IngestError as exc:
            invalid.append((f, [str(exc)]))
            continue
        errs = _validate(record)
        warns = _lint(record, yaml_path=f)
        if strict:
            errs = errs + warns
            warns = []
        if errs:
            invalid.append((f, errs))
        else:
            parsed.append((f, record))
            if warns:
                lint_warnings.append((f, warns))

    # --- Phase 2: consolidated review summary ---
    print(f"\n{'=' * 60}")
    print(f"  Batch directory: {directory}")
    print(f"  Files found:     {len(yaml_files)}")
    print(f"  Valid:           {len(parsed)}")
    print(f"  Invalid:         {len(invalid)}")
    print(f"{'=' * 60}")

    for f, errs in invalid:
        print(f"\n  INVALID: {f.name}")
        for e in errs:
            print(f"    - {e}", file=sys.stderr)

    if lint_warnings:
        total = sum(len(ws) for _, ws in lint_warnings)
        print(
            f"\n  {total} lint warning(s) across {len(lint_warnings)} file(s) "
            f"(use --strict to promote to errors):",
            file=sys.stderr,
        )
        for f, ws in lint_warnings:
            print(f"    {f.name}", file=sys.stderr)
            for w in ws:
                print(f"      - {w}", file=sys.stderr)

    if parsed:
        print("\n  Skills to load:")
        for _f, r in parsed:
            frag_info = f"  {len(r.fragments)} fragment(s)" if r.skill_type == "domain" else ""
            print(f"    {r.skill_id:<40} [{r.skill_type}]{frag_info}")

    print()

    if not parsed:
        print("No valid files to load.")
        return EXIT_VALIDATION if invalid else EXIT_OK

    # --- Phase 3: open DB and check duplicates ---
    settings = get_settings()
    store = LadybugStore(settings.ladybug_db_path)
    try:
        store.open()
    except Exception as exc:
        print(
            f"error: failed to open LadybugDB at '{settings.ladybug_db_path}': {exc}",
            file=sys.stderr,
        )
        return EXIT_DB

    blocked: list[tuple[Path, ReviewRecord, str]] = []
    to_load: list[tuple[Path, ReviewRecord]] = []

    for f, record in parsed:
        existing_name = store.scalar(
            "MATCH (s:Skill {skill_id: $id}) RETURN s.canonical_name",
            {"id": record.skill_id},
        )
        if existing_name is not None and not force:
            blocked.append(
                (f, record, f"skill_id '{record.skill_id}' already exists — use --force")
            )
            continue

        if existing_name is None:
            existing_id = store.scalar(
                "MATCH (s:Skill {canonical_name: $name}) RETURN s.skill_id",
                {"name": record.canonical_name},
            )
            if existing_id is not None and not force:
                blocked.append(
                    (
                        f,
                        record,
                        f"canonical_name '{record.canonical_name}' "
                        f"already used by '{existing_id}' — use --force",
                    )
                )
                continue

        to_load.append((f, record))

    if blocked:
        print(f"  {len(blocked)} file(s) blocked by duplicate check:", file=sys.stderr)
        for f, _, reason in blocked:
            print(f"    {f.name}: {reason}", file=sys.stderr)

    if not to_load:
        print("Nothing to load after duplicate check.", file=sys.stderr)
        store.close()
        return EXIT_VALIDATION

    if not yes:
        try:
            answer = input(f"Load {len(to_load)} skill(s)? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            store.close()
            return EXIT_USAGE

    # --- Phase 4: load transactionally (all-or-nothing with manual rollback) ---
    loaded = 0
    failed = 0
    inserted_skill_ids: list[str] = []
    error_details: list[str] = []
    try:
        for f, record in to_load:
            try:
                tier, _src = resolve_skill_tier(f)
                record.tier = tier
                _insert(store, record, force=force)
                print(f"ok: {record.skill_id} ({record.canonical_name})")
                loaded += 1
                inserted_skill_ids.append(record.skill_id)
            except Exception as exc:
                failed += 1
                error_details.append(f"{f.name}: {record.skill_id} — {exc}")
                logger.error(
                    "Batch ingest failure during %s: %s",
                    record.skill_id,
                    exc,
                )
                # Roll back all previously inserted skills to maintain consistency
                if inserted_skill_ids:
                    logger.warning(
                        "Rolling back %d previously inserted skill(s): %s",
                        len(inserted_skill_ids),
                        ", ".join(inserted_skill_ids),
                    )
                    store.rollback_batch(inserted_skill_ids)
                    inserted_skill_ids.clear()
                break  # Abort the batch on first failure

    except Exception as exc:
        # Top-level error — roll back everything
        if inserted_skill_ids:
            logger.error("Batch ingest top-level error — rolling back: %s", exc)
            store.rollback_batch(inserted_skill_ids)
            inserted_skill_ids.clear()
        raise

    finally:
        store.close()

    if failed > 0:
        logger.error(
            "Batch ingest failed — %d operation(s) failed:\n  %s",
            len(error_details),
            "\n  ".join(error_details),
        )

    skipped = len(invalid) + len(blocked)
    print(f"\nLoaded: {loaded}  Failed: {failed}  Skipped: {skipped}")
    return EXIT_OK if failed == 0 else EXIT_DB


def _load_yaml(path: Path) -> ReviewRecord:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise IngestError(f"{path}: YAML parse error: {exc}") from exc

    if not isinstance(raw, dict):
        raise IngestError(f"{path}: expected a YAML mapping at the top level")

    data: dict[str, Any] = cast("dict[str, Any]", raw)

    def _str(key: str, default: str = "") -> str:
        v: Any = data.get(key, default)
        return str(v).strip() if v is not None else default

    def _bool(key: str, default: bool = False) -> bool:
        v: Any = data.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("true", "yes", "1")
        return bool(v)

    def _strlist(key: str) -> list[str]:
        v: Any = data.get(key)
        if not v:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in cast(list[Any], v) if x]
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return []

    skill_type = _str("skill_type")
    if skill_type not in ("domain", "system"):
        raise IngestError(f"{path}: 'skill_type' must be 'domain' or 'system', got '{skill_type}'")

    raw_fragments: Any = data.get("fragments") or []
    if not isinstance(raw_fragments, list):
        raise IngestError(f"{path}: 'fragments' must be a list")

    fragments: list[FragmentRecord] = []
    for i, frag in enumerate(cast(list[Any], raw_fragments)):
        if not isinstance(frag, dict):
            raise IngestError(f"{path}: fragment[{i}] must be a mapping")
        frag_data: dict[str, Any] = cast("dict[str, Any]", frag)
        fragments.append(
            FragmentRecord(
                sequence=int(frag_data.get("sequence", i + 1)),
                fragment_type=str(frag_data.get("fragment_type", "")).strip(),
                content=str(frag_data.get("content", "")).strip(),
            )
        )

    return ReviewRecord(
        skill_type=skill_type,
        skill_id=_str("skill_id"),
        canonical_name=_str("canonical_name"),
        category=_str("category"),
        skill_class=_str("skill_class", skill_type),
        domain_tags=_strlist("domain_tags"),
        always_apply=_bool("always_apply"),
        phase_scope=_strlist("phase_scope"),
        category_scope=_strlist("category_scope"),
        author=_str("author", "operator"),
        change_summary=_str("change_summary", "initial authoring"),
        raw_prose=_str("raw_prose"),
        fragments=fragments,
        deprecated=_bool("deprecated"),
        superseded_by=_str("superseded_by"),
        description=_str("description"),
    )


def _validate(record: ReviewRecord) -> list[str]:
    errors: list[str] = []

    if not record.skill_id:
        errors.append("skill_id is required")

    if not record.canonical_name:
        errors.append("canonical_name is required")

    if not record.raw_prose:
        errors.append("raw_prose is required")

    # --- deprecation validation ---
    if record.deprecated and not record.superseded_by:
        errors.append(
            "deprecated: true requires 'superseded_by' to be set — "
            "a skill cannot be deprecated without a replacement"
        )

    if record.superseded_by and not re.match(r"^[a-z0-9-]+$", record.superseded_by):
        errors.append(f"superseded_by '{record.superseded_by}' must be kebab-case, lowercase ASCII")

    if record.skill_type == "system":
        if not record.skill_id.startswith("sys-"):
            errors.append(f"system skill_id '{record.skill_id}' must start with 'sys-'")
        if record.category and record.category not in _VALID_SYSTEM_CATEGORIES:
            errors.append(
                f"category '{record.category}' is not valid for system skills "
                f"(must be one of {sorted(_VALID_SYSTEM_CATEGORIES)})"
            )
        if not record.always_apply and not record.phase_scope and not record.category_scope:
            errors.append(
                "system skill must declare applicability: "
                "set always_apply=true, phase_scope, or category_scope"
            )
        if record.always_apply and (record.phase_scope or record.category_scope):
            errors.append(
                "always_apply=true is mutually exclusive with phase_scope / category_scope"
            )
        for phase in record.phase_scope:
            if phase not in _VALID_PHASES:
                errors.append(
                    f"phase_scope '{phase}' is not valid (must be one of {sorted(_VALID_PHASES)})"
                )
        if record.fragments:
            errors.append(
                "system skills do not declare fragments — the ingest CLI generates "
                "a single guardrail fragment from raw_prose automatically"
            )

    elif record.skill_type == "domain":
        if record.category and record.category not in _VALID_DOMAIN_CATEGORIES:
            errors.append(
                f"category '{record.category}' is not valid for domain skills "
                f"(must be one of {sorted(_VALID_DOMAIN_CATEGORIES)})"
            )
        if not record.fragments:
            errors.append("domain skill requires at least one fragment")
        else:
            types = {f.fragment_type for f in record.fragments}
            if "execution" not in types:
                errors.append("domain skill requires at least one 'execution' fragment")
            sequences = sorted(f.sequence for f in record.fragments)
            expected = list(range(sequences[0], sequences[0] + len(sequences)))
            if sequences != expected:
                errors.append(f"fragment sequences are not contiguous: got {sequences}")
            for frag in record.fragments:
                if frag.fragment_type not in _VALID_FRAGMENT_TYPES:
                    errors.append(
                        f"fragment_type '{frag.fragment_type}' is not valid "
                        f"(must be one of {sorted(_VALID_FRAGMENT_TYPES)})"
                    )
                if not frag.content:
                    errors.append(f"fragment sequence {frag.sequence} has empty content")
                    continue
                wc = _word_count(frag.content)
                if wc < _FRAG_WORDS_HARD_MIN:
                    errors.append(
                        f"fragment sequence {frag.sequence} is {wc} words; "
                        f"hard floor is {_FRAG_WORDS_HARD_MIN} — merge with "
                        f"adjacent fragment or expand"
                    )
                if wc > _FRAG_WORDS_HARD_MAX:
                    errors.append(
                        f"fragment sequence {frag.sequence} is {wc} words; "
                        f"hard ceiling is {_FRAG_WORDS_HARD_MAX} — split at semantic boundary"
                    )
                if _is_heading_only(frag.content):
                    errors.append(
                        f"fragment sequence {frag.sequence} is a heading-only stub "
                        f"({wc} words); merge with the next fragment or drop it"
                    )
        if len(record.domain_tags) > _TAGS_VALIDATE_HARD_CAP:
            errors.append(
                f"domain_tags has {len(record.domain_tags)} entries; hard ceiling is "
                f"{_TAGS_VALIDATE_HARD_CAP}"
            )

    return errors


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _is_heading_only(content: str) -> bool:
    """A fragment is a heading-only stub if it contains nothing but a single
    markdown heading (no body) — these embed under-discriminatively and should
    be merged into the following fragment."""
    stripped = (content or "").strip()
    if not stripped.startswith("#"):
        return False
    # Single-line heading: '# Title' with no body
    if "\n" not in stripped and _word_count(stripped) <= _HEADING_ONLY_MAX_WORDS:
        return True
    # Multi-line where every non-empty line is itself a heading
    lines = [line for line in stripped.splitlines() if line.strip()]
    if all(line.lstrip().startswith("#") for line in lines):
        return _word_count(stripped) <= _HEADING_ONLY_MAX_WORDS
    return False


def _lint(record: ReviewRecord, yaml_path: Path | None = None) -> list[str]:
    """Quality-bar warnings derived from fixtures/skill-authoring-guidelines.md
    (R1–R8) and fixtures/skill-authoring-agent.md. Non-blocking unless --strict.
    """
    from agentalloy.lint_tags_mechanical import lint_tags_mechanical

    warnings: list[str] = []

    # --- Mechanical tag lint ---
    tier: str | None = None
    if yaml_path is not None:
        tier, _source = resolve_skill_tier(yaml_path)
        record.tier = tier  # propagate so callers don't need a second walk
    tag_verdicts = lint_tags_mechanical(
        tags=record.domain_tags,
        skill_class=record.skill_class,
        canonical_name=record.canonical_name,
        tier=tier,
    )
    for tv in tag_verdicts:
        warnings.append(f"tag lint [{tv.rule}] '{tv.tag}': {tv.verdict} — {tv.detail}")

    if record.skill_type != "domain" or not record.fragments:
        return warnings

    normalized_prose = _normalize_ws(record.raw_prose)
    for frag in record.fragments:
        if _normalize_ws(frag.content) and _normalize_ws(frag.content) not in normalized_prose:
            warnings.append(
                f"fragment sequence {frag.sequence} content is not a contiguous "
                f"slice of raw_prose (modulo whitespace) — drift breaks "
                f"BM25/full-text retrieval against the canonical body "
                f"(fixtures/skill-authoring-agent.md §'Domain skill rules')"
            )

    types = {f.fragment_type for f in record.fragments}

    if types == {"execution"} and len(record.fragments) > 1:
        warnings.append(
            "all fragments are 'execution' — diversify into setup/example/"
            "verification/guardrail/rationale per the 6-type taxonomy"
        )

    if "rationale" not in types:
        warnings.append(
            "no 'rationale' fragment — R8: rationale anchors retrieval for "
            "'why' queries; add one with ≥3 obvious-query keywords"
        )

    if "verification" not in types:
        warnings.append(
            "no 'verification' fragment — R3: verification items are contracts "
            "for downstream agents; add mechanically-checkable post-conditions"
        )

    for frag in record.fragments:
        wc = _word_count(frag.content)
        if wc < _FRAG_WORDS_WARN_MIN and wc >= _FRAG_WORDS_HARD_MIN:
            warnings.append(
                f"fragment sequence {frag.sequence} is {wc} words; below the "
                f"{_FRAG_WORDS_WARN_MIN}-word floor — qwen3-embedding:0.6b "
                f"produces under-discriminative vectors at this size"
            )
        elif wc < _FRAG_WORDS_HARD_MIN:
            # Hard-fail surfaces in _validate; suppress dup warning here.
            pass
        if wc > _FRAG_WORDS_WARN_MAX and wc <= _FRAG_WORDS_HARD_MAX:
            warnings.append(
                f"fragment sequence {frag.sequence} is {wc} words; above the "
                f"{_FRAG_WORDS_WARN_MAX}-word target — split at a semantic boundary"
            )
        if frag.fragment_type == "execution":
            fence_count = frag.content.count("```")
            if fence_count >= 2 and len(frag.content) > 200:
                code_lines = sum(
                    1
                    for line in frag.content.splitlines()
                    if line.strip()
                    and not line.lstrip().startswith("#")
                    and not line.lstrip().startswith(">")
                )
                if fence_count >= 4 or (
                    fence_count >= 2 and code_lines >= _word_count(frag.content) // 4
                ):
                    warnings.append(
                        f"fragment sequence {frag.sequence} ('execution') is "
                        f"code-fence-heavy — likely should be 'example' per "
                        f"fixtures/skill-authoring-agent.md §'Special cases'"
                    )

    cs = (record.change_summary or "").lower()
    if "imported from" in cs and len(record.raw_prose) > 4000:
        warnings.append(
            "change_summary says 'imported from ...' but raw_prose >4000 chars — "
            "verify per R6: if scaffolded, use 'scaffold by agentalloy around "
            "upstream prose preserved in fragment <N>'"
        )

    return warnings


def _print_summary(record: ReviewRecord, *, existing: bool) -> None:
    action = "OVERWRITE" if existing else "INSERT"
    print(f"\n{'=' * 60}")
    print(f"  Action:         {action}")
    print(f"  skill_type:     {record.skill_type}")
    print(f"  skill_id:       {record.skill_id}")
    print(f"  canonical_name: {record.canonical_name}")
    print(f"  category:       {record.category}")
    print(f"  deprecated:     {record.deprecated}")
    print(f"  superseded_by:  {record.superseded_by or '(none)'}")
    print(f"  always_apply:   {record.always_apply}")
    print(f"  phase_scope:    {record.phase_scope or '(none)'}")
    print(f"  category_scope: {record.category_scope or '(none)'}")
    print(f"  author:         {record.author}")
    print(f"  prose length:   {len(record.raw_prose)} chars")
    if record.skill_type == "domain":
        frag_types = ", ".join(f.fragment_type for f in record.fragments)
        print(f"  fragments:      {len(record.fragments)} ({frag_types})")
    print(f"{'=' * 60}\n")


def _insert(store: LadybugStore, record: ReviewRecord, *, force: bool) -> None:
    version_id = f"{record.skill_id}-v1"
    now = datetime.now(tz=UTC)

    if force:
        store.execute(
            """
            MATCH (s:Skill {skill_id: $id})
            OPTIONAL MATCH (s)-[:HAS_VERSION]->(v:SkillVersion)
            OPTIONAL MATCH (v)-[:DECOMPOSES_TO]->(f:Fragment)
            DETACH DELETE s, v, f
            """,
            {"id": record.skill_id},
        )

    store.execute(
        """
        CREATE (:Skill {
            skill_id: $skill_id,
            canonical_name: $canonical_name,
            category: $category,
            skill_class: $skill_class,
            domain_tags: $domain_tags,
            deprecated: $deprecated,
            superseded_by: $superseded_by,
            always_apply: $always_apply,
            phase_scope: $phase_scope,
            category_scope: $category_scope,
            tier: $tier,
            description: $description
        })
        """,
        {
            "skill_id": record.skill_id,
            "canonical_name": record.canonical_name,
            "category": record.category,
            "skill_class": record.skill_class,
            "domain_tags": record.domain_tags,
            "deprecated": record.deprecated,
            "superseded_by": record.superseded_by or "",
            "always_apply": record.always_apply,
            "phase_scope": record.phase_scope,
            "category_scope": record.category_scope,
            "tier": record.tier,
            # "" → NULL keeps the column absent-tolerant for blank-description skills.
            "description": record.description or None,
        },
    )

    store.execute(
        """
        CREATE (:SkillVersion {
            version_id: $version_id,
            version_number: 1,
            authored_at: $authored_at,
            author: $author,
            change_summary: $change_summary,
            status: 'active',
            raw_prose: $raw_prose
        })
        """,
        {
            "version_id": version_id,
            "authored_at": now,
            "author": record.author,
            "change_summary": record.change_summary,
            "raw_prose": record.raw_prose,
        },
    )

    store.execute(
        """
        MATCH (s:Skill {skill_id: $skill_id}), (v:SkillVersion {version_id: $version_id})
        CREATE (s)-[:HAS_VERSION]->(v)
        CREATE (s)-[:CURRENT_VERSION]->(v)
        """,
        {"skill_id": record.skill_id, "version_id": version_id},
    )

    if record.skill_type == "system":
        fragment_id = f"{record.skill_id}-v1-f1"
        store.execute(
            """
            CREATE (:Fragment {
                fragment_id: $fragment_id,
                fragment_type: 'guardrail',
                sequence: 1,
                content: $content
            })
            """,
            {
                "fragment_id": fragment_id,
                "content": record.raw_prose,
            },
        )
        store.execute(
            """
            MATCH (v:SkillVersion {version_id: $version_id}), (f:Fragment {fragment_id: $fragment_id})
            CREATE (v)-[:DECOMPOSES_TO]->(f)
            """,
            {"version_id": version_id, "fragment_id": fragment_id},
        )
    else:
        for frag in record.fragments:
            fragment_id = f"{record.skill_id}-v1-f{frag.sequence}"
            store.execute(
                """
                CREATE (:Fragment {
                    fragment_id: $fragment_id,
                    fragment_type: $fragment_type,
                    sequence: $sequence,
                    content: $content
                })
                """,
                {
                    "fragment_id": fragment_id,
                    "fragment_type": frag.fragment_type,
                    "sequence": frag.sequence,
                    "content": frag.content,
                },
            )
            store.execute(
                """
                MATCH (v:SkillVersion {version_id: $version_id}),
                      (f:Fragment {fragment_id: $fragment_id})
                CREATE (v)-[:DECOMPOSES_TO]->(f)
                """,
                {"version_id": version_id, "fragment_id": fragment_id},
            )


if __name__ == "__main__":
    sys.exit(main())
