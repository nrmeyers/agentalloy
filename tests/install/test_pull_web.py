"""Tests for the ``pull-web`` subcommand (web UI bundle download)."""

from __future__ import annotations

import argparse
import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agentalloy.install.subcommands import pull_web


def _make_bundle(path: Path, *, with_index: bool = True) -> None:
    """Write a web-dist.tar.gz whose members sit at the archive root."""
    with tarfile.open(path, "w:gz") as tf:
        files = {"assets/app.js": b"console.log('ok')"}
        if with_index:
            files["index.html"] = b"<html>ui</html>"
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _fake_download(bundle_ok: bool = True, *, with_index: bool = True):
    def fake(url: str, dest_path: Path, *, label: str = "") -> dict[str, Any]:
        if not bundle_ok:
            return {"success": False, "error": "HTTP Error 404: Not Found", "duration_ms": 1}
        _make_bundle(dest_path, with_index=with_index)
        return {"success": True, "error": None, "duration_ms": 1}

    return fake


@pytest.fixture(autouse=True)
def _no_head_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the asset-existence probe — no test here may touch the network."""
    monkeypatch.setattr(pull_web, "_asset_available", lambda url: True)


def test_pull_installs_versioned_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_web, "_download_with_retry", _fake_download())
    result = pull_web.pull_web_dist("9.9.9")
    assert result["success"] and not result["skipped"]
    dest = pull_web.web_dist_dir("9.9.9")
    assert (dest / "index.html").is_file()
    assert (dest / "assets" / "app.js").is_file()


def test_pull_is_idempotent_unless_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_web, "_download_with_retry", _fake_download())
    assert pull_web.pull_web_dist("9.9.9")["skipped"] is False
    assert pull_web.pull_web_dist("9.9.9")["skipped"] is True
    assert pull_web.pull_web_dist("9.9.9", force=True)["skipped"] is False


def test_pull_prunes_other_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_web, "_download_with_retry", _fake_download())
    pull_web.pull_web_dist("1.0.0")
    pull_web.pull_web_dist("2.0.0")
    assert not pull_web.web_dist_dir("1.0.0").exists()
    assert (pull_web.web_dist_dir("2.0.0") / "index.html").is_file()


def test_missing_asset_fails_fast_without_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_web, "_asset_available", lambda url: False)

    def boom(url: str, dest_path: Path, *, label: str = "") -> dict[str, Any]:
        raise AssertionError("retry downloader must not run when the asset is absent")

    monkeypatch.setattr(pull_web, "_download_with_retry", boom)
    result = pull_web.pull_web_dist("9.9.9")
    assert result["success"] is False
    assert "no web UI bundle published" in result["error"]


def test_download_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_web, "_download_with_retry", _fake_download(bundle_ok=False))
    result = pull_web.pull_web_dist("9.9.9")
    assert result["success"] is False
    assert "404" in result["error"]
    assert not pull_web.web_dist_dir("9.9.9").exists()


def test_bundle_without_index_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_web, "_download_with_retry", _fake_download(with_index=False))
    result = pull_web.pull_web_dist("9.9.9")
    assert result["success"] is False
    assert "index.html" in result["error"]
    assert not pull_web.web_dist_dir("9.9.9").exists()


def test_default_version_is_installed_version(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(url: str, dest_path: Path, *, label: str = "") -> dict[str, Any]:
        seen.append(url)
        _make_bundle(dest_path)
        return {"success": True, "error": None, "duration_ms": 1}

    monkeypatch.setattr(pull_web, "_download_with_retry", fake)
    monkeypatch.setattr(pull_web, "current_version", lambda: "3.2.1")
    result = pull_web.pull_web_dist()
    assert result["version"] == "3.2.1"
    assert seen == [f"https://github.com/{pull_web.REPO}/releases/download/v3.2.1/web-dist.tar.gz"]


def test_run_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    ns = argparse.Namespace(version="9.9.9", force=False)
    monkeypatch.setattr(pull_web, "_download_with_retry", _fake_download())
    assert pull_web.run(ns) == 0
    monkeypatch.setattr(pull_web, "_download_with_retry", _fake_download(bundle_ok=False))
    assert pull_web.run(argparse.Namespace(version="8.8.8", force=False)) == 1
