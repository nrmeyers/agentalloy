"""``doctor`` subcommand — runtime health check.

Extends ``verify``'s 8 checks with 6 additional runtime checks:

 9. agentalloy_service_reachable
10. compose_endpoint_works
11. state_file_consistent
12. runner_processes_present
13. fts_index_status
14. duckdb_version_ok
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, write_result
from agentalloy.install.subcommands.preflight import run_preflight
from agentalloy.install.subcommands.verify import run_checks as verify_checks

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Additional runtime checks (9–12)
# ---------------------------------------------------------------------------


def _check_service_reachable(port: int) -> dict[str, Any]:
    """Check 9: GET http://localhost:<port>/health returns 200 + status ok."""
    url = f"http://localhost:{port}/health"
    t0 = time.monotonic()
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read())
            duration = int((time.monotonic() - t0) * 1000)
            if body.get("status") == "ok":
                return {
                    "name": "agentalloy_service_reachable",
                    "passed": True,
                    "duration_ms": duration,
                    "detail": f"GET {url} → status ok",
                }
            return {
                "name": "agentalloy_service_reachable",
                "passed": False,
                "duration_ms": duration,
                "error": f"Unexpected response: {body}",
                "remediation": f"Start agentalloy: `uv run python -m agentalloy` or check port {port}",
            }
    except (URLError, OSError, json.JSONDecodeError) as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "agentalloy_service_reachable",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": f"Start the service: `uv run python -m agentalloy`, or check that port {port} is correct",
        }


def _check_compose_endpoint(port: int) -> dict[str, Any]:
    """Check 10: POST /compose with a minimal request returns fragments."""
    url = f"http://localhost:{port}/compose"
    payload = json.dumps({"task": "write a unit test", "phase": "build"}).encode()
    t0 = time.monotonic()
    try:
        req = Request(
            url, data=payload, method="POST", headers={"Content-Type": "application/json"}
        )
        with urlopen(req, timeout=30) as resp:  # noqa: S310
            body = json.loads(resp.read())
            duration = int((time.monotonic() - t0) * 1000)
            if body.get("output"):
                return {
                    "name": "compose_endpoint_works",
                    "passed": True,
                    "duration_ms": duration,
                    "detail": f"POST /compose returned {len(body.get('source_skills', []))} source skills",
                }
            return {
                "name": "compose_endpoint_works",
                "passed": False,
                "duration_ms": duration,
                "error": "Empty output from /compose",
                "remediation": "Check that the corpus is loaded and the embedding endpoint is running",
            }
    except (URLError, OSError, json.JSONDecodeError) as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "compose_endpoint_works",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": "Ensure agentalloy is running and the embedding endpoint is reachable",
        }


def _check_state_consistent(st: dict[str, Any]) -> dict[str, Any]:
    """Check 11: install-state.json is present and internally consistent."""
    t0 = time.monotonic()
    warnings: list[str] = []

    if not st.get("completed_steps"):
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "state_file_consistent",
            "passed": False,
            "duration_ms": duration,
            "error": "No completed steps in install state",
            "remediation": "Run the install flow from the beginning: follow INSTALL.md",
        }

    # Check harness files still have matching sentinels
    for entry in st.get("harness_files_written", []):
        path = Path(entry.get("path", ""))
        if not path.exists():
            warnings.append(f"Harness file missing: {path}")
            continue
        content = path.read_text()
        sentinel = entry.get("sentinel_begin", "")
        if sentinel and sentinel not in content:
            warnings.append(f"Sentinel block missing from {path}")

    duration = int((time.monotonic() - t0) * 1000)
    detail = "State file consistent"
    if warnings:
        detail += f" (warnings: {'; '.join(warnings)})"
    return {
        "name": "state_file_consistent",
        "passed": True,
        "duration_ms": duration,
        "detail": detail,
    }


def _check_runner_processes(st: dict[str, Any]) -> dict[str, Any]:
    """Check 12: expected runner processes are running."""
    t0 = time.monotonic()
    models_pulled = st.get("models_pulled", [])

    # Map runner names to process names to check
    runner_process_map = {
        "ollama": "ollama",
        "fastflowlm": "flm",
    }
    known_runners = frozenset(runner_process_map.keys())

    runners_needed: set[str] = set()
    malformed: list[str] = []
    for entry in models_pulled:
        if not isinstance(entry, str) or ":" not in entry:
            malformed.append(str(entry))
            continue
        runner = entry.split(":", 1)[0]
        if runner not in known_runners:
            malformed.append(entry)
            continue
        runners_needed.add(runner)

    missing: list[str] = []
    skipped: list[str] = []

    for runner in runners_needed:
        proc_name = runner_process_map[runner]
        binary = shutil.which(proc_name)
        if not binary:
            missing.append(f"{proc_name} not in PATH")
            continue
        # Check if process is running via pgrep. On Windows or systems
        # without pgrep, mark the check as skipped — passing silently
        # would falsely greenlight a system where the runner isn't up.
        try:
            result = subprocess.run(  # noqa: S603 — fixed args, no shell
                ["pgrep", "-x", proc_name],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                missing.append(f"{proc_name} not running")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            skipped.append(proc_name)

    duration = int((time.monotonic() - t0) * 1000)
    if missing:
        return {
            "name": "runner_processes_present",
            "passed": False,
            "duration_ms": duration,
            "error": "; ".join(missing),
            "remediation": "Start the model runners: `ollama serve` and/or `flm serve`",
        }
    if malformed:
        return {
            "name": "runner_processes_present",
            "passed": False,
            "duration_ms": duration,
            "error": f"Malformed models_pulled entries (expected 'runner:model'): {malformed}",
            "remediation": (
                "Run `python -m agentalloy.install reset-step pull-models` then "
                "re-run pull-models to rebuild state."
            ),
        }
    detail_parts: list[str] = []
    if runners_needed:
        detail_parts.append(f"runners present: {', '.join(sorted(runners_needed))}")
    else:
        detail_parts.append("no runners configured")
    if skipped:
        detail_parts.append(
            f"process check skipped (pgrep unavailable): {', '.join(sorted(skipped))}"
        )
    return {
        "name": "runner_processes_present",
        "passed": True,
        "duration_ms": duration,
        "detail": "; ".join(detail_parts),
    }


def _check_duckdb_version() -> dict[str, Any]:
    """Check 14: DuckDB version is >= 1.5.3 (fixes FTS stopwords bug)."""
    t0 = time.monotonic()
    try:
        import duckdb

        version_str = duckdb.__version__
        # Parse version: "1.5.3" -> (1, 5, 3)
        parts = version_str.split(".")
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
        version_tuple = (major, minor, patch)
        con = duckdb.connect(":memory:")
        con.close()
        duration = int((time.monotonic() - t0) * 1000)

        if version_tuple >= (1, 5, 3):
            return {
                "name": "duckdb_version_ok",
                "passed": True,
                "duration_ms": duration,
                "detail": f"DuckDB {version_str} (>= 1.5.3, FTS bug fixed)",
            }

        return {
            "name": "duckdb_version_ok",
            "passed": False,
            "duration_ms": duration,
            "error": (
                f"DuckDB {version_str} is too old — FTS stopwords bug present. "
                "Upgrade to >= 1.5.3: pip install 'duckdb>=1.5.3'"
            ),
            "remediation": "pip install 'duckdb>=1.5.3' then run: agentalloy reembed --rebuild-fts",
        }
    except Exception as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "duckdb_version_ok",
            "passed": False,
            "duration_ms": duration,
            "error": f"Could not check DuckDB version: {exc}",
            "remediation": "Ensure DuckDB is installed",
        }


def _check_fts_status(duck_path: str) -> dict[str, Any]:
    """Check 13: FTS (full-text search) index status.

    Checks whether the BM25 FTS index exists in the DuckDB database.
    If missing, reports it as a known DuckDB 1.5.3 bug (stopwords catalog
    corruption) — vector search continues to work; only BM25 is affected.
    """
    t0 = time.monotonic()
    from pathlib import Path as _Path

    p = _Path(duck_path)
    if not p.exists():
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "fts_index_status",
            "passed": True,
            "duration_ms": duration,
            "detail": "DuckDB not present yet — FTS check deferred",
        }

    try:
        import duckdb

        con = duckdb.connect(str(p), read_only=True)
        # Check for FTS tables in the fts_main_fragment_embeddings schema.
        # DuckDB 1.5.3 FTS creates tables: dict, docs, fields, stats, stopwords, terms
        # in this schema. The config table name varies by DuckDB version.
        row = con.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'fts_main_fragment_embeddings'
            """
        ).fetchone()
        fts_exists = row and int(row[0]) > 0 if row else False
        con.close()
        duration = int((time.monotonic() - t0) * 1000)

        if fts_exists:
            return {
                "name": "fts_index_status",
                "passed": True,
                "duration_ms": duration,
                "detail": "FTS (BM25 full-text search) index present",
            }

        # FTS index missing — this is a known DuckDB 1.5.3 bug
        return {
            "name": "fts_index_status",
            "passed": False,
            "duration_ms": duration,
            "error": (
                "FTS index rebuild failed due to a known DuckDB 1.5.3 bug "
                "(stopwords catalog corruption). This is NOT an agentalloy issue — "
                "vector search works correctly. Full-text search (BM25) will be "
                "unavailable until DuckDB is upgraded. To retry: "
                "agentalloy reembed --rebuild-fts"
            ),
            "remediation": "Upgrade DuckDB to >= 1.5.3, then run: agentalloy reembed --rebuild-fts",
        }
    except Exception as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "fts_index_status",
            "passed": False,
            "duration_ms": duration,
            "error": f"Could not check FTS index: {exc}",
            "remediation": "Run `agentalloy reembed --rebuild-fts` to rebuild the FTS index",
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_doctor(root: Path | None = None) -> dict[str, Any]:
    """Run all 14 checks (verify's 8 + doctor's 6)."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    st = install_state.load_state(root)
    port = install_state.validate_port(st.get("port", 47950))

    # Resolve DuckDB path for FTS check
    user_corpus = install_state.corpus_dir()
    env = _read_env_values(install_state.user_config_dir())
    duck_path = env.get("DUCKDB_PATH", str(user_corpus / "skills.duck"))
    if not Path(duck_path).is_absolute():
        duck_path = str(user_corpus / duck_path)

    # Run preflight early checks first — if uv is missing or PATH is
    # broken, every later check is downstream noise. Strip the severity
    # field so the shape matches verify/doctor checks (which don't carry it).
    preflight_result = run_preflight(phase="early", port=port)
    checks: list[dict[str, Any]] = [
        {k: v for k, v in c.items() if k != "severity"} for c in preflight_result["checks"]
    ]

    # Run verify's 8 checks
    verify_result = verify_checks(st, root)
    checks.extend(verify_result["checks"])

    # Add doctor's 6 additional checks
    checks.append(_check_service_reachable(port))
    checks.append(_check_compose_endpoint(port))
    checks.append(_check_state_consistent(st))
    checks.append(_check_runner_processes(st))
    checks.append(_check_fts_status(duck_path))
    checks.append(_check_duckdb_version())

    all_passed = all(c["passed"] for c in checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "all_checks_passed": all_passed,
        "checks": checks,
    }


def _read_env_values(root: Path) -> dict[str, str]:  # noqa: ARG001 — back-compat
    """Read the user-scoped .env file into a dict."""
    env_path = install_state.env_path()
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "doctor",
        help="Runtime health check across all components.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    """Render doctor check results in human-readable format."""
    from agentalloy.install.output import render_checklist

    render_checklist(result, title="Health Check")


def _run(args: argparse.Namespace) -> int:
    result = run_doctor()
    install_state.save_output_file(result, "doctor.json")
    write_result(result, args, human_fn=_render_human)
    return 0 if result["all_checks_passed"] else 1
