"""``agentalloy reset`` — nuclear reset for profile overrides.

Commands:
    agentalloy reset                   — reset active profile overrides (prompts)
    agentalloy reset --profile <name>  — reset a specific profile
    agentalloy reset --all-profiles    — reset every profile
    agentalloy reset --include-domain  — also wipe and re-ingest domain.duck
    agentalloy reset --yes             — skip confirmation
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


def _reingest_profile_defaults(profile_name: str) -> list[str]:
    """Re-ingest shipped default system+workflow skills into a profile's datastore."""
    import agentalloy

    packs_root = Path(agentalloy.__file__).resolve().parent / "_packs"
    ingested: list[str] = []

    if not packs_root.is_dir():
        return ingested

    try:
        import yaml

        from agentalloy.install.subcommands.customize import (
            _ingest_skill,  # pyright: ignore[reportPrivateUsage]
        )

        for yaml_file in sorted(packs_root.rglob("*.yaml")):
            if yaml_file.name == "pack.yaml":
                continue
            try:
                data: dict[str, Any] = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            skill_class: str = str(data.get("skill_class", ""))
            if skill_class not in ("system", "workflow"):
                continue

            # Ingest into the profile datastore
            try:
                skill_id: str = str(data.get("skill_id") or yaml_file.stem)
                _ingest_skill(profile_name, data)
                ingested.append(skill_id)
            except Exception:
                continue
    except ImportError:
        pass

    return ingested


def _reset_profile(
    name: str,
    include_domain: bool = False,
) -> dict[str, Any]:
    """Reset a single profile: delete overrides, re-ingest defaults."""
    from agentalloy.profiles import get_profile, profile_skills_dir

    try:
        get_profile(name)
    except KeyError:
        return {"profile": name, "error": f"Profile '{name}' not found"}

    # Delete override files
    skills = profile_skills_dir(name)
    deleted_overrides: list[str] = []
    for class_dir in ("system", "workflow"):
        d = skills / class_dir
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file():
                    deleted_overrides.append(str(f.relative_to(skills)))
                    f.unlink()

    # Re-ingest defaults
    ingested = _reingest_profile_defaults(name)

    result: dict[str, Any] = {
        "profile": name,
        "deleted_overrides": deleted_overrides,
        "reingested_defaults": ingested,
    }

    if include_domain:
        from agentalloy.profiles import domain_datastore_path

        domain_path = domain_datastore_path()
        if domain_path.exists():
            domain_path.unlink()
        # Full re-embed is triggered by seed_corpus
        try:
            from agentalloy.install.subcommands import seed_corpus

            seed_result: dict[str, Any] = seed_corpus.check_corpus()
            result["domain_reset"] = seed_result
        except Exception as exc:
            result["domain_reset"] = {"error": str(exc)}

    return result


def reset(
    profile: str | None = None,
    all_profiles: bool = False,
    include_domain: bool = False,
    yes: bool = False,
) -> dict[str, Any]:
    """Run the reset flow. Returns a summary dict."""
    from agentalloy.profiles import detect_profile, load_profiles_config

    t0 = time.monotonic()

    # Determine which profiles to reset
    if all_profiles:
        config = load_profiles_config()
        target_names = list(config.profiles.keys())
        target_names.insert(0, "default")
        # De-duplicate while preserving order
        seen: set[str] = set()
        targets: list[str] = []
        for n in target_names:
            if n not in seen:
                targets.append(n)
                seen.add(n)
        confirmation_target = "ALL profiles"
    else:
        if profile:
            targets = [profile]
        else:
            active = detect_profile(cwd=Path.cwd())
            targets = [active.name]
        confirmation_target = f"profile '{targets[0]}'"

    # Confirmation
    if not yes:
        if sys.stdin.isatty():
            prompt = f"  Reset {confirmation_target}? All override files will be deleted. Type 'yes' to confirm: "
            confirm = input(prompt).strip()
            if confirm.lower() != "yes":
                return {"cancelled": True}
        else:
            # Non-TTY without --yes: refuse
            print(
                f"  [error] Resetting {confirmation_target} requires --yes in non-interactive mode.",
                file=sys.stderr,
            )
            return {"error": "confirmation required"}

    results = [_reset_profile(name, include_domain=include_domain) for name in targets]

    summary: dict[str, Any] = {
        "reset_profiles": results,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
    return summary


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser(
        "reset",
        help="Reset profile overrides and re-ingest defaults.",
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Target a specific profile (default: active profile for cwd).",
    )
    p.add_argument(
        "--all-profiles",
        dest="all_profiles",
        action="store_true",
        help="Reset every configured profile.",
    )
    p.add_argument(
        "--include-domain",
        dest="include_domain",
        action="store_true",
        help="Also wipe and re-ingest the shared domain datastore (slow).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt (dangerous).",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    result = reset(
        profile=args.profile,
        all_profiles=args.all_profiles,
        include_domain=args.include_domain,
        yes=args.yes,
    )
    if result.get("cancelled"):
        print("  Reset cancelled.")
        return 0
    if result.get("error"):
        print(f"  [error] {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0
