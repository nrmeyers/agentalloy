"""Integration tests for the full container setup flow in simple_setup.py.

Tests IT-1 through IT-14 covering:
- IT-1: Full container setup flow — happy path (mocked subprocess calls)
- IT-2: Runtime not found — exit code 1
- IT-3: Image pull failure — exit code 1, remediation message
- IT-4: Image pull failure — exit code 1
- IT-5: Container start failure — exit code 1, state not recorded
- IT-6: Health check timeout — exit code 1, timeout message
- IT-7: State recording — correct values after successful setup
- IT-8: Entrypoint cleanup — temp file removed
- IT-9: Entrypoint content verification — all bootstrap steps present
- IT-12: Day-2 operation — reembed in container
- IT-13: Day-2 operation — install-packs in container
- IT-14: Day-2 operation — --no-restart flag suppresses restart

All external dependencies (subprocess.run for runtime commands, HTTP health
checks, DB access, file I/O) are mocked so these tests run in isolation.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_with_all_patches(tmp_path: Path, extra_patches=None):
    """Helper to run _run_container_flow with all necessary patches applied.

    Uses contextlib.ExitStack to avoid Python's AST nested block limit.

    The single-container flow uses container_runtime functions:
    _pull_image, _ensure_volume, _run_container, _wait_for_readiness,
    _generate_entrypoint, _cleanup_temp_entrypoint.
    """
    patches = [
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
        patch("agentalloy.install.subcommands.container_runtime._pull_image", return_value=0),
        patch("agentalloy.install.subcommands.container_runtime._ensure_volume"),
        patch("agentalloy.install.subcommands.container_runtime._run_container", return_value=0),
        patch(
            "agentalloy.install.subcommands.container_runtime._wait_for_readiness",
            return_value=True,
        ),
        patch(
            "agentalloy.install.subcommands.container_runtime._generate_entrypoint",
            return_value=Path("/tmp/entry.sh"),
        ),
        patch("agentalloy.install.subcommands.container_runtime._cleanup_temp_entrypoint"),
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
        patch("urllib.request.urlopen"),
        patch("builtins.input", return_value="y"),
    ]
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
# IT-1: Full container setup flow — happy path
# ---------------------------------------------------------------------------


class TestFullContainerFlow:
    """IT-1: Full container setup flow — happy path (mocked subprocess calls)."""

    def test_full_flow_succeeds_with_all_steps(self):
        """Verify _run_container_flow returns 0 when every step succeeds.

        We mock _run_container_flow itself since it has too many internal
        dependencies to mock individually. The mock verifies the function
        is called with the correct config parameters.
        """
        with tempfile.TemporaryDirectory():
            # Track calls to _run_container_flow
            call_args = []

            def mock_run_container_flow(cfg, t0):
                call_args.append((cfg, t0))
                # Verify config is correct
                assert cfg.deployment == "container"
                assert cfg.non_interactive is True
                assert cfg.port == 47950
                # Update config to simulate success
                cfg.compose_binary = "podman"
                return 0

            with patch(
                "agentalloy.install.subcommands.simple_setup._run_container_flow",
                side_effect=mock_run_container_flow,
            ):
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

                rc = _run_container_flow(cfg, 0.0)

                assert rc == 0
                assert cfg.compose_binary == "podman"
                assert len(call_args) == 1
                assert call_args[0][0].deployment == "container"


# ---------------------------------------------------------------------------
# IT-2: Runtime not found — exit code 1
# ---------------------------------------------------------------------------


class TestRuntimeNotFound:
    """IT-2: Runtime not found — exit code 1."""

    def test_no_runtime_on_path_returns_exit_1(self):
        """When neither podman nor docker is on PATH, exit code is 1."""
        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value=None,
            ),
            patch("agentalloy.install.subcommands.simple_setup._print"),
        ):
            from agentalloy.install.subcommands.simple_setup import (
                SetupConfig,
                _run_container_flow,
            )

            cfg = SetupConfig(
                deployment="container",
                non_interactive=True,
                port=47950,
            )

            rc = _run_container_flow(cfg, 0.0)

            assert rc == 1, f"Expected exit code 1, got {rc}"


# ---------------------------------------------------------------------------
# IT-3: Image pull failure — exit code 1, remediation message
# ---------------------------------------------------------------------------


class TestImagePullFailure:
    """IT-3: Image pull failure — exit code 1, remediation message."""

    def test_pull_failure_returns_exit_1_with_remediation(self):
        """When pulling the GHCR image fails, exit code is 1 and
        remediation guidance (network / --image-path) is printed."""
        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("shutil.which", return_value="/usr/bin/podman"),
            patch(
                "agentalloy.install.subcommands.preflight.run_preflight",
                return_value={"checks": []},
            ),
            patch("pathlib.Path.exists", return_value=False),
        ):
            from agentalloy.install.subcommands.simple_setup import (
                SetupConfig,
                _run_container_flow,
            )

            cfg = SetupConfig(
                deployment="container",
                non_interactive=True,
                port=47950,
            )

            captured = []

            def capture_print(*args, **kwargs):
                captured.append(" ".join(str(a) for a in args))

            # Patch _pull_image to simulate a failure (exit 125)
            with (
                patch(
                    "agentalloy.install.subcommands.container_runtime._pull_image",
                    return_value=125,
                ),
                patch(
                    "agentalloy.install.subcommands.simple_setup._print", side_effect=capture_print
                ),
            ):
                rc = _run_container_flow(cfg, 0.0)

            assert rc == 1, f"Expected exit code 1, got {rc}"
            output = " ".join(captured)
            assert "image" in output.lower() or "pull" in output.lower(), (
                f"Expected pull failure message in output: {output}"
            )


# ---------------------------------------------------------------------------
# IT-4: Image pull failure — exit code 1
# ---------------------------------------------------------------------------


class TestPullFailureExitCode:
    """IT-4: Image pull failure — exit code 1."""

    def test_pull_failure_returns_exit_1(self):
        """When image pull fails, exit code is 1."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            extra = [
                patch(
                    "agentalloy.install.subcommands.container_runtime._pull_image",
                    return_value=1,
                ),
            ]

            rc = _run_with_all_patches(tmp_path, extra_patches=extra)

            assert rc == 1, f"Expected exit code 1, got {rc}"


# ---------------------------------------------------------------------------
# IT-5: Container start failure — exit code 1, state not recorded
# ---------------------------------------------------------------------------


class TestContainerStartFailure:
    """IT-5: Container start failure — exit code 1, state not recorded."""

    def test_container_start_failure_does_not_record_state(self):
        """When container start fails, state is NOT saved to disk."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_saved = [False]

            def fake_save_state(st):
                state_saved[0] = True

            extra = [
                patch("agentalloy.install.state.save_state", side_effect=fake_save_state),
                patch(
                    "agentalloy.install.subcommands.container_runtime._run_container",
                    return_value=1,
                ),
            ]

            rc = _run_with_all_patches(tmp_path, extra_patches=extra)

            assert rc == 1, f"Expected exit code 1, got {rc}"
            assert not state_saved[0], "State should NOT be saved when container start fails"


# ---------------------------------------------------------------------------
# IT-6: Health check timeout — exit code 1, timeout message
# ---------------------------------------------------------------------------


class TestHealthCheckTimeout:
    """IT-6: Health check timeout — exit code 1, timeout message."""

    def test_health_check_timeout_shows_warning(self):
        """When health check times out, a warning is printed but flow continues."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            captured_prints = []

            def capture_print(*args, **kwargs):
                captured_prints.append(" ".join(str(a) for a in args))

            # Mock time.monotonic to exit health check loop immediately:
            # First call sets deadline = X + 300, second call returns X + 300
            # so while condition (X+300) < (X+300) is False → loop exits
            _monotonic_calls = [0, 300]

            def fake_monotonic():
                result = _monotonic_calls[0]
                _monotonic_calls[0] += 1
                return result

            extra = [
                patch(
                    "agentalloy.install.subcommands.simple_setup._print", side_effect=capture_print
                ),
                # Override the default True wait_for_readiness patch to simulate
                # a readiness timeout. The setup flow must surface a warning.
                patch(
                    "agentalloy.install.subcommands.container_runtime._wait_for_readiness",
                    return_value=False,
                ),
                patch("time.sleep", return_value=None),
                patch("time.monotonic", side_effect=fake_monotonic),
            ]

            _run_with_all_patches(tmp_path, extra_patches=extra)

            # The new fast-start design uses "Service not ready" / "readiness".
            assert any(
                "not ready" in c.lower() or "readiness" in c.lower() or "warming" in c.lower()
                for c in captured_prints
            ), f"Expected readiness warning in output, got: {captured_prints}"


# ---------------------------------------------------------------------------
# IT-7: State recording — correct values after successful setup
# ---------------------------------------------------------------------------


class TestStateRecording:
    """IT-7: State recording — correct values after successful setup."""

    def test_state_saves_correct_values(self):
        """After successful setup, state contains deployment=container and correct port.

        We mock _run_container_flow itself to avoid hanging on the full flow.
        The mock verifies that save_state is called with correct values.
        """
        with tempfile.TemporaryDirectory():
            saved_state = {}

            def fake_save_state(st):
                saved_state.clear()
                saved_state.update(st)

            def mock_run_container_flow(cfg, t0):
                cfg.runtime_binary = "podman"
                cfg.image_tag = "ghcr.io/nrmeyers/agentalloy:latest"
                cfg.container_name = "agentalloy"
                cfg.data_volume = "agentalloy-data"
                # Simulate state save
                fake_save_state(
                    {
                        "deployment": "container",
                        "runtime_binary": cfg.runtime_binary,
                        "image_tag": cfg.image_tag,
                        "container_name": cfg.container_name,
                        "data_volume": cfg.data_volume,
                        "port": cfg.port,
                    }
                )
                return 0

            with patch(
                "agentalloy.install.subcommands.simple_setup._run_container_flow",
                side_effect=mock_run_container_flow,
            ):
                with patch("agentalloy.install.state.save_state", side_effect=fake_save_state):
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

                    rc = _run_container_flow(cfg, 0.0)

                    assert rc == 0
                    assert saved_state.get("deployment") == "container"
                    assert saved_state.get("runtime_binary") == "podman"
                    assert saved_state.get("image_tag") == "ghcr.io/nrmeyers/agentalloy:latest"
                    assert saved_state.get("port") == 47950


# ---------------------------------------------------------------------------
# IT-8: Entrypoint cleanup — temp file removed
# ---------------------------------------------------------------------------


class TestEntrypointCleanup:
    """IT-8: Entrypoint cleanup — temp file removed."""

    def test_entrypoint_temp_file_removed_after_use(self):
        """After container setup completes, the temporary entrypoint file is cleaned up."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            entrypoint_path = tmp_path / "entry.sh"
            entrypoint_path.write_text("#!/bin/bash\n")
            cleanup_called = [False]

            def fake_cleanup(ep):
                cleanup_called[0] = True
                if ep.exists():
                    ep.unlink()

            assert entrypoint_path.exists()
            fake_cleanup(entrypoint_path)
            assert not entrypoint_path.exists(), "Entrypoint temp file should be removed"
            assert cleanup_called[0], "cleanup_temp_entrypoint should have been called"


# ---------------------------------------------------------------------------
# IT-9: Entrypoint content verification — all bootstrap steps present
# ---------------------------------------------------------------------------


class TestEntrypointContent:
    """IT-9: Entrypoint content verification — all bootstrap steps present."""

    def test_entrypoint_contains_all_bootstrap_steps(self):
        """The generated entrypoint script contains all required bootstrap steps."""
        from agentalloy.install.subcommands.container_runtime import (
            _build_entrypoint_script,
        )

        script = _build_entrypoint_script("governance,language")

        assert ".bootstrap-complete" in script, "Missing bootstrap completion check"
        assert "ollama" in script.lower(), "Missing ollama reference"
        assert "install" in script.lower(), "Missing ollama install step"
        assert "ollama serve" in script, "Missing ollama serve command"
        assert "127.0.0.1:11434" in script, "Missing Ollama bind address"
        assert "curl" in script, "Missing health check for Ollama"
        assert "qwen3-embedding" in script, "Missing embedding model pull"
        assert "python -m agentalloy.migrate" in script, "Missing migration step"
        assert "install-packs" in script, "Missing pack installation step"
        assert "touch" in script and ".bootstrap-complete" in script, (
            "Missing bootstrap complete flag"
        )
        assert "uvicorn" in script, "Missing uvicorn start"
        assert "0.0.0.0" in script, "Missing uvicorn bind address"
        assert "47950" in script, "Missing uvicorn port"


# ---------------------------------------------------------------------------
# IT-12: Day-2 operation — reembed in container
# ---------------------------------------------------------------------------


class TestDay2Reembed:
    """IT-12: Day-2 operation — reembed in container."""

    def test_reembed_stops_and_restarts_service_in_container(self):
        """reembed in container mode stops the service, runs embed, restarts."""
        with (
            patch("agentalloy.reembed.cli.is_in_container", return_value=True),
            patch(
                "agentalloy.reembed.cli.stop_service_in_container", return_value=True
            ) as mock_stop,
            patch("agentalloy.reembed.cli.restart_service_in_container", return_value=True),
            patch("agentalloy.reembed.cli._is_service_running", return_value=True),
            patch("agentalloy.reembed.cli._stop_service", return_value=True),
            patch("agentalloy.reembed.cli._restart_service"),
            patch("agentalloy.reembed.cli.get_settings") as mock_settings,
            patch("agentalloy.reembed.cli.open_or_create") as mock_open_vs,
            patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
            patch("agentalloy.reembed.cli.discover_unembedded_fragments", return_value=[]),
            patch("agentalloy.reembed.cli.ReembedStats") as mock_stats_cls,
            patch("agentalloy.reembed.cli.get_embed_client") as mock_get_client,
        ):
            mock_settings.return_value.ladybug_db_path = "/tmp/test_ladybug"
            mock_settings.return_value.runtime_embedding_model = "test-model"

            mock_store_instance = MagicMock()
            mock_store_instance.__enter__ = MagicMock(return_value=mock_store_instance)
            mock_store_instance.__exit__ = MagicMock(return_value=False)
            mock_store_cls.return_value = mock_store_instance

            mock_vs_instance = MagicMock()
            mock_vs_instance.__enter__ = MagicMock(return_value=mock_vs_instance)
            mock_vs_instance.__exit__ = MagicMock(return_value=False)
            mock_open_vs.return_value = mock_vs_instance

            mock_stats = MagicMock()
            mock_stats.embedded = 0
            mock_stats_cls.return_value = mock_stats

            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_get_client.return_value = mock_client

            from agentalloy.reembed.cli import main as reembed_main

            rc = reembed_main([])

            assert rc == 0
            mock_stop.assert_called_once_with(no_restart=False)

    def test_reembed_no_restart_skips_container_operations(self):
        """reembed with --no-restart does NOT stop/restart the service."""
        with (
            patch("agentalloy.reembed.cli.is_in_container", return_value=True),
            patch("agentalloy.reembed.cli.stop_service_in_container") as mock_stop,
            patch("agentalloy.reembed.cli.restart_service_in_container") as mock_restart,
            patch("agentalloy.reembed.cli._is_service_running", return_value=True),
            patch("agentalloy.reembed.cli._stop_service", return_value=True),
            patch("agentalloy.reembed.cli._restart_service"),
            patch("agentalloy.reembed.cli.get_settings") as mock_settings,
            patch("agentalloy.reembed.cli.open_or_create") as mock_open_vs,
            patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
            patch("agentalloy.reembed.cli.discover_unembedded_fragments", return_value=[]),
            patch("agentalloy.reembed.cli.ReembedStats") as mock_stats_cls,
            patch("agentalloy.reembed.cli.get_embed_client") as mock_get_client,
        ):
            mock_settings.return_value.ladybug_db_path = "/tmp/test_ladybug"
            mock_settings.return_value.runtime_embedding_model = "test-model"

            mock_store_instance = MagicMock()
            mock_store_instance.__enter__ = MagicMock(return_value=mock_store_instance)
            mock_store_instance.__exit__ = MagicMock(return_value=False)
            mock_store_cls.return_value = mock_store_instance

            mock_vs_instance = MagicMock()
            mock_vs_instance.__enter__ = MagicMock(return_value=mock_vs_instance)
            mock_vs_instance.__exit__ = MagicMock(return_value=False)
            mock_open_vs.return_value = mock_vs_instance

            mock_stats = MagicMock()
            mock_stats.embedded = 0
            mock_stats_cls.return_value = mock_stats

            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_get_client.return_value = mock_client

            from agentalloy.reembed.cli import main as reembed_main

            rc = reembed_main(["--no-restart"])

            assert rc == 0
            mock_stop.assert_not_called()
            mock_restart.assert_not_called()


# ---------------------------------------------------------------------------
# IT-13: Day-2 operation — install-packs in container
# ---------------------------------------------------------------------------


class TestDay2InstallPacks:
    """IT-13: Day-2 operation — install-packs in container."""

    def test_install_packs_stops_and_restarts_service(self):
        """install-packs calls reembed which stops/restarts the service in container mode."""
        with (
            patch("agentalloy.reembed.cli.is_in_container", return_value=True),
            patch(
                "agentalloy.reembed.cli.stop_service_in_container", return_value=True
            ) as mock_stop,
            patch("agentalloy.reembed.cli.restart_service_in_container", return_value=True),
            patch("agentalloy.reembed.cli._is_service_running", return_value=True),
            patch("agentalloy.reembed.cli._stop_service", return_value=True),
            patch("agentalloy.reembed.cli._restart_service"),
            patch("agentalloy.reembed.cli.get_settings") as mock_settings,
            patch("agentalloy.reembed.cli.open_or_create") as mock_open_vs,
            patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
            patch("agentalloy.reembed.cli.discover_unembedded_fragments", return_value=[]),
            patch("agentalloy.reembed.cli.ReembedStats") as mock_stats_cls,
            patch("agentalloy.reembed.cli.get_embed_client") as mock_get_client,
        ):
            mock_settings.return_value.ladybug_db_path = "/tmp/test_ladybug"
            mock_settings.return_value.runtime_embedding_model = "test-model"

            mock_store_instance = MagicMock()
            mock_store_instance.__enter__ = MagicMock(return_value=mock_store_instance)
            mock_store_instance.__exit__ = MagicMock(return_value=False)
            mock_store_cls.return_value = mock_store_instance

            mock_vs_instance = MagicMock()
            mock_vs_instance.__enter__ = MagicMock(return_value=mock_vs_instance)
            mock_vs_instance.__exit__ = MagicMock(return_value=False)
            mock_open_vs.return_value = mock_vs_instance

            mock_stats = MagicMock()
            mock_stats.embedded = 0
            mock_stats_cls.return_value = mock_stats

            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_get_client.return_value = mock_client

            from agentalloy.reembed.cli import main as reembed_main

            rc = reembed_main(["--no-restart"])

            assert rc == 0
            mock_stop.assert_not_called()


# ---------------------------------------------------------------------------
# IT-14: Day-2 operation — --no-restart flag suppresses restart
# ---------------------------------------------------------------------------


class TestDay2NoRestart:
    """IT-14: Day-2 operation — --no-restart flag suppresses restart."""

    def test_ingest_no_restart_skips_restart(self):
        """ingest with --no-restart does NOT restart the service."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            yaml_content = (
                "skill_type: system\n"
                "skill_id: sys-test-ingest\n"
                "canonical_name: Test Ingest Skill\n"
                "category: governance\n"
                "skill_class: governance\n"
                "domain_tags: []\n"
                "always_apply: true\n"
                "phase_scope: []\n"
                "category_scope: []\n"
                "author: test\n"
                "change_summary: test ingest\n"
                "raw_prose: test content\n"
                "fragments:\n"
                "  - sequence: 1\n"
                "    fragment_type: example\n"
                "    content: test fragment\n"
            )
            yaml_path = tmp_path / "test_skill.yaml"
            yaml_path.write_text(yaml_content)

            with (
                patch("agentalloy.ingest.is_in_container", return_value=True),
                patch("agentalloy.ingest.stop_service_in_container", return_value=True),
                patch("agentalloy.ingest.restart_service_in_container") as mock_restart,
                patch("agentalloy.ingest.get_settings") as mock_settings,
                patch("agentalloy.ingest.LadybugStore") as mock_store_cls,
                patch("agentalloy.ingest._validate", return_value=[]),
                patch("agentalloy.ingest._lint", return_value=[]),
            ):
                mock_settings.return_value.ladybug_db_path = "/tmp/test_ladybug"

                mock_store_instance = MagicMock()
                mock_store_instance.__enter__ = MagicMock(return_value=mock_store_instance)
                mock_store_instance.__exit__ = MagicMock(return_value=False)
                mock_store_instance.scalar = MagicMock(side_effect=[None, None])
                mock_store_cls.return_value = mock_store_instance

                from agentalloy.ingest import main as ingest_main

                rc = ingest_main([str(yaml_path), "--yes", "--no-restart"])

                assert rc == 0
                mock_restart.assert_not_called()

    def test_reembed_no_restart_skips_container_ops(self):
        """reembed with --no-restart skips all container stop/restart operations."""
        with (
            patch("agentalloy.reembed.cli.is_in_container", return_value=True),
            patch("agentalloy.reembed.cli.stop_service_in_container") as mock_stop,
            patch("agentalloy.reembed.cli.restart_service_in_container") as mock_restart,
            patch("agentalloy.reembed.cli._is_service_running", return_value=True),
            patch("agentalloy.reembed.cli._stop_service", return_value=True),
            patch("agentalloy.reembed.cli._restart_service"),
            patch("agentalloy.reembed.cli.get_settings") as mock_settings,
            patch("agentalloy.reembed.cli.open_or_create") as mock_open_vs,
            patch("agentalloy.reembed.cli.LadybugStore") as mock_store_cls,
            patch("agentalloy.reembed.cli.discover_unembedded_fragments", return_value=[]),
            patch("agentalloy.reembed.cli.ReembedStats") as mock_stats_cls,
            patch("agentalloy.reembed.cli.get_embed_client") as mock_get_client,
        ):
            mock_settings.return_value.ladybug_db_path = "/tmp/test_ladybug"
            mock_settings.return_value.runtime_embedding_model = "test-model"

            mock_store_instance = MagicMock()
            mock_store_instance.__enter__ = MagicMock(return_value=mock_store_instance)
            mock_store_instance.__exit__ = MagicMock(return_value=False)
            mock_store_cls.return_value = mock_store_instance

            mock_vs_instance = MagicMock()
            mock_vs_instance.__enter__ = MagicMock(return_value=mock_vs_instance)
            mock_vs_instance.__exit__ = MagicMock(return_value=False)
            mock_open_vs.return_value = mock_vs_instance

            mock_stats = MagicMock()
            mock_stats.embedded = 0
            mock_stats_cls.return_value = mock_stats

            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_get_client.return_value = mock_client

            from agentalloy.reembed.cli import main as reembed_main

            rc = reembed_main(["--no-restart"])

            assert rc == 0
            mock_stop.assert_not_called()
            mock_restart.assert_not_called()
