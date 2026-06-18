# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""``doctor`` subcommand — diagnose and optionally repair a broken install.

Nine checks covering the full install surface:

 1. config          — .env exists; RUNTIME_EMBED_BASE_URL / RUNTIME_EMBEDDING_MODEL set
 2. embed_server    — GET {RUNTIME_EMBED_BASE_URL} reachable; model listed (warn, not fail)
 3. corpus_files    — ladybug/ + skills.duck present at corpus_dir()
 4. ladybug_schema  — Skill table exists; lock-held → report PID + stop-service remediation
 5. corpus_count    — skill count >= 25 (LadybugDB); embedded-vector count > 0 (DuckDB)
 6. embedding_dim   — stored DuckDB dim matches EMBEDDING_DIM constant
 7. service         — port /health responding (down is ok; up-degraded is warned)
 8. pack_manifests  — every bundled pack.yaml parses cleanly (drift → fail)
 9. reranker        — signal-intent reranker (:47952) reachable (warn, not fail)

``--repair``:  migrate → install-packs → reembed → re-diagnose (in that order).
Lock-held aborts repair immediately — repair must not kill processes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 2
_MIN_SKILL_COUNT = 25


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_config() -> dict[str, Any]:
    """Check 1: .env exists and has required keys."""
    t0 = time.monotonic()
    env_file = install_state.env_path()
    if not env_file.exists():
        return {
            "name": "config",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f".env not found at {env_file}",
            "remediation": (
                "Run `agentalloy write-env` to create the config file, "
                "or copy one of the .env.* presets: `cp .env.cpu .env`"
            ),
        }
    env = install_state.parse_env_file(env_file)
    missing = [k for k in ("RUNTIME_EMBED_BASE_URL", "RUNTIME_EMBEDDING_MODEL") if k not in env]
    if missing:
        return {
            "name": "config",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Missing keys in .env: {', '.join(missing)}",
            "remediation": (
                f"Add the missing keys to {env_file}. See a .env.* preset for reference values."
            ),
        }
    return {
        "name": "config",
        "passed": True,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "detail": (
            f"RUNTIME_EMBED_BASE_URL={env['RUNTIME_EMBED_BASE_URL']!r}  "
            f"RUNTIME_EMBEDDING_MODEL={env['RUNTIME_EMBEDDING_MODEL']!r}"
        ),
    }


def _check_embed_server(base_url: str, model: str) -> dict[str, Any]:
    """Check 2: embed server reachable; model listed via /api/tags (best-effort warn)."""
    t0 = time.monotonic()
    try:
        req = Request(base_url, method="GET")
        with urlopen(req, timeout=5) as resp:  # noqa: S310
            resp.read()
    except (URLError, OSError) as exc:
        return {
            "name": "embed_server",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Cannot reach {base_url}: {exc}",
            "remediation": (
                "Start the embedding server (e.g. `ollama serve`) and ensure "
                f"RUNTIME_EMBED_BASE_URL={base_url!r} is correct in .env"
            ),
        }

    # Best-effort: check /api/tags for model presence (Ollama-specific; warn only)
    tags_url = base_url.rstrip("/") + "/api/tags"
    try:
        req2 = Request(tags_url, method="GET")
        with urlopen(req2, timeout=5) as resp2:  # noqa: S310
            body = json.loads(resp2.read())
        models = [m.get("name", "") for m in (body.get("models") or [])]
        listed = any(model in m for m in models)
        if not listed:
            return {
                "name": "embed_server",
                "passed": True,
                "severity": "warn",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "detail": f"Server reachable but model {model!r} not found in /api/tags",
                "remediation": f"Pull the model: `ollama pull {model}`",
            }
        return {
            "name": "embed_server",
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": f"Server reachable; model {model!r} listed",
        }
    except (URLError, OSError, json.JSONDecodeError):
        # Non-Ollama server or /api/tags unavailable — server is up, model check skipped
        return {
            "name": "embed_server",
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": f"Server reachable at {base_url} (model listing not available)",
        }


def _check_corpus_files(cdir: Path) -> dict[str, Any]:
    """Check 3: ladybug/ and skills.duck present."""
    t0 = time.monotonic()
    ladybug = cdir / "ladybug"
    duckdb = cdir / "skills.duck"
    missing = [str(p) for p in (ladybug, duckdb) if not p.exists()]
    if missing:
        return {
            "name": "corpus_files",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Missing corpus files: {', '.join(missing)}",
            "remediation": "Run `agentalloy install-packs` to populate the corpus.",
        }
    return {
        "name": "corpus_files",
        "passed": True,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "detail": f"ladybug/ and skills.duck present at {cdir}",
    }


def _check_ladybug_schema(ladybug_path: str) -> dict[str, Any]:
    """Check 4: Skill table exists; distinguish lock-held from schema-missing."""
    t0 = time.monotonic()
    from agentalloy.storage.ladybug import LOCK_HELD_REMEDIATION, LadybugStore, is_lock_held_error

    try:
        with LadybugStore(ladybug_path) as store:
            rows = store.execute("MATCH (s:Skill) RETURN count(s) LIMIT 1")
            _ = rows  # just confirming the table exists
        return {
            "name": "ladybug_schema",
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": "Skill table present",
        }
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if is_lock_held_error(err):
            return {
                "name": "ladybug_schema",
                "passed": False,
                "lock_held": True,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": f"DB lock held: {err}",
                "remediation": LOCK_HELD_REMEDIATION,
            }
        return {
            "name": "ladybug_schema",
            "passed": False,
            "lock_held": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Schema missing or corrupt: {err}",
            "remediation": (
                "Run `agentalloy doctor --repair` to migrate the schema, "
                "or run `agentalloy install-packs` directly."
            ),
        }


def _check_corpus_count(ladybug_path: str, duckdb_path: str) -> dict[str, Any]:
    """Check 5: skill count >= 25 in LadybugDB; embedded-vector count > 0 in DuckDB."""
    t0 = time.monotonic()
    from agentalloy.storage.ladybug import LadybugStore, is_lock_held_error
    from agentalloy.storage.vector_store import open_or_create

    skill_count = 0
    vec_count = 0
    skill_err: str | None = None
    vec_err: str | None = None

    try:
        with LadybugStore(ladybug_path) as store:
            rows = store.execute("MATCH (s:Skill) RETURN count(s)")
            skill_count = int(rows[0][0]) if rows and rows[0] else 0
    except Exception as exc:  # noqa: BLE001
        skill_err = str(exc)
        if is_lock_held_error(skill_err):
            # Lock-held is already caught in check 4; skip double-reporting
            return {
                "name": "corpus_count",
                "passed": False,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": f"Cannot count skills — DB lock held: {skill_err}",
                "remediation": "Stop the agentalloy service and retry.",
            }

    try:
        vs = open_or_create(Path(duckdb_path))
        vec_count = vs.count_embeddings()
    except Exception as exc:  # noqa: BLE001
        vec_err = str(exc)

    errors: list[str] = []
    remediations: list[str] = []
    if skill_err:
        errors.append(f"LadybugDB: {skill_err}")
    elif skill_count < _MIN_SKILL_COUNT:
        errors.append(f"skill count {skill_count} < {_MIN_SKILL_COUNT}")
        remediations.append("Run `agentalloy install-packs` to install skills.")
    if vec_err:
        errors.append(f"DuckDB: {vec_err}")
    elif vec_count == 0:
        errors.append("no embedded vectors in DuckDB")
        remediations.append("Run `agentalloy reembed` to populate embeddings.")

    if errors:
        return {
            "name": "corpus_count",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": "; ".join(errors),
            "remediation": " ".join(remediations) if remediations else None,
        }
    return {
        "name": "corpus_count",
        "passed": True,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "detail": f"{skill_count} skills; {vec_count} embedded vectors",
    }


def _read_stored_dim(duckdb_path: str) -> int | None:
    """Read the stored embedding dim directly, bypassing open_or_create's guard."""
    try:
        import duckdb

        con = duckdb.connect(duckdb_path, read_only=True)
        try:
            row = con.execute("SELECT len(embedding) FROM fragment_embeddings LIMIT 1").fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None


def _check_embedding_dim(duckdb_path: str) -> dict[str, Any]:
    """Check 6: stored DuckDB embedding dim matches EMBEDDING_DIM constant."""
    t0 = time.monotonic()
    from agentalloy.storage.vector_store import (
        EMBEDDING_DIM,
        EmbeddingDimMismatch,
        open_or_create,
    )

    try:
        vs = open_or_create(Path(duckdb_path))
        stored_dim = vs.embedding_dim()
    except EmbeddingDimMismatch:
        # open_or_create's startup guard raises for exactly the mismatch case, so
        # surface the tailored remediation (read the real stored dim directly,
        # bypassing the guard) instead of the generic "cannot read" error.
        stored = _read_stored_dim(duckdb_path)
        return {
            "name": "embedding_dim",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Stored dim {stored} != expected {EMBEDDING_DIM}",
            "remediation": (
                "Embedding model changed. Run `agentalloy reembed --force` "
                "to rebuild the vector store at the current dimension."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "embedding_dim",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Cannot read DuckDB dim: {exc}",
            "remediation": "Run `agentalloy reembed --force` after checking EMBEDDING_DIM.",
        }

    if stored_dim is None:
        # Empty corpus — not a dim-mismatch
        return {
            "name": "embedding_dim",
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": f"No embeddings yet (expected dim={EMBEDDING_DIM})",
        }

    if stored_dim != EMBEDDING_DIM:
        return {
            "name": "embedding_dim",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Stored dim {stored_dim} != expected {EMBEDDING_DIM}",
            "remediation": (
                "Embedding model changed. Run `agentalloy reembed --force` "
                "to rebuild the vector store at the current dimension."
            ),
        }
    return {
        "name": "embedding_dim",
        "passed": True,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "detail": f"dim={stored_dim} matches EMBEDDING_DIM={EMBEDDING_DIM}",
    }


def _check_service(port: int) -> dict[str, Any]:
    """Check 7: service /health (down is ok; up-but-degraded is warned)."""
    t0 = time.monotonic()
    url = f"http://localhost:{port}/health"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:  # noqa: S310
            body = json.loads(resp.read())
        if body.get("status") == "ok":
            return {
                "name": "service",
                "passed": True,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "detail": f"Service up on port {port}, status=ok",
            }
        return {
            "name": "service",
            "passed": True,
            "severity": "warn",
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": f"Service up but degraded: {body}",
            "remediation": f"Check service logs. Port {port}.",
        }
    except (URLError, OSError):
        return {
            "name": "service",
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": f"Service not running on port {port} (not required for corpus ops)",
        }
    except json.JSONDecodeError as exc:
        return {
            "name": "service",
            "passed": True,
            "severity": "warn",
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": f"Service responded but body not JSON: {exc}",
        }


def _check_reranker(env: dict[str, str]) -> dict[str, Any]:
    """Check 9: signal-intent reranker reachable (soft warn, never fatal).

    The reranker (Qwen3-Reranker on :47952) is the primary phase-transition
    trigger; when it's down the signal layer falls back to the cosine floor, so
    an absent reranker warns rather than fails (matching the service check).
    ``SIGNAL_INTENT_BACKEND=cosine`` → plain pass (reranker not used)."""
    t0 = time.monotonic()
    backend, url = install_state.resolve_intent_reranker(env)
    if backend == "cosine":
        return {
            "name": "reranker",
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": "SIGNAL_INTENT_BACKEND=cosine — reranker not used (embedder-based intent)",
        }
    if install_state.rerank_reachable(url):
        return {
            "name": "reranker",
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": f"Reranker reachable at {url} — intent-based phase detection active",
        }
    return {
        "name": "reranker",
        "passed": True,
        "severity": "warn",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "detail": f"Reranker not reachable at {url}; phase detection falls back to the cosine floor",
        "remediation": (
            "Start it with `agentalloy enable-service`, or set "
            "SIGNAL_INTENT_BACKEND=cosine in .env to use the embedder-based floor."
        ),
    }


def _check_pack_manifests() -> dict[str, Any]:
    """Check 8: every bundled pack manifest passes full drift validation."""
    t0 = time.monotonic()
    try:
        import agentalloy

        packs_root = Path(agentalloy.__file__).resolve().parent / "_packs"
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "pack_manifests",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Cannot locate _packs dir: {exc}",
            "remediation": "Reinstall agentalloy: `uv tool install agentalloy`",
        }

    if not packs_root.is_dir():
        return {
            "name": "pack_manifests",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"_packs directory not found at {packs_root}",
            "remediation": "Reinstall agentalloy: `uv tool install agentalloy`",
        }

    from agentalloy.install.subcommands.install_pack import _read_pack_manifest

    bad: list[str] = []
    total = 0
    for pack_dir in sorted(packs_root.iterdir()):
        if not (pack_dir / "pack.yaml").is_file():
            continue
        total += 1
        manifest, errors = _read_pack_manifest(pack_dir)
        if manifest is None:
            bad.append(f"{pack_dir.name}: manifest failed to parse")
        elif errors:
            bad.append(f"{pack_dir.name}: {errors[0]}")

    if bad:
        return {
            "name": "pack_manifests",
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": f"{len(bad)}/{total} manifest(s) failed: {'; '.join(bad[:5])}",
            "remediation": "Reinstall agentalloy to restore bundled packs.",
        }
    return {
        "name": "pack_manifests",
        "passed": True,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "detail": f"{total} pack manifest(s) valid",
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_doctor() -> dict[str, Any]:
    """Run all 8 doctor checks. Returns a result dict."""
    from agentalloy.config import get_settings
    from agentalloy.install.state import corpus_dir, env_path, parse_env_file

    # Config check first — need env values for subsequent checks
    config_check = _check_config()
    checks: list[dict[str, Any]] = [config_check]

    # Resolve embed URL / model from .env (fall back to Settings defaults)
    env = parse_env_file(env_path())
    base_url = env.get("RUNTIME_EMBED_BASE_URL", "http://localhost:47951")
    model = env.get("RUNTIME_EMBEDDING_MODEL", "nomic-embed-text-v1.5.Q8_0.gguf")

    checks.append(_check_embed_server(base_url, model))

    cdir = corpus_dir()
    checks.append(_check_corpus_files(cdir))

    # Resolve DB paths via Settings (honours XDG overrides in tests)
    try:
        settings = get_settings()
        ladybug_path = settings.ladybug_db_path
        duckdb_path = settings.duckdb_path
    except Exception:  # noqa: BLE001
        ladybug_path = str(cdir / "ladybug")
        duckdb_path = str(cdir / "skills.duck")

    checks.append(_check_ladybug_schema(ladybug_path))
    checks.append(_check_corpus_count(ladybug_path, duckdb_path))
    checks.append(_check_embedding_dim(duckdb_path))

    st = install_state.load_state()
    port = install_state.validate_port(st.get("port", 47950))
    checks.append(_check_service(port))
    checks.append(_check_pack_manifests())
    checks.append(_check_reranker(env))

    all_passed = all(c["passed"] for c in checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "all_checks_passed": all_passed,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _repair(result: dict[str, Any]) -> int:
    """Execute repair sequence for failed checks. Returns 0 on success."""
    checks_by_name = {c["name"]: c for c in result["checks"]}

    # Lock-held: abort immediately — must not kill processes
    schema_check = checks_by_name.get("ladybug_schema", {})
    if schema_check.get("lock_held"):
        print_rich(
            "[red]ABORT:[/red] DB lock is held by another process. "
            "Stop the agentalloy service first, then re-run doctor --repair."
        )
        rem = schema_check.get("remediation", "")
        if rem:
            print_rich(f"  {rem}")
        return 1

    any_failed = not result["all_checks_passed"]
    if not any_failed:
        print_rich("[green]All checks passed — nothing to repair.[/green]")
        return 0

    rc = 0

    # Step 1: migrate schema (idempotent)
    schema_failed = not checks_by_name.get("ladybug_schema", {}).get("passed", True)
    corpus_failed = not checks_by_name.get("corpus_files", {}).get("passed", True)
    if schema_failed and not corpus_failed:
        print_rich("[yellow]→ Running schema migration…[/yellow]")
        from agentalloy.config import get_settings
        from agentalloy.storage.ladybug import LadybugStore

        try:
            settings = get_settings()
            with LadybugStore(settings.ladybug_db_path) as store:
                store.migrate()
            print_rich("[green]  Schema migration OK[/green]")
        except Exception as exc:  # noqa: BLE001
            print_rich(f"[red]  Schema migration failed: {exc}[/red]")
            rc = 1

    # Step 2: install-packs if corpus is empty or files missing
    count_check = checks_by_name.get("corpus_count", {})
    corpus_needs_packs = corpus_failed or not count_check.get("passed", True)
    if corpus_needs_packs:
        print_rich("[yellow]→ Running install-packs --packs all…[/yellow]")
        try:
            import subprocess

            sub_rc = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "agentalloy.install", "install-packs", "--packs", "all"],
                check=False,
            ).returncode
            if sub_rc == 0:
                print_rich("[green]  install-packs OK[/green]")
            else:
                print_rich(f"[red]  install-packs exited {sub_rc}[/red]")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print_rich(f"[red]  install-packs error: {exc}[/red]")
            rc = 1

    # Step 3: reembed if dim mismatch or no vectors
    dim_check = checks_by_name.get("embedding_dim", {})
    reembed_needed = not count_check.get("passed", True) or not dim_check.get("passed", True)
    if reembed_needed:
        force_flag = ["--force"] if not dim_check.get("passed", True) else []
        print_rich(f"[yellow]→ Running reembed {' '.join(force_flag)}…[/yellow]")
        try:
            from agentalloy.reembed.cli import main as reembed_main

            reembed_rc = reembed_main(force_flag)
            if reembed_rc == 0:
                print_rich("[green]  reembed OK[/green]")
            else:
                print_rich(f"[red]  reembed exited {reembed_rc}[/red]")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print_rich(f"[red]  reembed error: {exc}[/red]")
            rc = 1

    # Step 4: re-diagnose and print after-picture
    print_rich("")
    print_rich("[bold]After repair:[/bold]")
    after = run_doctor()
    _render_human_result(after)
    if not after["all_checks_passed"]:
        rc = 1

    return rc


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _render_human_result(result: dict[str, Any]) -> None:
    from agentalloy.install.output import render_checklist

    render_checklist(result, title="Doctor")

    warns = [
        c
        for c in result["checks"]
        if c.get("passed") is not False  # skip failures
        and c.get("severity") == "warn"
    ]
    if warns:
        print_rich()
        print_rich(f"  [yellow]{len(warns)} warning(s) — install functional.[/yellow]")


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "doctor",
        help=(
            "Diagnose broken installs: config, embed server, corpus files, schema, "
            "skill count, embedding dim, service, and pack manifests. "
            "Pass --repair to auto-fix what's broken."
        ),
    )
    p.add_argument(
        "--repair",
        action="store_true",
        default=False,
        help=(
            "Attempt to repair detected failures: migrate schema → install-packs "
            "→ reembed → re-diagnose. Lock-held state aborts with a remediation message."
        ),
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    _render_human_result(result)


def _run(args: argparse.Namespace) -> int:
    result = run_doctor()
    install_state.save_output_file(result, "doctor.json")

    if getattr(args, "repair", False):
        write_result(result, args, human_fn=_render_human)
        return _repair(result)

    write_result(result, args, human_fn=_render_human)
    return 0 if result["all_checks_passed"] else 1
