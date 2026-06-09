"""Tests for Code-Indexer integration — contract → query construction and wire-harness detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_contract(path: Path, *, scope_touches: list[str] | None = None) -> Path:
    touches = ["src/auth/**"] if scope_touches is None else scope_touches
    fm = {
        "phase": "build",
        "task_slug": "add-auth-middleware",
        "domain_tags": ["NestJS", "JWT validation"],
        "scope": {"touches": touches, "avoids": []},
        "success_criteria": ["Tests pass"],
        "related_contracts": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.dump(fm)}---\n\n# Add Auth Middleware\n\nTask description here.\n")
    return path


# ---------------------------------------------------------------------------
# code_indexer_query_params
# ---------------------------------------------------------------------------


def test_query_params_from_full_contract(tmp_path: Path):
    from agentalloy.contracts import code_indexer_query_params, parse_contract

    f = _write_contract(tmp_path / "c.md")
    contract = parse_contract(f)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout="git@github.com:nrmeyers/agentalloy.git\n",
            returncode=0,
        )
        params = code_indexer_query_params(contract, tmp_path)

    assert params.repo == "github.com/nrmeyers__agentalloy"
    assert params.semantic_q == "Add Auth Middleware"
    assert params.lexical_q == "NestJS JWT validation"
    assert "src/auth/**" in params.path_globs


def test_query_params_empty_scope_touches_whole_repo(tmp_path: Path):
    from agentalloy.contracts import code_indexer_query_params, parse_contract

    f = _write_contract(tmp_path / "c.md", scope_touches=[])
    contract = parse_contract(f)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        params = code_indexer_query_params(contract, tmp_path)

    assert params.path_globs == []


def test_query_params_handles_non_github_remote(tmp_path: Path):
    from agentalloy.contracts import code_indexer_query_params, parse_contract

    f = _write_contract(tmp_path / "c.md")
    contract = parse_contract(f)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout="https://gitlab.com/myorg/myrepo.git\n",
            returncode=0,
        )
        params = code_indexer_query_params(contract, tmp_path)

    assert params.repo == "gitlab.com/myorg__myrepo"


def test_query_params_falls_back_to_dir_name(tmp_path: Path):
    from agentalloy.contracts import code_indexer_query_params, parse_contract

    f = _write_contract(tmp_path / "c.md")
    contract = parse_contract(f)

    with patch("subprocess.run", side_effect=OSError("no git")):
        params = code_indexer_query_params(contract, tmp_path)

    assert params.repo == tmp_path.name


# ---------------------------------------------------------------------------
# Wire-harness: probe code-indexer and write to state.json
# ---------------------------------------------------------------------------


def test_state_json_records_code_indexer_presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agentalloy.install import state as install_state
    from agentalloy.install.subcommands.wire_harness import (
        _probe_code_indexer,  # pyright: ignore[reportPrivateUsage]
    )

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _probe_code_indexer(tmp_path)

    st = install_state.load_state(tmp_path)
    assert "code_indexer" in st
    assert st["code_indexer"]["reachable"] is True
    assert "last_health_at" in st["code_indexer"]


def test_state_json_records_unreachable(tmp_path: Path):
    from agentalloy.install import state as install_state
    from agentalloy.install.subcommands.wire_harness import (
        _probe_code_indexer,  # pyright: ignore[reportPrivateUsage]
    )

    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        _probe_code_indexer(tmp_path)

    st = install_state.load_state(tmp_path)
    assert st["code_indexer"]["reachable"] is False


# ---------------------------------------------------------------------------
# Hook skips code-indexer when unreachable
# ---------------------------------------------------------------------------


def test_hook_skips_code_indexer_when_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """code-indexer-from-contract returns 0 and emits no output when code-indexer is down."""
    from agentalloy.install.subcommands import signal as sig

    contract_path = tmp_path / ".agentalloy" / "contracts" / "build" / "task.md"
    _write_contract(contract_path)

    import io
    import sys

    captured = io.StringIO()
    with (
        patch("urllib.request.urlopen", side_effect=OSError("refused")),
        patch.object(sig, "_write_telemetry"),
    ):
        import argparse

        args = argparse.Namespace(path=str(contract_path))
        sys.stdout = captured
        try:
            rc = sig._code_indexer_from_contract(args)  # pyright: ignore[reportPrivateUsage]
        finally:
            sys.stdout = sys.__stdout__

    assert rc == 0
    assert captured.getvalue() == ""  # no output when unreachable
