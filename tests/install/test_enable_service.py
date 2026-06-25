"""Unit tests for the ``enable-service`` subcommand."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.enable_service import (
    _detect_os,  # pyright: ignore[reportPrivateUsage]
    _native_available,  # pyright: ignore[reportPrivateUsage]
    _ngl_for_target,  # pyright: ignore[reportPrivateUsage]
    _poll_health,  # pyright: ignore[reportPrivateUsage]
    _read_env_file,  # pyright: ignore[reportPrivateUsage]
    _render_launchd_plist,  # pyright: ignore[reportPrivateUsage]
    _render_llama_embed_unit,  # pyright: ignore[reportPrivateUsage]
    _render_llama_launchd_plist,  # pyright: ignore[reportPrivateUsage]
    _render_llama_rerank_unit,  # pyright: ignore[reportPrivateUsage]
    _render_systemd_unit,  # pyright: ignore[reportPrivateUsage]
    _resolve_preset,  # pyright: ignore[reportPrivateUsage]
    _write_llama_launchd_agents,  # pyright: ignore[reportPrivateUsage]
    enable_service,
)

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


class TestDetectOS:
    def test_returns_linux_on_linux(self) -> None:
        with patch("platform.system", return_value="Linux"):
            assert _detect_os() == "linux"

    def test_returns_macos_on_darwin(self) -> None:
        with patch("platform.system", return_value="Darwin"):
            assert _detect_os() == "macos"

    def test_returns_windows(self) -> None:
        with patch("platform.system", return_value="Windows"):
            assert _detect_os() == "windows"


class TestNativeAvailable:
    def test_linux_with_systemctl(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", return_value="/usr/bin/systemctl"),
        ):
            assert _native_available() is True

    def test_linux_without_systemctl(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("shutil.which", return_value=None),
        ):
            assert _native_available() is False

    def test_macos_with_launchctl(self) -> None:
        with (
            patch("platform.system", return_value="Darwin"),
            patch("shutil.which", return_value="/bin/launchctl"),
        ):
            assert _native_available() is True

    def test_windows_always_false(self) -> None:
        with patch("platform.system", return_value="Windows"):
            assert _native_available() is False


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


class TestRenderSystemdUnit:
    def test_contains_exec_start(self) -> None:
        content = _render_systemd_unit(
            "/usr/bin/uv", Path("/app"), 8000, Path("/home/u/.config/agentalloy/.env")
        )
        assert "ExecStart=/usr/bin/uv run uvicorn agentalloy.app:app" in content
        assert "--port 8000" in content

    def test_contains_working_directory(self) -> None:
        content = _render_systemd_unit(
            "/usr/bin/uv", Path("/app"), 8000, Path("/home/u/.config/agentalloy/.env")
        )
        assert "WorkingDirectory=/app" in content

    def test_contains_environment_file(self) -> None:
        content = _render_systemd_unit(
            "/usr/bin/uv", Path("/app"), 8000, Path("/home/u/.config/agentalloy/.env")
        )
        assert "EnvironmentFile=/home/u/.config/agentalloy/.env" in content

    def test_has_install_section(self) -> None:
        content = _render_systemd_unit("/usr/bin/uv", Path("/app"), 8000, Path("/env"))
        assert "[Install]" in content
        assert "WantedBy=default.target" in content


class TestRenderLlamaUnits:
    """The embed (47951) and reranker (47952) llama-server units."""

    def test_embed_unit_uses_embeddings_mode_on_47951(self) -> None:
        content = _render_llama_embed_unit(
            "/usr/bin/llama-server", Path("/data/models/Qwen3-Embedding-0.6B-Q8_0.gguf")
        )
        assert "--embeddings" in content
        assert "--port 47951" in content
        assert "Qwen3-Embedding-0.6B-Q8_0.gguf" in content
        assert "WantedBy=default.target" in content

    def test_rerank_unit_is_completions_mode_on_47952(self) -> None:
        content = _render_llama_rerank_unit(
            "/usr/bin/llama-server", Path("/data/models/Qwen3-Reranker-0.6B-Q8_0.gguf")
        )
        # Reranker is a completions server — must NOT pass --embeddings.
        assert "--embeddings" not in content
        assert "--port 47952" in content
        assert "Qwen3-Reranker-0.6B-Q8_0.gguf" in content
        assert "WantedBy=default.target" in content

    def test_units_add_ngl_for_gpu_target(self) -> None:
        """GPU targets (ngl > 0) append -ngl so persistent units offload like setup did."""
        embed = _render_llama_embed_unit("/usr/bin/llama-server", Path("/m/e.gguf"), 999)
        rerank = _render_llama_rerank_unit("/usr/bin/llama-server", Path("/m/r.gguf"), 999)
        assert "-ngl 999" in embed
        assert "-ngl 999" in rerank

    def test_units_omit_ngl_for_cpu(self) -> None:
        """CPU (ngl=0, the default) omits -ngl entirely."""
        embed = _render_llama_embed_unit("/usr/bin/llama-server", Path("/m/e.gguf"), 0)
        rerank = _render_llama_rerank_unit("/usr/bin/llama-server", Path("/m/r.gguf"))
        assert "-ngl" not in embed
        assert "-ngl" not in rerank


class TestResolvePreset:
    """Regression coverage for the hardware-preset resolution that selects -ngl.

    The persistent embed/reranker units were registered CPU-only on every GPU
    host because ``enable-service`` read the preset from ``st.get("preset")`` —
    a key install state never writes — so it always resolved to ``None``. The
    preset actually lives in recommend-models.json (the source the setup-time
    launchers read). These tests pin that resolution.
    """

    def _write_recommend_models(self, preset: str) -> None:
        out = install_state.outputs_dir()
        out.mkdir(parents=True, exist_ok=True)
        (out / "recommend-models.json").write_text(json.dumps({"preset": preset}))

    def test_reads_preset_from_recommend_models(self) -> None:
        self._write_recommend_models("apple-silicon")
        # Install state has no "preset" key (the historical bug); resolution
        # must come from recommend-models.json, not fall through to None.
        assert _resolve_preset({"port": 47950}) == "apple-silicon"

    def test_resolved_gpu_preset_yields_offload(self) -> None:
        """End-to-end: a GPU preset in recommend-models.json must drive -ngl > 0."""
        for preset in ("apple-silicon", "nvidia", "radeon"):
            self._write_recommend_models(preset)
            assert _ngl_for_target(_resolve_preset({})) == 999

    def test_cpu_preset_keeps_offload_off(self) -> None:
        self._write_recommend_models("cpu")
        assert _ngl_for_target(_resolve_preset({})) == 0

    def test_falls_back_to_state_then_none(self) -> None:
        # No recommend-models.json present -> fall back to install state, then None.
        assert _resolve_preset({"preset": "nvidia"}) == "nvidia"
        assert _resolve_preset({}) is None

    def test_malformed_recommend_models_falls_back(self) -> None:
        out = install_state.outputs_dir()
        out.mkdir(parents=True, exist_ok=True)
        (out / "recommend-models.json").write_text("{not json")
        assert _resolve_preset({"preset": "radeon"}) == "radeon"


class TestRenderLaunchdPlist:
    def test_valid_xml_structure(self) -> None:
        content = _render_launchd_plist("/usr/bin/uv", Path("/app"), 8000, {"LOG_LEVEL": "INFO"})
        assert '<?xml version="1.0"' in content
        assert "<key>Label</key>" in content
        assert "<string>ai.agentalloy</string>" in content

    def test_port_injected(self) -> None:
        content = _render_launchd_plist("/usr/bin/uv", Path("/app"), 9000, {})
        assert "<string>9000</string>" in content

    def test_env_vars_inlined(self) -> None:
        content = _render_launchd_plist("/usr/bin/uv", Path("/app"), 8000, {"MY_KEY": "MY_VAL"})
        assert "<key>MY_KEY</key>" in content
        assert "<string>MY_VAL</string>" in content

    def test_run_at_load_true(self) -> None:
        content = _render_launchd_plist("/usr/bin/uv", Path("/app"), 8000, {})
        assert "<key>RunAtLoad</key>" in content
        assert "<true/>" in content


class TestRenderLlamaLaunchdPlist:
    """The macOS LaunchAgent plists for the embed/reranker llama-servers."""

    def test_embed_plist_uses_embeddings_mode_on_47951(self) -> None:
        content = _render_llama_launchd_plist(
            "ai.agentalloy.embed",
            [
                "/usr/bin/llama-server",
                "--embeddings",
                "--port",
                "47951",
                "-m",
                "/data/models/Qwen3-Embedding-0.6B-Q8_0.gguf",
            ],
        )
        assert '<?xml version="1.0"' in content
        assert "<string>ai.agentalloy.embed</string>" in content
        assert "<string>--embeddings</string>" in content
        assert "<string>47951</string>" in content
        assert "<key>RunAtLoad</key>" in content
        assert "<key>KeepAlive</key>" in content

    def test_rerank_plist_is_completions_mode_on_47952(self) -> None:
        content = _render_llama_launchd_plist(
            "ai.agentalloy.rerank",
            [
                "/usr/bin/llama-server",
                "--port",
                "47952",
                "-m",
                "/data/models/Qwen3-Reranker-0.6B-Q8_0.gguf",
            ],
        )
        # Reranker is a completions server — must NOT pass --embeddings.
        assert "--embeddings" not in content
        assert "<string>47952</string>" in content
        assert "<string>ai.agentalloy.rerank</string>" in content


class TestWriteLlamaLaunchdAgents:
    """``_write_llama_launchd_agents`` — the macOS mirror of ``_write_llama_units``."""

    def test_skips_gracefully_when_llama_server_absent(self) -> None:
        with patch(
            "agentalloy.install.subcommands.enable_service.shutil.which",
            return_value=None,
        ):
            written = _write_llama_launchd_agents()
        assert written == []

    def test_writes_and_loads_both_agents(self, tmp_path: Path) -> None:
        loaded: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            loaded.append(cmd)
            return MagicMock(returncode=0, stderr="")

        with (
            patch(
                "agentalloy.install.subcommands.enable_service.shutil.which",
                return_value="/usr/local/bin/llama-server",
            ),
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "agentalloy.install.subcommands.enable_service.subprocess.run",
                side_effect=fake_run,
            ),
            patch("agentalloy.install.subcommands.enable_service.os.chmod"),
        ):
            written = _write_llama_launchd_agents()

        agents_dir = tmp_path / "Library" / "LaunchAgents"
        assert str(agents_dir / "ai.agentalloy.embed.plist") in written
        assert str(agents_dir / "ai.agentalloy.rerank.plist") in written
        # Both plists were actually written to disk.
        assert (agents_dir / "ai.agentalloy.embed.plist").exists()
        assert (agents_dir / "ai.agentalloy.rerank.plist").exists()
        # launchctl load was invoked for both labels (plus unload calls).
        load_cmds = [c for c in loaded if "load" in c]
        assert len(load_cmds) == 2


class TestReadEnvFile:
    def test_parses_key_value(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("FOO=bar\nBAZ=qux\n")
        assert _read_env_file(env) == {"FOO": "bar", "BAZ": "qux"}

    def test_skips_comments(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("# comment\nKEY=val\n")
        assert _read_env_file(env) == {"KEY": "val"}

    def test_strips_quotes(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('KEY="quoted"\n')
        assert _read_env_file(env) == {"KEY": "quoted"}

    def test_handles_export_prefix(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("export KEY=val\n")
        assert _read_env_file(env) == {"KEY": "val"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _read_env_file(tmp_path / "nonexistent.env") == {}


# ---------------------------------------------------------------------------
# Poll health
# ---------------------------------------------------------------------------


class TestPollHealth:
    def test_returns_true_on_ok_status(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s  # type: ignore[misc]
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"status": "ok"}'

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert _poll_health(8000) is True

    def test_returns_false_on_timeout(self) -> None:
        import urllib.error

        with (
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")),
            patch("agentalloy.install.subcommands.enable_service._HEALTH_TIMEOUT_S", 0),
        ):
            assert _poll_health(8000) is False

    def test_returns_false_on_degraded_status(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s  # type: ignore[misc]
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"status": "degraded"}'

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch(
                "agentalloy.install.subcommands.enable_service._HEALTH_TIMEOUT_S",
                0,
            ),
        ):
            assert _poll_health(8000) is False


# ---------------------------------------------------------------------------
# enable_service() integration-level unit tests (all I/O mocked)
# ---------------------------------------------------------------------------


class TestEnableServiceManual:
    def test_manual_mode_returns_correct_schema(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = enable_service(mode="manual", port=8000, repo_root=tmp_path)
        assert result["schema_version"] == 1
        assert result["mode"] == "manual"
        assert result["service_started"] is False
        assert result["runtime"] is None
        assert result["unit_path"] is None

    def test_manual_mode_prints_serve_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        enable_service(mode="manual", port=8000, repo_root=tmp_path)
        captured = capsys.readouterr()
        assert "agentalloy serve" in captured.err


class TestEnableServiceInvalidMode:
    def test_unknown_mode_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            enable_service(mode="bogus", port=8000, repo_root=tmp_path)


class TestEnableServiceNativeWindows:
    def test_windows_native_exits(self, tmp_path: Path) -> None:
        with patch("platform.system", return_value="Windows"), pytest.raises(SystemExit):
            enable_service(mode="native", port=8000, repo_root=tmp_path)


class TestOutputSchema:
    def test_all_required_keys_present(self, tmp_path: Path) -> None:
        result = enable_service(mode="manual", port=8000, repo_root=tmp_path)
        for key in (
            "schema_version",
            "mode",
            "runtime",
            "unit_path",
            "ollama_unit_written",
            "service_started",
        ):
            assert key in result, f"missing key: {key}"

    def test_schema_version_is_1(self, tmp_path: Path) -> None:
        result = enable_service(mode="manual", port=8000, repo_root=tmp_path)
        assert result["schema_version"] == 1
