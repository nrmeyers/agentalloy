"""Tests for GET /diagnostics/corpus — corpus counts off the live store handles."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_corpus_counts_returned(app: FastAPI) -> None:
    store = MagicMock()
    store.execute.return_value = [[30]]
    vector_store = MagicMock()
    vector_store.count_embeddings.return_value = 412
    app.state.store = store
    app.state.vector_store = vector_store

    with TestClient(app) as client:
        resp = client.get("/diagnostics/corpus")

    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_count"] == 30
    assert body["embedded_vector_count"] == 412
    assert body["embedding_dim"] == 768


def test_corpus_partial_when_one_store_fails(app: FastAPI) -> None:
    # Skill store raises; vector store still answers — endpoint must not 500.
    store = MagicMock()
    store.execute.side_effect = Exception("Could not set lock on file")
    vector_store = MagicMock()
    vector_store.count_embeddings.return_value = 7
    app.state.store = store
    app.state.vector_store = vector_store

    with TestClient(app) as client:
        resp = client.get("/diagnostics/corpus")

    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_count"] == 0
    assert body["embedded_vector_count"] == 7
    assert body["embedding_dim"] == 768


def test_corpus_no_handles_returns_zeros(app: FastAPI) -> None:
    # No store handles on app.state (no lifespan) → 0 / null, never a 500.
    with TestClient(app) as client:
        resp = client.get("/diagnostics/corpus")

    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_count"] == 0
    assert body["embedded_vector_count"] == 0
    assert body["embedding_dim"] is None
