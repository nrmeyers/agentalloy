"""Unit tests for the ``detect`` subcommand.

Maps to test-plan.md § Layer 1 — Idempotency (detect) and schema validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.state import state_path
from agentalloy.install.subcommands.detect import (
    _detect_cpu_linux,  # pyright: ignore[reportPrivateUsage]
    _detect_cuda,  # pyright: ignore[reportPrivateUsage]
    _detect_gpu_linux,  # pyright: ignore[reportPrivateUsage]
    _detect_memory_gb,  # pyright: ignore[reportPrivateUsage]
    _detect_npu,  # pyright: ignore[reportPrivateUsage]
    _detect_os,  # pyright: ignore[reportPrivateUsage]
    _detect_rocm,  # pyright: ignore[reportPrivateUsage]
    add_parser,
    detect_hardware,
    run,
)

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

REQUIRED_TOP_KEYS = {
    "schema_version",
    "os",
    "cpu",
    "memory_gb",
    "disk_free_gb",
    "gpu",
    "npu",
    "metal",
    "cuda",
    "rocm",
}
REQUIRED_OS_KEYS = {"kind", "distro", "version", "kernel", "arch"}
REQUIRED_CPU_KEYS = {"vendor", "model", "cores_physical", "cores_logical", "max_freq_mhz"}
REQUIRED_GPU_KEYS = {"discrete", "integrated"}
REQUIRED_NPU_KEYS = {"present", "vendor", "model"}


class TestDetectSchema:
    """Validate detect output matches contracts.md schema."""

    def test_detect_returns_all_top_level_keys(self) -> None:
        result = detect_hardware()
        assert REQUIRED_TOP_KEYS.issubset(result.keys())

    def test_schema_version_is_1(self) -> None:
        result = detect_hardware()
        assert result["schema_version"] == 1

    def test_os_has_required_keys(self) -> None:
        result = detect_hardware()
        assert REQUIRED_OS_KEYS.issubset(result["os"].keys())

    def test_os_kind_is_valid(self) -> None:
        result = detect_hardware()
        assert result["os"]["kind"] in {"linux", "macos", "windows"}

    def test_cpu_has_required_keys(self) -> None:
        result = detect_hardware()
        assert REQUIRED_CPU_KEYS.issubset(result["cpu"].keys())

    def test_gpu_has_required_keys(self) -> None:
        result = detect_hardware()
        assert REQUIRED_GPU_KEYS.issubset(result["gpu"].keys())
        assert isinstance(result["gpu"]["discrete"], list)
        assert isinstance(result["gpu"]["integrated"], list)

    def test_npu_has_required_keys(self) -> None:
        result = detect_hardware()
        assert REQUIRED_NPU_KEYS.issubset(result["npu"].keys())

    def test_metal_is_bool(self) -> None:
        result = detect_hardware()
        assert isinstance(result["metal"], bool)

    def test_cuda_is_string_or_none(self) -> None:
        result = detect_hardware()
        assert result["cuda"] is None or isinstance(result["cuda"], str)

    def test_rocm_is_bool(self) -> None:
        result = detect_hardware()
        assert isinstance(result["rocm"], bool)

    def test_memory_gb_is_int_or_none(self) -> None:
        result = detect_hardware()
        assert result["memory_gb"] is None or isinstance(result["memory_gb"], int)

    def test_disk_free_gb_is_int_or_none(self) -> None:
        result = detect_hardware()
        assert result["disk_free_gb"] is None or isinstance(result["disk_free_gb"], int)

    def test_output_is_json_serializable(self) -> None:
        result = detect_hardware()
        # Should not raise
        json.dumps(result)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestDetectIdempotent:
    """test_detect_idempotent_within_session"""

    def test_two_calls_return_identical_output(self) -> None:
        r1 = detect_hardware()
        r2 = detect_hardware()
        # duration_ms varies per call by design; everything else must match
        r1.pop("duration_ms", None)
        r2.pop("duration_ms", None)
        assert r1 == r2


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


class TestDetectCLI:
    """Test the run() function writes state and outputs JSON."""

    def test_run_exits_0(self, repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        import argparse

        args = argparse.Namespace()
        with patch(
            "agentalloy.install.subcommands.detect.install_state._repo_root", return_value=repo_root
        ):
            rc = run(args)
        assert rc == 0

    def test_run_writes_state_file(self, repo_root: Path) -> None:
        import argparse

        args = argparse.Namespace()
        with patch(
            "agentalloy.install.subcommands.detect.install_state._repo_root", return_value=repo_root
        ):
            run(args)
        fp = state_path(repo_root)
        assert fp.exists()
        data = json.loads(fp.read_text())
        assert any(s["step"] == "detect" for s in data["completed_steps"])

    def test_run_writes_output_file(self, repo_root: Path) -> None:
        import argparse

        from agentalloy.install import state as install_state

        args = argparse.Namespace()
        with patch(
            "agentalloy.install.subcommands.detect.install_state._repo_root", return_value=repo_root
        ):
            run(args)
        # Output now lives under XDG_DATA_HOME (the conftest redirects it).
        output_fp = install_state.outputs_dir() / "detect.json"
        assert output_fp.exists()
        data = json.loads(output_fp.read_text())
        assert data["schema_version"] == 1

    def test_run_emits_json_to_stdout(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import argparse

        args = argparse.Namespace()
        with patch(
            "agentalloy.install.subcommands.detect.install_state._repo_root", return_value=repo_root
        ):
            run(args)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "os" in data
        assert "cpu" in data


class TestDetectDispatcher:
    """Test the argparse integration works."""

    def test_add_parser_creates_detect_subcommand(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)
        args = parser.parse_args(["detect"])
        assert args.subcommand == "detect"
        assert hasattr(args, "func")


# ---------------------------------------------------------------------------
# Detection logic with mocked system calls
# ---------------------------------------------------------------------------


class TestDetectLinuxMocked:
    """Test Linux detection paths with mocked subprocess calls."""

    LSCPU_OUTPUT = """\
Architecture:            x86_64
CPU(s):                  24
Vendor ID:               AuthenticAMD
Model name:              AMD Ryzen AI 9 HX 370
Core(s) per socket:      12
Socket(s):               1
CPU max MHz:             5100.0000"""

    MEMINFO = "MemTotal:       65536000 kB\nMemFree:        32000000 kB\n"

    @patch("agentalloy.install.subcommands.detect.platform")
    @patch("agentalloy.install.subcommands.detect._run")
    @patch("agentalloy.install.subcommands.detect._read_file")
    def test_linux_cpu_detection(
        self, mock_read: MagicMock, mock_run: MagicMock, mock_platform: MagicMock
    ) -> None:
        mock_platform.system.return_value = "Linux"
        mock_platform.machine.return_value = "x86_64"

        def run_side_effect(cmd: list[str], **kw: Any) -> str | None:
            if cmd == ["lscpu"]:
                return self.LSCPU_OUTPUT
            return None

        mock_run.side_effect = run_side_effect
        mock_read.return_value = None

        cpu = _detect_cpu_linux()  # pyright: ignore[reportPrivateUsage]
        assert cpu["vendor"] == "amd"
        assert cpu["model"] == "AMD Ryzen AI 9 HX 370"
        assert cpu["cores_physical"] == 12
        assert cpu["cores_logical"] == 24
        assert cpu["max_freq_mhz"] == 5100

    @patch("agentalloy.install.subcommands.detect._read_file")
    def test_linux_memory_from_meminfo(self, mock_read: MagicMock) -> None:
        mock_read.return_value = self.MEMINFO

        with patch("agentalloy.install.subcommands.detect.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            mem = _detect_memory_gb()  # pyright: ignore[reportPrivateUsage]
        assert mem == 62  # 65536000 kB ≈ 62 GB

    @patch("agentalloy.install.subcommands.detect._run")
    def test_nvidia_gpu_detection(self, mock_run: MagicMock) -> None:
        def side_effect(cmd: list[str], **kw: Any) -> str | None:
            if cmd == ["nvidia-smi", "-L"]:
                return "GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-abc)"
            if cmd == ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]:
                return "24576"
            if cmd == ["lspci"]:
                return ""
            return None

        mock_run.side_effect = side_effect

        discrete, _integrated = _detect_gpu_linux()  # pyright: ignore[reportPrivateUsage]
        assert len(discrete) == 1
        assert discrete[0]["vendor"] == "nvidia"
        assert discrete[0]["model"] == "NVIDIA GeForce RTX 4090"
        assert discrete[0]["vram_gb"] == 24

    @patch("agentalloy.install.subcommands.detect._run")
    def test_missing_nvidia_smi_returns_null_cuda(self, mock_run: MagicMock) -> None:
        mock_run.return_value = None

        assert _detect_cuda() is None  # pyright: ignore[reportPrivateUsage]

    @patch("agentalloy.install.subcommands.detect._run")
    def test_missing_rocm_smi_returns_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = None

        assert _detect_rocm() is False  # pyright: ignore[reportPrivateUsage]

    @patch("agentalloy.install.subcommands.detect._read_file")
    def test_os_release_parsing(self, mock_read: MagicMock) -> None:
        mock_read.return_value = 'ID=ubuntu\nVERSION_ID="24.04"\n'

        with patch("agentalloy.install.subcommands.detect.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            mock_platform.machine.return_value = "x86_64"
            with patch("agentalloy.install.subcommands.detect._run") as mock_run:
                mock_run.return_value = "6.17.0-1017-oem"
                os_info = _detect_os()  # pyright: ignore[reportPrivateUsage]
        assert os_info["kind"] == "linux"
        assert os_info["distro"] == "ubuntu"
        assert os_info["version"] == "24.04"

    @patch("agentalloy.install.subcommands.detect.Path")
    def test_npu_from_sys_accel(self, mock_path_cls: MagicMock) -> None:
        """NPU detected via /sys/class/accel."""

        # Mock /sys/class/accel/<dev>/device/name
        mock_accel = mock_path_cls.return_value
        mock_accel.exists.return_value = True
        mock_dev = type("MockDev", (), {"__truediv__": lambda self, x: self})()  # pyright: ignore[reportUnknownLambdaType]
        mock_accel.iterdir.return_value = [mock_dev]

        with (
            patch("agentalloy.install.subcommands.detect._read_file", return_value="AMD XDNA NPU"),
            patch("agentalloy.install.subcommands.detect.platform") as mock_platform,
        ):
            mock_platform.system.return_value = "Linux"
            npu = _detect_npu()  # pyright: ignore[reportPrivateUsage]
        assert npu["present"] is True
        assert npu["vendor"] == "amd"
