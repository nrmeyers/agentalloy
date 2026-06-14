"""Unit tests for the ``start-rerank-server`` subcommand.

The reranker runs as a SECOND llama-server instance (separate from the embed
server) on port 47952 in COMPLETIONS mode (no ``--embeddings``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from agentalloy.install.subcommands import start_rerank_server as srs


def _models_file(tmp_path: Path, *, rerank_model: str = "Qwen3-Reranker-0.6B-Q8_0.gguf") -> Path:
    p = tmp_path / "recommend-models.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "options": [
                    {
                        "default": True,
                        "embed_model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
                        "embed_runner": "llama-server",
                        "rerank_model": rerank_model,
                        "rerank_runner": "llama-server",
                    }
                ],
            }
        )
    )
    return p


def _args(models: Path, **kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "models": str(models),
        "hardware_target": "cpu",
        "timeout": 1.0,
        "json": False,
        "quiet": True,
    }
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Constants / wiring
# ---------------------------------------------------------------------------


class TestConstants:
    def test_port_is_47952(self) -> None:
        assert srs.LLAMA_RERANK_PORT == 47952

    def test_ngl_map_covers_targets(self) -> None:
        for t in ("cpu", "nvidia", "radeon", "apple-silicon"):
            assert t in srs._NGL_BY_TARGET  # pyright: ignore[reportPrivateUsage]
        assert srs._NGL_BY_TARGET["cpu"] == 0  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


class TestResolveRerankModel:
    def test_reads_rerank_model(self, tmp_path: Path) -> None:
        data = json.loads(_models_file(tmp_path).read_text())
        assert (
            srs._resolve_rerank_model(data)  # pyright: ignore[reportPrivateUsage]
            == "Qwen3-Reranker-0.6B-Q8_0.gguf"
        )

    def test_missing_rerank_model_returns_empty(self) -> None:
        data: dict[str, Any] = {"options": [{"default": True, "embed_model": "x"}]}
        assert srs._resolve_rerank_model(data) == ""  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_skips_when_port_already_open(self, tmp_path: Path) -> None:
        models = _models_file(tmp_path)
        with (
            patch.object(srs, "_port_open", return_value=True),
            patch.object(srs, "_save"),
            patch("subprocess.Popen") as popen,
        ):
            rc = srs.run(_args(models))
        assert rc == 0
        popen.assert_not_called()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_models_file(self, tmp_path: Path) -> None:
        rc = srs.run(_args(tmp_path / "nope.json"))
        assert rc == 1

    def test_missing_gguf_on_disk(self, tmp_path: Path) -> None:
        models = _models_file(tmp_path)
        with patch.object(srs, "_port_open", return_value=False):
            rc = srs.run(_args(models))
        # GGUF was never downloaded into the data dir → error
        assert rc == 1


# ---------------------------------------------------------------------------
# Launch — completions mode (NO --embeddings)
# ---------------------------------------------------------------------------


class TestLaunch:
    def test_completions_mode_no_embeddings_flag(self, tmp_path: Path) -> None:
        models = _models_file(tmp_path)
        gguf_dir = tmp_path / "models"
        gguf_dir.mkdir()
        (gguf_dir / "Qwen3-Reranker-0.6B-Q8_0.gguf").write_text("stub")

        # First _port_open call (idempotency) → False; subsequent (health poll) → True.
        port_states = iter([False, True])

        with (
            patch.object(srs.install_state, "user_data_dir", return_value=tmp_path),
            patch.object(srs, "_port_open", side_effect=lambda *a, **k: next(port_states, True)),
            patch.object(srs, "_save"),
            patch("subprocess.Popen") as popen,
        ):
            popen.return_value = MagicMock()
            rc = srs.run(_args(models, hardware_target="nvidia"))

        assert rc == 0
        cmd = popen.call_args[0][0]
        assert "llama-server" in cmd
        assert "--embeddings" not in cmd  # reranker is a completions server
        assert "--port" in cmd
        assert str(srs.LLAMA_RERANK_PORT) in cmd
        # nvidia target offloads all layers
        assert "-ngl" in cmd
        assert "999" in cmd
