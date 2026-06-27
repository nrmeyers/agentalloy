"""Tests for the rerank-warmup install subcommand (#16 — eliminates the
first-request Stage B fallback after a cold reranker restart)."""

from __future__ import annotations

import argparse
import urllib.error
from unittest.mock import MagicMock, patch

from agentalloy.install.subcommands import rerank_warmup


def _args(port: int = 47952, health_timeout: float = 5.0, parallel: int = 8) -> argparse.Namespace:
    return argparse.Namespace(port=port, health_timeout=health_timeout, parallel=parallel)


def test_run_returns_zero_when_health_never_responds() -> None:
    """The unit-blocking contract: a transient warmup failure must never block
    the systemd unit from becoming active — return 0 unconditionally."""
    with patch.object(rerank_warmup, "_wait_for_health", return_value=False) as wait_mock:
        rc = rerank_warmup._run(_args(health_timeout=0.1))
    assert rc == 0
    wait_mock.assert_called_once()


def test_run_returns_zero_when_warmup_request_fails() -> None:
    """Even a successful health probe followed by a failed completion call must
    not block the unit — the breaker handles persistent reranker problems at
    compose time."""
    with (
        patch.object(rerank_warmup, "_wait_for_health", return_value=True),
        patch.object(rerank_warmup, "_warmup_request", return_value=None),
    ):
        assert rerank_warmup._run(_args()) == 0


def test_run_returns_zero_on_successful_warmup() -> None:
    with (
        patch.object(rerank_warmup, "_wait_for_health", return_value=True),
        patch.object(rerank_warmup, "_warmup_request", return_value=0.12),
    ):
        assert rerank_warmup._run(_args()) == 0


def test_run_fans_out_to_all_slots() -> None:
    """The whole point of #16: one warmup compiles ONE slot's graph; the next
    Stage B fan-out then hits the OTHER N-1 cold slots. Must fire N parallel
    warmups so every slot is warm before real traffic arrives."""
    calls: list[str] = []
    with (
        patch.object(rerank_warmup, "_wait_for_health", return_value=True),
        patch.object(
            rerank_warmup,
            "_warmup_request",
            side_effect=lambda base: (calls.append(base), 0.1)[1],
        ),
    ):
        rerank_warmup._run(_args(parallel=8))
    assert len(calls) == 8, f"expected 8 parallel warmups, got {len(calls)}"


def test_warmup_all_slots_counts_successes() -> None:
    # 5 of 8 succeed; failures don't crash the helper.
    results = [0.1, None, 0.2, None, 0.3, 0.4, None, 0.5]
    with patch.object(rerank_warmup, "_warmup_request", side_effect=results):
        n = rerank_warmup._warmup_all_slots("http://127.0.0.1:47952", parallel=8)
    assert n == 5


def test_wait_for_health_returns_true_on_200() -> None:
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.__enter__ = lambda self: fake_resp
    fake_resp.__exit__ = lambda self, *a: None
    with patch.object(rerank_warmup.urllib.request, "urlopen", return_value=fake_resp):
        assert rerank_warmup._wait_for_health("http://127.0.0.1:47952", timeout_s=1.0)


def test_wait_for_health_returns_false_on_persistent_refused() -> None:
    def _raise(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("connection refused")

    with patch.object(rerank_warmup.urllib.request, "urlopen", side_effect=_raise):
        # Tight timeout so the test doesn't hang.
        assert not rerank_warmup._wait_for_health("http://127.0.0.1:47952", timeout_s=0.2)


def test_warmup_request_returns_none_on_error() -> None:
    def _raise(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("boom")

    with patch.object(rerank_warmup.urllib.request, "urlopen", side_effect=_raise):
        assert rerank_warmup._warmup_request("http://127.0.0.1:47952") is None


def test_add_parser_registers_subcommand() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")
    rerank_warmup.add_parser(subparsers)
    args = parser.parse_args(["rerank-warmup", "--port", "47000"])
    assert args.subcommand == "rerank-warmup"
    assert args.port == 47000
    assert callable(args.func)
