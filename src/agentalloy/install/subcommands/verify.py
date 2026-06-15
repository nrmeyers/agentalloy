# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false, reportArgumentType=false
"""``verify`` subcommand — install-time smoke test.

Runs 8 enumerated checks from contracts.md:
1. embedding_endpoint_reachable
2. embedding_endpoint_returns_expected_dim
3. duckdb_present
4. ladybug_present
5. skill_count_meets_minimum
6. harness_config_present
7. harness_config_url_matches
8. runtime_port_available
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, write_result
from agentalloy.storage.vector_store import EMBEDDING_DIM

SCHEMA_VERSION = 1
# Sanity floor — flags truly empty corpora; not a quality bar.
MIN_SKILL_COUNT = 25

# Allowed schemes for the embedding-runtime URL probe. `.env` is operator-
# controlled but a hostile dependency or shared dotfile could rewrite it
# to `file://` (local file disclosure) or an internal IP for SSRF — apply
# the same allowlist install-pack uses.
_ALLOWED_PROBE_SCHEMES = frozenset({"http", "https"})


def _validate_probe_url(url: str, kind: str) -> dict[str, Any] | None:
    """Return a check-failure dict if ``url``'s scheme isn't allowed, else None."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_PROBE_SCHEMES:
        return {
            "name": kind,
            "passed": False,
            "duration_ms": 0,
            "error": f"{kind} URL has disallowed scheme '{parsed.scheme}': {url}",
            "remediation": (
                f"Set {kind} to an http:// or https:// URL in .env, then re-run write-env."
            ),
        }
    return None


_SENTINEL_BEGIN = "<!-- BEGIN agentalloy install -->"


def _probe_diagnostics(port: int) -> dict[str, Any] | None:
    """Probe the running service's diagnostics endpoint.

    Returns the parsed JSON body when the service is up and responding,
    None on any failure. Used by the DB checks to avoid racing the
    service for the Kuzu file lock.
    """
    url = f"http://localhost:{port}/diagnostics/runtime"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=2) as resp:  # noqa: S310 — localhost-only probe
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except (URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_embedding_via_diagnostics(
    diag: dict[str, Any] | None, check_name: str
) -> dict[str, Any]:
    """Read embedder status from /diagnostics/runtime.

    Used by container deployments where the host can't reach the embedder
    directly (Ollama runs inside the agentalloy container). The
    diagnostics endpoint reports the same embedding_runtime dep status the
    agentalloy service uses internally — "ok" means the service successfully
    embedded against the configured backend at startup or last refresh.

    The diagnostics JSON wire field is ``dependency_readiness`` (see
    ``DependencyReadiness`` in ``agentalloy.api.diagnostics_router``); the
    legacy ``dep_readiness`` key is checked too for forward/backward safety
    against snapshot drift.
    """
    t0 = time.monotonic()
    if diag is None:
        return {
            "name": check_name,
            "passed": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": "/diagnostics/runtime unreachable",
            "remediation": (
                "Container not responding on the configured port. Check the "
                "container logs (e.g. `podman logs agentalloy` or "
                "`docker logs agentalloy`) for startup errors."
            ),
        }
    readiness = diag.get("dependency_readiness") or diag.get("dep_readiness") or {}
    status = readiness.get("embedding_runtime")
    if status == "ok":
        return {
            "name": check_name,
            "passed": True,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "detail": "embedding_runtime=ok (via /diagnostics/runtime)",
        }
    return {
        "name": check_name,
        "passed": False,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "error": f"embedding_runtime status={status!r} (via /diagnostics/runtime)",
        "remediation": (
            "Bundled Ollama isn't healthy yet. Check the container logs — "
            "e.g. `podman logs agentalloy` (Ollama runs inside the same "
            "container; substitute `docker` if you're on Docker)."
        ),
    }


def _check_embedding_endpoint_reachable(embed_url: str) -> dict[str, Any]:
    """Check 1: GET <embed_url>/v1/models returns 200."""
    bad = _validate_probe_url(embed_url, "embedding_endpoint_reachable")
    if bad:
        return bad
    t0 = time.monotonic()
    url = f"{embed_url.rstrip('/')}/v1/models"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:  # noqa: S310 — URL is user-configured, not arbitrary
            status = resp.status
        duration = int((time.monotonic() - t0) * 1000)
        if status == 200:
            return {
                "name": "embedding_endpoint_reachable",
                "passed": True,
                "duration_ms": duration,
                "detail": f"GET {url} returned 200",
            }
        return {
            "name": "embedding_endpoint_reachable",
            "passed": False,
            "duration_ms": duration,
            "error": f"GET {url} returned {status}",
            "remediation": f"Check that Ollama is running at {embed_url}",
        }
    except (URLError, OSError, TimeoutError) as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "embedding_endpoint_reachable",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": "Start Ollama with `ollama serve`, then re-run verify",
        }


def _check_embedding_expected_dim(embed_url: str, model: str) -> dict[str, Any]:
    """Check 2: POST /v1/embeddings returns a 768-dim vector."""
    bad = _validate_probe_url(embed_url, "embedding_expected_dim")
    if bad:
        return bad
    t0 = time.monotonic()
    url = f"{embed_url.rstrip('/')}/v1/embeddings"
    body = json.dumps({"model": model, "input": "hello world"}).encode()
    try:
        req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read())
        embeddings = data.get("data", [])
        if not embeddings:
            duration = int((time.monotonic() - t0) * 1000)
            return {
                "name": "embedding_endpoint_returns_expected_dim",
                "passed": False,
                "duration_ms": duration,
                "error": "No embeddings returned",
                "remediation": f"Ensure model '{model}' is pulled: `ollama pull {model}`",
            }
        dim = len(embeddings[0].get("embedding", []))
        duration = int((time.monotonic() - t0) * 1000)
        if dim == EMBEDDING_DIM:
            return {
                "name": "embedding_endpoint_returns_expected_dim",
                "passed": True,
                "duration_ms": duration,
                "detail": f"POST /v1/embeddings with model={model} returned {dim}-dim vector",
            }
        return {
            "name": "embedding_endpoint_returns_expected_dim",
            "passed": False,
            "duration_ms": duration,
            "error": f"Expected {EMBEDDING_DIM}-dim, got {dim}-dim",
            "remediation": f"Wrong embedding model. Expected a {EMBEDDING_DIM}-dim model; '{model}' returned {dim} dimensions.",
        }
    except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "embedding_endpoint_returns_expected_dim",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": f"Ensure Ollama is running and model '{model}' is pulled.",
        }


def _check_duckdb_present(
    duck_path: str,
    diag: dict[str, Any] | None = None,
    is_container: bool = False,
) -> dict[str, Any]:
    """Check 3: DuckDB file exists with fragment rows.

    When the service is running (``diag`` is non-None), defer to its
    telemetry-store readiness probe instead of opening the file directly —
    a concurrent open races the service for the file lock.

    In container mode, direct file access is not available on the host;
    the diagnostics endpoint is the only reliable source.
    """
    t0 = time.monotonic()
    if diag is not None:
        readiness = diag.get("dependency_readiness", {})
        status = readiness.get("telemetry_store")
        duration = int((time.monotonic() - t0) * 1000)
        if status == "ok":
            return {
                "name": "duckdb_present",
                "passed": True,
                "duration_ms": duration,
                "detail": "verified via /diagnostics/runtime (service holds DB lock)",
            }
        return {
            "name": "duckdb_present",
            "passed": False,
            "duration_ms": duration,
            "error": f"/diagnostics/runtime reports telemetry_store status={status!r}",
            "remediation": "Inspect server log; restart with `agentalloy server-restart`",
        }
    # Container fallback: direct file access not available on host
    if is_container:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "duckdb_present",
            "passed": False,
            "duration_ms": duration,
            "error": (
                "Service not reachable on the runtime port; "
                "cannot verify DuckDB without the diagnostics endpoint"
            ),
            "remediation": (
                "Check the container logs (e.g. `podman logs agentalloy`) "
                "for startup errors. The service may still be bootstrapping."
            ),
        }
    p = Path(duck_path)
    if not p.exists():
        return {
            "name": "duckdb_present",
            "passed": False,
            "duration_ms": 0,
            "error": f"{duck_path} not found",
            "remediation": "Run `python -m agentalloy.install seed-corpus`",
        }
    try:
        import duckdb

        con = duckdb.connect(str(p), read_only=True)
        count = con.execute("SELECT count(*) FROM fragment_embeddings").fetchone()[0]  # type: ignore[index]
        con.close()
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "duckdb_present",
            "passed": True,
            "duration_ms": duration,
            "detail": f"{duck_path} has {count} fragments",
        }
    except Exception as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "duckdb_present",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": "Run `python -m agentalloy.install seed-corpus`",
        }


def _check_ladybug_present(
    ladybug_path: str,
    diag: dict[str, Any] | None = None,
    is_container: bool = False,
) -> dict[str, Any]:
    """Check 4: Kuzu directory exists with Skill nodes.

    When the service is running (``diag`` is non-None), defer to its
    runtime-store readiness probe — Kuzu holds an exclusive lock that
    blocks a second open from this process.

    In container mode, direct file access is not available on the host;
    the diagnostics endpoint is the only reliable source.
    """
    t0 = time.monotonic()
    if diag is not None:
        readiness = diag.get("dependency_readiness", {})
        status = readiness.get("runtime_store")
        skill_count = len(diag.get("store_state", []))
        duration = int((time.monotonic() - t0) * 1000)
        if status == "ok":
            return {
                "name": "ladybug_present",
                "passed": True,
                "duration_ms": duration,
                "detail": (
                    f"verified via /diagnostics/runtime (service holds DB lock); "
                    f"{skill_count} active skills"
                ),
            }
        return {
            "name": "ladybug_present",
            "passed": False,
            "duration_ms": duration,
            "error": f"/diagnostics/runtime reports runtime_store status={status!r}",
            "remediation": "Inspect server log; restart with `agentalloy server-restart`",
        }
    # Container fallback: direct file access not available on host
    if is_container:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "ladybug_present",
            "passed": False,
            "duration_ms": duration,
            "error": (
                "Service not reachable on the runtime port; "
                "cannot verify Kuzu DB without the diagnostics endpoint"
            ),
            "remediation": (
                "Check the container logs (e.g. `podman logs agentalloy`) "
                "for startup errors. The service may still be bootstrapping."
            ),
        }
    p = Path(ladybug_path)
    if not p.exists():
        return {
            "name": "ladybug_present",
            "passed": False,
            "duration_ms": 0,
            "error": f"{ladybug_path} not found",
            "remediation": "Run `python -m agentalloy.install seed-corpus`",
        }
    try:
        import ladybug

        db = ladybug.Database(str(p))
        conn = ladybug.Connection(db)
        result = conn.execute("MATCH (s:Skill) RETURN count(s) AS c")
        count = 0
        if result.has_next():
            count = result.get_next()[0]
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "ladybug_present",
            "passed": True,
            "duration_ms": duration,
            "detail": f"{ladybug_path} has {count} skills",
        }
    except Exception as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "ladybug_present",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": "Run `python -m agentalloy.install seed-corpus`",
        }


def _check_skill_count(
    ladybug_path: str,
    diag: dict[str, Any] | None = None,
    is_container: bool = False,
) -> dict[str, Any]:
    """Check 5: Skill count >= MIN_SKILL_COUNT.

    When the service is running, count active skills via
    /diagnostics/runtime; otherwise fall back to a direct Kuzu read.

    In container mode, direct file access is not available on the host;
    the diagnostics endpoint is the only reliable source.
    """
    t0 = time.monotonic()
    if diag is not None:
        count = len(diag.get("store_state", []))
        duration = int((time.monotonic() - t0) * 1000)
        if count >= MIN_SKILL_COUNT:
            return {
                "name": "skill_count_meets_minimum",
                "passed": True,
                "duration_ms": duration,
                "detail": (f"{count} >= {MIN_SKILL_COUNT} (via /diagnostics/runtime)"),
            }
        return {
            "name": "skill_count_meets_minimum",
            "passed": False,
            "duration_ms": duration,
            "error": f"{count} < {MIN_SKILL_COUNT} (via /diagnostics/runtime)",
            "remediation": "Corpus is incomplete. Run `python -m agentalloy.install install-packs`",
        }
    # Container fallback: direct file access not available on host
    if is_container:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "skill_count_meets_minimum",
            "passed": False,
            "duration_ms": duration,
            "error": (
                "Service not reachable on the runtime port; "
                "cannot count skills without the diagnostics endpoint"
            ),
            "remediation": (
                "Check the container logs (e.g. `podman logs agentalloy`) "
                "for startup errors. The service may still be bootstrapping."
            ),
        }
    try:
        import ladybug

        db = ladybug.Database(str(ladybug_path))
        conn = ladybug.Connection(db)
        result = conn.execute("MATCH (s:Skill) RETURN count(s) AS c")
        count = 0
        if result.has_next():
            count = result.get_next()[0]
        duration = int((time.monotonic() - t0) * 1000)
        if count >= MIN_SKILL_COUNT:
            return {
                "name": "skill_count_meets_minimum",
                "passed": True,
                "duration_ms": duration,
                "detail": f"{count} >= {MIN_SKILL_COUNT} (MIN_SKILL_COUNT)",
            }
        return {
            "name": "skill_count_meets_minimum",
            "passed": False,
            "duration_ms": duration,
            "error": f"{count} < {MIN_SKILL_COUNT} (MIN_SKILL_COUNT)",
            "remediation": "Corpus is incomplete. Run `python -m agentalloy.install install-packs`",
        }
    except Exception as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "skill_count_meets_minimum",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": "Run `python -m agentalloy.install seed-corpus`",
        }


def _check_harness_config_present(st: dict[str, Any]) -> dict[str, Any]:
    """Check 6: Harness config file contains the sentinel block."""
    t0 = time.monotonic()
    files_written = st.get("harness_files_written", [])
    # harness_files_written is empty if: (a) user chose "manual" harness,
    # (b) wire-harness was never run. We assume (a) since simple_setup
    # skips wire-harness when harness=="manual".
    if not files_written:
        return {
            "name": "harness_config_present",
            "passed": True,
            "duration_ms": 0,
            "detail": "manual harness — user-owned config, no files to verify",
        }
    for entry in files_written:
        fp = Path(entry["path"])
        if not fp.exists():
            duration = int((time.monotonic() - t0) * 1000)
            return {
                "name": "harness_config_present",
                "passed": False,
                "duration_ms": duration,
                "error": f"File not found: {entry['path']}",
                "remediation": "Re-run `python -m agentalloy.install wire-harness`",
            }
        content = fp.read_text()
        # sentinel_begin is null for dedicated files (we own the whole file),
        # so only check for sentinel presence when one was recorded.
        sentinel = entry.get("sentinel_begin")
        if sentinel and sentinel not in content:
            duration = int((time.monotonic() - t0) * 1000)
            return {
                "name": "harness_config_present",
                "passed": False,
                "duration_ms": duration,
                "error": f"Sentinel block missing from {entry['path']}",
                "remediation": "Re-run `python -m agentalloy.install wire-harness`",
            }
    duration = int((time.monotonic() - t0) * 1000)
    path = files_written[0]["path"]
    return {
        "name": "harness_config_present",
        "passed": True,
        "duration_ms": duration,
        "detail": f"{path} contains agentalloy sentinel block",
    }


def _check_harness_config_url(st: dict[str, Any]) -> dict[str, Any]:
    """Check 7: Injected URL matches the configured port."""
    t0 = time.monotonic()
    port = install_state.validate_port(st.get("port", 47950))
    expected_url = f"http://localhost:{port}"
    files_written = st.get("harness_files_written", [])
    # harness_files_written is empty if: (a) user chose "manual" harness,
    # (b) wire-harness was never run. We assume (a) since simple_setup
    # skips wire-harness when harness=="manual".
    if not files_written:
        return {
            "name": "harness_config_url_matches",
            "passed": True,
            "duration_ms": 0,
            "detail": "manual harness — user-owned config, URL not verified",
        }
    for entry in files_written:
        fp = Path(entry["path"])
        if fp.exists():
            content = fp.read_text()
            if expected_url in content:
                duration = int((time.monotonic() - t0) * 1000)
                return {
                    "name": "harness_config_url_matches",
                    "passed": True,
                    "duration_ms": duration,
                    "detail": f"Injected URL {expected_url} matches configured port",
                }
    duration = int((time.monotonic() - t0) * 1000)
    return {
        "name": "harness_config_url_matches",
        "passed": False,
        "duration_ms": duration,
        "error": f"Expected URL {expected_url} not found in harness config",
        "remediation": "Re-run wire-harness; port may have changed since last wiring",
    }


def _check_port_available(port: int) -> dict[str, Any]:
    """Check 8: Port is free or already bound by agentalloy."""
    t0 = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            result = s.connect_ex(("127.0.0.1", port))
        duration = int((time.monotonic() - t0) * 1000)
        if result != 0:
            # Port is free
            return {
                "name": "runtime_port_available",
                "passed": True,
                "duration_ms": duration,
                "detail": f"Port {port} is available",
            }
        # Port is in use — check if it's agentalloy via /health
        try:
            req = Request(f"http://localhost:{port}/health", method="GET")
            with urlopen(req, timeout=5) as resp:  # noqa: S310
                body = json.loads(resp.read())
            # /health uses three-state status: "healthy" (all-green),
            # "degraded" (embed/telemetry down but service is bound), or
            # "unavailable". Both healthy and degraded mean a agentalloy
            # server holds the port — the check's actual question.
            if body.get("status") in ("healthy", "degraded"):
                return {
                    "name": "runtime_port_available",
                    "passed": True,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "detail": (
                        f"Port {port} is already bound by agentalloy "
                        f"(status={body.get('status')!r})"
                    ),
                }
            # /health responded but with non-ok status — foreign service
            return {
                "name": "runtime_port_available",
                "passed": False,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": (
                    f"Port {port} is bound by a service that returned status={body.get('status')!r}"
                ),
                "remediation": (
                    f"Stop the foreign service on port {port}, or re-run "
                    f"`python -m agentalloy.install write-env --port <other-port>` "
                    f"and `wire-harness` to reconfigure for a different port."
                ),
            }
        except json.JSONDecodeError as exc:
            # /health responded but body wasn't JSON — likely agentalloy
            # serving an older/error response, or a foreign service
            # returning HTML. Don't tell the user to kill the process
            # without diagnostics; surface the parse failure directly.
            return {
                "name": "runtime_port_available",
                "passed": False,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": f"Port {port} /health returned non-JSON response: {exc}",
                "remediation": (
                    f"Probe `curl http://localhost:{port}/health` to see the response. "
                    f"If it's agentalloy, restart the service; if foreign, free the port."
                ),
            }
        except Exception as exc:
            # urlopen URLError, ConnectionRefusedError, timeout, etc. —
            # something accepted the TCP connect but isn't speaking HTTP
            # the way we expect.
            return {
                "name": "runtime_port_available",
                "passed": False,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": f"Port {port} is bound by a non-agentalloy process ({exc})",
                "remediation": (
                    f"Stop the foreign process on port {port} (try `lsof -i :{port}` to identify it), "
                    f"or re-run `python -m agentalloy.install write-env --port <other-port>` "
                    f"and `wire-harness` to reconfigure."
                ),
            }
    except OSError as exc:
        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "runtime_port_available",
            "passed": False,
            "duration_ms": duration,
            "error": str(exc),
            "remediation": f"Port {port} check failed",
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


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


def _check_bootstrap_in_progress(port: int) -> dict[str, Any] | None:
    """Return a short-circuit verify result if the container is mid-bootstrap.

    Calls ``/readiness`` on the local API port. Behavior:

    * ``warming_up``  → returns a ``bootstrap_in_progress`` result so the
                        caller can skip the full check suite (which would
                        otherwise misreport "port bound by non-agentalloy"
                        and "diagnostics unreachable" as failures).
    * ``error``       → returns a ``bootstrap_error`` result with details.
    * ``ready``       → returns None (proceed with normal checks).
    * Connection refused / timeout → returns None. We can't distinguish
                        "service down" from "service starting" from this
                        signal alone; the normal checks will surface the
                        correct error in either case.
    """
    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = f"http://127.0.0.1:{port}/readiness"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310
            body = _json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, _json.JSONDecodeError):
        return None

    status = body.get("status")
    progress = body.get("progress") or {}
    if status == "warming_up":
        return {
            "schema_version": SCHEMA_VERSION,
            "all_checks_passed": False,
            "status": "bootstrap_in_progress",
            "guidance": (
                "Bootstrap is still in progress. The service is warming up — "
                "please wait a few more minutes and run `agentalloy verify` again."
            ),
            "progress": progress,
            "checks": [],
        }
    if status == "error":
        return {
            "schema_version": SCHEMA_VERSION,
            "all_checks_passed": False,
            "status": "bootstrap_error",
            "guidance": (
                "Bootstrap reported an error. Inspect the container log "
                "(e.g. `podman logs agentalloy`) for details."
            ),
            "progress": progress,
            "checks": [],
        }
    return None


def run_checks(st: dict[str, Any], root: Path | None = None) -> dict[str, Any]:  # noqa: ARG001
    """Run all 8 verify checks and return the contract-shaped result."""
    # Container deployments may be in fast-start bootstrap — the API is up
    # but packs haven't been ingested yet. Short-circuit on warming_up so the
    # user sees "wait, bootstrap in progress" instead of a wall of false
    # "port bound by other process" failures.
    if st.get("deployment") == "container":
        port = install_state.validate_port(st.get("port", 47950))
        bootstrap_result = _check_bootstrap_in_progress(port)
        if bootstrap_result is not None:
            return bootstrap_result

    env = _read_env_values(install_state.user_config_dir())

    embed_url = env.get("RUNTIME_EMBED_BASE_URL", "http://localhost:47951")
    embed_model = env.get("RUNTIME_EMBEDDING_MODEL", "nomic-embed-text-v1.5.Q8_0.gguf")
    user_corpus = install_state.corpus_dir()
    duck_path = env.get("DUCKDB_PATH", str(user_corpus / "skills.duck"))
    ladybug_path = env.get("LADYBUG_DB_PATH", str(user_corpus / "ladybug"))
    port = install_state.validate_port(st.get("port", 47950))

    # Resolve relative paths against the user corpus dir (not the cwd) —
    # the service no longer assumes a project-relative working directory.
    if not Path(duck_path).is_absolute():
        duck_path = str(user_corpus / duck_path)
    if not Path(ladybug_path).is_absolute():
        ladybug_path = str(user_corpus / ladybug_path)

    # Probe the running service once. When it's up, the three DB checks
    # below skip the direct file open (which races the service for the
    # Kuzu file lock) and use the diagnostics response instead.
    diag = _probe_diagnostics(port)

    # Container deployments: the host can't reach the embedder directly
    # (Ollama runs inside the agentalloy container). Both embedding checks
    # collapse to a single source — the embedding_runtime dep status exposed
    # via /diagnostics/runtime, which the agentalloy service computes from
    # its actual embed probes inside the container.
    is_container = st.get("deployment") == "container"
    if is_container:
        embed_checks = [
            _check_embedding_via_diagnostics(diag, "embedding_endpoint_reachable"),
            _check_embedding_via_diagnostics(diag, "embedding_endpoint_returns_expected_dim"),
        ]
    else:
        embed_checks = [
            _check_embedding_endpoint_reachable(embed_url),
            _check_embedding_expected_dim(embed_url, embed_model),
        ]

    checks = [
        *embed_checks,
        _check_duckdb_present(duck_path, diag=diag, is_container=is_container),
        _check_ladybug_present(ladybug_path, diag=diag, is_container=is_container),
        _check_skill_count(ladybug_path, diag=diag, is_container=is_container),
        _check_harness_config_present(st),
        _check_harness_config_url(st),
        _check_port_available(port),
    ]

    all_passed = all(c["passed"] for c in checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "all_checks_passed": all_passed,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p: argparse.ArgumentParser = subparsers.add_parser(
        "verify",
        help="Install-time smoke test (embed → retrieve → 768-dim, harness config, etc.).",
    )
    add_json_flag(p)
    p.set_defaults(func=run)


def _render_human(result: dict[str, Any]) -> None:
    """Render verify check results in human-readable format."""
    from agentalloy.install.output import render_checklist

    render_checklist(result, title="Verification")


def run(args: argparse.Namespace) -> int:
    """Execute the verify subcommand."""
    st = install_state.load_state()

    result = run_checks(st)

    fp, digest = install_state.save_output_file(result, "verify.json")

    if result["all_checks_passed"]:
        install_state.record_step(
            st,
            "verify",
            extra={
                "output_digest": digest,
                "output_path": str(fp),
                "all_checks_passed": True,
            },
        )
        from datetime import UTC, datetime

        st["last_verify_passed_at"] = datetime.now(UTC).isoformat()
        install_state.save_state(st)

    write_result(result, args, human_fn=_render_human)
    return 0 if result["all_checks_passed"] else 1
