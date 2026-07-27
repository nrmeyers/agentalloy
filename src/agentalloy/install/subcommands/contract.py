"""``agentalloy contract`` — contract management subcommand.

All contract operations go through StateClient over HTTP.  Service down
means a non-zero exit naming the service — never a silent local write.

Commands:
    agentalloy contract init --phase <name> --slug <slug>
    agentalloy contract show <contract_id>
    agentalloy contract validate <contract_id>
    agentalloy contract edit <contract_id> [--body ...] [--domain-tags ...]
    agentalloy contract supersede <contract_id> --new-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.install.output import add_json_flag, print_rich, write_result

# ---------------------------------------------------------------------------
# Helpers shared across subcommands
# ---------------------------------------------------------------------------


def _get_client() -> StateClient:
    """Return a StateClient and verify the service is running."""
    client = StateClient()
    if not client.is_running():
        print(
            "Error: agentalloy service is not running. "
            "Start the service or run `agentalloy start`.",
            file=sys.stderr,
        )
        sys.exit(1)
    return client


def _active_design_slug(project_root: Path) -> str | None:
    """The active design work-item slug, for stamping onto a new build contract.

    Reads the design cursor via the canonical resolver but accepts it only when it
    resolves into ``contracts/design/`` (phase-strict). ``None`` when no single
    design work-item resolves.
    """
    from agentalloy.contracts import active_dir, resolve_current_contract
    from agentalloy.signals.skill_loader import (
        cli_session_key,  # pyright: ignore[reportPrivateUsage]
    )

    _cid, path = resolve_current_contract(project_root, "design", cli_session_key())
    if path is None:
        return None
    design_dir = active_dir(project_root, "design").resolve()
    if not path.resolve().is_relative_to(design_dir):
        return None
    return path.stem


def _inject_work_item(content: str, slug: str | None) -> str:
    """Insert a ``work_item: <slug>`` line into a contract's YAML frontmatter,
    right after ``task_slug:``. No-op when *slug* is None, the field is already
    present, or the content has no ``task_slug:`` frontmatter line to anchor to."""
    if slug is None or "\nwork_item:" in content or content.startswith("work_item:"):
        return content
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("task_slug:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i + 1, f"{indent}work_item: {slug}\n")
            return "".join(lines)
    return content


def _concretize_glob(path_glob: str, slug: str) -> str | None:
    """Resolve a gate path glob to a concrete repo-relative file path for *slug*."""
    concrete = path_glob.replace("<slug>", slug)
    segments = [slug if seg == "**" else seg for seg in concrete.split("/")]
    if segments and "*" not in "/".join(segments[:-1]):
        last = segments[-1]
        if last.startswith("*") and "*" not in last[1:]:
            segments[-1] = slug + last[1:]
    concrete = "/".join(segments)
    if "*" in concrete:
        return None
    return concrete


def _scaffold_phase_docs(phase: str, slug: str, project_root: Path) -> list[str]:
    """Create stub docs for each ``artifact_contains`` gate of *phase*."""
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
            conn.execute(
                "SELECT applies_to_phases, raw_prose FROM profile_skills WHERE skill_class = 'workflow'"
            ).fetchall()
        except Exception:
            pass
        finally:
            conn.close()
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


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_validate(result: dict[str, Any]) -> None:
    """Render contract validation in human-readable format."""
    print_rich("\n  [bold]Contract Validation[/bold]\n")
    print_rich(f"  ID: {result.get('contract_id', 'N/A')}")
    print_rich(f"  Phase: {result.get('phase', 'N/A')}")
    print_rich(f"  Slug: {result.get('slug', 'N/A')}")
    if result.get("valid"):
        print_rich("  [green]Valid[/green]")
    else:
        issues = result.get("issues", [])
        print_rich(f"  [red]Issues: {len(issues)}[/red]")
        for issue in issues:
            print_rich(f"  [red]x[/red] {issue}")
    print_rich()


def _render_show(result: dict[str, Any]) -> None:
    """Render contract display in human-readable format."""
    print_rich("\n  [bold]Contract[/bold]\n")
    print_rich(f"  ID: {result.get('contract_id')}")
    print_rich(f"  Phase: {result.get('phase')}")
    print_rich(f"  Slug: {result.get('slug')}")
    tags = result.get("domain_tags") or []
    print_rich(f"  Tags: {', '.join(tags)}")
    touches = result.get("scope_touches") or []
    avoids = result.get("scope_avoids") or []
    if touches or avoids:
        print_rich("\n  [bold]Scope[/bold]")
        print_rich(f"  Touches: {', '.join(touches)}")
        print_rich(f"  Avoids: {', '.join(avoids)}")
    criteria = result.get("success_criteria") or []
    if criteria:
        print_rich("\n  [bold]Success Criteria[/bold]")
        for criterion in criteria:
            print_rich(f"  - {criterion}")
    body = result.get("body")
    if body:
        print_rich(f"\n  [bold]Body[/bold]\n{body}")
    print_rich()


def _render_init(result: dict[str, Any]) -> None:
    """Render contract init in human-readable format."""
    print_rich("\n  [bold]Contract Init[/bold]\n")
    print_rich(f"  ID: {result['contract_id']}")
    print_rich(f"  Phase: {result['phase']}")
    print_rich(f"  Slug: {result['slug']}")
    print_rich("  [green]Created[/green]")
    scaffolded = result.get("scaffolded") or []
    if scaffolded:
        print_rich("\n  [bold]Scaffolded docs[/bold] (with required headings)")
        for path in scaffolded:
            print_rich(f"  [green]+[/green] {path}")
    print_rich()


def _render_edit(result: dict[str, Any]) -> None:
    """Render contract edit result."""
    print_rich("\n  [bold]Contract Edit[/bold]\n")
    print_rich(f"  ID: {result['contract_id']}")
    print_rich("  [green]Updated[/green]")
    print_rich()


def _render_supersede(result: dict[str, Any]) -> None:
    """Render contract supersede result."""
    print_rich("\n  [bold]Contract Supersede[/bold]\n")
    print_rich(f"  Old ID: {result['supersedes']}")
    print_rich(f"  New ID: {result['contract_id']}")
    print_rich("  [green]Superseded[/green]")
    print_rich()


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _init(args: argparse.Namespace) -> int:
    """Scaffold a contract and store it via StateClient."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    client = _get_client()
    project_root = _repo_root()

    # --phase defaults to the active phase
    phase: str | None = args.phase
    if phase is None:
        from agentalloy.api.state_client import StateClient

        sc = StateClient()
        if sc.is_running():
            phase_data = sc.get_state("phase")
            if phase_data:
                # Parse JSON response or raw string
                if isinstance(phase_data, str):
                    try:
                        phase_data = json.loads(phase_data)
                    except json.JSONDecodeError:
                        phase_data = {"value": phase_data.strip()}
                phase = (
                    phase_data.get("value", phase_data.get("phase"))
                    if isinstance(phase_data, dict)
                    else phase_data
                )
        if not phase:
            print(
                "  [error] No --phase given and no active phase. Pass --phase explicitly.",
                file=sys.stderr,
            )
            return 1

    slug: str = args.slug
    route: str = getattr(args, "route", "full")

    # Build contract content locally (template + work-item stamping)
    template = _load_contract_template(phase)
    if template is None:
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
            "created_at: {created_at}\n"
            "---\n\n"
            "# {task_slug_title}\n\n"
            "## Task description\n\n"
            "<fill in what you intend to do and why>\n"
        )

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = slug.replace("-", " ").title()
    content = (
        template.replace("{{phase}}", phase)
        .replace("{{task_slug}}", slug)
        .replace("{{created_at}}", now)
        .replace("{{route}}", route)
        .replace("{{task_slug_title}}", title)
        .replace("{phase}", phase)
        .replace("{task_slug}", slug)
        .replace("{created_at}", now)
        .replace("{route}", route)
        .replace("{task_slug_title}", title)
    )

    # Stamp work_item for build contracts
    if phase == "build":
        content = _inject_work_item(content, _active_design_slug(project_root))

    # Scaffold phase docs (local file operation, not state mutation)
    scaffolded = _scaffold_phase_docs(phase, slug, project_root)

    # Generate contract_id from phase + slug
    contract_id = f"{phase}/{slug}"

    # Store via StateClient
    try:
        result_data = client.create_contract(
            {
                "contract_id": contract_id,
                "phase": phase,
                "slug": slug,
                "route": route,
                "body": content,
            }
        )
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    # Parse the response — may be the full contract or a simple dict
    response_id = result_data.get("contract_id", contract_id)

    result = {
        "contract_id": response_id,
        "phase": phase,
        "slug": slug,
        "scaffolded": scaffolded,
    }
    write_result(result, args, human_fn=_render_init)
    return 0


def _show(args: argparse.Namespace) -> int:
    """Show a contract by ID via StateClient."""
    client = _get_client()
    contract_id = args.contract_id

    try:
        contract = client.get_contract(contract_id)
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    if contract is None:
        print(f"Error: Contract {contract_id!r} not found.", file=sys.stderr)
        return 1

    result = {
        "contract_id": contract.get("contract_id"),
        "phase": contract.get("phase"),
        "slug": contract.get("slug"),
        "domain_tags": contract.get("domain_tags"),
        "scope_touches": contract.get("scope_touches"),
        "scope_avoids": contract.get("scope_avoids"),
        "success_criteria": contract.get("success_criteria"),
        "body": contract.get("body"),
        "status": contract.get("status"),
    }
    write_result(result, args, human_fn=_render_show)
    return 0


def _validate(args: argparse.Namespace) -> int:
    """Validate a contract by ID via StateClient."""
    client = _get_client()
    contract_id = args.contract_id

    try:
        contract = client.get_contract(contract_id)
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    if contract is None:
        print(f"Error: Contract {contract_id!r} not found.", file=sys.stderr)
        return 1

    from agentalloy.contracts import validate_contract_from_dict

    issues = validate_contract_from_dict(contract)

    result: dict[str, Any] = {
        "valid": not issues,
        "contract_id": contract.get("contract_id"),
        "phase": contract.get("phase"),
        "slug": contract.get("slug"),
        "domain_tags": contract.get("domain_tags"),
        "issues": issues,
    }
    write_result(result, args, human_fn=_render_validate)
    return 0 if not issues else 1


def _edit(args: argparse.Namespace) -> int:
    """In-place correction of a contract via StateClient."""
    client = _get_client()
    contract_id = args.contract_id

    updates: dict[str, Any] = {}
    if args.body is not None:
        updates["body"] = args.body
    if args.domain_tags is not None:
        updates["domain_tags"] = args.domain_tags
    if args.scope_touches is not None:
        updates["scope_touches"] = args.scope_touches
    if args.scope_avoids is not None:
        updates["scope_avoids"] = args.scope_avoids
    if args.success_criteria is not None:
        updates["success_criteria"] = args.success_criteria

    if not updates:
        print("Error: Provide at least one field to update.", file=sys.stderr)
        return 1

    try:
        result_data = client.patch_contract(contract_id, updates)
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    result = {
        "contract_id": result_data.get("contract_id", contract_id),
    }
    write_result(result, args, human_fn=_render_edit)
    return 0


def _supersede(args: argparse.Namespace) -> int:
    """Supersede a contract with a new revision via StateClient."""
    client = _get_client()
    contract_id = args.contract_id
    new_id = args.new_id

    payload: dict[str, Any] = {
        "new_contract_id": new_id,
    }
    if args.phase is not None:
        payload["phase"] = args.phase
    if args.slug is not None:
        payload["slug"] = args.slug
    if args.body is not None:
        payload["body"] = args.body
    if args.domain_tags is not None:
        payload["domain_tags"] = args.domain_tags
    if args.scope_touches is not None:
        payload["scope_touches"] = args.scope_touches
    if args.scope_avoids is not None:
        payload["scope_avoids"] = args.scope_avoids
    if args.success_criteria is not None:
        payload["success_criteria"] = args.success_criteria

    try:
        result_data = client.supersede_contract(contract_id, payload)
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    result = {
        "contract_id": result_data.get("contract_id", new_id),
        "supersedes": result_data.get("supersedes", contract_id),
    }
    write_result(result, args, human_fn=_render_supersede)
    return 0


# ---------------------------------------------------------------------------
# Parser setup
# ---------------------------------------------------------------------------


_HANDLERS = {
    "init": _init,
    "show": _show,
    "validate": _validate,
    "edit": _edit,
    "supersede": _supersede,
}


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser("contract", help="Manage task contracts.")
    add_json_flag(p)
    sub = p.add_subparsers(dest="contract_cmd")

    # init
    init_p = sub.add_parser("init", help="Scaffold a contract and store it in the state service.")
    init_p.add_argument(
        "--phase",
        default=None,
        help="Phase (e.g. build, spec, design). Defaults to the active phase.",
    )
    init_p.add_argument("--slug", required=True, help="Task slug (kebab-case identifier).")
    init_p.add_argument(
        "--route",
        choices=("full", "sdd-fast", "add-skill"),
        default="full",
        help="Workflow route.",
    )
    add_json_flag(init_p)

    # show
    show_p = sub.add_parser("show", help="Display a contract by ID.")
    show_p.add_argument("contract_id", help="Contract ID.")
    add_json_flag(show_p)

    # validate
    val_p = sub.add_parser("validate", help="Validate a contract by ID.")
    val_p.add_argument("contract_id", help="Contract ID.")
    add_json_flag(val_p)

    # edit
    edit_p = sub.add_parser("edit", help="In-place correction of a contract.")
    edit_p.add_argument("contract_id", help="Contract ID to edit.")
    edit_p.add_argument("--body", default=None, help="New body text.")
    edit_p.add_argument(
        "--domain-tags", default=None, action="append", help="Domain tags (can repeat)."
    )
    edit_p.add_argument(
        "--scope-touches", default=None, action="append", help="Scope touches (can repeat)."
    )
    edit_p.add_argument(
        "--scope-avoids", default=None, action="append", help="Scope avoids (can repeat)."
    )
    edit_p.add_argument(
        "--success-criteria",
        default=None,
        action="append",
        help="Success criteria (can repeat).",
    )
    add_json_flag(edit_p)

    # supersede
    sup_p = sub.add_parser("supersede", help="Supersede a contract with a new revision.")
    sup_p.add_argument("contract_id", help="Contract ID to supersede.")
    sup_p.add_argument("--new-id", required=True, help="New contract ID.")
    sup_p.add_argument("--phase", default=None, help="Phase.")
    sup_p.add_argument("--slug", default=None, help="Slug.")
    sup_p.add_argument("--body", default=None, help="New body text.")
    sup_p.add_argument(
        "--domain-tags", default=None, action="append", help="Domain tags (can repeat)."
    )
    sup_p.add_argument(
        "--scope-touches", default=None, action="append", help="Scope touches (can repeat)."
    )
    sup_p.add_argument(
        "--scope-avoids", default=None, action="append", help="Scope avoids (can repeat)."
    )
    sup_p.add_argument(
        "--success-criteria",
        default=None,
        action="append",
        help="Success criteria (can repeat).",
    )
    add_json_flag(sup_p)

    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    cmd = getattr(args, "contract_cmd", None)
    if not cmd:
        print("  Usage: agentalloy contract {init,show,validate,edit,supersede}", file=sys.stderr)
        return 1
    handler = _HANDLERS.get(cmd)
    if not handler:
        print(f"  Unknown contract command: {cmd}", file=sys.stderr)
        return 1
    return handler(args)
