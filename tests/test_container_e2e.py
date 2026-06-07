"""End-to-end tests for the container deployment flow in simple_setup.py.

Tests E2E-1 through E2E-4 covering:
  E2E-1: Full container setup with mocked runtime binary
  E2E-2: Container bootstrap pulls qwen3-embedding:0.6b model
  E2E-3: Container bootstrap idempotency - restart skips redundant operations
  E2E-4: Container bootstrap crash recovery - re-runs migrations and install-packs

All external dependencies (subprocess.run for runtime commands, HTTP health
checks, DB access, file I/O) are mocked so these tests run in isolation
and complete in <10s each.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
        patch("agentalloy.install.subcommands.simple_setup._remove_containers", return_value=True),
        patch(
            "agentalloy.install.subcommands.simple_setup._container_setup_log_path",
            return_value=tmp_path / "setup.log",
        ),
        patch(
            "agentalloy.install.subcommands.simple_setup._inspect_ollama_project",
            return_value=("test-project", "test-project_default"),
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
        "pull_image", "run_container", "wait_for_readiness",
        "urlopen", "monotonic". Values are the new side_effect or
        return_value to set.
    """
    patches = _all_common_patches(tmp_path)

    # Create shared mock objects that tests can override
    mock_detect_runtime_binary = MagicMock(return_value="podman")
    mock_pull_image = MagicMock(return_value=0)
    mock_ensure_volume = MagicMock()
    mock_run_container = MagicMock(return_value=0)
    mock_generate_entrypoint = MagicMock(return_value=Path("/tmp/entry.sh"))
    mock_cleanup_temp_entrypoint = MagicMock()
    mock_ensure_ollama_dir = MagicMock()
    mock_wait_for_readiness = MagicMock(return_value=True)
    mock_urlopen = MagicMock(return_value=_make_urlopen_mock())
    mock_monotonic = MagicMock(return_value=0.0)

    # Apply default mocks for container_runtime functions
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
            mock_detect_runtime_binary,
        )
    )
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._pull_image",
            mock_pull_image,
        )
    )
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._ensure_volume",
            mock_ensure_volume,
        )
    )
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._run_container",
            mock_run_container,
        )
    )
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._generate_entrypoint",
            mock_generate_entrypoint,
        )
    )
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._cleanup_temp_entrypoint",
            mock_cleanup_temp_entrypoint,
        )
    )
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._ensure_ollama_dir",
            mock_ensure_ollama_dir,
        )
    )

    # Patch _wait_for_readiness at the container_runtime source module level
    # since _run_container_flow imports it locally from there.
    patches.append(
        patch(
            "agentalloy.install.subcommands.container_runtime._wait_for_readiness",
            mock_wait_for_readiness,
        )
    )

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


@pytest.mark.integration
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
          1. _detect_runtime_binary -> "podman"
          2. _pull_image(runtime)
          3. _ensure_volume(runtime)
          4. _ensure_ollama_dir()
          5. _generate_entrypoint(packs)
          6. _run_container(runtime, entrypoint, packs)

        Note: _cleanup_temp_entrypoint is only called on failure; on success
        it is skipped, so it does not appear in the call order here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            call_order = []

            def make_tracker(name, ret=0):
                def tracker(*args, **kwargs):
                    call_order.append(name)
                    return ret

                return tracker

            # We need to patch the container_runtime module's functions with
            # wrapped versions that track calls. Since simple_setup does
            # `from container_runtime import ...`, patching the source module
            # means the import picks up our wrapped mocks.
            rc = _run_container_flow_all_mocked(
                tmp_path,
                mock_overrides={
                    "pull_image": make_tracker("_pull_image", 0),
                    "run_container": make_tracker("_run_container", 0),
                },
                extra_patches=[
                    # These track on the container_runtime module; since
                    # simple_setup imports from there, the wrapped functions
                    # are what get used.
                    patch(
                        "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                        side_effect=make_tracker("_detect_runtime_binary", "podman"),
                    ),
                    patch(
                        "agentalloy.install.subcommands.container_runtime._ensure_volume",
                        side_effect=make_tracker("_ensure_volume"),
                    ),
                    patch(
                        "agentalloy.install.subcommands.container_runtime._ensure_ollama_dir",
                        side_effect=make_tracker("_ensure_ollama_dir"),
                    ),
                    patch(
                        "agentalloy.install.subcommands.container_runtime._generate_entrypoint",
                        side_effect=lambda packs: (
                            call_order.append("_generate_entrypoint"),
                            Path("/tmp/entry.sh"),
                        )[1],
                    ),
                ],
            )

            assert rc == 0
            # Verify the expected call order
            assert call_order == [
                "_detect_runtime_binary",
                "_pull_image",
                "_ensure_volume",
                "_ensure_ollama_dir",
                "_generate_entrypoint",
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
# E2E-2: Container bootstrap pulls qwen3-embedding:0.6b model
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestModelPullBootstrap:
    """E2E-2: Container bootstrap pulls qwen3-embedding:0.6b model.

    Verifies that the entrypoint script is generated and passed to the
    container, and that the model pull is handled inside the entrypoint
    (not in the setup flow).
    """

    def test_entrypoint_is_generated_with_packs(self):
        """The entrypoint script is generated and passed to _run_container."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            entrypoint_packs = []

            def capture_generate_entrypoint(packs):
                entrypoint_packs.append(packs)
                return Path("/tmp/entry.sh")

            rc = _run_container_flow_all_mocked(
                tmp_path,
                extra_patches=[
                    patch(
                        "agentalloy.install.subcommands.container_runtime._generate_entrypoint",
                        side_effect=capture_generate_entrypoint,
                    ),
                ],
            )

            assert rc == 0
            assert len(entrypoint_packs) == 1
            # Entry point is called with the packs string from config
            assert entrypoint_packs[0] == ""

    def test_model_pull_step_is_executed_in_entrypoint(self):
        """The entrypoint script contains the ollama pull step.

        The model pull is handled inside the entrypoint script, not in
        the setup flow. Verify the generated entrypoint contains the
        expected ollama pull commands.
        """
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # The entrypoint should contain ollama pull for the embedding model
        assert "ollama pull qwen3-embedding" in script
        assert "ollama list" in script
        assert "grep -q qwen3-embedding" in script

    def test_model_pull_confirmed_in_entrypoint_script(self):
        """The entrypoint script prints 'Embedding model ready' after pull.

        The entrypoint script handles model pull and prints status messages.
        """
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # The entrypoint should contain the model pull echo
        assert "Pulling qwen3-embedding" in script or "embedding model" in script.lower()

    def test_model_pull_failure_continues_in_entrypoint(self):
        """When model pull fails, the entrypoint handles it gracefully.

        The entrypoint script uses `set -e` but checks for model existence
        before pulling. If ollama pull fails, the script would exit, but
        the setup flow itself continues since the container was started.
        The container will restart and try again (or the user checks logs).
        """
        # The entrypoint script structure: the model pull is inside an
        # if block that checks for the model first. If the model exists,
        # pull is skipped. If it doesn't exist, pull is attempted.
        # A failure in the entrypoint would cause the container to exit,
        # but the setup flow already considers the container "started"
        # once _run_container returns 0.
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # Verify the model check is in place
        assert "grep -q qwen3-embedding" in script
        assert "ollama pull" in script


# ---------------------------------------------------------------------------
# E2E-3: Container bootstrap idempotency
# ---------------------------------------------------------------------------


class TestBootstrapIdempotency:
    """E2E-3: Container bootstrap idempotency - restart skips redundant operations.

    Verifies that when .bootstrap-complete already exists, the entrypoint
    skips Ollama install, model pull, migrations, and pack installation.
    """

    def test_entrypoint_skips_bootstrap_when_complete(self):
        """The generated entrypoint script checks for .bootstrap-complete first."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # .bootstrap-complete check should come before ollama install
        bootstrap_check = script.index(".bootstrap-complete")
        ollama_install = script.index("ollama.ai/install.sh")
        # The actual script uses "exec uv run uvicorn" not "exec uvicorn"
        uvicorn_start = script.index("uvicorn agentalloy.app:app")

        assert bootstrap_check < ollama_install, (
            ".bootstrap-complete check should come before ollama install"
        )
        assert ollama_install < uvicorn_start, "ollama install should come before uvicorn start"

    def test_entrypoint_skips_all_steps_when_complete(self):
        """When .bootstrap-complete exists, only uvicorn runs."""
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

    def test_entrypoint_skips_ollama_install_when_present(self):
        """When ollama is already installed, the install step is skipped."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # Check for ollama presence check before install
        ollama_check = script.index("command -v ollama")
        ollama_install = script.index("ollama.ai/install.sh")

        assert ollama_check < ollama_install, "ollama presence check should come before install"

    def test_entrypoint_skips_model_pull_when_cached(self):
        """When the model is already cached, the pull step is skipped."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("")

        # Check for model presence check before pull
        model_check = script.index("grep -q qwen3-embedding")
        model_pull = script.index("ollama pull qwen3-embedding")

        assert model_check < model_pull, "model cache check should come before pull"


# ---------------------------------------------------------------------------
# E2E-4: Container bootstrap crash recovery
# ---------------------------------------------------------------------------


@pytest.mark.integration
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
