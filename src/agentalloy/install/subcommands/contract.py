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
    print_rich()


def _init(args: argparse.Namespace) -> int:
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    phase: str = args.phase
    slug: str = args.slug
    force: bool = getattr(args, "force", False)

    project_root = _repo_root()
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
        .replace("{phase}", phase)
        .replace("{task_slug}", slug)
        .replace("{created_at}", now)
        .replace("{task_slug_title}", slug.replace("-", " ").title())
    )

    target.write_text(content, encoding="utf-8")
    result = {"path": str(target), "phase": phase, "task_slug": slug}
    write_result(result, args, human_fn=_render_init)
    return 0


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
    init_p.add_argument("--phase", required=True, help="Phase (e.g. build, spec, design).")
    init_p.add_argument("--slug", required=True, help="Task slug (kebab-case identifier).")
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
