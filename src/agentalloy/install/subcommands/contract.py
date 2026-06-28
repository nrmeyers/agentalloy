"""``agentalloy contract`` — contract management subcommand.

Commands:
    agentalloy contract validate <path>
    agentalloy contract show <path>
    agentalloy contract init --phase <name> --slug <slug>
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentalloy.install.output import add_json_flag, print_rich, write_result


def _render_validate(result: dict[str, Any]) -> None:
    """Render contract validation in human-readable format."""
    print_rich("\n  [bold]Contract Validation[/bold]\n")
    print_rich(f"  Path: {result['path']}")
    print_rich(f"  Phase: {result['phase']}")
    print_rich(f"  Slug: {result['task_slug']}")
    if result["valid"]:
        print_rich("  [green]Valid[/green]")
    else:
        print_rich(f"  [red]Issues: {len(result['issues'])}[/red]")
        for issue in result["issues"]:
            print_rich(f"  [red]x[/red] {issue}")
    print_rich()


def _validate(args: argparse.Namespace) -> int:
    from agentalloy.contracts import ContractMalformed, parse_contract, validate_contract
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    path = Path(args.path).resolve()
    try:
        contract = parse_contract(path)
    except ContractMalformed as exc:
        result = {"valid": False, "error": str(exc), "issues": [str(exc)]}
        write_result(result, args, human_fn=_render_validate)
        return 1

    project_root = _repo_root()
    issues = validate_contract(contract, project_root)

    result: dict[str, Any] = {
        "valid": not issues,
        "path": str(path),
        "phase": contract.phase,
        "task_slug": contract.task_slug,
        "domain_tags": contract.domain_tags,
        "issues": issues,
    }
    write_result(result, args, human_fn=_render_validate)
    return 0 if not issues else 1


def _render_show(result: dict[str, Any]) -> None:
    """Render contract display in human-readable format."""
    print_rich("\n  [bold]Contract[/bold]\n")
    print_rich(f"  Phase: {result['phase']}")
    print_rich(f"  Slug: {result['task_slug']}")
    print_rich(f"  Tags: {', '.join(result['domain_tags'])}")
    print_rich("\n  [bold]Scope[/bold]")
    print_rich(f"  Touches: {', '.join(result['scope']['touches'])}")
    print_rich(f"  Avoids: {', '.join(result['scope']['avoids'])}")
    if result.get("success_criteria"):
        print_rich("\n  [bold]Success Criteria[/bold]")
        for criterion in result["success_criteria"]:
            print_rich(f"  - {criterion}")
    if result.get("body"):
        print_rich(f"\n  [bold]Body[/bold]\n{result['body']}")
    print_rich()


def _show(args: argparse.Namespace) -> int:
    from agentalloy.contracts import ContractMalformed, parse_contract

    path = Path(args.path).resolve()
    try:
        contract = parse_contract(path)
    except ContractMalformed as exc:
        print(f"  [error] {exc}", file=sys.stderr)
        return 1

    result: dict[str, Any] = {
        "path": str(contract.path),
        "phase": contract.phase,
        "task_slug": contract.task_slug,
        "domain_tags": contract.domain_tags,
        "scope": {
            "touches": contract.scope.touches,
            "avoids": contract.scope.avoids,
        },
        "success_criteria": contract.success_criteria,
        "related_contracts": [str(p) for p in contract.related_contracts],
        "created_at": contract.created_at.isoformat() if contract.created_at else None,
        "body": contract.body,
    }

    write_result(result, args, human_fn=_render_show)
    return 0


def _render_init(result: dict[str, Any]) -> None:
    """Render contract init in human-readable format."""
    print_rich("\n  [bold]Contract Init[/bold]\n")
    print_rich(f"  Path: {result['path']}")
    print_rich(f"  Phase: {result['phase']}")
    print_rich(f"  Slug: {result['task_slug']}")
    print_rich("  [green]Created[/green]")
    scaffolded = result.get("scaffolded") or []
    if scaffolded:
        print_rich("\n  [bold]Scaffolded docs[/bold] (with required headings)")
        for path in scaffolded:
            print_rich(f"  [green]+[/green] {path}")
    print_rich()


def _init(args: argparse.Namespace) -> int:
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    project_root = _repo_root()

    # --phase defaults to the active phase in .agentalloy/phase when omitted, so only
    # --slug is required in the common case (the phase is already tracked).
    phase: str | None = args.phase
    if phase is None:
        from agentalloy.signals.skill_loader import (  # pyright: ignore[reportPrivateUsage]
            _read_phase,
        )

        phase = _read_phase(project_root)
        if phase is None:
            print(
                "  [error] No --phase given and no active phase in .agentalloy/phase. "
                "Pass --phase explicitly.",
                file=sys.stderr,
            )
            return 1

    slug: str = args.slug
    route: str = getattr(args, "route", "full")
    force: bool = getattr(args, "force", False)

    contracts_dir = project_root / ".agentalloy" / "contracts" / phase
    contracts_dir.mkdir(parents=True, exist_ok=True)
    target = contracts_dir / f"{slug}.md"

    if target.exists() and not force:
        print(
            f"  [error] Contract already exists: {target}. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    # Try to load contract_template from active workflow skill
    template = _load_contract_template(phase)
    if template is None:
        # Fallback minimal template
        template = (
            "---\n"
            "phase: {phase}\n"
            "task_slug: {task_slug}\n"
            "route: {route}\n"
            "domain_tags: []\n"
            "scope:\n"
            "  touches: []\n"
            "  avoids: []\n"
            "success_criteria: []\n"
            "related_contracts: []\n"
            "created_at: {created_at}\n"
            "---\n\n"
            "# {task_slug_title}\n\n"
            "## Task description\n\n"
            "<fill in what you intend to do and why>\n"
        )

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        template.replace("{{phase}}", phase)
        .replace("{{task_slug}}", slug)
        .replace("{{created_at}}", now)
        .replace("{{route}}", route)
        .replace("{phase}", phase)
        .replace("{task_slug}", slug)
        .replace("{created_at}", now)
        .replace("{route}", route)
        .replace("{task_slug_title}", slug.replace("-", " ").title())
    )

    target.write_text(content, encoding="utf-8")

    # Scaffold the phase's exit-gate doc files (e.g. design's approach/tasks/test-plan)
    # seeded with the exact `## Section` headings the gate requires, so the agent doesn't
    # have to read the skill YAML to discover them. Derived from the gate spec (single
    # source of truth); never overwrites an existing file.
    scaffolded = _scaffold_phase_docs(phase, slug, project_root)

    result = {
        "path": str(target),
        "phase": phase,
        "task_slug": slug,
        "scaffolded": scaffolded,
    }
    write_result(result, args, human_fn=_render_init)
    return 0


def _concretize_glob(path_glob: str, slug: str) -> str | None:
    """Resolve a gate path glob to a concrete repo-relative file path for *slug*.

    Substitutes, in order:
    - a literal ``<slug>`` placeholder with the slug;
    - a ``**`` directory segment with the slug
      (``docs/design/**/approach.md`` -> ``docs/design/<slug>/approach.md``);
    - a *terminal basename* wildcard — the final segment's leading ``*`` — with the slug
      (``docs/qa/*.md`` -> ``docs/qa/<slug>.md``), but only when it is the sole remaining
      wildcard and confined to that last segment. This names the per-feature artifact after
      the slug, which is what the qa/spec gates intend.

    Returns None when any wildcard still remains — an ambiguous/multi-match glob such as a
    non-terminal ``*`` must not be scaffolded to a single file.
    """
    concrete = path_glob.replace("<slug>", slug)
    segments = [slug if seg == "**" else seg for seg in concrete.split("/")]
    # Terminal basename wildcard: only when nothing before the last segment wildcards and
    # the last segment has a single leading ``*`` (e.g. ``*.md``). A non-terminal or
    # multi-wildcard glob falls through to the ``*`` guard below and stays unscaffolded.
    if segments and "*" not in "/".join(segments[:-1]):
        last = segments[-1]
        if last.startswith("*") and "*" not in last[1:]:
            segments[-1] = slug + last[1:]
    concrete = "/".join(segments)
    if "*" in concrete:
        return None
    return concrete


def _scaffold_phase_docs(phase: str, slug: str, project_root: Path) -> list[str]:
    """Create stub docs for each ``artifact_contains`` gate of *phase*, with headings.

    Returns the repo-relative paths actually created — skips files that already exist and
    globs that don't resolve to a single concrete path. Soft: any failure returns the
    paths created so far rather than raising (scaffolding is a convenience, not a gate).
    """
    created: list[str] = []
    try:
        from agentalloy.signals.prefilter import (  # pyright: ignore[reportPrivateUsage]
            _extract_artifact_contains_specs,
        )
        from agentalloy.signals.skill_loader import exit_gates_for_phase

        gates = exit_gates_for_phase(phase) or {}
        title = slug.replace("-", " ").title()
        for path_glob, sections in _extract_artifact_contains_specs(gates):
            concrete = _concretize_glob(path_glob, slug)
            if concrete is None:
                continue
            target = project_root / concrete
            if target.exists():
                continue
            lines = [f"# {title}", ""]
            for section in sections:
                lines += [f"## {section}", "", f"<{section.lower()} goes here>", ""]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            created.append(concrete)
    except Exception:
        pass
    return created


def _load_contract_template(phase: str) -> str | None:
    """Load the contract_template field from the workflow skill for this phase."""
    try:
        import duckdb

        from agentalloy.profiles import detect_profile, profile_datastore_path

        profile = detect_profile()
        ds_path = profile_datastore_path(profile.name)
        if not ds_path.exists():
            return _load_template_from_packs(phase)

        conn = duckdb.connect(str(ds_path), read_only=True)
        try:
            # Check if profile_skills table has a workflow skill for this phase
            rows = conn.execute(
                "SELECT applies_to_phases, raw_prose FROM profile_skills WHERE skill_class = 'workflow'"
            ).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()

        for row in rows:
            phases_raw, _raw_prose = row
            phases: list[Any] = phases_raw or []
            if phase in phases:
                # The Phase 1 profile_skills table doesn't persist
                # contract_template yet, so even when the profile datastore
                # has a workflow skill for this phase we still need the
                # shipped pack's template. Fall through to packs lookup.
                break

    except Exception:
        pass
    return _load_template_from_packs(phase)


def _load_template_from_packs(phase: str) -> str | None:
    """Load contract_template from _packs sdd-*.yaml for the given phase."""
    try:
        import yaml

        import agentalloy

        packs_root = Path(agentalloy.__file__).resolve().parent / "_packs"
        for yaml_file in packs_root.rglob("*.yaml"):
            if yaml_file.name == "pack.yaml":
                continue
            try:
                data: dict[str, Any] = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if data.get("skill_class") != "workflow":
                continue
            applies: list[Any] = data.get("applies_to_phases") or []
            if phase not in applies:
                continue
            template: Any = data.get("contract_template")
            if template:
                return str(template)
    except Exception:
        pass
    return None


_HANDLERS = {
    "validate": _validate,
    "show": _show,
    "init": _init,
}


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser("contract", help="Manage task contracts.")
    add_json_flag(p)
    sub = p.add_subparsers(dest="contract_cmd")

    # validate
    val_p = sub.add_parser("validate", help="Validate a contract file.")
    val_p.add_argument("path", help="Path to the contract markdown file.")

    # show
    show_p = sub.add_parser("show", help="Display a parsed contract.")
    show_p.add_argument("path", help="Path to the contract markdown file.")

    # init
    init_p = sub.add_parser(
        "init", help="Scaffold a contract from the active workflow skill's template."
    )
    init_p.add_argument(
        "--phase",
        default=None,
        help="Phase (e.g. build, spec, design). Defaults to the active phase in .agentalloy/phase.",
    )
    init_p.add_argument("--slug", required=True, help="Task slug (kebab-case identifier).")
    init_p.add_argument(
        "--route",
        choices=("full", "fast"),
        default="full",
        help="Workflow route chosen at intake: 'full' (spec→…→ship) or 'fast' (sdd-fast).",
    )
    init_p.add_argument("--force", action="store_true", help="Overwrite existing contract.")

    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    cmd = getattr(args, "contract_cmd", None)
    if not cmd:
        print("  Usage: agentalloy contract {validate,show,init}", file=sys.stderr)
        return 1
    handler = _HANDLERS.get(cmd)
    if not handler:
        print(f"  Unknown contract command: {cmd}", file=sys.stderr)
        return 1
    return handler(args)
