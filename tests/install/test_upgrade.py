"""Unit tests for the `upgrade` subcommand (src/agentalloy/install/subcommands/upgrade.py).

Fully offline: the GitHub API, package swap, container runtime, and all shelled
`agentalloy <step>` calls are mocked. We exercise version resolution, the
no-mutation guarantees of `--check` / already-current, native step ordering +
install-method handling, the dim-mismatch re-embed branch, and container
recreate (incl. `-full` tag preservation).
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

from agentalloy.install.subcommands import upgrade as up


def _proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["agentalloy"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- version helpers --------------------------------------------------------


def test_parse_semver_strips_v_and_extras():
    assert up._parse_semver("v2.2.1") == (2, 2, 1)
    assert up._parse_semver("2.2.1") == (2, 2, 1)
    assert up._parse_semver("2.2.1-rc1") == (2, 2, 1)
    assert up._parse_semver("2.3") == (2, 3, 0)


def test_parse_semver_orders_correctly():
    assert up._parse_semver("v2.3.0") > up._parse_semver("v2.2.9")
    assert up._parse_semver("v2.2.1") == up._parse_semver("2.2.1")
    assert up._parse_semver("v2.2.0") < up._parse_semver("v2.2.1")


def test_latest_release_tag_parses_tag_name():
    resp = MagicMock()
    resp.read.return_value = b'{"tag_name": "v2.3.0", "name": "x"}'
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda *a: False
    with patch.object(up.urllib.request, "urlopen", return_value=resp):
        assert up._latest_release_tag() == "v2.3.0"


def test_latest_release_tag_offline_returns_none():
    with patch.object(up.urllib.request, "urlopen", side_effect=OSError("offline")):
        assert up._latest_release_tag() is None


def test_is_dim_mismatch():
    assert up._is_dim_mismatch(_proc(1, stderr="error: embedding_dim 1024 != 768")) is True
    assert up._is_dim_mismatch(_proc(1, stderr="EmbeddingDimMismatch: rebuild")) is True
    assert up._is_dim_mismatch(_proc(0, stderr="embedding_dim ok")) is False
    assert up._is_dim_mismatch(_proc(1, stderr="some other failure")) is False


def test_target_image_preserves_full_variant():
    assert (
        up._target_image("ghcr.io/nrmeyers/agentalloy:2.2.0-full", "v2.2.1")
        == "ghcr.io/nrmeyers/agentalloy:2.2.1-full"
    )
    assert (
        up._target_image("ghcr.io/nrmeyers/agentalloy:latest", "v2.2.1")
        == "ghcr.io/nrmeyers/agentalloy:2.2.1"
    )


# --- orchestration: check / already-current ---------------------------------


def test_check_makes_no_mutation():
    with (
        patch.object(up, "_current_version", return_value="2.2.1"),
        patch.object(up, "_latest_release_tag", return_value="v2.3.0"),
        patch.object(up, "_upgrade_native") as native,
        patch.object(up, "_upgrade_container") as container,
    ):
        result = up.upgrade(check=True)
    assert result["update_available"] is True
    assert result["latest_release"] == "v2.3.0"
    native.assert_not_called()
    container.assert_not_called()


def test_already_latest_short_circuits():
    with (
        patch.object(up, "_current_version", return_value="2.2.1"),
        patch.object(up, "_latest_release_tag", return_value="v2.2.1"),
        patch.object(up, "_upgrade_native") as native,
        patch.object(up, "_upgrade_container") as container,
    ):
        result = up.upgrade()
    assert result["update_available"] is False
    assert any("already on the latest" in a for a in result["actions"])
    native.assert_not_called()
    container.assert_not_called()


def test_api_unreachable_warns_without_ref():
    with (
        patch.object(up, "_current_version", return_value="2.2.1"),
        patch.object(up, "_latest_release_tag", return_value=None),
        patch.object(up, "_upgrade_native") as native,
    ):
        result = up.upgrade()
    assert result["latest_release"] is None
    assert any("GitHub releases API" in w for w in result["warnings"])
    native.assert_not_called()


def test_force_upgrades_even_when_current():
    with (
        patch.object(up, "_current_version", return_value="2.2.1"),
        patch.object(up, "_latest_release_tag", return_value="v2.2.1"),
        patch.object(up.install_state, "load_state", return_value={"deployment": "native"}),
        patch.object(up, "_upgrade_native", return_value=(["did it"], [])) as native,
    ):
        result = up.upgrade(force=True)
    native.assert_called_once()
    assert result["deployment"] == "native"


# --- native flow ------------------------------------------------------------


def test_native_source_checkout_skips_swap():
    state: dict[str, Any] = {"installed_packs": ["core"]}
    with (
        patch.object(up, "_detect_install_method", return_value="source"),
        patch.object(up.subprocess, "run") as run,
    ):
        actions, warnings = up._upgrade_native("v2.3.0", state, assume_yes=True)
    run.assert_not_called()
    assert actions == []
    assert any("source" in w and "git pull" in w for w in warnings)


def test_native_ordering_uv_tool():
    calls: list[str] = []
    state = {"installed_packs": ["core", "fastapi"]}

    def rec_cli(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append("cli:" + " ".join(args))
        return _proc(0)

    def rec_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append("swap:" + cmd[0])
        return _proc(0)

    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", side_effect=lambda: calls.append("stop") or "systemd"),
        patch.object(up, "_start_inference_servers", side_effect=lambda: calls.append("inference")),
        patch.object(up, "_start_service", side_effect=lambda: calls.append("start")),
        patch.object(up, "_run_cli", side_effect=rec_cli),
        patch.object(up.subprocess, "run", side_effect=rec_run),
    ):
        actions, warnings = up._upgrade_native("v2.3.0", state, assume_yes=True)

    # stop -> swap -> inference up -> install-packs -> update -> start
    assert calls[0] == "stop"
    assert calls[1] == "swap:uv"
    assert "inference" in calls
    assert any(c.startswith("cli:install-packs") and "core,fastapi" in c for c in calls)
    assert any(c.startswith("cli:update") for c in calls)
    assert calls[-1] == "start"
    assert not warnings


def test_native_dim_mismatch_triggers_forced_reembed():
    state = {"installed_packs": ["core"]}
    cli_calls: list[str] = []

    def rec_cli(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        cli_calls.append(" ".join(args))
        if (
            args[0] == "install-packs"
            and cli_calls.count("install-packs --packs core --no-restart") == 1
        ):
            return _proc(1, stderr="embedding_dim 1024 != 768")
        return _proc(0)

    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", return_value="systemd"),
        patch.object(up, "_start_inference_servers"),
        patch.object(up, "_start_service"),
        patch.object(up, "_run_cli", side_effect=rec_cli),
        patch.object(up.subprocess, "run", return_value=_proc(0)),
    ):
        actions, warnings = up._upgrade_native("v2.3.0", state, assume_yes=True)

    assert any(c == "reembed --force --no-restart" for c in cli_calls)
    assert any("re-embedded" in a for a in actions)


def test_native_dim_mismatch_declined_warns():
    state = {"installed_packs": ["core"]}
    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", return_value="systemd"),
        patch.object(up, "_start_inference_servers"),
        patch.object(up, "_start_service"),
        patch.object(up, "_run_cli", return_value=_proc(1, stderr="embedding_dim mismatch")),
        patch.object(up.subprocess, "run", return_value=_proc(0)),
        patch.object(up, "_confirm", return_value=False),
    ):
        actions, warnings = up._upgrade_native("v2.3.0", state, assume_yes=False)
    assert any("re-embed skipped" in w for w in warnings)


def test_native_swap_failure_aborts():
    state = {"installed_packs": ["core"]}
    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", return_value="systemd"),
        patch.object(
            up.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["uv"]),
        ),
        patch.object(up, "_run_cli") as cli,
    ):
        actions, warnings = up._upgrade_native("v2.3.0", state, assume_yes=True)
    cli.assert_not_called()  # never reached install-packs
    assert any("package install failed" in w for w in warnings)


# --- container flow ---------------------------------------------------------


def test_container_recreates_with_versioned_image():
    state = {
        "deployment": "container",
        "runtime_binary": "podman",
        "image_tag": "ghcr.io/nrmeyers/agentalloy:2.2.0-full",
        "installed_packs": ["core"],
    }
    from agentalloy.install.subcommands import container_runtime as cr

    with (
        patch.object(up, "_detect_install_method", return_value="source"),  # skip CLI swap
        patch.object(cr, "_pull_image", return_value=0) as pull,
        patch.object(cr, "_generate_entrypoint", return_value="/tmp/entry.sh"),
        patch.object(cr, "_run_container", return_value=0) as run_ct,
    ):
        actions, warnings = up._upgrade_container("v2.2.1", state, assume_yes=True)

    pull.assert_called_once_with("podman", "ghcr.io/nrmeyers/agentalloy:2.2.1-full")
    assert run_ct.call_args.kwargs["image_ref"] == "ghcr.io/nrmeyers/agentalloy:2.2.1-full"
    assert any("recreated container" in a for a in actions)
    assert not warnings


def test_container_pull_failure_warns():
    state = {"deployment": "container", "runtime_binary": "podman", "image_tag": None}
    from agentalloy.install.subcommands import container_runtime as cr

    with (
        patch.object(up, "_detect_install_method", return_value="source"),
        patch.object(cr, "_pull_image", return_value=1),
        patch.object(cr, "_run_container") as run_ct,
    ):
        actions, warnings = up._upgrade_container("v2.2.1", state, assume_yes=True)
    run_ct.assert_not_called()
    assert any("failed to pull" in w for w in warnings)
