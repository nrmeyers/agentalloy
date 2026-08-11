# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
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
import contextlib
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
    """A client for a verified-running service, or exit 1.

    Contract writes have no in-process equivalent (the store exposes reads and
    ``supersede``, not the create/patch pair these verbs need), so this stays a
    service-only path.  What it shares with the phase surfaces is the *message*:
    it used to print its own wording, naming an ``agentalloy start`` command
    that does not exist.
    """
    from agentalloy.install.subcommands._state import SERVICE_DOWN_MESSAGE  # noqa: PLC0415

    client = StateClient()
    if not client.is_running():
        print(SERVICE_DOWN_MESSAGE, file=sys.stderr)
        sys.exit(1)
    return client


def _active_design_slug(project_root: Path) -> str | None:
    """The active design work-item slug, for stamping onto a new build contract.

    Resolves via: store cursor (scoped then shared) → first active design
    contract in store. Returns the slug when the contract_id points at a
    known active design contract, ``None`` otherwise.
    """
    from agentalloy.signals.skill_loader import (
        _state_view,
        cli_session_key,
    )

    session_key = cli_session_key()

    # Try cursor (scoped then shared) from store
    cursor_val: str | None = None
    try:
        view = _state_view(project_root)
        if view is not None:
            if session_key:
                with contextlib.suppress(Exception):
                    cursor_val = view.read("cursor", session_key=session_key)
            if cursor_val is None:
                with contextlib.suppress(Exception):
                    cursor_val = view.read("cursor", session_key=None)
    except Exception:
        pass

    if cursor_val is not None:
        # Cursor points to a path like "active/design/01-foo.md" or contract_id
        # Normalise to just the contract_id
        if "/" in cursor_val:
            parts = cursor_val.rsplit("/", 1)
            path_part = parts[0]  # e.g. "active/design"
            file_part = parts[1].replace(".md", "")  # e.g. "01-foo"
            # Validate it points to a design contract
            if path_part == "active/design":
                cursor_val = file_part
            else:
                # Cross-phase cursor → None
                return None
        # cursor_val is already a contract_id; validate it exists in store
        try:
            view = _state_view(project_root)
            if view is not None:
                rows = view.list_contracts(phase="design", slug=cursor_val, status="active")
                if rows:
                    return cursor_val
        except Exception:
            pass
        # Cursor points to non-existent contract → None
        return None

    # No cursor: only return the slug when there's exactly one active design contract in store
    try:
        view = _state_view(project_root)
        if view is not None:
            rows = view.list_contracts(phase="design", status="active")
            if len(rows) == 1:
                return rows[0]["contract_id"]
    except Exception:
        pass

    return None


def _inject_work_item(content: str, slug: str | None) -> str:
    """Insert a ``work_item: <slug>`` line into a contract's YAML frontmatter,
    right after ``task_slug:``. No-op when *slug* is None, the field is already
    present, or the content has no ``task_slug:`` frontmatter line to anchor to.
    """
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
        from agentalloy.signals.prefilter import (
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
                "SELECT applies_to_phases, raw_prose FROM profile_skills WHERE skill_class = 'workflow'",
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
            if isinstance(criterion, dict):
                print_rich(f"  - {criterion.get('id', '')}: {criterion.get('text', '')}")
            else:
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
    from agentalloy.install.state import _repo_root

    client = _get_client()
    project_root = _repo_root()

    # --phase defaults to the active phase, read through the shared store seam
    # (the service is already verified running by `_get_client` above).
    phase: str | None = args.phase
    if phase is None:
        from agentalloy.install.subcommands._state import (  # noqa: PLC0415
            fail_on_state_error,
            phase_access,
        )

        try:
            state = phase_access(project_root).read()
        except StateClientError as exc:
            fail_on_state_error(exc)
            raise  # unreachable
        phase = state.phase if state else None
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
            },
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


def _artifact_set(args: argparse.Namespace) -> int:
    """Upsert a deliverable artifact body (docs/spec/<slug>.md and friends,
    store-backed) via StateClient. This is the write path an agent uses in
    place of its file-write tool once phase gates no longer read the disk.
    """
    client = _get_client()
    body = args.body
    if args.body_file is not None:
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text()
    if body is None:
        print("Error: provide --body or --body-file.", file=sys.stderr)
        return 1

    try:
        result_data = client.set_artifact(args.phase, args.slug, args.name, body)
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    result = {
        "phase": result_data.get("phase", args.phase),
        "slug": result_data.get("slug", args.slug),
        "name": result_data.get("name", args.name),
        "updated_at": result_data.get("updated_at"),
    }
    write_result(
        result,
        args,
        human_fn=lambda r: print_rich(f"[green]Stored[/] {r['phase']}/{r['slug']}/{r['name']}"),
    )
    return 0


def _artifact_list(args: argparse.Namespace) -> int:
    """List deliverable artifacts for a phase via StateClient."""
    client = _get_client()
    try:
        rows = client.list_artifacts(args.phase, slug=args.slug, name_glob=args.name_glob)
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    result = {"artifacts": rows}
    write_result(
        result,
        args,
        human_fn=lambda r: print_rich(
            "\n".join(f"{a['phase']}/{a['slug']}/{a['name']}" for a in r["artifacts"]) or "(none)",
        ),
    )
    return 0


def _artifact_show(args: argparse.Namespace) -> int:
    """Show the content of a stored deliverable artifact.

    Prints raw artifact body to stdout by default (no framing) so agents
    can consume it as context.  Use ``--json`` for structured output.

    Accepts either a positional ``phase/slug/name`` triple or individual
    ``--phase``, ``--slug``, ``--name`` flags.
    """
    client = _get_client()

    # Resolve phase/slug/name from triple or flags
    phase, slug, name = None, None, None
    if args.triple is not None:
        parts = args.triple.split("/")
        if len(parts) != 3:
            print(
                f"Error: triple must be phase/slug/name (got '{args.triple}')",
                file=sys.stderr,
            )
            return 1
        phase, slug, name = parts
    else:
        phase, slug, name = args.phase, args.slug, args.name

    if not all([phase, slug, name]):
        print(
            "Error: provide either 'phase/slug/name' or --phase + --slug + --name.",
            file=sys.stderr,
        )
        return 1

    try:
        result_data = client.get_artifact(phase, slug, name)
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    if result_data is None:
        print(f"Error: artifact {phase}/{slug}/{name} not found", file=sys.stderr)
        return 1

    if args.json:
        write_result(result_data, args, human_fn=lambda r: print(r["content"] or ""))
    else:
        # Non-JSON: print raw body (no header, no framing) for agent consumption
        print(result_data.get("content") or "", end="")
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
    "artifact-set": _artifact_set,
    "artifact-show": _artifact_show,
    "artifact-list": _artifact_list,
}


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
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
        "--domain-tags",
        default=None,
        action="append",
        help="Domain tags (can repeat).",
    )
    edit_p.add_argument(
        "--scope-touches",
        default=None,
        action="append",
        help="Scope touches (can repeat).",
    )
    edit_p.add_argument(
        "--scope-avoids",
        default=None,
        action="append",
        help="Scope avoids (can repeat).",
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
        "--domain-tags",
        default=None,
        action="append",
        help="Domain tags (can repeat).",
    )
    sup_p.add_argument(
        "--scope-touches",
        default=None,
        action="append",
        help="Scope touches (can repeat).",
    )
    sup_p.add_argument(
        "--scope-avoids",
        default=None,
        action="append",
        help="Scope avoids (can repeat).",
    )
    sup_p.add_argument(
        "--success-criteria",
        default=None,
        action="append",
        help="Success criteria (can repeat).",
    )
    add_json_flag(sup_p)

    # artifact-set
    aset_p = sub.add_parser(
        "artifact-set",
        help="Store a deliverable artifact body (spec.md, approach.md, ...).",
    )
    aset_p.add_argument("--phase", required=True, help="Phase (spec, design, ...).")
    aset_p.add_argument("--slug", required=True, help="Task slug.")
    aset_p.add_argument("--name", required=True, help="Artifact name, e.g. 'spec.md'.")
    aset_p.add_argument("--body", default=None, help="Artifact content.")
    aset_p.add_argument("--body-file", default=None, help="Read content from file ('-' for stdin).")
    add_json_flag(aset_p)

    # artifact-list
    alist_p = sub.add_parser("artifact-list", help="List deliverable artifacts for a phase.")
    alist_p.add_argument("--phase", required=True, help="Phase to list.")
    alist_p.add_argument("--slug", default=None, help="Filter by slug.")
    alist_p.add_argument("--name-glob", default=None, help="fnmatch pattern over name.")
    add_json_flag(alist_p)

    # artifact-show
    ashow_p = sub.add_parser(
        "artifact-show",
        help="Show the content of a stored deliverable artifact.",
    )
    ashow_p.add_argument(
        "triple",
        nargs="?",
        default=None,
        help="Phase/slug/name triple, e.g. 'spec/add-telemetry/spec.md'.",
    )
    ashow_p.add_argument("--phase", default=None, help="Phase (spec, design, ...).")
    ashow_p.add_argument("--slug", default=None, help="Task slug.")
    ashow_p.add_argument("--name", default=None, help="Artifact name, e.g. 'spec.md'.")
    add_json_flag(ashow_p)

    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    cmd = getattr(args, "contract_cmd", None)
    if not cmd:
        print(
            "  Usage: agentalloy contract "
            "{init,show,validate,edit,supersede,artifact-set,artifact-list}",
            file=sys.stderr,
        )
        return 1
    handler = _HANDLERS.get(cmd)
    if not handler:
        print(f"  Unknown contract command: {cmd}", file=sys.stderr)
        return 1
    return handler(args)
