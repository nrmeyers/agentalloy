# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false
"""``validate-pack`` subcommand — dry-run of install-pack's Gate 1, zero side effects.

Lets a user check a local pack (hand-written or scaffolded via
``agentalloy new-skill-pack``) BEFORE committing to a real install. Reuses
the exact same manifest-loading helper (``install_pack._read_pack_manifest``)
and schema+lint gate (``pack_validation.validate_pack_skills``) that
``install-pack``'s Gate 1 runs, so the two paths can never drift apart.

No ingestion, no reembed, no network, no corpus mutation of any kind.

Exit codes
----------
0  pack.yaml parsed cleanly and every skill passed Gate 1
1  pack.yaml parsed but has manifest-level errors, and/or one or more
   skills failed schema/lint validation
2  usage error — pack_dir doesn't exist, or has no readable pack.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agentalloy.install.output import add_json_flag, print_rich, write_result
from agentalloy.install.subcommands.install_pack import _read_pack_manifest
from agentalloy.pack_validation import PackValidationResult, validate_pack_skills

SCHEMA_VERSION = 1


def validate_pack(pack_dir: Path, *, strict: bool = True) -> dict[str, Any]:
    """Pure dry-run of install-pack's Gate 1 (schema + lint validation).

    Returns a contract-shaped result dict. Check ``result["action"]``:
    ``"usage_error"`` (no pack_dir / no pack.yaml — caller should exit 2),
    ``"valid"`` (exit 0), or ``"invalid"`` (manifest and/or per-skill errors
    — exit 1).
    """
    if not pack_dir.is_dir():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "usage_error",
            "pack_dir": str(pack_dir),
            "error": f"not a directory: {pack_dir}",
        }

    manifest, manifest_errors = _read_pack_manifest(pack_dir)
    if manifest is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "usage_error",
            "pack_dir": str(pack_dir),
            "error": "; ".join(manifest_errors) or f"no pack.yaml in {pack_dir}",
        }

    skills_entries: list[dict[str, Any]] = manifest.get("skills") or []
    schema_result: PackValidationResult = validate_pack_skills(
        pack_dir, skills_entries, strict=strict
    )

    ok = schema_result.ok and not manifest_errors
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "valid" if ok else "invalid",
        "ok": ok,
        "pack_dir": str(pack_dir),
        "pack": manifest.get("name"),
        "strict": strict,
        "skill_count": len(skills_entries),
        "manifest_errors": manifest_errors,
        "skills": [
            {
                "skill_id": str(e.get("skill_id", "")),
                "file": str(e.get("file", "")),
            }
            for e in skills_entries
            if isinstance(e, dict)
        ],
        "errors": [
            {"skill_id": e.skill_id, "file": e.file, "errors": e.errors}
            for e in schema_result.errors
        ],
        "formatted_errors": schema_result.format_errors(),
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser(
        "validate-pack",
        help=(
            "Dry-run a local pack's schema + lint gate (install-pack's Gate 1) "
            "with zero side effects — no ingestion, no network, no corpus mutation."
        ),
    )
    p.add_argument(
        "pack_dir",
        help="Path to the local pack directory containing pack.yaml.",
    )
    p.add_argument(
        "--allow-lint-warnings",
        action="store_true",
        help=(
            "Downgrade authoring-contract lint warnings (fragment sizes, missing "
            "rationale/verification, tag issues) from errors to warnings for this "
            "check — mirrors install-pack's identical flag."
        ),
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    pack = result.get("pack") or "(unknown)"
    pack_dir = result.get("pack_dir", "")
    print_rich(f"\n  [bold]validate-pack: {pack}[/bold]  ({pack_dir})\n")

    for err in result.get("manifest_errors") or []:
        print_rich(f"  [red]FAIL[/red] pack.yaml: {err}")

    error_by_file: dict[str, dict[str, Any]] = {
        str(e.get("file", "")): e for e in result.get("errors") or []
    }
    passed = 0
    failed = 0
    for s in result.get("skills") or []:
        fname = s.get("file", "")
        sid = s.get("skill_id") or fname or "(unknown)"
        err = error_by_file.get(fname)
        if err:
            failed += 1
            print_rich(f"  [red]FAIL[/red] {sid} ({fname})")
            for msg in err.get("errors") or []:
                print_rich(f"         {msg}")
        else:
            passed += 1
            print_rich(f"  [green]PASS[/green] {sid} ({fname})")

    print_rich(f"\n  Passed: {passed}  Failed: {failed}  Total: {len(result.get('skills') or [])}")
    if result.get("ok"):
        print_rich("  [green]Pack is valid.[/green]\n")
    else:
        print_rich("  [red]Pack has validation errors — see above.[/red]\n")


def _run(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir)
    strict = not args.allow_lint_warnings
    result = validate_pack(pack_dir, strict=strict)

    if result.get("action") == "usage_error":
        print(f"error: {result.get('error')}", file=sys.stderr)
        return 2

    write_result(result, args, human_fn=_render_human)
    return 0 if result.get("ok") else 1
