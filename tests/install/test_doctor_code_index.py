"""Doctor: code-index module check + legacy codebase-indexer leftovers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentalloy.install.subcommands import doctor


class TestCheckCodeIndex:
    def test_toggle_off_passes_quietly(self) -> None:
        result = doctor._check_code_index({}, 47950)  # pyright: ignore[reportPrivateUsage]
        assert result["passed"] is True
        assert "off" in result["detail"]

    def test_service_down_passes_with_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor, "_fetch_health", lambda port: None)
        result = doctor._check_code_index({"CODE_INDEX_ENABLED": "1"}, 47950)  # pyright: ignore[reportPrivateUsage]
        assert result["passed"] is True
        assert "not running" in result["detail"]

    def test_unavailable_module_fails_with_extra_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            doctor,
            "_fetch_health",
            lambda port: {"status": "healthy", "modules": {"code_index": "unavailable"}},
        )
        result = doctor._check_code_index({"CODE_INDEX_ENABLED": "1"}, 47950)  # pyright: ignore[reportPrivateUsage]
        assert result["passed"] is False
        assert "agentalloy[code-index]" in result["remediation"]

    def test_disabled_in_service_warns_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor,
            "_fetch_health",
            lambda port: {"status": "healthy", "modules": {"code_index": "disabled"}},
        )
        result = doctor._check_code_index({"CODE_INDEX_ENABLED": "1"}, 47950)  # pyright: ignore[reportPrivateUsage]
        assert result["passed"] is True
        assert result["severity"] == "warn"
        assert "server-restart" in result["remediation"]

    def test_enabled_reports_repo_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor,
            "_fetch_health",
            lambda port: {"status": "healthy", "modules": {"code_index": "enabled"}},
        )

        class _Resp:
            def read(self) -> bytes:
                return b'[{"slug": "a"}, {"slug": "b"}]'

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        monkeypatch.setattr(doctor, "urlopen", lambda req, timeout=5: _Resp())
        result = doctor._check_code_index({"CODE_INDEX_ENABLED": "1"}, 47950)  # pyright: ignore[reportPrivateUsage]
        assert result["passed"] is True
        assert "2 repo(s) indexed" in result["detail"]


class TestLegacyDetection:
    def test_no_leftovers_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(doctor, "_legacy_port_listening", lambda: False)
        monkeypatch.setattr(doctor, "_legacy_code_indexer_data_dir", lambda: tmp_path / "absent")
        result = doctor._check_code_indexer_legacy()  # pyright: ignore[reportPrivateUsage]
        assert result["passed"] is True
        assert "severity" not in result

    def test_daemon_and_data_dir_warn_with_migration_guidance(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy_dir = tmp_path / "codebase-indexer"
        legacy_dir.mkdir()
        monkeypatch.setattr(doctor, "_legacy_port_listening", lambda: True)
        monkeypatch.setattr(doctor, "_legacy_code_indexer_data_dir", lambda: legacy_dir)
        result = doctor._check_code_indexer_legacy()  # pyright: ignore[reportPrivateUsage]
        assert result["passed"] is True  # warn, never fail
        assert result["severity"] == "warn"
        assert ":8003" in result["detail"]
        assert str(legacy_dir) in result["detail"]
        assert "code-indexer stop" in result["remediation"]
        assert "agentalloy code index" in result["remediation"]

    def test_host_doctor_includes_new_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor, "_legacy_port_listening", lambda: False)
        result = doctor._run_doctor_host()  # pyright: ignore[reportPrivateUsage]
        names = [c["name"] for c in result["checks"]]
        assert "code_index" in names
        assert "code_indexer_legacy" in names
