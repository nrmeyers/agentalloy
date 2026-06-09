"""``detect`` subcommand — platform-appropriate hardware detection.

Emits a JSON document matching the ``detect`` schema in contracts.md.
All detection is best-effort: if a tool is missing (e.g. ``nvidia-smi``),
the corresponding field is ``null`` or ``false`` — never an error.

This module is Linux-first but structured for macOS/Windows extension.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, timeout: int = 10) -> str | None:
    """Run a command, return stdout or None on any failure."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _read_file(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _first_match(pattern: str, text: str, group: int = 1) -> str | None:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(group) if m else None


# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------


def _detect_os() -> dict[str, Any]:
    kind = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(
        platform.system(), platform.system().lower()
    )
    result: dict[str, Any] = {
        "kind": kind,
        "distro": None,
        "version": None,
        "kernel": None,
        "arch": platform.machine() or None,
    }

    if kind == "linux":
        result["kernel"] = _run(["uname", "-r"])
        osrel = _read_file("/etc/os-release")
        if osrel:
            result["distro"] = _first_match(r'^ID=["\']?(\w+)', osrel)
            result["version"] = _first_match(r'^VERSION_ID=["\']?([^"\'\s]+)', osrel)
        if not result["distro"]:
            lsb = _run(["lsb_release", "-a"])
            if lsb:
                result["distro"] = _first_match(r"Distributor ID:\s*(\S+)", lsb)
                result["version"] = _first_match(r"Release:\s*(\S+)", lsb)
    elif kind == "macos":
        result["kernel"] = _run(["uname", "-r"])
        sw = _run(["sw_vers"])
        if sw:
            result["distro"] = "macos"
            result["version"] = _first_match(r"ProductVersion:\s*(\S+)", sw)
    elif kind == "windows":
        result["distro"] = "windows"
        result["version"] = platform.version()
        result["kernel"] = platform.version()
    return result


# ---------------------------------------------------------------------------
# CPU detection
# ---------------------------------------------------------------------------


def _detect_cpu_linux() -> dict[str, Any]:
    out: dict[str, Any] = {
        "vendor": None,
        "model": None,
        "cores_physical": None,
        "cores_logical": None,
        "max_freq_mhz": None,
    }
    lscpu = _run(["lscpu"])
    if lscpu:
        vendor_raw = _first_match(r"Vendor ID:\s*(.+)", lscpu)
        if vendor_raw:
            vl = vendor_raw.lower()
            if "amd" in vl:
                out["vendor"] = "amd"
            elif "intel" in vl:
                out["vendor"] = "intel"
            else:
                out["vendor"] = vl
        out["model"] = _first_match(r"Model name:\s*(.+)", lscpu)
        cores_per = _first_match(r"Core\(s\) per socket:\s*(\d+)", lscpu)
        sockets = _first_match(r"Socket\(s\):\s*(\d+)", lscpu)
        if cores_per and sockets:
            out["cores_physical"] = int(cores_per) * int(sockets)
        cpus = _first_match(r"CPU\(s\):\s*(\d+)", lscpu)
        if cpus:
            out["cores_logical"] = int(cpus)
        freq = _first_match(r"CPU max MHz:\s*([\d.]+)", lscpu)
        if freq:
            out["max_freq_mhz"] = int(float(freq))
        else:
            freq = _first_match(r"CPU MHz:\s*([\d.]+)", lscpu)
            if freq:
                out["max_freq_mhz"] = int(float(freq))
    return out


def _detect_cpu_macos() -> dict[str, Any]:
    out: dict[str, Any] = {
        "vendor": "apple",
        "model": None,
        "cores_physical": None,
        "cores_logical": None,
        "max_freq_mhz": None,
    }
    brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if brand:
        out["model"] = brand
        if "intel" in brand.lower():
            out["vendor"] = "intel"
    phys = _run(["sysctl", "-n", "hw.physicalcpu"])
    if phys:
        out["cores_physical"] = int(phys)
    log = _run(["sysctl", "-n", "hw.logicalcpu"])
    if log:
        out["cores_logical"] = int(log)
    return out


def _detect_cpu() -> dict[str, Any]:
    kind = platform.system()
    if kind == "Linux":
        return _detect_cpu_linux()
    elif kind == "Darwin":
        return _detect_cpu_macos()
    return {
        "vendor": None,
        "model": None,
        "cores_physical": os.cpu_count(),
        "cores_logical": os.cpu_count(),
        "max_freq_mhz": None,
    }


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _detect_memory_gb() -> int | None:
    kind = platform.system()
    if kind == "Linux":
        meminfo = _read_file("/proc/meminfo")
        if meminfo:
            kb = _first_match(r"MemTotal:\s*(\d+)\s*kB", meminfo)
            if kb:
                return int(kb) // (1024 * 1024)
    elif kind == "Darwin":
        raw = _run(["sysctl", "-n", "hw.memsize"])
        if raw:
            return int(raw) // (1024**3)
    return None


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------


def _detect_disk_free_gb() -> int | None:
    """Return free disk on the root filesystem in GB. Uses stdlib so it doesn't
    parse `df` output (locale + multi-line + LVM quirks made the previous parser
    fragile)."""
    import shutil as _shutil

    try:
        usage = _shutil.disk_usage("/")
    except (OSError, FileNotFoundError):
        return None
    return int(usage.free / (1024**3))


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------


def _detect_gpu_linux() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    discrete: list[dict[str, Any]] = []
    integrated: list[dict[str, Any]] = []

    # NVIDIA discrete via nvidia-smi
    nv = _run(["nvidia-smi", "-L"])
    if nv:
        for line in nv.splitlines():
            name = _first_match(r"GPU \d+: (.+?) \(UUID", line)
            if name:
                discrete.append({"vendor": "nvidia", "model": name, "vram_gb": None})
        # Try to get VRAM
        mem = _run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ]
        )
        if mem:
            for i, line in enumerate(mem.strip().splitlines()):
                if i < len(discrete):
                    with contextlib.suppress(ValueError):
                        discrete[i]["vram_gb"] = math.ceil(int(line.strip()) / 1024)

    # lspci for anything else. Classify discrete vs. integrated by model
    # signature so an AMD Radeon RX or Intel Arc dGPU isn't silently
    # demoted to integrated (which downstream causes recommend-host-targets
    # to skip the dGPU preset).
    discrete_signatures = (
        "radeon rx",
        "radeon pro",
        "rx ",  # e.g. "RX 7900"
        "arc ",  # Intel Arc A380/A750/A770
        "intel(r) arc",
    )
    integrated_signatures = (
        "radeon graphics",
        "radeon vega",
        "ryzen",  # often appears for integrated APUs
        "uhd graphics",
        "iris",
        "hd graphics",
        "780m",
        "680m",
        "660m",
    )
    lspci = _run(["lspci"])
    if lspci:
        for line in lspci.splitlines():
            lower = line.lower()
            if "vga" not in lower and "3d" not in lower and "display" not in lower:
                continue
            # Skip if already captured by nvidia-smi
            if any(d["vendor"] == "nvidia" for d in discrete) and "nvidia" in lower:
                continue
            entry: dict[str, Any] = {"vendor": None, "model": None, "vram_gb": None}
            if "amd" in lower or "ati" in lower:
                entry["vendor"] = "amd"
            elif "intel" in lower:
                entry["vendor"] = "intel"
            # Extract model from the description part after the colon
            desc = line.split(": ", 1)[-1] if ": " in line else line
            entry["model"] = desc.strip()
            desc_lower = desc.lower()
            # Discrete wins over integrated when both signatures match,
            # because the integrated set leans inclusive ("ryzen" appears
            # in both APU and dGPU-bearing system descriptions).
            if any(sig in desc_lower for sig in discrete_signatures):
                discrete.append(entry)
            elif any(sig in desc_lower for sig in integrated_signatures):
                integrated.append(entry)
            else:
                # Unknown model — default to integrated to stay conservative.
                integrated.append(entry)

    return discrete, integrated


def _detect_gpu_macos() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    discrete: list[dict[str, Any]] = []
    integrated: list[dict[str, Any]] = []
    sp = _run(["system_profiler", "SPDisplaysDataType"])
    if sp:
        # Apple Silicon always has an integrated GPU
        chipset = _first_match(r"Chipset Model:\s*(.+)", sp)
        if chipset:
            entry: dict[str, Any] = {"vendor": "apple", "model": chipset, "vram_gb": None}
            vram = _first_match(r"VRAM.*?:\s*(\d+)", sp)
            if vram:
                entry["vram_gb"] = int(vram) // 1024 if int(vram) >= 1024 else int(vram)
            integrated.append(entry)
    return discrete, integrated


def _detect_gpu() -> dict[str, Any]:
    kind = platform.system()
    if kind == "Linux":
        d, i = _detect_gpu_linux()
    elif kind == "Darwin":
        d, i = _detect_gpu_macos()
    else:
        d, i = [], []
    return {"discrete": d, "integrated": i}


# ---------------------------------------------------------------------------
# NPU detection
# ---------------------------------------------------------------------------


def _detect_npu() -> dict[str, Any]:
    result: dict[str, Any] = {"present": False, "vendor": None, "model": None}
    kind = platform.system()

    if kind == "Linux":
        # AMD XDNA check via /sys/class/accel
        accel = Path("/sys/class/accel")
        if accel.exists():
            for dev in accel.iterdir():
                device_dir = dev / "device"
                # Try explicit name file first
                name_file = device_dir / "name"
                raw = _read_file(str(name_file))
                if raw and ("xdna" in raw.lower() or "npu" in raw.lower()):
                    result = {"present": True, "vendor": "amd", "model": raw}
                    return result
                # Fallback: check uevent for amdxdna driver
                uevent = _read_file(str(device_dir / "uevent"))
                if uevent and "amdxdna" in uevent.lower():
                    model = "AMD XDNA NPU"
                    # Try to get more specific model from lspci
                    slot = _first_match(r"PCI_SLOT_NAME=(\S+)", uevent)
                    if slot:
                        lspci_line = _run(["lspci", "-s", slot])
                        if lspci_line:
                            desc = lspci_line.split(": ", 1)[-1] if ": " in lspci_line else None
                            if desc:
                                model = f"AMD XDNA NPU ({desc.strip()})"
                    result = {"present": True, "vendor": "amd", "model": model}
                    return result
        # Fallback: lspci
        lspci = _run(["lspci"])
        if lspci:
            for line in lspci.splitlines():
                lower = line.lower()
                if "npu" in lower or "neural" in lower:
                    desc = line.split(": ", 1)[-1] if ": " in line else line
                    vendor = "amd" if "amd" in lower else None
                    result = {"present": True, "vendor": vendor, "model": desc.strip()}
                    return result
    elif kind == "Darwin":
        # Apple Neural Engine — present on all Apple Silicon
        arch = platform.machine()
        if arch == "arm64":
            result = {"present": True, "vendor": "apple", "model": "Apple Neural Engine"}
    return result


# ---------------------------------------------------------------------------
# CUDA / ROCm / Metal
# ---------------------------------------------------------------------------


def _detect_cuda() -> str | None:
    raw = _run(
        [
            "nvidia-smi",
            "--query-gpu=driver_version",
            "--format=csv,noheader",
        ]
    )
    if raw:
        return raw.splitlines()[0].strip() or None
    return None


def _detect_rocm() -> bool:
    return _run(["rocm-smi", "--version"]) is not None


def _detect_metal() -> bool:
    if platform.system() != "Darwin":
        return False
    sp = _run(["system_profiler", "SPDisplaysDataType"])
    return bool(sp and "metal" in sp.lower())


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------


def detect_hardware() -> dict[str, Any]:
    """Run all detection probes and return the canonical detect JSON."""
    t0 = time.monotonic()
    result = {
        "schema_version": SCHEMA_VERSION,
        "os": _detect_os(),
        "cpu": _detect_cpu(),
        "memory_gb": _detect_memory_gb(),
        "disk_free_gb": _detect_disk_free_gb(),
        "gpu": _detect_gpu(),
        "npu": _detect_npu(),
        "metal": _detect_metal(),
        "cuda": _detect_cuda(),
        "rocm": _detect_rocm(),
    }
    result["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return result


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p: argparse.ArgumentParser = subparsers.add_parser(
        "detect",
        help="Run platform-appropriate hardware detection.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Execute the detect subcommand.

    Always re-runs hardware detection. Hardware can change between runs
    (USB GPU attached, NPU driver loaded, dock connected) and the runbook
    explicitly supports the user correcting detected fields, so caching
    the previous output would silently mask both real changes and
    user corrections.
    """
    st = install_state.load_state()
    result = detect_hardware()

    # Save large output to file
    fp, digest = install_state.save_output_file(result, "detect.json")
    install_state.record_step(
        st,
        "detect",
        extra={
            "output_digest": digest,
            "output_path": str(fp),
        },
    )
    install_state.save_state(st)

    if not getattr(args, "quiet", False):
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0
