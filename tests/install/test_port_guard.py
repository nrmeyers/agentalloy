"""Unit tests for the shared port_guard helper."""

from __future__ import annotations

import socket

from agentalloy.install import port_guard
from agentalloy.install.runtime_artifacts import EMBED_PORT, RERANK_PORT


class TestClassifyPort:
    def test_free_port(self) -> None:
        # Bind an ephemeral port, then release it so it's almost certainly free.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        status, detail = port_guard.classify_port(port)
        assert status == "free"
        assert str(port) in detail

    def test_non_http_listener_is_foreign(self) -> None:
        # A bare TCP listener that never answers HTTP → foreign_nonhttp.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            status, _ = port_guard.classify_port(port, timeout=1.0)
            assert status == "foreign_nonhttp"
        finally:
            srv.close()


class TestReservedPorts:
    def test_derives_from_preset_urls(self) -> None:
        values = {
            "RUNTIME_EMBED_BASE_URL": "http://localhost:47951",
            "SIGNAL_INTENT_RERANK_URL": "http://127.0.0.1:47952",
        }
        reserved = port_guard.reserved_ports(values)
        assert reserved == {47951: "embed", 47952: "reranker"}

    def test_honors_overridden_url(self) -> None:
        values = {
            "RUNTIME_EMBED_BASE_URL": "http://localhost:48000",
            "SIGNAL_INTENT_RERANK_URL": "http://127.0.0.1:47952",
        }
        reserved = port_guard.reserved_ports(values)
        assert reserved == {48000: "embed", 47952: "reranker"}

    def test_falls_back_to_constants(self) -> None:
        reserved = port_guard.reserved_ports({})
        assert reserved == {EMBED_PORT: "embed", RERANK_PORT: "reranker"}
