# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""``new-skill-pack`` subcommand — scaffold a local pack.yaml + skill YAML.

Closes the "hand-write a pack.yaml from scratch against the spec doc" gap in
the custom-skill journey. Generates a schema-correct skill YAML (for the
requested ``--skill-class``) with clearly-marked ``[FILL IN]`` placeholder
prose that is substantive enough to clear ``agentalloy.ingest``'s WARN-level
lint thresholds under ``--strict`` — the point is that a user's first
``agentalloy validate-pack`` run on the freshly-scaffolded pack is clean, not
a wall of warnings-as-errors.

Pure local file scaffolding: no corpus mutation, no network, no install state.
Recommended flow::

    agentalloy new-skill-pack <dir> --skill-id <id>   # scaffold
    # ... edit the [FILL IN] placeholders ...
    agentalloy validate-pack <dir>                    # check (Gate 1 dry-run)
    agentalloy install-pack <dir>                     # ship it
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any

import yaml as _yaml

from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1

_VALID_SKILL_CLASSES = ("domain", "system", "workflow")

# Mirrors install_pack._PACK_NAME_RE's intent: safe on-disk names, no path
# traversal (`..`, `/`) or scheme injection via the generated file paths.
_SKILL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# Domain category — "tooling" is the safe, broadly-applicable default from
# ingest._VALID_DOMAIN_CATEGORIES.
_DOMAIN_CATEGORY = "tooling"
# System category — "operational" from ingest._VALID_SYSTEM_CATEGORIES.
_SYSTEM_CATEGORY = "operational"
# Workflow category — valid in both ingest._VALID_DOMAIN_CATEGORIES and
# _VALID_SYSTEM_CATEGORIES (ingest._validate accepts the union for workflow).
_WORKFLOW_CATEGORY = "tooling"

# A real WORKFLOW_POSITION_MARKERS entry (lint_tags_mechanical.py's W1 rule)
# so the scaffolded workflow skill clears strict lint with zero warnings too.
_WORKFLOW_PLACEHOLDER_TAG = "phase:build"


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------


def _derive_canonical_name(skill_id: str) -> str:
    """ "foo-bar" -> "Foo Bar". Strips a leading "sys-" so system skills get a
    clean title rather than "Sys Foo Bar"."""
    base = skill_id[len("sys-") :] if skill_id.startswith("sys-") else skill_id
    parts = re.split(r"[-_]+", base.strip())
    title = " ".join(p.capitalize() for p in parts if p)
    return title or skill_id


def _derive_domain_tags(skill_id: str) -> list[str]:
    """Two placeholder domain_tags derived from the skill_id's first token.

    Deliberately share only the first token (not the full tag string) between
    the two tags so lint_tags_mechanical's R3-stem check (full stem-SET
    equality) never fires — {primary, "tool"} != {primary, "guid"}.
    """
    tokens = [t for t in re.split(r"[^a-zA-Z0-9]+", skill_id.strip().lower()) if t]
    primary = tokens[0] if tokens else "custom"
    return [f"{primary}-tooling", f"{primary}-guide"]


def _domain_fragments(skill_id: str) -> list[dict[str, Any]]:
    execution = (
        "## Execution\n\n"
        "[FILL IN] Describe the concrete steps, commands, or code changes an "
        "agent should take to accomplish this skill's task. Replace this "
        "paragraph with the specific actions: which commands to run, which "
        "files to edit, and the order of operations. Be concrete enough that "
        "a fresh agent unfamiliar with this skill could follow the steps "
        "without any additional context or clarification from a human."
    )
    verification = (
        "## Verification\n\n"
        "[FILL IN] Describe how to confirm this skill's task was completed "
        "correctly: which commands to re-run, which output or files to "
        "inspect, and what a passing result looks like. Replace this "
        "paragraph with mechanically-checkable post-conditions so a "
        "downstream agent or reviewer can confirm success without guessing "
        "or re-deriving the acceptance criteria from scratch."
    )
    rationale = (
        "## Rationale\n\n"
        "[FILL IN] Explain why this approach matters: the failure mode it "
        "prevents, the tradeoff it makes, or the context that justifies the "
        "steps above. Replace this paragraph with the reasoning an agent "
        "needs to recognize when this skill applies and why the guidance "
        f"holds, using the obvious keywords a real query about '{skill_id}' "
        "would contain."
    )
    return [
        {"sequence": 0, "fragment_type": "execution", "content": execution},
        {"sequence": 1, "fragment_type": "verification", "content": verification},
        {"sequence": 2, "fragment_type": "rationale", "content": rationale},
    ]


def _build_skill_record(
    effective_skill_id: str, skill_class: str, canonical_name: str
) -> dict[str, Any]:
    """Build the skill YAML content (as an ordered dict) for *skill_class*."""
    description = f"[FILL IN] one-line summary of what '{canonical_name}' teaches an agent."

    if skill_class == "domain":
        fragments = _domain_fragments(effective_skill_id)
        # Hard requirement (ingest._lint's fragment/raw_prose drift check):
        # raw_prose must be the literal concatenation of the fragments'
        # content, in order, separated by blank lines.
        raw_prose = "\n\n".join(str(f["content"]) for f in fragments)
        return {
            "skill_id": effective_skill_id,
            "canonical_name": canonical_name,
            "description": description,
            "category": _DOMAIN_CATEGORY,
            "skill_class": "domain",
            "domain_tags": _derive_domain_tags(effective_skill_id),
            "always_apply": False,
            "phase_scope": [],
            "category_scope": [],
            "author": "local",
            "change_summary": "initial scaffold via `agentalloy new-skill-pack`",
            "raw_prose": raw_prose,
            "fragments": fragments,
        }

    if skill_class == "system":
        raw_prose = (
            f"# {canonical_name}\n\n"
            "[FILL IN] Describe exactly when this system skill fires (which "
            "tool, file glob, or git state triggers it) and the single "
            "guardrail or instruction it must enforce every time. System "
            "skills are injected verbatim, so keep this prose short, "
            "imperative, and unambiguous."
        )
        return {
            "skill_id": effective_skill_id,
            "canonical_name": canonical_name,
            "description": description,
            "category": _SYSTEM_CATEGORY,
            "skill_class": "system",
            "domain_tags": [],
            "always_apply": True,
            "author": "local",
            "change_summary": "initial scaffold via `agentalloy new-skill-pack`",
            "raw_prose": raw_prose,
        }

    # workflow
    raw_prose = (
        f"# {canonical_name}\n\n"
        "[FILL IN] Describe what an agent must do during this phase: the "
        "order of operations, the commands to run, and the artifact(s) it "
        "must produce before advancing. Replace this placeholder with the "
        "real phase guidance; keep any exact command strings or paths your "
        "exit_gates reference so a later edit doesn't silently break the "
        "phase-transition check."
    )
    contract_template = (
        "---\n"
        "phase: build\n"
        "task_slug: {{task_slug}}\n"
        "domain_tags: []\n"
        "---\n\n"
        "# {{task_slug}}\n\n"
        "[FILL IN] the task contract body an agent should see for this phase.\n"
    )
    return {
        "skill_id": effective_skill_id,
        "canonical_name": canonical_name,
        "description": description,
        "category": _WORKFLOW_CATEGORY,
        "skill_class": "workflow",
        "domain_tags": [_WORKFLOW_PLACEHOLDER_TAG],
        "applies_to_phases": ["build"],
        "author": "local",
        "change_summary": "initial scaffold via `agentalloy new-skill-pack`",
        "raw_prose": raw_prose,
        "contract_template": contract_template,
        "signal_keywords": ["[FILL IN] a phrase that signals this phase is done"],
        # A structurally-valid exit_gates leaf (see ingest._validate_gate_spec
        # and signals.predicates.eval_artifact_exists's args shape).
        "exit_gates": {
            "artifact_exists": {"path": f".agentalloy/contracts/build/{effective_skill_id}-*.md"}
        },
    }


def _default_pack_manifest(pack_name: str) -> dict[str, Any]:
    return {
        "name": pack_name,
        "version": "0.1.0",
        "tier": "domain",
        "description": f"Custom local skill pack: {pack_name}.",
        "author": "local",
        "embed_model": "nomic-embed-text-v1.5",
        "embedding_dim": 768,
        "license": "MIT",
        "always_install": False,
        "skills": [],
    }


class _BlockDumper(_yaml.SafeDumper):
    """Local (non-global) Dumper subclass so multi-line strings render as
    readable YAML block scalars (``|``) without mutating yaml.SafeDumper for
    the whole process."""


def _str_presenter(dumper: _yaml.SafeDumper, data: str) -> Any:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockDumper.add_representer(str, _str_presenter)


def _dump_yaml(data: dict[str, Any]) -> str:
    return _yaml.dump(
        data,
        Dumper=_BlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def new_skill_pack(
    pack_dir: Path,
    *,
    skill_id: str,
    skill_class: str,
    canonical_name: str | None = None,
    pack_name: str | None = None,
) -> dict[str, Any]:
    """Scaffold *pack_dir* with a pack.yaml (created or appended-to) and a new
    skill YAML for *skill_id*. Returns a contract-shaped result dict.

    Refuses (no files written) if the target skill YAML already exists, or if
    *skill_id* isn't a safe on-disk name.
    """
    t0 = time.monotonic()
    skill_id = skill_id.strip()

    if skill_class not in _VALID_SKILL_CLASSES:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "invalid_skill_class",
            "pack_dir": str(pack_dir),
            "error": f"skill_class '{skill_class}' must be one of {_VALID_SKILL_CLASSES}",
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    if not _SKILL_ID_RE.match(skill_id):
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "invalid_skill_id",
            "pack_dir": str(pack_dir),
            "error": (
                f"--skill-id '{skill_id}' contains disallowed characters. "
                "Must match [a-zA-Z0-9][a-zA-Z0-9_-]{0,63} (no slashes, dots, "
                "or path traversal)."
            ),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    if pack_dir.exists() and not pack_dir.is_dir():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "pack_dir_not_a_directory",
            "pack_dir": str(pack_dir),
            "error": f"{pack_dir} exists and is not a directory.",
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    prefixed_note: str | None = None
    effective_skill_id = skill_id
    if skill_class == "system" and not skill_id.startswith("sys-"):
        effective_skill_id = f"sys-{skill_id}"
        prefixed_note = (
            f"--skill-id auto-prefixed to '{effective_skill_id}' — system skills "
            "must start with 'sys-' (ingest._validate hard requirement)."
        )

    skill_yaml_path = pack_dir / f"{effective_skill_id}.yaml"
    if skill_yaml_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "skill_already_exists",
            "pack_dir": str(pack_dir),
            "skill_id": effective_skill_id,
            "error": f"{skill_yaml_path} already exists — refusing to overwrite.",
            "remediation": ("Choose a different --skill-id, or edit the existing file directly."),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    title_source = skill_id[len("sys-") :] if skill_id.startswith("sys-") else skill_id
    resolved_canonical_name = canonical_name or _derive_canonical_name(title_source)
    resolved_pack_name = pack_name or (pack_dir.name or "custom-pack")

    # --- Load-or-create pack.yaml ---
    manifest_path = pack_dir / "pack.yaml"
    manifest_existed = manifest_path.is_file()
    if manifest_existed:
        try:
            manifest: Any = _yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except _yaml.YAMLError as exc:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "manifest_parse_error",
                "pack_dir": str(pack_dir),
                "error": f"{manifest_path}: YAML parse error: {exc}",
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
        if not isinstance(manifest, dict):
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "manifest_invalid",
                "pack_dir": str(pack_dir),
                "error": f"{manifest_path}: expected a YAML mapping at the top level",
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
    else:
        manifest = _default_pack_manifest(resolved_pack_name)

    existing_skills = manifest.get("skills")
    skills_list: list[Any] = list(existing_skills) if isinstance(existing_skills, list) else []
    fragment_count = 3 if skill_class == "domain" else 0
    skills_list.append(
        {
            "skill_id": effective_skill_id,
            "file": f"{effective_skill_id}.yaml",
            "fragment_count": fragment_count,
        }
    )
    manifest["skills"] = skills_list

    # --- Write files only after all validation has passed ---
    pack_dir.mkdir(parents=True, exist_ok=True)
    skill_record = _build_skill_record(effective_skill_id, skill_class, resolved_canonical_name)
    skill_yaml_path.write_text(_dump_yaml(skill_record), encoding="utf-8")
    manifest_path.write_text(_dump_yaml(manifest), encoding="utf-8")

    files_written = [str(skill_yaml_path)] + ([] if manifest_existed else [str(manifest_path)])
    files_modified = [str(manifest_path)] if manifest_existed else []

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "scaffolded",
        "pack_dir": str(pack_dir),
        "pack_name": manifest.get("name"),
        "skill_id": effective_skill_id,
        "skill_class": skill_class,
        "canonical_name": resolved_canonical_name,
        "files_written": files_written,
        "files_modified": files_modified,
        "note": prefixed_note,
        "next_steps": [
            f"Edit {skill_yaml_path} and replace the [FILL IN] placeholders.",
            f"agentalloy validate-pack {pack_dir}",
            f"agentalloy install-pack {pack_dir}",
        ],
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser(
        "new-skill-pack",
        help=(
            "Scaffold a local pack.yaml + skill YAML (schema-correct, lint-clean "
            "placeholders) ready for validate-pack / install-pack."
        ),
    )
    p.add_argument(
        "pack_dir",
        help="Directory to create (or add a skill to). Created if missing.",
    )
    p.add_argument(
        "--skill-id",
        required=True,
        help=(
            "Skill id (kebab-case). System skills (--skill-class system) are "
            "auto-prefixed with 'sys-' if not already present."
        ),
    )
    p.add_argument(
        "--skill-class",
        choices=_VALID_SKILL_CLASSES,
        default="domain",
        help="Skill class to scaffold (default: domain).",
    )
    p.add_argument(
        "--canonical-name",
        default=None,
        help=(
            "Human-readable name (default: derived from --skill-id, e.g. 'foo-bar' -> 'Foo Bar')."
        ),
    )
    p.add_argument(
        "--pack-name",
        default=None,
        help=(
            "Pack name for a NEW pack.yaml (default: pack_dir's basename). "
            "Ignored when pack.yaml already exists."
        ),
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    action = result.get("action", "unknown")
    if action != "scaffolded":
        print_rich("\n  [bold]new-skill-pack[/bold]\n")
        print_rich(f"  [red]FAILED[/red]: {result.get('error', action)}")
        remediation = result.get("remediation")
        if remediation:
            print_rich(f"  [dim]FIX: {remediation}[/dim]")
        print_rich()
        return

    print_rich("\n  [bold]new-skill-pack[/bold]\n")
    print_rich(f"  Pack:  {result.get('pack_name')}  ({result.get('pack_dir')})")
    print_rich(f"  Skill: {result.get('skill_id')}  ({result.get('skill_class')})")
    print_rich(f"  Name:  {result.get('canonical_name')}")
    note = result.get("note")
    if note:
        print_rich(f"  [yellow]note[/yellow]: {note}")
    for f in result.get("files_written") or []:
        print_rich(f"    [green]+[/green] {f}")
    for f in result.get("files_modified") or []:
        print_rich(f"    [yellow]~[/yellow] {f}")
    print_rich("\n  Next steps:")
    for step in result.get("next_steps") or []:
        print_rich(f"    - {step}")
    print_rich()


def _run(args: argparse.Namespace) -> int:
    result = new_skill_pack(
        Path(args.pack_dir),
        skill_id=args.skill_id,
        skill_class=args.skill_class,
        canonical_name=args.canonical_name,
        pack_name=args.pack_name,
    )
    write_result(result, args, human_fn=_render_human)
    return 0 if result.get("action") == "scaffolded" else 1
