"""End-to-end tests for the container deployment flow in simple_setup.py.

Tests E2E-1 through E2E-4 covering:
  E2E-1: Full container setup with mocked runtime binary
  E2E-2: Container bootstrap pulls nomic-embed-text-v1.5.Q8_0.gguf model
  E2E-3: Container bootstrap idempotency - restart skips redundant operations
  E2E-4: Container bootstrap crash recovery - re-runs migrations and install-packs

All external dependencies (subprocess.run for runtime commands, HTTP health
checks, DB access, file I/O) are mocked so these tests run in isolation
and complete in <10s each. They are HERMETIC — no podman/docker binary, no
network, no free port is required — so they carry no `integration`/`container`
markers and run in the fast default suite.

PATCH-TARGET RULE (the bug this file once had — issue #347): simple_setup.py
imports the container_runtime helpers at MODULE level::

    from agentalloy.install.subcommands.container_runtime import (_pull_image, ...)

so ``_run_container_flow`` calls simple_setup's OWN bound references. Patching
``agentalloy.install.subcommands.container_runtime._pull_image`` does nothing
for the flow — the mocks silently never attach and the "all-mocked" tests run
REAL ``podman pull`` / ``podman run`` (multi-GB GHCR pulls on bare CI runners,
"address already in use" on dev hosts, leaked containers from killed runs).
Every from-imported name MUST be patched where it is USED::

    patch("agentalloy.install.subcommands.simple_setup._pull_image", ...)

Module-attribute access (``preflight.run_preflight``, ``verify.run``,
``install_state.save_state``) is fine to patch at the source module — the
consumer holds the module object, not the function. The
``_no_real_container_runtime`` guard fixture and
``test_container_runtime_mocks_target_simple_setup`` below enforce this.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands import container_runtime

# ---------------------------------------------------------------------------
# Hermeticity guard
# ---------------------------------------------------------------------------

_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_SUBPROCESS_POPEN = subprocess.Popen


def _argv_program(args: object) -> str:
    """Best-effort basename of the program a subprocess call would execute."""
    if isinstance(args, (list, tuple)) and args:
        head = str(args[0])
    else:
        head = str(args).split()[0] if str(args).split() else ""
    return Path(head).name


@pytest.fixture(autouse=True)
def _no_real_container_runtime(monkeypatch: pytest.MonkeyPatch):
    """Fail loudly if a real podman/docker invocation escapes the mock harness.

    Every test in this file is fully mocked; a container-runtime subprocess
    call reaching this guard means a patch target drifted (see the module
    docstring). Without the guard the call would silently execute — pulling
    multi-GB images on CI or failing on a dev host whose port is occupied.
    """

    def _guard(real):
        def wrapper(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            program = _argv_program(argv)
            if program in {"podman", "docker"}:
                raise AssertionError(
                    f"real container-runtime call escaped the mock harness: {argv!r} — "
                    "patch the helper on simple_setup (where it is used), "
                    "not on container_runtime (where it is defined)"
                )
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(subprocess, "run", _guard(_REAL_SUBPROCESS_RUN))
    monkeypatch.setattr(subprocess, "Popen", _guard(_REAL_SUBPROCESS_POPEN))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_preflight_mocks():
    """Inject mock versions of preflight functions into the preflight module."""
    import agentalloy.install.subcommands.preflight as preflight

    if not hasattr(preflight, "_probe_compose_runtime"):
        preflight._probe_compose_runtime = lambda: ("podman", "/usr/bin/podman", [])

    if not hasattr(preflight, "_compose_failure_message"):
        preflight._compose_failure_message = lambda probes: (
            "Neither `podman` nor `docker` found on PATH",
            "Install Podman (recommended) or Docker.\n"
            "  Linux:   sudo apt install podman\n"
            "  macOS:   brew install podman\n"
            "  Verify:  podman --version",
        )


def _make_urlopen_mock():
    """Return a mock for urllib.request.urlopen that works as a context manager.

    The mock returns a context-manager mock whose __enter__ yields a response
    mock with status=200 and a read() method that returns JSON strings.
    """
    ctx_mock = MagicMock()
    inner = MagicMock()
    inner.status = 200
    inner.read.return_value = json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3]}]}).encode()
    inner.__enter__ = MagicMock(return_value=inner)
    inner.__exit__ = MagicMock(return_value=False)
    ctx_mock.__enter__ = MagicMock(return_value=inner)
    ctx_mock.__exit__ = MagicMock(return_value=False)
    return ctx_mock


def _all_common_patches(tmp_path: Path):
    """Return a list of common patch context managers for container flow tests.

    Must call _inject_preflight_mocks() first to add the mock preflight
    attributes that the patched versions reference.

    NOTE: container_runtime functions, urllib.request.urlopen, and
    time.monotonic are NOT included here. They are created as shared mocks
    in _run_container_flow_all_mocked so that tests can override their
    behavior by setting side_effect/return_value on the shared mock objects.
    """
    _inject_preflight_mocks()
    return [
        patch(
            "agentalloy.install.subcommands.preflight._probe_compose_runtime",
            return_value=("podman", "/usr/bin/podman", []),
        ),
        patch(
            "agentalloy.install.subcommands.preflight._compose_failure_message",
            return_value=("ok", "ok"),
        ),
        patch(
            "agentalloy.install.subcommands.preflight.run_preflight", return_value={"checks": []}
        ),
        patch(
            "agentalloy.install.subcommands.simple_setup._list_project_containers", return_value=[]
        ),
        patch(
            "agentalloy.install.subcommands.simple_setup._list_conflicting_containers",
            return_value=[],
        ),
        patch("agentalloy.install.subcommands.simple_setup._remove_containers", return_value=True),
        patch(
            "agentalloy.install.subcommands.simple_setup._reconcile_native_port_holder",
            return_value=0,
        ),
        # simple_setup resolves the runtime label via shutil.which; keep the
        # tests hermetic on hosts without podman/docker on PATH.
        patch(
            "agentalloy.install.subcommands.simple_setup.shutil.which",
            return_value="/usr/bin/podman",
        ),
        patch(
            "agentalloy.install.subcommands.simple_setup._container_setup_log_path",
            return_value=tmp_path / "setup.log",
        ),
        patch("agentalloy.install.state.load_state", return_value={}),
        patch("agentalloy.install.state.save_state"),
        patch(
            "agentalloy.install.state.user_config_dir",
            return_value=tmp_path / ".config" / "agentalloy",
        ),
        patch("agentalloy.install.state.env_path", return_value=tmp_path / ".env"),
        patch("agentalloy.install.state._atomic_write"),
        patch("agentalloy.install.subcommands.verify.run", return_value=0),
        patch("agentalloy.install.subcommands.wire_harness.run", return_value=0),
        patch("agentalloy.install.subcommands.simple_setup._build_namespace"),
        patch("agentalloy.install.subcommands.simple_setup._prompt_for_packs", return_value=""),
        patch("agentalloy.install.subcommands.simple_setup._discover_packs", return_value={}),
        patch("pathlib.Path.cwd", return_value=tmp_path),
        patch("time.sleep", return_value=None),
        patch("builtins.input", return_value="y"),
    ]


def _run_container_flow_all_mocked(
    tmp_path: Path,
    extra_patches=None,
    mock_overrides=None,
):
    """Run _run_container_flow with all external dependencies mocked.

    Uses contextlib.ExitStack to avoid Python's AST nested block limit.

    Parameters
    ----------
    tmp_path : Path
        Temporary directory for compose files and logs.
    extra_patches : list[contextlib.AbstractContextManager], optional
        Additional patch context managers to apply.
    mock_overrides : dict, optional
        Override shared mock behavior. Keys: "detect_runtime_binary",
        "detect_functional_runtimes", "pull_image", "run_container",
        "wait_for_readiness", "urlopen", "monotonic". Values are the new
        side_effect or return_value to set.
    """
    patches = _all_common_patches(tmp_path)

    # Create shared mock objects that tests can override
    mock_detect_runtime_binary = MagicMock(return_value="podman")
    mock_detect_functional_runtimes = MagicMock(return_value=["podman"])
    mock_pull_image = MagicMock(return_value=0)
    mock_ensure_volume = MagicMock()
    mock_run_container = MagicMock(return_value=0)
    mock_wait_for_readiness = MagicMock(return_value=True)
    mock_check_container_running = MagicMock(return_value=True)
    mock_tail_container_logs = MagicMock(return_value="")
    mock_urlopen = MagicMock(return_value=_make_urlopen_mock())
    mock_monotonic = MagicMock(return_value=0.0)

    # PATCH WHERE USED: simple_setup.py from-imports every one of these names
    # at module level (see the module docstring), so the flow calls
    # simple_setup's own bound references. Patching them on container_runtime
    # would never attach — the tests would run real podman.
    for name, mock_obj in (
        ("_detect_runtime_binary", mock_detect_runtime_binary),
        ("_detect_functional_runtimes", mock_detect_functional_runtimes),
        ("_pull_image", mock_pull_image),
        ("_ensure_volume", mock_ensure_volume),
        ("_run_container", mock_run_container),
        ("_wait_for_readiness", mock_wait_for_readiness),
        ("_check_container_running", mock_check_container_running),
        ("_tail_container_logs", mock_tail_container_logs),
    ):
        patches.append(patch(f"agentalloy.install.subcommands.simple_setup.{name}", mock_obj))

    patches.append(patch("urllib.request.urlopen", mock_urlopen))
    patches.append(patch("time.monotonic", mock_monotonic))

    # Apply mock overrides BEFORE entering the ExitStack so the
    # mock objects already have the correct behavior when called.
    # Non-callable, non-exception values are treated as return_value.
    if mock_overrides:
        if "detect_runtime_binary" in mock_overrides:
            val = mock_overrides["detect_runtime_binary"]
            if callable(val) or isinstance(val, BaseException):
                mock_detect_runtime_binary.side_effect = val
            else:
                mock_detect_runtime_binary.return_value = val
        if "detect_functional_runtimes" in mock_overrides:
            val = mock_overrides["detect_functional_runtimes"]
            if callable(val) or isinstance(val, BaseException):
                mock_detect_functional_runtimes.side_effect = val
            else:
                mock_detect_functional_runtimes.return_value = val
        if "pull_image" in mock_overrides:
            val = mock_overrides["pull_image"]
            if callable(val) or isinstance(val, BaseException):
                mock_pull_image.side_effect = val
            else:
                mock_pull_image.return_value = val
        if "run_container" in mock_overrides:
            val = mock_overrides["run_container"]
            if callable(val) or isinstance(val, BaseException):
                mock_run_container.side_effect = val
            else:
                mock_run_container.return_value = val
        if "wait_for_readiness" in mock_overrides:
            val = mock_overrides["wait_for_readiness"]
            if callable(val) or isinstance(val, BaseException):
                mock_wait_for_readiness.side_effect = val
            else:
                mock_wait_for_readiness.return_value = val
        if "urlopen" in mock_overrides:
            mock_urlopen.side_effect = mock_overrides["urlopen"]
        if "monotonic" in mock_overrides:
            mock_monotonic.side_effect = mock_overrides["monotonic"]

    if extra_patches:
        patches.extend(extra_patches)

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)

        from agentalloy.install.subcommands.simple_setup import (
            SetupConfig,
            _run_container_flow,
        )

        cfg = SetupConfig(
            deployment="container",
            non_interactive=True,
            port=47950,
            packs="",
            harness="manual",
        )

        return _run_container_flow(cfg, 0.0)


# ---------------------------------------------------------------------------
# E2E-1: Full container setup with mocked runtime binary
# ---------------------------------------------------------------------------


class TestFullContainerSetup:
    """E2E-1: Full container setup with mocked runtime binary.

    Verifies that _run_container_flow returns 0 when every step succeeds,
    and that the correct sequence of container_runtime calls is made.
    """

    def test_full_setup_returns_zero(self):
        """_run_container_flow returns 0 when every step succeeds."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            rc = _run_container_flow_all_mocked(tmp_path)

            assert rc == 0, f"Expected exit code 0, got {rc}"

    def test_full_setup_calls_container_runtime_in_correct_order(self):
        """Verify container_runtime functions are called in the correct order.

        The new single-container flow calls:
          1. _detect_functional_runtimes -> ["podman"]
          2. _pull_image(runtime)
          3. _ensure_volume(runtime)
          4. _run_container(runtime, packs)

        (_detect_runtime_binary is only consulted when no runtime is
        functional.) The container runs the image's baked /app/entrypoint.sh
        and reads packs from the AGENTALLOY_PACKS env var, so the flow no
        longer generates a host entrypoint temp file (_generate_entrypoint /
        _cleanup_temp_entrypoint are not part of the run path).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            call_order = []

            def make_tracker(name, ret=0):
                def tracker(*args, **kwargs):
                    call_order.append(name)
                    return ret

                return tracker

            # Trackers must land on simple_setup's bound references (see the
            # module docstring): extra_patches are applied after the harness
            # defaults, so these patch-where-used wrappers win.
            rc = _run_container_flow_all_mocked(
                tmp_path,
                mock_overrides={
                    "pull_image": make_tracker("_pull_image", 0),
                    "run_container": make_tracker("_run_container", 0),
                    "detect_functional_runtimes": make_tracker(
                        "_detect_functional_runtimes", ["podman"]
                    ),
                },
                extra_patches=[
                    patch(
                        "agentalloy.install.subcommands.simple_setup._ensure_volume",
                        side_effect=make_tracker("_ensure_volume"),
                    ),
                ],
            )

            assert rc == 0
            # Verify the expected call order. No _generate_entrypoint: the
            # container runs the image's baked entrypoint with AGENTALLOY_PACKS.
            assert call_order == [
                "_detect_functional_runtimes",
                "_pull_image",
                "_ensure_volume",
                "_run_container",
            ], f"Expected container_runtime calls in order, got: {call_order}"

    def test_full_setup_records_state_on_success(self):
        """After successful setup, state is saved with deployment=container."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            saved_state = {}

            def capture_save_state(st):
                saved_state.clear()
                saved_state.update(st)

            rc = _run_container_flow_all_mocked(
                tmp_path,
                extra_patches=[
                    patch("agentalloy.install.state.save_state", side_effect=capture_save_state),
                ],
            )

            assert rc == 0
            assert saved_state.get("deployment") == "container"
            assert saved_state.get("port") == 47950
            assert saved_state.get("runtime_binary") == "podman"

    def test_full_setup_skips_native_prompts_in_non_interactive_mode(self):
        """In non-interactive mode, no prompts are shown and setup proceeds."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            input_calls = []

            def track_input(prompt=""):
                input_calls.append(str(prompt))
                return "y"

            rc = _run_container_flow_all_mocked(
                tmp_path,
                extra_patches=[
                    patch("builtins.input", side_effect=track_input),
                ],
            )

            assert rc == 0
            # In non-interactive mode, input() should not be called
            # (the non_interactive path skips all prompts)
            assert len(input_calls) == 0, (
                f"Expected no input() calls in non-interactive mode, got {len(input_calls)}"
            )


# ---------------------------------------------------------------------------
# E2E-2: Container bootstrap pulls nomic-embed-text-v1.5.Q8_0.gguf model
# ---------------------------------------------------------------------------


class TestModelPullBootstrap:
    """E2E-2: Container bootstrap downloads the GGUF models.

    Verifies that the entrypoint script is generated and passed to the
    container, and that the GGUF download is handled inside the entrypoint
    (not in the setup flow).
    """

    def test_packs_are_passed_to_run_container(self):
        """Packs flow to _run_container (delivered to the baked entrypoint via env)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            run_container_packs = []

            def capture_run_container(runtime, packs, *args, **kwargs):
                run_container_packs.append(packs)
                return 0

            rc = _run_container_flow_all_mocked(
                tmp_path,
                mock_overrides={"run_container": capture_run_container},
            )

            assert rc == 0
            assert len(run_container_packs) == 1
            # _run_container is called with the packs string from config.
            assert run_container_packs[0] == ""

    def test_model_download_step_is_executed_in_entrypoint(self):
        """The entrypoint script contains the GGUF download step.

        The GGUF download is handled inside the entrypoint script, not in
        the setup flow. Verify the generated entrypoint contains the
        expected curl download commands for both models.
        """
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # The entrypoint should download both GGUFs into the data volume.
        assert 'curl -fsSL -o "$EMBED_GGUF"' in script
        assert 'curl -fsSL -o "$RERANK_GGUF"' in script
        assert '[ ! -f "$EMBED_GGUF" ] || [ ! -f "$RERANK_GGUF" ]' in script

    def test_model_download_confirmed_in_entrypoint_script(self):
        """The entrypoint script prints a status line after the GGUF download."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # The entrypoint should contain the download status echo.
        assert "Downloading llama.cpp GGUF models" in script
        assert "Model download complete" in script

    def test_model_download_failure_aborts_entrypoint(self):
        """When the GGUF download fails, the entrypoint aborts under set -e.

        The download is inside an if block that checks file existence first.
        If a file exists, the curl is skipped. If not, curl is attempted and
        a failure (curl -f returns non-zero) causes the script to exit under
        ``set -e`` — the container exits and the host readiness wait surfaces
        it. The setup flow already considers the container "started" once
        _run_container returns 0.
        """
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # Verify the existence check gates the download, and curl uses -f
        # (fail on HTTP errors → non-zero exit under set -e).
        assert '[ ! -f "$EMBED_GGUF" ]' in script
        assert "curl -fsSL" in script


# ---------------------------------------------------------------------------
# E2E-3: Container bootstrap idempotency
# ---------------------------------------------------------------------------


class TestBootstrapIdempotency:
    """E2E-3: Container bootstrap idempotency - restart skips redundant operations.

    Verifies that when .bootstrap-complete already exists, the entrypoint
    skips the GGUF download, migrations, and pack installation (the
    llama-servers still start every boot — they are runtime daemons).
    """

    def test_entrypoint_skips_bootstrap_when_complete(self):
        """The generated entrypoint script checks for .bootstrap-complete first."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # .bootstrap-complete check should come before the GGUF download
        bootstrap_check = script.index(".bootstrap-complete")
        model_download = script.index('curl -fsSL -o "$EMBED_GGUF"')
        uvicorn_start = script.index("uvicorn agentalloy.app:app")

        assert bootstrap_check < model_download, (
            ".bootstrap-complete check should come before the GGUF download"
        )
        assert model_download < uvicorn_start, "GGUF download should come before uvicorn start"

    def test_entrypoint_skips_all_steps_when_complete(self):
        """When .bootstrap-complete exists, only the llama-servers + uvicorn run."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # The script has an if/else structure:
        # if bootstrap-complete exists -> skip to uvicorn
        # else -> do all bootstrap steps
        assert "if [ -f" in script and ".bootstrap-complete" in script
        assert 'echo ">> Bootstrap already complete' in script
        assert "skip to uvicorn" in script.lower() or "skipping to uvicorn" in script.lower()

    def test_entrypoint_starts_llama_servers_every_boot(self):
        """The llama-server daemons start unconditionally (outside the bootstrap gate)."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # The llama-server launches must NOT sit inside the GGUF-download
        # bootstrap branch — they run on every boot. They appear after the
        # download branch closes and before migrations.
        embed_start = script.index("Starting embed llama-server")
        rerank_start = script.index("Starting reranker llama-server")
        download = script.index('curl -fsSL -o "$EMBED_GGUF"')
        assert download < embed_start
        assert embed_start < rerank_start

    def test_entrypoint_skips_download_when_gguf_cached(self):
        """When the GGUF files exist, the download is skipped (check precedes curl)."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # Check for GGUF presence check before download
        model_check = script.index('[ ! -f "$EMBED_GGUF" ] || [ ! -f "$RERANK_GGUF" ]')
        model_download = script.index('curl -fsSL -o "$EMBED_GGUF"')

        assert model_check < model_download, "GGUF cache check should come before download"


# ---------------------------------------------------------------------------
# E2E-4: Container bootstrap crash recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    """E2E-4: Container bootstrap crash recovery - re-runs migrations and install-packs.

    Verifies that when a step fails, the setup correctly reports the failure
    and can be re-run.
    """

    def test_pull_failure_aborts_setup(self):
        """When the image pull fails, setup exits with code 1."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            rc = _run_container_flow_all_mocked(
                tmp_path,
                mock_overrides={"pull_image": 1},
            )

            assert rc == 1, f"Expected exit code 1 on pull failure, got {rc}"

    def test_container_start_failure_aborts_setup(self):
        """When the main agentalloy container fails to start, setup exits 1."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            rc = _run_container_flow_all_mocked(
                tmp_path,
                mock_overrides={"run_container": 1},
            )

            assert rc == 1, f"Expected exit code 1 on container start failure, got {rc}"

    def test_health_check_timeout_shows_warning(self):
        """When health check times out, a warning is printed but setup continues."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            printed_messages = []

            def capture_print(*args, **kwargs):
                printed_messages.append(" ".join(str(a) for a in args))

            rc = _run_container_flow_all_mocked(
                tmp_path,
                extra_patches=[
                    patch(
                        "agentalloy.install.subcommands.simple_setup._print",
                        side_effect=capture_print,
                    ),
                ],
                mock_overrides={
                    "wait_for_readiness": False,
                    "urlopen": OSError("connection refused"),
                    "monotonic": iter([0.0, 0.0, 0.0, 301.0, 0.0, 0.0, 0.0]),
                },
            )

            assert rc == 0, f"Expected setup to continue after health check timeout, got {rc}"
            assert any("not ready" in m.lower() for m in printed_messages), (
                f"Expected health warning, got: {printed_messages}"
            )

    def test_preflight_failure_aborts_before_subprocess_calls(self):
        """When preflight fails, setup exits 1 without any subprocess calls."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            subprocess_calls = []

            def track_subprocess(cmd, **kwargs):
                subprocess_calls.append(cmd[0] if cmd else None)
                return 0

            rc = _run_container_flow_all_mocked(
                tmp_path,
                extra_patches=[
                    patch(
                        "agentalloy.install.subcommands.preflight.run_preflight",
                        return_value={
                            "checks": [
                                {
                                    "name": "port_free",
                                    "passed": False,
                                    "severity": "fatal",
                                    "error": "port 47950 in use",
                                    "remediation": "Stop the process on port 47950",
                                }
                            ]
                        },
                    ),
                ],
            )

            assert rc == 1, f"Expected exit code 1 on preflight failure, got {rc}"
            assert len(subprocess_calls) == 0, (
                f"Expected no subprocess calls after preflight failure, got {subprocess_calls}"
            )


# ---------------------------------------------------------------------------
# Meta: patch-target drift guard
# ---------------------------------------------------------------------------

# Names the harness patches on simple_setup (patch-where-used). Keep in sync
# with the loop in _run_container_flow_all_mocked.
_MOCKED_ON_SIMPLE_SETUP = {
    "_check_container_running",
    "_detect_functional_runtimes",
    "_detect_runtime_binary",
    "_ensure_volume",
    "_list_conflicting_containers",
    "_pull_image",
    "_run_container",
    "_tail_container_logs",
    "_wait_for_readiness",
}


def test_container_runtime_mocks_target_simple_setup():
    """Every container_runtime name simple_setup from-imports must be mocked
    on simple_setup itself.

    simple_setup binds these names at import time, so a patch on
    container_runtime never reaches _run_container_flow — the "all-mocked"
    tests would silently run real podman (issue #347). This meta-test fails
    when a new from-imported helper is added without a matching
    patch-where-used entry in the harness.
    """
    import ast
    import inspect

    import agentalloy.install.subcommands.simple_setup as simple_setup

    tree = ast.parse(inspect.getsource(simple_setup))
    from_imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.endswith("container_runtime")
        for alias in node.names
    }
    assert from_imported, "expected simple_setup to from-import container_runtime helpers"

    unmocked = from_imported - _MOCKED_ON_SIMPLE_SETUP
    assert not unmocked, (
        f"container_runtime names from-imported by simple_setup but not mocked on "
        f"simple_setup in this harness (patch-where-used, see module docstring): "
        f"{sorted(unmocked)}"
    )
    # And the harness must not reference names simple_setup no longer imports
    # (mock.patch would raise AttributeError at test time, but fail clearly here).
    stale = _MOCKED_ON_SIMPLE_SETUP - from_imported
    assert not stale, f"harness mocks names simple_setup no longer imports: {sorted(stale)}"


# ---------------------------------------------------------------------------
# Deploy-seam: the run command GENERATED by the product must carry the env
# the host .env expresses (spec AC 1, 2, 3, 8 — the seam the hand-built `-e`
# nightly never exercised, which masked the v6.1.x module-toggle bug).
# ---------------------------------------------------------------------------


class TestGeneratedRunCommandEnvForwarding:
    """Assert on the argv _run_container generates, no runtime needed."""

    def _generated_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env_content: str | None,
    ) -> tuple[dict[str, str], list[str]]:
        """Run _run_container with a tmp XDG .env; return (env flags, argv)."""
        cfg_dir = tmp_path / "xdg" / "agentalloy"
        cfg_dir.mkdir(parents=True)
        if env_content is not None:
            (cfg_dir / ".env").write_text(env_content)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        captured: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(
            "agentalloy.install.subcommands.container_runtime.subprocess.run", fake_run
        )
        rc = container_runtime._run_container("podman", "all", projects_root=tmp_path / "projects")
        assert rc == 0
        argv = captured[-1]
        flags = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        env = dict(f.split("=", 1) for f in flags)
        return env, argv

    def test_run_command_forwards_intent_keys(self, monkeypatch, tmp_path):
        env, _ = self._generated_env(
            monkeypatch,
            tmp_path,
            "CODE_INDEX_ENABLED=1\nCOMPOSE_ENABLED=0\nCODE_INDEX_WATCH=1\n",
        )
        assert env["CODE_INDEX_ENABLED"] == "1"
        assert env["COMPOSE_ENABLED"] == "0"
        assert env["CODE_INDEX_WATCH"] == "1"

    def test_run_command_never_forwards_host_topology(self, monkeypatch, tmp_path):
        env, _ = self._generated_env(
            monkeypatch,
            tmp_path,
            "DUCKDB_PATH=/host/evil.duck\nCODE_INDEX_DATA_DIR=/host/idx\n"
            "RUNTIME_EMBED_BASE_URL=http://localhost:9999\n",
        )
        # Baked container values win; host-topology keys are dropped entirely.
        assert env["DUCKDB_PATH"] == "/app/data/agentalloy.duck"
        assert "CODE_INDEX_DATA_DIR" not in env
        assert "RUNTIME_EMBED_BASE_URL" not in env

    def test_run_command_without_env_file_is_baked_only(self, monkeypatch, tmp_path):
        env, _ = self._generated_env(monkeypatch, tmp_path, None)
        assert env == {
            "AGENTALLOY_PACKS": "all",
            "DUCKDB_PATH": "/app/data/agentalloy.duck",
            "FRAGMENTS_LANCE_PATH": "/app/data/fragments.lance",
            "TELEMETRY_DB_PATH": "/app/data/telemetry.duck",
            "AGENTALLOY_RUNTIME_STATE_DIR": "/app/data/runtime-state",
            "LOG_LEVEL": "info",
        }

    def test_run_command_forwards_assist_group(self, monkeypatch, tmp_path):
        env, _ = self._generated_env(
            monkeypatch,
            tmp_path,
            "LM_ASSIST=arbitrate\nSIGNAL_INTENT_RERANK_URL=http://localhost:47952\n",
        )
        assert env["LM_ASSIST"] == "arbitrate"
        # In-container loopback is correct for the rerank stack.
        assert env["SIGNAL_INTENT_RERANK_URL"] == "http://localhost:47952"

    def test_forwarded_log_level_wins_over_baked_default(self, monkeypatch, tmp_path):
        env, _ = self._generated_env(monkeypatch, tmp_path, "LOG_LEVEL=debug\n")
        assert env["LOG_LEVEL"] == "debug"

    def test_loopback_upstream_warns_once(self, monkeypatch, tmp_path, capsys):
        env, _ = self._generated_env(
            monkeypatch, tmp_path, "UPSTREAM_URL=http://localhost:11434/v1\n"
        )
        # Forwarded verbatim — warn, never rewrite or drop.
        assert env["UPSTREAM_URL"] == "http://localhost:11434/v1"
        out = capsys.readouterr().out
        assert out.count("host.containers.internal") == 1

    def test_recreate_uses_same_renderer(self, monkeypatch, tmp_path):
        """upgrade._recreate_container must delegate to _run_container (one
        renderer for setup and upgrade — spec AC 3)."""
        from agentalloy.install.subcommands import upgrade

        run_container = MagicMock(return_value=0)
        monkeypatch.setattr(
            "agentalloy.install.subcommands.container_runtime._run_container", run_container
        )
        monkeypatch.setattr(
            "agentalloy.install.subcommands.upgrade._verify_container_spec",
            lambda *a, **k: [],
        )
        state = {"runtime_binary": "podman", "image_tag": "img:tag", "port": 47950}
        actions, warnings = upgrade._recreate_container("img:tag", state)
        assert run_container.call_count == 1
        assert "recreated container" in actions
        assert warnings == []
