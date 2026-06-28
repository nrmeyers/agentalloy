"""Unit tests for the `upgrade` subcommand (src/agentalloy/install/subcommands/upgrade.py).

Fully offline: the GitHub API, package swap, container runtime, and all shelled
`agentalloy <step>` calls are mocked. We exercise version resolution, the
no-mutation guarantees of `--check` / already-current, native step ordering +
install-method handling, the dim-mismatch re-embed branch, and container
recreate (incl. `-full` tag preservation).
"""

from __future__ import annotations

import argparse
import json
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
        patch(
            "agentalloy.install.subcommands.seed_corpus.corpus_skill_count",
            return_value=100,
        ),
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


def test_native_empty_corpus_after_upgrade_warns():
    """install-packs 'succeeds' but the corpus didn't populate → loud warning
    (so _run returns a non-clean status), mirroring setup's #261 guard."""
    state = {"installed_packs": ["core"]}
    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", return_value="systemd"),
        patch.object(up, "_start_inference_servers"),
        patch.object(up, "_start_service"),
        patch.object(up, "_run_cli", return_value=_proc(0)),
        patch.object(up.subprocess, "run", return_value=_proc(0)),
        patch(
            "agentalloy.install.subcommands.seed_corpus.corpus_skill_count",
            return_value=0,
        ),
    ):
        actions, warnings = up._upgrade_native("v2.3.0", state, assume_yes=True)
    assert any("corpus is missing or empty after upgrade" in w for w in warnings)


def test_native_populated_corpus_no_warning():
    """A healthy corpus after upgrade adds no corpus warning."""
    state = {"installed_packs": ["core"]}
    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", return_value="systemd"),
        patch.object(up, "_start_inference_servers"),
        patch.object(up, "_start_service"),
        patch.object(up, "_run_cli", return_value=_proc(0)),
        patch.object(up.subprocess, "run", return_value=_proc(0)),
        patch(
            "agentalloy.install.subcommands.seed_corpus.corpus_skill_count",
            return_value=300,
        ),
    ):
        actions, warnings = up._upgrade_native("v2.3.0", state, assume_yes=True)
    assert not any("corpus is missing or empty" in w for w in warnings)


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


def test_new_version_read_from_swapped_binary():
    # Regression: after the package swap the in-process __version__ is frozen at the
    # pre-upgrade value. `new_version` must come from the freshly-installed binary
    # (shelled `agentalloy --version`), not from _current_version().
    def cli(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if args == ["--version"]:
            return _proc(0, stdout="agentalloy 2.4.0\n")
        return _proc(0)

    with (
        patch.object(up, "_current_version", return_value="2.3.5"),
        patch.object(up, "_latest_release_tag", return_value="v2.4.0"),
        patch.object(up.install_state, "load_state", return_value={"deployment": "native"}),
        patch.object(up, "_upgrade_native", return_value=([], [])),
        patch.object(up, "_run_cli", side_effect=cli),
    ):
        result = up.upgrade(force=True)

    assert result["current_version"] == "2.3.5"  # captured before the swap
    assert result["new_version"] == "2.4.0"  # NOT the stale in-process 2.3.5


def test_swap_output_is_captured_not_spilled():
    # Regression: the `uv tool install` swap must capture its output so the raw
    # Resolved/Built/Installed lines do not spill into the upgrade stdout.
    captured: dict[str, Any] = {}

    def rec_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kw)
        return _proc(0)

    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", return_value="systemd"),
        patch.object(up, "_start_inference_servers"),
        patch.object(up, "_start_service"),
        patch.object(up, "_run_cli", return_value=_proc(0)),
        patch.object(up.subprocess, "run", side_effect=rec_run),
    ):
        up._upgrade_native("v2.4.0", {"installed_packs": ["core"]}, assume_yes=True)

    assert captured.get("capture_output") is True


def test_update_warnings_folded_not_dumped():
    # Regression: the `update` step emits a JSON blob; we capture it (no terminal
    # spill) and surface only its warnings cleanly into the upgrade summary.
    def cli(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "update":
            assert kw.get("capture") is True  # must be captured, not printed
            return _proc(0, stdout='{"warnings": ["corpus has no corpus_meta table"]}')
        return _proc(0)

    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(up, "_stop_service", return_value="systemd"),
        patch.object(up, "_start_inference_servers"),
        patch.object(up, "_start_service"),
        patch.object(up, "_run_cli", side_effect=cli),
        patch.object(up.subprocess, "run", return_value=_proc(0)),
    ):
        actions, warnings = up._upgrade_native(
            "v2.4.0", {"installed_packs": ["core"]}, assume_yes=True
        )

    assert any("corpus_meta" in w for w in warnings)


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
        patch.object(cr, "_run_container", return_value=0) as run_ct,
        patch("agentalloy.install.state.save_state") as save,
    ):
        actions, warnings = up._upgrade_container("v2.2.1", state, assume_yes=True)

    pull.assert_called_once_with("podman", "ghcr.io/nrmeyers/agentalloy:2.2.1-full")
    assert run_ct.call_args.kwargs["image_ref"] == "ghcr.io/nrmeyers/agentalloy:2.2.1-full"
    # The recreate must NOT generate a host entrypoint (temp-leak fix) — packs
    # are delivered via env to the baked /app/entrypoint.sh.
    assert "entrypoint" not in run_ct.call_args.kwargs
    assert any("recreated container" in a for a in actions)
    assert not warnings
    # install-state image_tag is pinned to the image we actually ran (preserving
    # the -full variant), so doctor + the next upgrade's base reflect reality.
    assert state["image_tag"] == "ghcr.io/nrmeyers/agentalloy:2.2.1-full"
    save.assert_called_once()


def test_container_pull_failure_aborts_before_cli_swap():
    """A not-yet-published image must abort BEFORE the CLI swap, so we never strand
    a newer CLI orchestrating the old container (the v3.0.1 release-window race)."""
    state = {"deployment": "container", "runtime_binary": "podman", "image_tag": None}
    from agentalloy.install.subcommands import container_runtime as cr

    with (
        # A real install method so the swap WOULD run if we reached it.
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(cr, "_pull_image", return_value=1),
        patch.object(up.subprocess, "run") as run_swap,
        patch.object(cr, "_run_container") as run_ct,
    ):
        actions, warnings = up._upgrade_container("v3.0.2", state, assume_yes=True)
    run_swap.assert_not_called()  # CLI was NOT swapped
    run_ct.assert_not_called()  # container was NOT recreated
    assert not any("upgraded CLI" in a for a in actions)
    assert any("isn't available yet" in w for w in warnings)


def test_target_image_falsy_version_does_not_crash():
    """A falsy version (no --ref, API unreachable) must fall back to the base ref,
    never build `repo:` — the AttributeError/invalid-image that aborted recreate."""
    default = up._target_image(None, None)
    assert default.startswith("ghcr.io/") and ":" in default  # a real, tagged ref
    assert up._target_image("ghcr.io/x/y:3.0.4", "") == "ghcr.io/x/y:3.0.4"
    assert up._target_image("ghcr.io/x/y:3.0.4", None) == "ghcr.io/x/y:3.0.4"
    # a real version still pins, preserving the -full variant
    assert up._target_image("ghcr.io/x/y:3.0.4-full", "v9.9.9") == "ghcr.io/x/y:9.9.9-full"


def test_container_recreate_runs_under_new_cli_after_swap():
    """After a real CLI swap the recreate must be delegated to the freshly-installed
    binary (`upgrade --recreate-only`), NOT run in-process with the stale module —
    the bug that shipped a new CLI orchestrating a mountless container."""
    state = {
        "deployment": "container",
        "runtime_binary": "podman",
        "image_tag": "ghcr.io/nrmeyers/agentalloy:3.0.4",
        "installed_packs": ["core"],
    }
    from agentalloy.install.subcommands import container_runtime as cr

    with (
        patch.object(up, "_detect_install_method", return_value="uv-tool"),
        patch.object(cr, "_pull_image", return_value=0),
        patch.object(up.subprocess, "run", return_value=_proc(0)),  # CLI swap
        patch.object(up, "_run_cli", return_value=_proc(0)) as run_cli,
        patch.object(cr, "_run_container") as run_ct,  # must NOT be called in-process
    ):
        actions, warnings = up._upgrade_container("v3.0.5", state, assume_yes=True)

    run_ct.assert_not_called()  # recreate did NOT happen in the stale process
    # Two post-swap child calls under the new CLI: recreate, then re-validate
    # customizations against the new _packs.
    assert run_cli.call_count == 2
    assert run_cli.call_args_list[0].args[0] == ["upgrade", "--recreate-only", "--ref", "v3.0.5"]
    assert run_cli.call_args_list[1].args[0] == ["customize", "revalidate", "--json"]
    assert any("recreated container (post-swap CLI)" in a for a in actions)
    assert any("re-validated overrides" in a for a in actions)
    assert not warnings


def test_container_recreate_source_stays_in_process():
    """A source checkout swaps nothing, so the running process IS the new code —
    recreate in-process and verify the spec."""
    state = {
        "deployment": "container",
        "runtime_binary": "podman",
        "image_tag": "ghcr.io/nrmeyers/agentalloy:3.0.4",
        "installed_packs": ["core"],
    }
    from agentalloy.install.subcommands import container_runtime as cr

    with (
        patch.object(up, "_detect_install_method", return_value="source"),
        patch.object(cr, "_pull_image", return_value=0),
        patch.object(cr, "_run_container", return_value=0) as run_ct,
        patch.object(up, "_run_cli") as run_cli,  # must NOT shell out for source
        patch.object(up, "_verify_container_spec", return_value=[]),
    ):
        actions, warnings = up._upgrade_container("v3.0.5", state, assume_yes=True)

    run_cli.assert_not_called()
    run_ct.assert_called_once()
    assert any("recreated container" in a for a in actions)
    assert not warnings


def test_verify_container_spec_warns_only_on_confirmed_missing_mount():
    """The post-condition warns when a mount is provably absent, and stays silent
    when it simply can't inspect (no false positives in CI / missing runtime)."""
    cr = MagicMock()
    cr.resolve_projects_root.return_value = "/home/nmeyers"

    # mount present -> clean
    with patch.object(up.subprocess, "run", return_value=_proc(0, "/app/data /home/nmeyers ")):
        assert up._verify_container_spec("podman", cr) == []

    # mount confirmed absent -> one loud warning
    with patch.object(up.subprocess, "run", return_value=_proc(0, "/app/data ")):
        warns = up._verify_container_spec("podman", cr)
    assert len(warns) == 1 and "missing the projects-root mount" in warns[0]

    # inspect failed -> can't confirm, stay silent
    with patch.object(up.subprocess, "run", return_value=_proc(1)):
        assert up._verify_container_spec("podman", cr) == []

    # '/' root was already refused at run time -> nothing to assert
    cr.resolve_projects_root.return_value = "/"
    assert up._verify_container_spec("podman", cr) == []


# --- preflight + dismiss (release-notification surface) ---------------------


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_preflight_decline_aborts_without_swap():
    with (
        patch.object(up, "_current_version", return_value="3.7.0"),
        patch.object(up, "_latest_release_tag", return_value="v3.8.0"),
        patch.object(up.install_state, "load_state", return_value={"deployment": "native"}),
        patch.object(up, "_preflight_confirm", return_value=False) as confirm,
        patch.object(up, "_upgrade_native") as native,
    ):
        result = up.upgrade(interactive=True)
    confirm.assert_called_once()
    native.assert_not_called()
    assert "upgrade declined by user" in result["actions"]
    assert "new_version" not in result


def test_preflight_skipped_when_not_interactive():
    with (
        patch.object(up, "_current_version", return_value="3.7.0"),
        patch.object(up, "_latest_release_tag", return_value="v3.8.0"),
        patch.object(up.install_state, "load_state", return_value={"deployment": "native"}),
        patch.object(up, "_preflight_confirm") as confirm,
        patch.object(up, "_upgrade_native", return_value=(["swapped"], [])),
        patch.object(up, "_installed_version_via_cli", return_value="3.8.0"),
    ):
        result = up.upgrade(interactive=False)
    confirm.assert_not_called()
    assert result["new_version"] == "3.8.0"


def test_preflight_skipped_with_assume_yes():
    with (
        patch.object(up, "_current_version", return_value="3.7.0"),
        patch.object(up, "_latest_release_tag", return_value="v3.8.0"),
        patch.object(up.install_state, "load_state", return_value={"deployment": "native"}),
        patch.object(up, "_preflight_confirm") as confirm,
        patch.object(up, "_upgrade_native", return_value=([], [])),
        patch.object(up, "_installed_version_via_cli", return_value="3.8.0"),
    ):
        up.upgrade(interactive=True, assume_yes=True)
    confirm.assert_not_called()


def test_progress_enabled_only_for_human_output():
    # The live spinner around the silent native steps must follow the human-output
    # flag: on when interactive (a real terminal session), off under --json/--quiet
    # so machine output stays clean. `interactive` is the carrier.
    def _run_native(interactive: bool) -> Any:
        with (
            patch.object(up, "_current_version", return_value="3.7.0"),
            patch.object(up, "_latest_release_tag", return_value="v3.8.0"),
            patch.object(up.install_state, "load_state", return_value={"deployment": "native"}),
            patch.object(up, "_preflight_confirm", return_value=True),
            patch.object(up, "_upgrade_native", return_value=([], [])) as native,
            patch.object(up, "_installed_version_via_cli", return_value="3.8.0"),
        ):
            up.upgrade(interactive=interactive, assume_yes=True)
        return native

    assert _run_native(True).call_args.kwargs["show_progress"] is True
    assert _run_native(False).call_args.kwargs["show_progress"] is False


def test_customized_skill_count_counts_non_default():
    rows = [{"layer": "default"}, {"layer": "profile"}, {"layer": "project"}, {"layer": "default"}]
    with patch.object(up, "_run_cli", return_value=_proc(0, stdout=json.dumps(rows))):
        assert up._customized_skill_count() == 2


def test_customized_skill_count_quiet_on_failure():
    with patch.object(up, "_run_cli", return_value=_proc(1, stdout="boom")):
        assert up._customized_skill_count() == 0


def test_dismiss_writes_dismissed_version(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTALLOY_RELEASE_CHECK", raising=False)
    from agentalloy.install import release_check as rc

    rc._write_cache({"enabled": True, "latest_tag": "v3.9.0", "dismissed_version": None})
    code = up._dismiss(_ns(dismiss=True, quiet=True, json=False))
    assert code == 0
    assert rc.read_cache()["dismissed_version"] == "v3.9.0"
