"""Stage A — cross-encoder rerank stage.

All tests use a FAKE scorer (the Reranker protocol) — no model downloads, no
network. Covers the factory, the failure latch, the reorder math, and the two
backends' import/transport plumbing.
"""

from __future__ import annotations

import httpx
import pytest

import agentalloy.retrieval.rerank as rerank_module
from agentalloy.retrieval.rerank import (
    HttpReranker,
    OnnxReranker,
    _FailureLatch,  # pyright: ignore[reportPrivateUsage]
    _LatchedReranker,  # pyright: ignore[reportPrivateUsage]
    build_reranker_from_env,
    rerank_max_pairs,
    reset_reranker_cache,
)

_RERANK_ENV = (
    "RUNTIME_RERANK_MODE",
    "RUNTIME_RERANK_ONNX_DIR",
    "RUNTIME_RERANK_BASE_URL",
    "RUNTIME_RERANK_MODEL",
    "RUNTIME_RERANK_TIMEOUT_MS",
    "RUNTIME_RERANK_MAX_PAIRS",
)


@pytest.fixture(autouse=True)
def _clean_rerank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RERANK_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_reranker_cache()
    yield
    reset_reranker_cache()


class FakeReranker:
    """Returns preset scores; records the calls it received."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        return list(self._scores[: len(passages)])


class RaisingReranker:
    def __init__(self) -> None:
        self.call_count = 0

    def score(self, query: str, passages: list[str]) -> list[float]:  # noqa: ARG002
        self.call_count += 1
        raise RuntimeError("boom")


# -------- factory --------


def test_factory_mode_off_returns_none() -> None:
    assert build_reranker_from_env() is None


def test_factory_onnx_missing_dir_returns_none_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("RUNTIME_RERANK_MODE", "onnx")
    monkeypatch.setenv("RUNTIME_RERANK_ONNX_DIR", "/no/such/dir")
    with caplog.at_level("WARNING"):
        assert build_reranker_from_env() is None
    assert any("RUNTIME_RERANK_ONNX_DIR" in r.message for r in caplog.records)


def test_factory_http_missing_base_url_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_RERANK_MODE", "http")
    monkeypatch.setenv("RUNTIME_RERANK_MODEL", "qwen-rerank")
    assert build_reranker_from_env() is None


def test_factory_unknown_mode_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_RERANK_MODE", "bogus")
    assert build_reranker_from_env() is None


def test_factory_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_RERANK_MODE", "http")
    monkeypatch.setenv("RUNTIME_RERANK_BASE_URL", "http://localhost:8090")
    monkeypatch.setenv("RUNTIME_RERANK_MODEL", "qwen-rerank")
    first = build_reranker_from_env()
    second = build_reranker_from_env()
    assert first is not None
    assert first is second


def test_rerank_max_pairs_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert rerank_max_pairs() == 32
    monkeypatch.setenv("RUNTIME_RERANK_MAX_PAIRS", "8")
    assert rerank_max_pairs() == 8


# -------- failure latch --------


def test_latch_opens_after_threshold_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rerank_module.time, "monotonic", lambda: now[0])
    latch = _FailureLatch(threshold=3, cooldown=60.0)

    for _ in range(3):
        assert latch.allow()
        latch.record_failure()
    # Latch open — calls blocked until cooldown elapses.
    assert not latch.allow()
    now[0] += 30.0
    assert not latch.allow()
    now[0] += 31.0
    assert latch.allow()  # cooldown elapsed → one retry allowed


def test_latch_success_resets_counter() -> None:
    latch = _FailureLatch(threshold=3, cooldown=60.0)
    latch.record_failure()
    latch.record_failure()
    latch.record_success()
    latch.record_failure()
    latch.record_failure()
    assert latch.allow()  # two then two — never reached threshold of 3


def test_latched_reranker_degrades_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(rerank_module.time, "monotonic", lambda: now[0])
    inner = RaisingReranker()
    latched = _LatchedReranker(inner)

    for _ in range(3):
        assert latched.score("q", ["p"]) == []
    assert inner.call_count == 3
    # Latch now open — inner scorer is not called during cooldown.
    assert latched.score("q", ["p"]) == []
    assert inner.call_count == 3
    # After cooldown, the scorer is retried.
    now[0] += 61.0
    assert latched.score("q", ["p"]) == []
    assert inner.call_count == 4


def test_latched_reranker_rejects_length_mismatch() -> None:
    latched = _LatchedReranker(FakeReranker([0.1]))
    assert latched.score("q", ["a", "b"]) == []  # 1 score for 2 passages → ignored


def test_latched_reranker_passes_scores_through() -> None:
    latched = _LatchedReranker(FakeReranker([0.5, 0.9]))
    assert latched.score("q", ["a", "b"]) == [0.5, 0.9]


# -------- HttpReranker --------


def _http_reranker(handler) -> HttpReranker:  # type: ignore[no-untyped-def]
    r = HttpReranker("http://localhost:8090", "qwen-rerank")
    r._client = httpx.Client(  # pyright: ignore[reportPrivateUsage]
        base_url="http://localhost:8090",
        transport=httpx.MockTransport(handler),
    )
    return r


def test_http_reranker_request_shape_and_score_extraction() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.5},
                ]
            },
        )

    r = _http_reranker(handler)
    scores = r.score("my query", ["a", "b", "c"])
    assert scores == [0.2, 0.9, 0.5]
    assert seen["path"] == "/v1/rerank"
    assert seen["body"] == {
        "model": "qwen-rerank",
        "query": "my query",
        "documents": ["a", "b", "c"],
    }


def test_http_reranker_out_of_order_results() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.7},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    r = _http_reranker(handler)
    # index 1 absent → defaults to 0.0
    assert r.score("q", ["a", "b", "c"]) == [0.1, 0.0, 0.7]


def test_http_reranker_malformed_response_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": []})

    r = _http_reranker(handler)
    with pytest.raises(ValueError):
        r.score("q", ["a"])


def test_http_reranker_latched_swallows_transport_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    latched = _LatchedReranker(_http_reranker(handler))
    assert latched.score("q", ["a"]) == []  # 503 → raise_for_status → swallowed


# -------- OnnxReranker --------


def test_onnx_reranker_lazy_import_error_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name in ("onnxruntime", "tokenizers"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = OnnxReranker("/some/dir")
    with pytest.raises(ImportError) as exc:
        r.score("q", ["passage"])
    assert "rerank" in str(exc.value)
    assert "onnxruntime" in str(exc.value)
