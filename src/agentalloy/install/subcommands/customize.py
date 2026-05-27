"""``agentalloy customize`` — user-facing skill override CLI.

Three-layer resolution (highest to lowest priority):
  1. Project:  <project>/.agentalloy/skills/{system,workflow}/<name>.yaml
  2. Profile:  ~/.agentalloy/profiles/<profile>/skills/{system,workflow}/<name>.yaml
  3. Default:  src/agentalloy/_packs/**/<name>.yaml  (shipped in package)

Commands:
    agentalloy customize list [--profile X]
    agentalloy customize edit <name> [--profile X | --project]
    agentalloy customize validate <name> [--profile X | --project]
    agentalloy customize update <name> [--profile X | --project]
    agentalloy customize update --all [--profile X]
    agentalloy customize diff <name>
    agentalloy customize reset <name> [--profile X | --project]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from agentalloy.install.output import add_json_flag, print_rich, write_result

if TYPE_CHECKING:
    from agentalloy.profiles import Profile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CUSTOMIZABLE_CLASSES = frozenset({"system", "workflow"})

# Profile skills are stored in a lightweight table in the profile DuckDB.
_PROFILE_SKILLS_DDL = """
CREATE TABLE IF NOT EXISTS profile_skills (
    skill_id       VARCHAR PRIMARY KEY,
    skill_class    VARCHAR NOT NULL,
    canonical_name VARCHAR NOT NULL,
    raw_prose      VARCHAR NOT NULL DEFAULT '',
    domain_tags    VARCHAR[] DEFAULT [],
    applies_to_phases VARCHAR[] DEFAULT [],
    applies_when   VARCHAR DEFAULT NULL,
    exit_gates     VARCHAR DEFAULT NULL,
    updated_at     BIGINT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Layer resolution helpers
# ---------------------------------------------------------------------------


def _packs_root() -> Path:
    import agentalloy

    return Path(agentalloy.__file__).resolve().parent / "_packs"


def _find_default_skill(name: str) -> Path | None:
    """Find the shipped default for a skill by name (stem match across all packs)."""
    root = _packs_root()
    for f in root.rglob(f"{name}.yaml"):
        if f.name != "pack.yaml":
            return f
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data


def _project_skills_dir(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ".agentalloy" / "skills"


def _resolve_skill_layers(
    name: str,
    profile_name: str | None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Return paths at each layer for a given skill name.

    Returns dict with keys 'project', 'profile', 'default' — values are
    Path or None if the layer has no file for that skill.
    """
    from agentalloy.profiles import detect_profile, get_profile

    # Resolve active profile
    if profile_name:
        try:
            profile = get_profile(profile_name)
        except KeyError:
            profile = detect_profile(cwd)
    else:
        profile = detect_profile(cwd)

    # Try each skill class subdirectory
    project_dir = _project_skills_dir(cwd)
    profile_skills = profile.skills_dir
    default_path = _find_default_skill(name)

    default_class = None
    if default_path:
        try:
            data = _load_yaml(default_path)
            default_class = data.get("skill_class")
        except Exception:
            pass

    # If we know the class from the default, use it for the override dirs.
    # Otherwise try both.
    classes = [default_class] if default_class else ["system", "workflow"]

    project_path: Path | None = None
    profile_path: Path | None = None

    for cls in classes:
        p = project_dir / cls / f"{name}.yaml"
        if p.exists():
            project_path = p
            break

    for cls in classes:
        p = profile_skills / cls / f"{name}.yaml"
        if p.exists():
            profile_path = p
            break

    return {
        "project": project_path,
        "profile": profile_path,
        "default": default_path,
        "active_profile_name": profile.name,
        "active_profile": profile,
        "skill_class": default_class,
    }


def _active_layer(layers: dict[str, Any]) -> tuple[str, Path | None]:
    """Return (layer_name, path) for the highest-priority non-None layer."""
    for layer in ("project", "profile"):
        if layers.get(layer):
            return layer, layers[layer]
    return "default", layers.get("default")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_skill_data(data: dict[str, Any], name: str) -> list[str]:
    """Validate a skill YAML dict. Returns a list of error strings (empty = ok)."""
    errors: list[str] = []

    skill_class = data.get("skill_class")
    if skill_class not in CUSTOMIZABLE_CLASSES:
        if skill_class == "domain":
            errors.append(
                f"customize is for system+workflow skills only. '{name}' is a domain skill "
                "(centrally curated). See docs/skill-authoring-and-overrides-spec.md."
            )
        else:
            errors.append(f"skill_class must be 'system' or 'workflow', got: {skill_class!r}")
        return errors  # No point checking further

    raw_prose = data.get("raw_prose", "")
    if len(raw_prose) < 80:
        errors.append(
            f"raw_prose must be at least 80 characters (got {len(raw_prose)}). Avoid empty stubs."
        )

    if skill_class == "workflow":
        if not data.get("applies_to_phases"):
            errors.append("workflow skill must have 'applies_to_phases' (non-empty list)")
        if not data.get("exit_gates"):
            errors.append("workflow skill must have 'exit_gates' (non-empty object)")
        if not data.get("contract_template"):
            errors.append("workflow skill must have 'contract_template' (non-empty string)")

    if skill_class == "system" and not data.get("applies_when"):
        errors.append("system skill must have 'applies_when' (non-empty object)")

    return errors


# ---------------------------------------------------------------------------
# Profile datastore helpers
# ---------------------------------------------------------------------------


def _open_profile_store(profile_name: str) -> Any:
    """Open the profile DuckDB and ensure the profile_skills table exists."""
    import duckdb

    from agentalloy.profiles import profile_datastore_path

    path = profile_datastore_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute(_PROFILE_SKILLS_DDL)
    return conn


def _ingest_skill(profile_name: str, data: dict[str, Any]) -> None:
    """Upsert a skill into the profile's DuckDB."""
    conn = _open_profile_store(profile_name)
    try:
        skill_id: str = str(data.get("skill_id") or data.get("canonical_name", "unknown"))
        canonical_name: str = str(data.get("canonical_name") or skill_id)
        skill_class: str = str(data.get("skill_class", ""))
        raw_prose: str = str(data.get("raw_prose", ""))
        domain_tags: list[Any] = data.get("domain_tags") or []
        applies_to_phases: list[Any] = data.get("applies_to_phases") or []
        applies_when = json.dumps(data["applies_when"]) if data.get("applies_when") else None
        exit_gates = json.dumps(data["exit_gates"]) if data.get("exit_gates") else None
        updated_at = int(time.time() * 1000)

        conn.execute(
            """
            INSERT OR REPLACE INTO profile_skills
              (skill_id, skill_class, canonical_name, raw_prose,
               domain_tags, applies_to_phases, applies_when, exit_gates, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                skill_id,
                skill_class,
                canonical_name,
                raw_prose,
                domain_tags,
                applies_to_phases,
                applies_when,
                exit_gates,
                updated_at,
            ],
        )
    finally:
        conn.close()


def _delete_from_store(profile_name: str, skill_id: str) -> None:
    """Remove a skill from the profile's DuckDB."""
    conn = _open_profile_store(profile_name)
    try:
        conn.execute("DELETE FROM profile_skills WHERE skill_id = ?", [skill_id])
    finally:
        conn.close()


def _skill_in_store(profile_name: str, skill_id: str) -> bool:
    conn = _open_profile_store(profile_name)
    try:
        row = conn.execute(
            "SELECT 1 FROM profile_skills WHERE skill_id = ? LIMIT 1", [skill_id]
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _list_skills(args: argparse.Namespace) -> int:
    from agentalloy.profiles import detect_profile, get_profile

    profile_name = getattr(args, "profile", None)
    if profile_name:
        try:
            profile = get_profile(profile_name)
        except KeyError as exc:
            print(f"  [error] {exc}", file=sys.stderr)
            return 1
    else:
        profile = detect_profile()

    # Scan all default skills
    root = _packs_root()
    rows: list[dict[str, Any]] = []
    for yaml_file in sorted(root.rglob("*.yaml")):
        if yaml_file.name == "pack.yaml":
            continue
        try:
            data = _load_yaml(yaml_file)
        except Exception:
            continue
        skill_class = data.get("skill_class", "")
        if skill_class not in CUSTOMIZABLE_CLASSES:
            continue

        name = yaml_file.stem
        skill_id = data.get("skill_id") or name

        # Check which layers exist
        project_path = None
        for cls in (skill_class, "system", "workflow"):
            p = _project_skills_dir() / cls / f"{name}.yaml"
            if p.exists():
                project_path = p
                break

        profile_override = profile.skills_dir / skill_class / f"{name}.yaml"
        if not profile_override.exists():
            profile_override = None  # type: ignore[assignment]

        layer = "project" if project_path else ("profile" if profile_override else "default")

        rows.append(
            {
                "name": name,
                "skill_id": skill_id,
                "skill_class": skill_class,
                "layer": layer,
                "project_path": str(project_path) if project_path else None,
                "profile_path": str(profile_override) if profile_override else None,
                "default_path": str(yaml_file),
            }
        )

    write_result(rows, args, human_fn=_render_skill_list)  # type: ignore[arg-type]
    return 0


def _render_skill_list(result: list[dict[str, Any]]) -> None:
    """Render skill list in human-readable format."""
    print_rich("\n  [bold]Customize List[/bold]\n")
    for r in result:
        print_rich(f"  {r['name']} ({r['skill_class']}) — {r['layer']}")
    print_rich()


def _edit_skill(args: argparse.Namespace) -> int:
    name: str = args.name
    use_project = getattr(args, "project", False)
    profile_name = getattr(args, "profile", None)

    layers = _resolve_skill_layers(name, profile_name)
    profile: Profile = cast("Profile", layers["active_profile"])
    skill_class: str | None = layers["skill_class"]

    if not skill_class:
        print(f"  [error] Skill '{name}' not found in default packs.", file=sys.stderr)
        return 1
    if skill_class not in CUSTOMIZABLE_CLASSES:
        print(
            f"  [error] customize is for system+workflow skills only. '{name}' is a domain skill "
            "(centrally curated). See docs/skill-authoring-and-overrides-spec.md.",
            file=sys.stderr,
        )
        return 1

    # Determine target override path
    if use_project:
        target = _project_skills_dir() / skill_class / f"{name}.yaml"
    else:
        target = profile.skills_dir / skill_class / f"{name}.yaml"

    # Copy from next-higher layer if override doesn't exist yet
    if not target.exists():
        source = layers["profile"] or layers["default"]
        if source:
            target.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(str(source), str(target))

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        subprocess.run([editor, str(target)], check=True)
    except FileNotFoundError:
        print(f"  [error] Editor '{editor}' not found. Set $EDITOR.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"  [error] Editor exited with code {exc.returncode}.", file=sys.stderr)
        return 1

    print(f"  Saved override: {target}")
    return 0


def _render_validate(result: dict[str, Any]) -> None:
    """Render validation result in human-readable format."""
    print_rich("\n  [bold]Validation[/bold]\n")
    if result["status"] == "failed":
        print_rich("  Status: [red]Failed[/red]")
        for err in result["errors"]:
            print_rich(f"  [red]x[/red] {err}")
    else:
        print_rich("  Status: [green]Passed[/green]")
        print_rich(f"  Skill: {result['skill_id']}")
        print_rich(f"  Layer: {result['layer']}")
    print_rich()


def _render_update(result: dict[str, Any]) -> None:
    """Render update result in human-readable format."""
    print_rich("\n  [bold]Update Skill[/bold]\n")
    print_rich(f"  Status: [green]{result['status']}[/green]")
    print_rich(f"  Skill: {result['skill_id']}")
    if "profile" in result:
        print_rich(f"  Profile: {result['profile']}")
    print_rich(f"  Layer: {result['layer']}")
    print_rich()


def _render_update_all(result: dict[str, Any]) -> None:
    """Render update-all result in human-readable format."""
    print_rich("\n  [bold]Update All Skills[/bold]\n")
    print_rich(f"  Profile: {result['profile']}")
    if result["ingested_count"]:
        print_rich(f"  Ingested: {result['ingested_count']}")
    if result["error_count"]:
        print_rich(f"  Errors: {result['error_count']}")
        for err in result["errors"]:
            print_rich(f"    [red]x[/red] {err}")
    print_rich()


def _render_reset(result: dict[str, Any]) -> None:
    """Render reset result in human-readable format."""
    print_rich("\n  [bold]Reset Skill[/bold]\n")
    print_rich(f"  Skill: {result['skill_id']}")
    if "profile" in result:
        print_rich(f"  Profile: {result['profile']}")
    else:
        print_rich("  Project-level")
    print_rich()


def _validate_skill(args: argparse.Namespace) -> int:
    name: str = args.name
    profile_name = getattr(args, "profile", None)

    layers = _resolve_skill_layers(name, profile_name)
    layer_name, path = _active_layer(layers)

    if not path or not path.exists():
        print(f"  [error] No override found for '{name}' (layer: {layer_name}).", file=sys.stderr)
        return 1

    try:
        data = _load_yaml(path)
    except yaml.YAMLError as exc:
        print(f"  [error] YAML parse error in {path}: {exc}", file=sys.stderr)
        return 1

    # Block domain-class skills
    skill_class = data.get("skill_class")
    if skill_class == "domain":
        print(
            f"  [error] customize is for system+workflow skills only. '{name}' is a domain skill "
            "(centrally curated). See docs/skill-authoring-and-overrides-spec.md.",
            file=sys.stderr,
        )
        return 1

    errors = _validate_skill_data(data, name)
    result = {
        "status": "failed" if errors else "passed",
        "skill_id": data.get("skill_id") or name,
        "layer": layer_name,
        "errors": errors,
    }
    write_result(result, args, human_fn=_render_validate)
    return 1 if errors else 0


def _update_skill(args: argparse.Namespace) -> int:
    update_all = getattr(args, "all", False)
    profile_name = getattr(args, "profile", None)
    use_project = getattr(args, "project", False)

    if update_all:
        return _update_all(profile_name, args)

    name: str = args.name
    layers = _resolve_skill_layers(name, profile_name)
    profile: Profile = cast("Profile", layers["active_profile"])

    # Validate first
    layer_name, path = _active_layer(layers)
    if not path or not path.exists():
        print(f"  [error] No override for '{name}' (checked all layers).", file=sys.stderr)
        return 1

    try:
        data = _load_yaml(path)
    except yaml.YAMLError as exc:
        print(f"  [error] YAML parse error: {exc}", file=sys.stderr)
        return 1

    skill_class = data.get("skill_class")
    if skill_class == "domain":
        print(
            "  [error] customize is for system+workflow skills only.",
            file=sys.stderr,
        )
        return 1

    errors = _validate_skill_data(data, name)
    if errors:
        for err in errors:
            print(f"  [error] {err}", file=sys.stderr)
        return 1

    # Check if content is identical to inherited default (revert-to-inherited case)
    default_path = layers["default"]
    if default_path and default_path.exists() and layer_name != "default":
        try:
            default_data = _load_yaml(default_path)
            if data.get("raw_prose") == default_data.get("raw_prose"):
                # Override is identical to default — delete it
                path.unlink()
                skill_id = data.get("skill_id") or name
                if not use_project:
                    _delete_from_store(profile.name, skill_id)
                print_rich("\n  [bold]Update Skill[/bold]\n")
                print_rich("  Status: reverted_to_inherited")
                print_rich(f"  Skill: {skill_id}")
                print("  Note: Override was identical to default; deleted.")
                print_rich()
                result = {
                    "status": "reverted_to_inherited",
                    "skill_id": skill_id,
                }
                write_result(result, args, human_fn=_render_update)
                return 0
        except Exception:
            pass

    # Ingest into profile (or project) datastore
    target_profile = profile.name if not use_project else None
    if target_profile:
        try:
            _ingest_skill(target_profile, data)
        except Exception as exc:
            print(f"  [error] Ingest failed: {exc}", file=sys.stderr)
            return 1

    skill_id = data.get("skill_id") or name
    result = {
        "status": "ingested",
        "skill_id": skill_id,
        "layer": layer_name,
    }
    if target_profile:
        result["profile"] = target_profile
    write_result(result, args, human_fn=_render_update)
    return 0


def _update_all(profile_name: str | None, args: argparse.Namespace) -> int:
    """Re-ingest all overridden skills for a profile."""
    from agentalloy.profiles import detect_profile, get_profile

    if profile_name:
        try:
            profile = get_profile(profile_name)
        except KeyError as exc:
            print(f"  [error] {exc}", file=sys.stderr)
            return 1
    else:
        profile = detect_profile()

    ingested: list[str] = []
    errors: list[str] = []

    for class_dir in ("system", "workflow"):
        d = profile.skills_dir / class_dir
        if not d.exists():
            continue
        for override_file in sorted(d.glob("*.yaml")):
            name = override_file.stem
            try:
                data = _load_yaml(override_file)
            except Exception as exc:
                errors.append(f"{name}: YAML error — {exc}")
                continue

            skill_errs = _validate_skill_data(data, name)
            if skill_errs:
                errors.append(f"{name}: validation failed — {skill_errs[0]}")
                continue

            try:
                _ingest_skill(profile.name, data)
                ingested.append(data.get("skill_id") or name)
            except Exception as exc:
                errors.append(f"{name}: ingest error — {exc}")

    result = {
        "profile": profile.name,
        "ingested": ingested,
        "ingested_count": len(ingested),
        "errors": errors,
        "error_count": len(errors),
    }
    write_result(result, args, human_fn=_render_update_all)
    return 0 if not errors else 1


def _diff_skill(args: argparse.Namespace) -> int:
    name: str = args.name
    profile_name = getattr(args, "profile", None)

    layers = _resolve_skill_layers(name, profile_name)
    _, active_path = _active_layer(layers)

    if not active_path or not active_path.exists():
        print(f"  [error] No override for '{name}'.", file=sys.stderr)
        return 1

    # Find next-higher layer
    active_layer_name, _ = _active_layer(layers)
    if active_layer_name == "project":
        compare_path = layers["profile"] or layers["default"]
    elif active_layer_name == "profile":
        compare_path = layers["default"]
    else:
        print(f"  '{name}' is at default layer — nothing to diff.", file=sys.stderr)
        return 0

    if not compare_path or not compare_path.exists():
        print("  [error] No lower layer to diff against.", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["diff", "-u", str(compare_path), str(active_path)],
        capture_output=False,
    )
    # diff exits 0 for no diff, 1 for diffs, 2 for errors
    return 0 if result.returncode in (0, 1) else 1


def _reset_skill(args: argparse.Namespace) -> int:
    name: str = args.name
    use_project = getattr(args, "project", False)
    profile_name = getattr(args, "profile", None)
    yes = getattr(args, "yes", False)

    layers = _resolve_skill_layers(name, profile_name)
    profile: Profile = cast("Profile", layers["active_profile"])
    skill_class: str | None = layers["skill_class"]

    if use_project:
        target = _project_skills_dir() / (skill_class or "system") / f"{name}.yaml"
    else:
        target = profile.skills_dir / (skill_class or "system") / f"{name}.yaml"

    if not target.exists():
        print(f"  No override found for '{name}' — nothing to reset.")
        return 0

    if not yes and sys.stdin.isatty():
        confirm = input(f"  Delete override for '{name}'? (yes/n): ").strip()
        if confirm.lower() != "yes":
            print("  Cancelled.")
            return 0

    target.unlink()

    # Remove from profile datastore
    if not use_project:
        try:
            data = _load_yaml(layers["default"]) if layers["default"] else {}
            skill_id = data.get("skill_id") or name
            _delete_from_store(profile.name, skill_id)
        except Exception:
            pass

    result = {
        "skill_id": name,
    }
    if not use_project:
        result["profile"] = profile.name
    write_result(result, args, human_fn=_render_reset)
    return 0


# ---------------------------------------------------------------------------
# Subcommand wiring
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser(
        "customize",
        help="Manage system and workflow skill overrides.",
    )
    add_json_flag(p)
    sub = p.add_subparsers(dest="customize_cmd")

    # list
    list_p = sub.add_parser("list", help="List all customizable skills and their active layer.")
    list_p.add_argument("--profile", default=None, help="Target profile.")

    # edit
    edit_p = sub.add_parser("edit", help="Open a skill override in $EDITOR.")
    edit_p.add_argument("name", help="Skill name (YAML stem).")
    edit_p.add_argument("--profile", default=None, help="Target profile.")
    edit_p.add_argument("--project", action="store_true", help="Edit project-level override.")

    # validate
    val_p = sub.add_parser("validate", help="Validate a skill override's frontmatter and body.")
    val_p.add_argument("name", help="Skill name.")
    val_p.add_argument("--profile", default=None)
    val_p.add_argument("--project", action="store_true")

    # update
    upd_p = sub.add_parser(
        "update", help="Validate and ingest a skill override into the datastore."
    )
    upd_p.add_argument("name", nargs="?", default=None, help="Skill name.")
    upd_p.add_argument("--all", action="store_true", help="Re-ingest all overrides.")
    upd_p.add_argument("--profile", default=None)
    upd_p.add_argument("--project", action="store_true")

    # diff
    diff_p = sub.add_parser("diff", help="Show diff vs next-higher layer.")
    diff_p.add_argument("name", help="Skill name.")
    diff_p.add_argument("--profile", default=None)

    # reset
    rst_p = sub.add_parser("reset", help="Delete a skill override.")
    rst_p.add_argument("name", help="Skill name.")
    rst_p.add_argument("--profile", default=None)
    rst_p.add_argument("--project", action="store_true")
    rst_p.add_argument("--yes", action="store_true", help="Skip confirmation.")

    p.set_defaults(func=_run)


_HANDLERS = {
    "list": _list_skills,
    "edit": _edit_skill,
    "validate": _validate_skill,
    "update": _update_skill,
    "diff": _diff_skill,
    "reset": _reset_skill,
}


def _run(args: argparse.Namespace) -> int:
    cmd = getattr(args, "customize_cmd", None)
    if not cmd:
        print(
            "  Usage: agentalloy customize {list,edit,validate,update,diff,reset}", file=sys.stderr
        )
        return 1
    handler = _HANDLERS.get(cmd)
    if not handler:
        print(f"  Unknown customize command: {cmd}", file=sys.stderr)
        return 1
    return handler(args)
