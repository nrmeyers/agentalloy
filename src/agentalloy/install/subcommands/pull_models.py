"""``pull-models`` subcommand.

Idempotent model provisioning for llama-server (llama.cpp), the sole
inference runner. When ``llama-server`` is not on PATH it downloads a
prebuilt CPU binary from the ggml-org GitHub releases (works headlessly;
falls back to a from-source build only when prompted interactively), then
downloads the required GGUF weights from Hugging Face into the persistent
models dir.

Reads the ``recommend-models`` JSON output (either from a file path or
from ``install-state.json``) to determine which models to provision.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from http.client import IncompleteRead
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1
STEP_NAME = "pull-models"


# ---------------------------------------------------------------------------
# Runner presence checks
# ---------------------------------------------------------------------------


def _is_model_present_llama_server(model: str) -> bool:
    """Check if a GGUF model file exists on disk for llama-server."""
    model_path = install_state.user_data_dir() / "models" / model
    return model_path.exists()


_PRESENCE_CHECKS: dict[str, Any] = {
    "llama-server": _is_model_present_llama_server,
}


# ---------------------------------------------------------------------------
# llama-server: build and GGUF download helpers
# ---------------------------------------------------------------------------

# Permanent directories — survive reinstalls and allow incremental updates.
_LLAMA_CPP_BUILD_ROOT = install_state.user_data_dir() / "build" / "llama.cpp"
_LLAMA_SERVER_BIN_DIR = Path.home() / ".local" / "bin"

# Hugging Face GGUF download map: local model filename → raw download URL.
# The local filename (the dict key) is the canonical name we save to
# <data>/models/; the remote filename in the URL may differ in case/separators.
# Embed: Qwen official GGUF. Reranker: ggml-org (the llama.cpp org) Q8_0 build —
# verified 2026-06-14 (639,153,184 bytes). No Unsloth GGUF exists for the
# 0.6B reranker.
_GGUF_URL_MAP: dict[str, str] = {
    "Qwen3-Embedding-0.6B-Q8_0.gguf": (
        "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF"
        "/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf"
    ),
    "nomic-embed-text-v1.5.Q8_0.gguf": (
        "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF"
        "/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"
    ),
    "Qwen3-Reranker-0.6B-Q8_0.gguf": (
        "https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"
        "/resolve/main/qwen3-reranker-0.6b-q8_0.gguf"
    ),
}


# --- Prebuilt llama-server provisioning -----------------------------------
#
# Downloading a known-good CPU build from the ggml-org GitHub releases is the
# DEFAULT way to provision llama-server. Unlike the from-source build it works
# non-interactively (no TTY prompt, no C++ toolchain) and is far faster — which
# is exactly the gap that broke `agentalloy setup -n` and CI. Pinned to a
# specific build for reproducibility, the same way the GGUF URLs above pin exact
# files. Bump deliberately.
_LLAMA_CPP_PREBUILT_BUILD = "b9631"
_LLAMA_CPP_RELEASE_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"

# (os_key, arch_key) -> EXACT plain-CPU asset suffix. The exactness is the whole
# point: the release also ships ubuntu-{vulkan,rocm,sycl,openvino} and
# win-{cuda,hip,sycl} variants, so a loose "ubuntu + x64" substring match would
# happily grab a GPU build that needs a runtime we don't have. Match the full
# suffix or nothing.
_PREBUILT_ASSET_SUFFIX: dict[tuple[str, str], str] = {
    ("linux", "x64"): "ubuntu-x64.tar.gz",
    ("linux", "arm64"): "ubuntu-arm64.tar.gz",
    ("darwin", "x64"): "macos-x64.tar.gz",
    ("darwin", "arm64"): "macos-arm64.tar.gz",
    ("win", "x64"): "win-cpu-x64.zip",
    ("win", "arm64"): "win-cpu-arm64.zip",
}

# (hardware, os_key, arch_key) -> GPU-capable asset suffix. On Linux the Vulkan
# build offloads to NVIDIA *and* AMD with only the GPU driver present (no CUDA
# toolkit / ROCm runtime), so it's the portable default for both vendors.
# Windows uses the vendor-native CUDA / HIP builds. Apple Silicon needs no entry:
# the macos-arm64 asset already bundles Metal, so `-ngl` alone offloads.
_GPU_PREBUILT_ASSET_SUFFIX: dict[tuple[str, str, str], str] = {
    ("nvidia", "linux", "x64"): "ubuntu-vulkan-x64.tar.gz",
    ("nvidia", "linux", "arm64"): "ubuntu-vulkan-arm64.tar.gz",
    ("radeon", "linux", "x64"): "ubuntu-vulkan-x64.tar.gz",
    ("radeon", "linux", "arm64"): "ubuntu-vulkan-arm64.tar.gz",
    ("nvidia", "win", "x64"): "win-cuda-13.3-x64.zip",
    ("radeon", "win", "x64"): "win-hip-radeon-x64.zip",
}

# Windows CUDA ships its runtime DLLs as a separate "cudart" asset that must be
# extracted alongside the server binary.
_WIN_CUDART_ASSET = "cudart-llama-bin-win-cuda-13.3-x64.zip"

# Hardware targets that should provision a GPU-offload build.
_GPU_HARDWARE_TARGETS = frozenset({"nvidia", "radeon"})


def _normalize_hardware(value: object) -> str:
    """Normalize a recommend-models preset / hardware string to a known target.

    Returns one of: ``nvidia``, ``radeon``, ``apple-silicon``, ``cpu`` (default).
    """
    s = str(value or "").strip().lower()
    if s in ("nvidia", "cuda"):
        return "nvidia"
    if s in ("radeon", "amd", "rocm", "hip"):
        return "radeon"
    if s in ("apple-silicon", "apple", "metal", "mps", "darwin"):
        return "apple-silicon"
    return "cpu"


def _asset_backend(asset: str) -> str:
    """Infer the offload backend from a prebuilt asset filename."""
    for token in ("vulkan", "cuda", "rocm", "hip", "sycl"):
        if token in asset:
            return token
    return "cpu"


# The extracted toolchain (binary + co-located shared libs) lives here; a thin
# wrapper placed on PATH points at it with LD_LIBRARY_PATH set. This mirrors the
# container/CI provisioning: the prebuilt binary is NOT built with an $ORIGIN
# rpath, so it can't find its sibling .so files without help.
_LLAMA_CPP_RUNTIME_DIR = install_state.user_data_dir() / "runtime" / "llama.cpp"

_MANUAL_LLAMA_BUILD_HINT = (
    "Provision llama-server manually, then re-run `pull-models`:\n"
    "  - Prebuilt: download the CPU build for your platform from\n"
    "    https://github.com/ggml-org/llama.cpp/releases and put `llama-server`\n"
    "    (with its co-located shared libraries) on your PATH; or\n"
    "  - From source:\n"
    "      git clone https://github.com/ggml-org/llama.cpp\n"
    "      cd llama.cpp && cmake -B build -DLLAMA_BUILD_SERVER=ON\n"
    "      cmake --build build --config Release -j\n"
    "      cp build/bin/llama-server ~/.local/bin/"
)


def _detect_prebuilt_platform() -> tuple[str, str] | None:
    """Map the running platform to ``(os_key, arch_key)`` keys, or None.

    None means there is no prebuilt asset we can use for this OS/arch (e.g.
    s390x, freebsd) — the caller falls back to a source build.
    """
    if sys.platform.startswith("linux"):
        os_key = "linux"
    elif sys.platform == "darwin":
        os_key = "darwin"
    elif sys.platform.startswith("win"):
        os_key = "win"
    else:
        return None

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        arch_key = "x64"
    elif machine in ("aarch64", "arm64"):
        arch_key = "arm64"
    else:
        return None

    return (os_key, arch_key)


def _prebuilt_asset(hardware: str = "cpu") -> tuple[str, str, str] | None:
    """Resolve ``(os_key, asset_filename, download_url)`` for this platform.

    For a GPU ``hardware`` target (``nvidia``/``radeon``) returns the GPU-offload
    asset where one is published (Linux Vulkan, Windows CUDA/HIP); otherwise the
    plain-CPU asset. ``apple-silicon`` needs no GPU asset — the macos-arm64 build
    already bundles Metal. Returns None when no asset is published for the OS/arch.
    """
    plat = _detect_prebuilt_platform()
    if plat is None:
        return None
    build = _LLAMA_CPP_PREBUILT_BUILD
    if hardware in _GPU_HARDWARE_TARGETS:
        gpu_suffix = _GPU_PREBUILT_ASSET_SUFFIX.get((hardware, plat[0], plat[1]))
        if gpu_suffix is not None:
            asset = f"llama-{build}-bin-{gpu_suffix}"
            return plat[0], asset, f"{_LLAMA_CPP_RELEASE_BASE}/{build}/{asset}"
        # No GPU asset for this OS/arch — fall through to the CPU build.
    suffix = _PREBUILT_ASSET_SUFFIX.get(plat)
    if suffix is None:
        return None
    asset = f"llama-{build}-bin-{suffix}"
    return plat[0], asset, f"{_LLAMA_CPP_RELEASE_BASE}/{build}/{asset}"


def _probe_gpu_devices(binary: Path) -> list[str]:
    """Return the GPU devices ``llama-server --list-devices`` can see.

    Used to verify a GPU build actually has a usable device (driver present)
    before we trust ``-ngl`` to offload — otherwise the install would silently
    run on CPU. Returns the matching device lines (e.g. ``Vulkan0: ...``), or [].
    """
    try:
        env = dict(os.environ)
        lib = str(binary.parent)
        env["LD_LIBRARY_PATH"] = f"{lib}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
        proc = subprocess.run(
            [str(binary), "--list-devices"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    devices: list[str] = []
    for line in out.splitlines():
        if re.match(r"\s*(?:Vulkan|CUDA|ROCm|HIP|Metal|SYCL)\d+\s*:", line):
            devices.append(line.strip())
    return devices


def _extract_archive(archive_path: Path, dest_dir: Path) -> None:
    """Extract a ``.tar.gz`` or ``.zip`` archive into ``dest_dir``.

    Uses the tarfile ``data`` filter (Python 3.12+) so a malicious/absolute
    member path can't escape ``dest_dir``.
    """
    if archive_path.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest_dir)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest_dir, filter="data")


def _find_llama_server_binary(root: Path) -> Path | None:
    """Locate the ``llama-server`` (or ``.exe``) binary anywhere under ``root``."""
    for name in ("llama-server", "llama-server.exe"):
        for candidate in root.rglob(name):
            if candidate.is_file():
                return candidate
    return None


def _write_llama_server_wrapper(os_key: str, runtime_dir: Path, bin_dir: Path) -> Path:
    """Install a thin launcher on PATH that runs the prebuilt with its libs.

    POSIX: an ``sh`` wrapper that puts the toolchain dir on LD_LIBRARY_PATH (and
    DYLD_LIBRARY_PATH for macOS) before exec. Windows: a ``.cmd`` shim that
    prepends the dir to PATH (Windows resolves DLLs from there). Returns the
    wrapper path.
    """
    if os_key == "win":
        wrapper = bin_dir / "llama-server.cmd"
        wrapper.write_text(
            "@echo off\r\n"
            f'set "PATH={runtime_dir};%PATH%"\r\n'
            f'"{runtime_dir}\\llama-server.exe" %*\r\n'
        )
    else:
        wrapper = bin_dir / "llama-server"
        wrapper.write_text(
            "#!/bin/sh\n"
            "# Auto-generated by `agentalloy pull-models`. Runs the prebuilt\n"
            "# llama-server with its co-located shared libraries on the loader path.\n"
            f'LLAMA_LIB_DIR="{runtime_dir}"\n'
            'export LD_LIBRARY_PATH="$LLAMA_LIB_DIR:${LD_LIBRARY_PATH:-}"\n'
            'export DYLD_LIBRARY_PATH="$LLAMA_LIB_DIR:${DYLD_LIBRARY_PATH:-}"\n'
            'exec "$LLAMA_LIB_DIR/llama-server" "$@"\n'
        )
    wrapper.chmod(0o755)
    return wrapper


def _download_prebuilt_llama_server(hardware: str = "cpu") -> dict[str, Any]:
    """Download + install a prebuilt llama-server from ggml-org releases.

    Resolves this platform + hardware target's asset — a GPU-offload build for
    ``nvidia``/``radeon`` where one is published (Linux Vulkan, Windows CUDA/HIP;
    apple-silicon's Metal ships in the macos-arm64 build), else the plain-CPU
    build — downloads it with the GGUF retry/backoff, extracts the toolchain into
    a permanent runtime dir, installs a wrapper on PATH, and (for GPU targets)
    verifies a GPU device is actually visible so ``-ngl`` won't silently no-op.

    Returns ``{success, binary_path, backend, warning, error, hint, duration_ms}``.
    """
    resolved = _prebuilt_asset(hardware)
    if resolved is None:
        return {
            "success": False,
            "binary_path": None,
            "error": (
                f"no prebuilt llama-server asset for this platform "
                f"({sys.platform}/{platform.machine()})"
            ),
            "hint": _MANUAL_LLAMA_BUILD_HINT,
        }

    os_key, asset, url = resolved
    backend = "metal" if hardware == "apple-silicon" else _asset_backend(asset)
    runtime_dir = _LLAMA_CPP_RUNTIME_DIR
    bin_dir = _LLAMA_SERVER_BIN_DIR
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    print(f"  llama-server: downloading prebuilt {asset} ...", file=sys.stderr)

    archive_path = runtime_dir.parent / asset
    staging = runtime_dir.parent / f"{asset}.extract"
    try:
        dl = _download_with_retry(url, archive_path, label="llama-server")
        if not dl["success"]:
            return {
                "success": False,
                "binary_path": None,
                "error": f"prebuilt download failed: {dl['error']}",
                "hint": _MANUAL_LLAMA_BUILD_HINT,
            }

        # Extract into a clean staging dir, then swap the toolchain into place.
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        _extract_archive(archive_path, staging)

        binary = _find_llama_server_binary(staging)
        if binary is None:
            return {
                "success": False,
                "binary_path": None,
                "error": f"llama-server binary not found inside {asset}",
                "hint": _MANUAL_LLAMA_BUILD_HINT,
            }

        # Replace the runtime dir with the extracted toolchain (binary's dir).
        src_dir = binary.parent
        shutil.rmtree(runtime_dir, ignore_errors=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        for item in src_dir.iterdir():
            dest = runtime_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        (runtime_dir / binary.name).chmod(0o755)

        # Windows CUDA ships its runtime DLLs in a separate "cudart" asset; the
        # server won't start without them, so co-locate them with the binary.
        if backend == "cuda" and os_key == "win":
            cudart_url = (
                f"{_LLAMA_CPP_RELEASE_BASE}/{_LLAMA_CPP_PREBUILT_BUILD}/{_WIN_CUDART_ASSET}"
            )
            cudart_archive = runtime_dir.parent / _WIN_CUDART_ASSET
            cudart_staging = runtime_dir.parent / f"{_WIN_CUDART_ASSET}.extract"
            cd = _download_with_retry(cudart_url, cudart_archive, label="cudart")
            if cd["success"]:
                shutil.rmtree(cudart_staging, ignore_errors=True)
                cudart_staging.mkdir(parents=True)
                _extract_archive(cudart_archive, cudart_staging)
                for dll in cudart_staging.rglob("*.dll"):
                    shutil.copy2(dll, runtime_dir / dll.name)
                shutil.rmtree(cudart_staging, ignore_errors=True)
                with contextlib.suppress(OSError):
                    cudart_archive.unlink()
            else:
                print(
                    f"  WARNING: CUDA runtime ({_WIN_CUDART_ASSET}) download failed; the "
                    "server may not start. Install the CUDA runtime manually.",
                    file=sys.stderr,
                )

        wrapper = _write_llama_server_wrapper(os_key, runtime_dir, bin_dir)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return {
            "success": False,
            "binary_path": None,
            "error": f"failed to install prebuilt llama-server: {exc}",
            "hint": _MANUAL_LLAMA_BUILD_HINT,
        }
    finally:
        with contextlib.suppress(OSError):
            archive_path.unlink()
        shutil.rmtree(staging, ignore_errors=True)

    duration_ms = int((time.monotonic() - t0) * 1000)
    print(f"  llama-server: installed prebuilt to {wrapper} ({duration_ms} ms)", file=sys.stderr)
    if str(bin_dir) not in os.environ.get("PATH", ""):
        print(
            f"  WARNING: {bin_dir} is not in your PATH. Add it to your shell "
            "profile so `llama-server` can be found at runtime.",
            file=sys.stderr,
        )

    # For a GPU target, confirm a device is actually visible — otherwise the
    # -ngl flags the install writes would silently no-op (run on CPU).
    gpu_warning: str | None = None
    if hardware in _GPU_HARDWARE_TARGETS or hardware == "apple-silicon":
        devices = _probe_gpu_devices(Path(wrapper))
        if devices:
            print(
                f"  llama-server: {backend} offload ready — {len(devices)} GPU device(s) "
                f"visible (e.g. {devices[0]}).",
                file=sys.stderr,
            )
        else:
            gpu_warning = (
                f"Installed the {backend} GPU build, but llama-server sees no GPU device "
                "(driver missing or unsupported). It will run on CPU and `-ngl` offload "
                "stays inert until a working GPU driver is present."
            )
            print(f"  WARNING: {gpu_warning}", file=sys.stderr)

    return {
        "success": True,
        "binary_path": str(wrapper),
        "backend": backend,
        "warning": gpu_warning,
        "error": None,
        "duration_ms": duration_ms,
    }


def _check_build_prereqs() -> list[str]:
    """Return a list of missing build prerequisites (empty = all present)."""
    missing: list[str] = []
    for tool in ("git", "cmake"):
        if not shutil.which(tool):
            missing.append(tool)
    # Need a C++ compiler: try c++ / g++ / clang++
    if not any(shutil.which(cc) for cc in ("c++", "g++", "clang++")):
        missing.append("C++ compiler (g++, clang++, or c++)")
    return missing


def _build_llama_server() -> dict[str, Any]:
    """Clone llama.cpp and build llama-server, installing to ~/.local/bin.

    The source tree is kept in a permanent directory so incremental ``git
    pull`` + rebuild is cheap on subsequent calls.

    Returns a result dict with keys: success, binary_path, error, duration_ms.
    """
    # Fast path: binary already on PATH.
    existing = shutil.which("llama-server")
    if existing:
        return {"success": True, "binary_path": existing, "error": None, "duration_ms": 0}

    # Check prereqs before doing anything expensive.
    missing = _check_build_prereqs()
    if missing:
        tools = ", ".join(missing)
        return {
            "success": False,
            "binary_path": None,
            "error": f"Missing build prerequisites: {tools}",
            "hint": (
                f"Install the following tools then re-run pull-models: {tools}. "
                "On Debian/Ubuntu: `sudo apt install git cmake build-essential`. "
                "On macOS: `xcode-select --install && brew install cmake`."
            ),
        }

    _LLAMA_CPP_BUILD_ROOT.parent.mkdir(parents=True, exist_ok=True)
    _LLAMA_SERVER_BIN_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    print(
        f"  llama-server: building from source in {_LLAMA_CPP_BUILD_ROOT} ...",
        file=sys.stderr,
    )

    try:
        # 1. Clone or update the source tree.
        if not _LLAMA_CPP_BUILD_ROOT.exists():
            print("  llama-server: cloning llama.cpp (this may take a minute) ...", file=sys.stderr)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://github.com/ggerganov/llama.cpp",
                    str(_LLAMA_CPP_BUILD_ROOT),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
        else:
            print("  llama-server: updating existing source tree ...", file=sys.stderr)
            subprocess.run(
                ["git", "-C", str(_LLAMA_CPP_BUILD_ROOT), "pull", "--ff-only"],
                check=True,
                capture_output=True,
                timeout=120,
            )

        # 2. CMake configure.
        build_dir = _LLAMA_CPP_BUILD_ROOT / "build"
        build_dir.mkdir(exist_ok=True)
        print("  llama-server: cmake configure ...", file=sys.stderr)
        subprocess.run(
            [
                "cmake",
                "-S",
                str(_LLAMA_CPP_BUILD_ROOT),
                "-B",
                str(build_dir),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DLLAMA_BUILD_SERVER=ON",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

        # 3. CMake build (-j uses all available cores).
        print("  llama-server: cmake build (this may take several minutes) ...", file=sys.stderr)
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--config", "Release", "-j"],
            check=True,
            capture_output=True,
            timeout=900,  # 15 min upper bound
        )

        # 4. Locate the built binary.
        candidate = build_dir / "bin" / "llama-server"
        if not candidate.exists():
            return {
                "success": False,
                "binary_path": None,
                "error": f"Build completed but binary not found at expected path: {candidate}",
            }

        # 5. Install to ~/.local/bin.
        dest = _LLAMA_SERVER_BIN_DIR / "llama-server"
        shutil.copy2(str(candidate), str(dest))
        dest.chmod(0o755)

        duration_ms = int((time.monotonic() - t0) * 1000)
        print(
            f"  llama-server: installed to {dest} ({duration_ms} ms)",
            file=sys.stderr,
        )

        # Warn if the install dir is not on PATH.
        if str(_LLAMA_SERVER_BIN_DIR) not in os.environ.get("PATH", ""):
            print(
                f"  WARNING: {_LLAMA_SERVER_BIN_DIR} is not in your PATH. "
                "Add it to your shell profile so `llama-server` can be found at runtime.",
                file=sys.stderr,
            )

        return {
            "success": True,
            "binary_path": str(dest),
            "error": None,
            "duration_ms": duration_ms,
        }

    except subprocess.CalledProcessError as exc:
        stderr_snippet = (exc.stderr or b"").decode(errors="replace").strip()[-500:]
        return {
            "success": False,
            "binary_path": None,
            "error": f"Build failed (exit {exc.returncode}): {stderr_snippet}",
            "hint": (
                "Check build output above. You can also build manually:\n"
                "  git clone https://github.com/ggerganov/llama.cpp\n"
                "  cd llama.cpp && cmake -B build -DLLAMA_BUILD_SERVER=ON\n"
                "  cmake --build build --config Release -j\n"
                "  cp build/bin/llama-server ~/.local/bin/"
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "binary_path": None,
            "error": "Build timed out",
            "hint": "Run the build manually in a terminal to see what's blocking.",
        }
    except OSError as exc:
        return {"success": False, "binary_path": None, "error": str(exc)}


# Network errors worth retrying. HuggingFace occasionally drops the TLS
# connection mid-handshake or mid-stream ("unexpected eof", "IncompleteRead"),
# which is transient — a fresh attempt usually succeeds.
_RETRYABLE_DOWNLOAD_ERRORS = (
    urllib.error.URLError,  # also the base class of HTTPError
    IncompleteRead,
    TimeoutError,
    OSError,  # covers socket.timeout / ConnectionReset surfaced as OSError
)

# Number of download attempts and the exponential-ish backoff (seconds) applied
# *between* attempts (after attempt 1 fails: 2s, then 4s, then 8s).
_DOWNLOAD_MAX_ATTEMPTS = 4
_DOWNLOAD_BACKOFFS = (2, 4, 8)


def _download_gguf_once(url: str, dest_path: Path) -> int:
    """Perform a single GGUF download attempt, returning bytes written.

    Streams the response to ``dest_path`` with a byte-counter progress
    indicator on stderr. Raises on any network/IO error so the caller's retry
    loop can decide whether to try again.
    """
    with urllib.request.urlopen(url, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk = 1 << 20  # 1 MiB chunks
        with open(dest_path, "wb") as out_f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out_f.write(buf)
                downloaded += len(buf)
                if total:
                    pct = downloaded * 100 // total
                    mb = downloaded / (1 << 20)
                    total_mb = total / (1 << 20)
                    print(
                        f"\r  llama-server: {mb:.1f}/{total_mb:.1f} MiB ({pct}%)",
                        end="",
                        file=sys.stderr,
                    )
        print("", file=sys.stderr)  # newline after progress
    # A clean mid-stream EOF (server flushes a partial body then closes without
    # error) yields a short read that would otherwise be saved as a "successful"
    # but truncated file. Raise so _download_with_retry re-attempts / fails loud.
    if total and downloaded != total:
        raise IncompleteRead(partial=b"", expected=total - downloaded)
    return downloaded


def _download_with_retry(
    url: str, dest_path: Path, *, label: str = "llama-server"
) -> dict[str, Any]:
    """Stream ``url`` to ``dest_path``, retrying transient network failures.

    Shared by the GGUF downloads and the prebuilt-binary download. TLS resets,
    ``IncompleteRead``, and timeouts are retried up to ``_DOWNLOAD_MAX_ATTEMPTS``
    times with exponential-ish backoff (2/4/8s); the partial file is removed
    between attempts so each retry starts clean. Returns ``{success, error,
    duration_ms}`` — the caller owns ``dest_path`` and any path reporting.
    """
    t0 = time.monotonic()
    last_error = "unknown download error"
    for attempt in range(1, _DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            _download_gguf_once(url, dest_path)
            return {
                "success": True,
                "error": None,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
        except _RETRYABLE_DOWNLOAD_ERRORS as exc:
            last_error = str(exc)
            # Remove a partial download so the next attempt starts clean.
            with contextlib.suppress(OSError):
                dest_path.unlink()
            if attempt < _DOWNLOAD_MAX_ATTEMPTS:
                backoff = _DOWNLOAD_BACKOFFS[min(attempt - 1, len(_DOWNLOAD_BACKOFFS) - 1)]
                print(
                    f"  {label}: download attempt {attempt}/{_DOWNLOAD_MAX_ATTEMPTS} "
                    f"failed ({last_error}); retrying in {backoff}s ...",
                    file=sys.stderr,
                )
                time.sleep(backoff)

    return {
        "success": False,
        "error": f"download failed after {_DOWNLOAD_MAX_ATTEMPTS} attempts: {last_error}",
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def _download_gguf(model_name: str) -> dict[str, Any]:
    """Download a GGUF model from Hugging Face into the persistent models dir.

    Shows a simple byte-counter progress indicator on stderr and retries
    transient network failures (see ``_download_with_retry``). Returns a result
    dict with keys: success, path, error, duration_ms.
    """
    url = _GGUF_URL_MAP.get(model_name)
    if not url:
        return {
            "success": False,
            "error": (
                f"No Hugging Face download URL defined for model '{model_name}'. "
                "Add it to _GGUF_URL_MAP or download the GGUF manually."
            ),
        }

    dest_dir = install_state.user_data_dir() / "models"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / model_name

    print(
        f"  llama-server: downloading {model_name} from Hugging Face ...",
        file=sys.stderr,
    )
    result = _download_with_retry(url, dest_path, label="llama-server")
    if result["success"]:
        return {
            "success": True,
            "path": str(dest_path),
            "error": None,
            "duration_ms": result["duration_ms"],
        }
    return {"success": False, "error": result["error"]}


def _ensure_llama_server_binary(interactive: bool, hardware: str = "cpu") -> dict[str, Any]:
    """Make ``llama-server`` available, downloading a prebuilt if it isn't.

    Order of preference (user decision 2026-06-14):
      1. Already on PATH → use it.
      2. Download a prebuilt CPU build from ggml-org releases (the default;
         works headlessly — this is the fix for non-interactive installs/CI).
      3. From-source build — only when a prebuilt is unavailable AND we're
         interactive (it needs a TTY prompt + a C++ toolchain). Non-interactive
         callers get an actionable error instead.

    Returns ``{success, binary_path, error, hint}``.
    """
    existing = shutil.which("llama-server")
    if existing:
        result: dict[str, Any] = {"success": True, "binary_path": existing, "error": None}
        # An existing on-PATH binary may be CPU-only; warn if a GPU target won't offload.
        if hardware in _GPU_HARDWARE_TARGETS and not _probe_gpu_devices(Path(existing)):
            result["warning"] = (
                "An existing llama-server is on PATH but exposes no GPU device — likely a "
                "CPU-only build, so `-ngl` offload will not work. Remove it (or reinstall "
                "with a GPU build) to use the GPU."
            )
            print(f"  WARNING: {result['warning']}", file=sys.stderr)
        return result

    prebuilt = _download_prebuilt_llama_server(hardware)
    if prebuilt["success"]:
        return prebuilt

    if not interactive:
        return {
            "success": False,
            "binary_path": None,
            "error": f"llama-server not found and prebuilt download failed: {prebuilt['error']}",
            "hint": prebuilt.get("hint", _MANUAL_LLAMA_BUILD_HINT),
        }

    # Interactive fallback: offer the slower from-source build.
    try:
        choice = (
            input("  Prebuilt llama-server unavailable. Build from source instead? [y/N]: ")
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        choice = "n"
    if choice != "y":
        return {
            "success": False,
            "binary_path": None,
            "error": "llama-server not provisioned (prebuilt failed, source build declined).",
            "hint": _MANUAL_LLAMA_BUILD_HINT,
        }
    return _build_llama_server()


def ensure_runner_binary(*, interactive: bool, preset: str = "cpu") -> dict[str, Any]:
    """Ensure ``llama-server`` is on PATH, downloading a prebuilt if it isn't.

    Public entry over the internal provisioning ``pull-models`` performs, so the
    setup wizard can offer to provision the runner at the preflight gate — where
    a fresh host has no binary yet — instead of dead-ending. ``preset`` is the
    hardware preset (e.g. ``"nvidia"``/``"strix-point"``/``"cpu"``); it's
    normalized to a GPU/CPU asset target internally. Returns
    ``{success, binary_path, error, hint, warning?}`` (the internal shape).
    """
    return _ensure_llama_server_binary(interactive, _normalize_hardware(preset))


def _handle_llama_server(
    model: str, interactive: bool, hardware: str = "cpu"
) -> tuple[
    list[dict[str, Any]],  # auto_pulled entries
    list[dict[str, Any]],  # error entries
]:
    """Orchestrate binary provisioning + GGUF download for llama-server.

    Returns (auto_pulled, errors) tuples to be merged into the main lists.
    """
    auto_pulled: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # ---- 1. Binary ----------------------------------------------------------
    bin_result = _ensure_llama_server_binary(interactive, hardware)
    if not bin_result["success"]:
        errors.append(
            {
                "runner": "llama-server",
                "model": model,
                "success": False,
                "error": bin_result.get("error", "unknown error provisioning llama-server"),
                "hint": bin_result.get("hint"),
            }
        )
        return auto_pulled, errors

    # ---- 2. GGUF model file -------------------------------------------------
    if _is_model_present_llama_server(model):
        # Already downloaded from a previous run.
        return auto_pulled, errors

    download_result = _download_gguf(model)
    if not download_result["success"]:
        errors.append(
            {
                "runner": "llama-server",
                "model": model,
                "success": False,
                "error": download_result.get("error", "unknown download error"),
            }
        )
        return auto_pulled, errors

    auto_pulled.append(
        {
            "runner": "llama-server",
            "model": model,
            "duration_ms": download_result.get("duration_ms", 0),
            "path": download_result.get("path"),
        }
    )
    return auto_pulled, errors


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _collect_model_runner_pairs(option: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract the (model, runner) pairs from a recommend-models option.

    Always yields the embed pair. When the option also carries a
    ``rerank_model`` (+ ``rerank_runner``, defaulting to the embed runner),
    the reranker pair is appended so the install pulls BOTH GGUFs in a single
    pass. Duplicate pairs are collapsed.
    """
    pairs: list[tuple[str, str]] = []

    embed_model = option.get("embed_model", "")
    embed_runner = option.get("embed_runner", "")
    if embed_model and embed_runner:
        pairs.append((embed_model, embed_runner))

    rerank_model = option.get("rerank_model", "")
    rerank_runner = option.get("rerank_runner") or embed_runner
    if rerank_model and rerank_runner:
        pair = (rerank_model, rerank_runner)
        if pair not in pairs:
            pairs.append(pair)

    return pairs


# Strict model-name pattern. Allowed characters cover the canonical
# `name:tag` form (letters, digits, `_`, `-`, `.`, `:`, `/` for org/repo
# namespaces). Rejects anything that could look like a CLI option (leading
# `-`) or carry shell metacharacters.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}$")


def pull_models(
    models_json: dict[str, Any],
    root: Path | None = None,
    runner_override: str | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Pull models based on recommend-models output.

    ``runner_override`` selects a specific runner from the options list,
    bypassing the ``default`` flag. Use when the agent captured the user's
    runner choice after ``recommend-models`` already ran non-interactively.

    Returns contract-shaped JSON with auto_pulled, manual_steps_required,
    and skipped_already_present arrays.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()

    # Idempotency: skip if already done
    st = install_state.load_state(root)
    if install_state.is_step_completed(st, STEP_NAME):
        prev = install_state.get_step_output(st, STEP_NAME)
        prev_data: dict[str, Any] = prev.get("output", {}) if prev else {}
        if not quiet:
            auto_pulled: list[dict[str, Any]] = prev_data.get("auto_pulled", [])
            skipped: list[dict[str, Any]] = prev_data.get("skipped_already_present", [])
            if auto_pulled and not skipped:
                print(f"  Models already pulled: {len(auto_pulled)}", file=sys.stderr)
            if skipped:
                print(f"  Already present: {len(skipped)}", file=sys.stderr)
        # Return cached result so main() can route through write_result
        return prev_data

    # Extract the option to use: explicit runner override > default flag > first.
    options = models_json.get("options", [])
    if not options:
        print("ERROR: No model options in recommend-models output", file=sys.stderr)
        raise SystemExit(1)

    if runner_override:
        option = next(
            (o for o in options if o.get("embed_runner") == runner_override),
            None,
        )
        if option is None:
            available = [o.get("embed_runner") for o in options]
            print(
                f"ERROR: Runner '{runner_override}' not found in recommend-models options.",
                file=sys.stderr,
            )
            print(f"CAUSE: Available runners: {available}", file=sys.stderr)
            print(
                "FIX:   Pass one of the above runners, or omit --runner to use the default.",
                file=sys.stderr,
            )
            raise SystemExit(1)
    else:
        option = next((o for o in options if o.get("default")), options[0])
    pairs = _collect_model_runner_pairs(option)

    auto_pulled: list[dict[str, Any]] = []
    manual_steps: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # Detect interactivity once for the whole pull loop.
    interactive = sys.stdin.isatty()

    # Hardware target (from the recommend-models preset) selects the GPU-offload
    # llama-server build — nvidia/radeon get a Vulkan/CUDA/HIP build, else CPU.
    hardware = _normalize_hardware(models_json.get("preset") or option.get("preset"))

    for model, runner in pairs:
        # Check presence — includes GGUF file check for llama-server. For
        # llama-server, the GGUF file alone is not enough: the runner binary must
        # also be on PATH, else we'd skip provisioning and leave a model with no
        # runtime. Fall through to _handle_llama_server (which re-provisions the
        # binary and short-circuits the GGUF download) when the binary is absent.
        presence_fn = _PRESENCE_CHECKS.get(runner)
        binary_ok = runner != "llama-server" or shutil.which("llama-server") is not None
        if presence_fn and presence_fn(model) and binary_ok:
            skipped.append({"runner": runner, "model": model})
            continue

        if runner == "llama-server":
            # llama-server has its own build + download flow.
            ls_pulled, ls_errors = _handle_llama_server(model, interactive, hardware)
            auto_pulled.extend(ls_pulled)
            errors.extend(ls_errors)
        else:
            print(f"WARNING: Unknown runner '{runner}' for model '{model}'", file=sys.stderr)
            manual_steps.append(
                {
                    "runner": runner,
                    "model": model,
                    "instruction": f"Manually install model '{model}' using runner '{runner}'.",
                }
            )

    output: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "auto_pulled": auto_pulled,
        "manual_steps_required": manual_steps,
        "skipped_already_present": skipped,
    }

    if errors:
        output["errors"] = errors
        # Print errors but don't fail — let the runbook decide
        for err in errors:
            print(
                f"ERROR: Failed to pull {err.get('model', '?')} via {err.get('runner', '?')}: {err.get('error', 'unknown')}",
                file=sys.stderr,
            )
            hint = err.get("hint")
            if hint:
                print(f"HINT: {hint}", file=sys.stderr)
        # Don't record completion when any pull failed — otherwise the
        # idempotency check will permanently skip this step on rerun and
        # the user has no path to retry without `reset-step pull-models`.
        return output

    # Record step (only when every pull either succeeded or was already present)
    st = install_state.record_step(st, STEP_NAME, extra={"output": output})
    # Track which models were pulled for uninstall reference
    st["models_pulled"] = [f"{p['runner']}:{p['model']}" for p in auto_pulled] + [
        f"{s['runner']}:{s['model']}" for s in skipped
    ]
    install_state.save_state(st, root)

    return output


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "pull-models",
        help="Idempotent model pulls for selected runners.",
    )
    p.add_argument(
        "--models",
        required=True,
        help="Path to the recommend-models JSON output file.",
    )
    p.add_argument(
        "--runner",
        default=None,
        help=(
            "Override the runner selected by recommend-models "
            "(e.g. llama-server). Use when the agent captured "
            "the user's choice after recommend-models ran non-interactively."
        ),
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    """Render pull models result in human-readable format."""
    auto_pulled = result.get("auto_pulled", [])
    manual_steps = result.get("manual_steps_required", [])
    skipped = result.get("skipped_already_present", [])
    errors = result.get("errors", [])

    print_rich("\n  [bold]Pull Models[/bold]\n")

    if auto_pulled:
        print_rich("  [green]Pulled:[/green]")
        for p in auto_pulled:
            print_rich(f"    {p.get('runner', '?')}:{p.get('model', '?')}")

    if skipped:
        print_rich("  [dim]Already present:[/dim]")
        for s in skipped:
            print_rich(f"    {s.get('runner', '?')}:{s.get('model', '?')}")

    if manual_steps:
        print_rich("  [yellow]Manual steps required:[/yellow]")
        for m in manual_steps:
            print_rich(f"    {m.get('runner', '?')}:{m.get('model', '?')}")
            print_rich(f"      {m.get('instruction', '')}")

    if errors:
        print_rich("  [red]Errors:[/red]")
        for e in errors:
            print_rich(f"    {e.get('runner', '?')}:{e.get('model', '?')} — {e.get('error', '')}")
            hint = e.get("hint")
            if hint:
                print_rich(f"      [yellow]Hint:[/yellow] {hint}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    models_path = Path(args.models)
    if not models_path.exists():
        print(f"ERROR: Models file not found: {models_path}", file=sys.stderr)
        print("CAUSE: The recommend-models output file is missing.", file=sys.stderr)
        print("FIX:   Run `recommend-models` first, or pass the correct path.", file=sys.stderr)
        return 1

    models_json = json.loads(models_path.read_text())
    result = pull_models(
        models_json,
        runner_override=getattr(args, "runner", None),
        quiet=getattr(args, "quiet", False),
    )
    write_result(result, args, human_fn=_render_human)

    # Non-zero exit if there were pull errors
    if result.get("errors"):
        return 1

    # Distinguish "no work needed" from "did real work" so the caller
    # (simple_setup) can render a "skipping" line instead of a
    # generic "Done". This mirrors EXIT_NOOP semantics used elsewhere
    # in the install pipeline (seed_corpus, etc.).
    pulled: list[Any] = result.get("auto_pulled") or []
    skipped: list[Any] = result.get("skipped_already_present") or []
    manual: list[Any] = result.get("manual_steps_required") or []
    if not pulled and not manual and skipped:
        return 4
    return 0


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers (e.g. simple_setup)."""
    return _run(args)
