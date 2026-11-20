"""End-to-end corpus pipeline: ingest → reembed → hybrid search → compose trace.

Exercises the corpus pipeline against a live embed runtime + the unified
OverGraph corpus store at a tmp path (does not touch ``data/``). Covers:
  - fresh store is empty
  - ingest populates the skill graph (graph-only)
  - reembed populates the fragment embedding index with L2-normalized vectors
  - self-search round-trip returns the exact match
  - category filter narrows search
  - compose writes a composition trace to telemetry
  - missing-embed-model surfaces as a structured 503
  - de-Ollama guard: no legacy ``agentalloy.ollama`` references remain

Skipped gracefully if the configured embed runtime
(``RUNTIME_EMBED_BASE_URL``, default ``http://localhost:47951``) is
unreachable.

Run locally::

    uv run pytest -m integration tests/integration/test_corpus_pipeline_e2e.py -v

All tests are marked ``integration`` per the marker registered in
pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx
import pytest

from agentalloy.config import Settings
from agentalloy.ingest import (
    _insert,  # pyright: ignore[reportPrivateUsage]
    _load_yaml,  # pyright: ignore[reportPrivateUsage]
    _validate,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.lm_client import (
    LMClientError,
    LMModelNotLoaded,
    OpenAICompatClient,
)
from agentalloy.reembed import discover_unembedded_fragments, reembed_fragments
from agentalloy.storage.overgraph_skill_store import OverGraphSkillStore, open_overgraph_skill_store
from agentalloy.storage.protocols import EMBEDDING_DIM
from agentalloy.storage.telemetry_store import open_telemetry_store

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_settings = Settings()
EMBED_BASE_URL = _settings.runtime_embed_base_url
EMBED_MODELS_URL = f"{EMBED_BASE_URL}/v1/models"
EMBEDDING_MODEL = _settings.runtime_embedding_model

REPO_ROOT = Path(__file__).resolve().parents[2]
# seeds/*.yaml are review-YAML-shaped (matches what ingest consumes). The
# fixtures/domain/*.yaml files use a different multi-version export shape
# that's loaded by fixtures/loader.py, not ingest.py.
FIXTURE_SKILL = (
    REPO_ROOT / "src" / "agentalloy" / "_packs" / "core" / "test-driven-development.yaml"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def embed_runtime_available() -> bool:
    """One probe per test module. Used by the ``embed_runtime_required`` fixture."""
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)) as c:
            resp = c.get(EMBED_MODELS_URL)
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def embed_runtime_models(embed_runtime_available: bool) -> list[str]:
    """The set of models currently loaded. Used to verify embedding model presence."""
    if not embed_runtime_available:
        return []
    with httpx.Client(timeout=5.0) as c:
        body = cast("dict[str, object]", c.get(EMBED_MODELS_URL).json())
    raw = body.get("data")
    if not isinstance(raw, list):
        return []
    return [str(e["id"]) for e in cast("list[dict[str, object]]", raw) if "id" in e]


@pytest.fixture
def embed_runtime_required(embed_runtime_available: bool) -> None:
    if not embed_runtime_available:
        pytest.skip("embed runtime not reachable at " + EMBED_MODELS_URL)


@pytest.fixture
def embedding_model_required(embed_runtime_models: list[str]) -> None:
    if EMBEDDING_MODEL not in embed_runtime_models:
        pytest.skip(f"{EMBEDDING_MODEL} not loaded in embed runtime (have: {embed_runtime_models})")


@pytest.fixture
def fresh_skills(tmp_path: Path):
    """A migrated, empty unified corpus store at a tmp path."""
    store = open_overgraph_skill_store(str(tmp_path / "agentalloy.overgraph"))
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def fresh_fragments(fresh_skills: OverGraphSkillStore):
    """Unified store: the fragment/vector side is the same store."""
    return fresh_skills


# ---------------------------------------------------------------------------
# Step 1: fresh state has no data
# ---------------------------------------------------------------------------


def test_step_1_fresh_stores_are_empty(
    fresh_skills: OverGraphSkillStore, fresh_fragments: OverGraphSkillStore
) -> None:
    """A fresh unified store carries no skills, fragments, or embeddings."""
    assert fresh_skills.count_skills() == 0
    assert fresh_skills.count_fragments() == 0
    assert fresh_fragments.count_embeddings() == 0


# ---------------------------------------------------------------------------
# Step 2+3: ingest populates the skill graph, embeddings remain empty
# ---------------------------------------------------------------------------


def test_step_2_ingest_populates_skill_store_only(
    fresh_skills: OverGraphSkillStore, fresh_fragments: OverGraphSkillStore
) -> None:
    """Ingest writes the skill graph. The fragment embedding index stays empty
    (the v1.5 model separates ingest from embedding)."""
    record = _load_yaml(FIXTURE_SKILL)
    assert _validate(record) == []
    _insert(fresh_skills, record, force=False)

    assert fresh_skills.get_skill(record.skill_id) is not None
    assert len(fresh_skills.get_versions_by_skill(record.skill_id)) == 1
    fragment_count = fresh_skills.count_fragments()
    assert fragment_count == len(record.fragments) > 0

    # Critically: nothing embedded yet — ingest is graph-only in the v1.5 model.
    assert fresh_fragments.count_embeddings() == 0


# ---------------------------------------------------------------------------
# Step 4+5: reembed populates the fragment embedding index with L2-normalized vectors
# ---------------------------------------------------------------------------


def test_step_4_reembed_populates_lance(
    fresh_skills: OverGraphSkillStore,
    fresh_fragments: OverGraphSkillStore,
    embed_runtime_required: None,
    embedding_model_required: None,
) -> None:
    """After reembed: the index has one row per Fragment, the embedding dim matches the
    runtime contract, denormalized columns are populated correctly."""
    record = _load_yaml(FIXTURE_SKILL)
    _insert(fresh_skills, record, force=False)

    fragments = discover_unembedded_fragments(fresh_skills, fresh_fragments)
    assert len(fragments) == len(record.fragments)
    assert all(f.category == record.category for f in fragments)

    with OpenAICompatClient(EMBED_BASE_URL) as client:

        def embed(text: str) -> list[float]:
            return client.embed(model=EMBEDDING_MODEL, texts=[text])[0]

        stats = reembed_fragments(
            fragments,
            embed_fn=embed,
            vector_store=fresh_fragments,
            embedding_model=EMBEDDING_MODEL,
        )

    assert stats.failed == 0
    assert stats.embedded == len(fragments)
    assert fresh_fragments.count_embeddings() == len(fragments)

    # Vectors are L2-normalized on insert inside the corpus store. The stored dim is
    # the contract; the normalized vectors' coherence is proven end-to-end by the
    # self-search round-trip in test_step_5 (near-zero cosine distance).
    assert fresh_fragments.embedding_dim() == EMBEDDING_DIM


def test_step_5_search_roundtrip_returns_exact_match(
    fresh_skills: OverGraphSkillStore,
    fresh_fragments: OverGraphSkillStore,
    embed_runtime_required: None,
    embedding_model_required: None,
) -> None:
    """Embedding the same content twice should yield a near-zero-distance
    search result — proves the full write/query path is coherent."""
    record = _load_yaml(FIXTURE_SKILL)
    _insert(fresh_skills, record, force=False)

    fragments = discover_unembedded_fragments(fresh_skills, fresh_fragments)

    with OpenAICompatClient(EMBED_BASE_URL) as client:

        def embed(text: str) -> list[float]:
            return client.embed(model=EMBEDDING_MODEL, texts=[text])[0]

        reembed_fragments(
            fragments,
            embed_fn=embed,
            vector_store=fresh_fragments,
            embedding_model=EMBEDDING_MODEL,
        )

        # Re-embed the first fragment's content and search — it should be its own top hit.
        target = fragments[0]
        query_vec = client.embed(model=EMBEDDING_MODEL, texts=[target.content])[0]

    hits = fresh_fragments.search_similar(query_vec, k=5)
    assert hits, "expected at least one hit"
    assert hits[0].fragment_id == target.fragment_id
    # Distance should be essentially zero — same content, same model, same norm.
    assert hits[0].distance < 1e-4


def test_step_5b_category_filter_narrows_search(
    fresh_skills: OverGraphSkillStore,
    fresh_fragments: OverGraphSkillStore,
    embed_runtime_required: None,
    embedding_model_required: None,
) -> None:
    """Denormalized-column filters should restrict results — a filter for a
    category the skill doesn't belong to returns nothing."""
    record = _load_yaml(FIXTURE_SKILL)
    _insert(fresh_skills, record, force=False)
    fragments = discover_unembedded_fragments(fresh_skills, fresh_fragments)

    with OpenAICompatClient(EMBED_BASE_URL) as client:

        def embed(text: str) -> list[float]:
            return client.embed(model=EMBEDDING_MODEL, texts=[text])[0]

        reembed_fragments(
            fragments,
            embed_fn=embed,
            vector_store=fresh_fragments,
            embedding_model=EMBEDDING_MODEL,
        )
        q = client.embed(model=EMBEDDING_MODEL, texts=[fragments[0].content])[0]

    # Filter on a category this skill doesn't have.
    other = "safety" if record.category != "safety" else "governance"
    hits = fresh_fragments.search_similar(q, k=10, categories=[other])
    assert hits == []


# ---------------------------------------------------------------------------
# Step 6+7: compose writes a composition trace
# ---------------------------------------------------------------------------


def test_step_6_compose_writes_composition_trace(tmp_path: Path) -> None:
    """End-to-end /compose call writes a composition_traces row to telemetry.duck."""
    import asyncio

    from agentalloy.api.compose_models import ComposeRequest
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.retrieval.domain import RetrievalResult
    from agentalloy.retrieval.system import SystemRetrievalResult
    from agentalloy.telemetry import DuckDBTelemetryWriter
    from tests.support import StubLMClient, fake_fragment

    fragment_store = open_overgraph_skill_store(str(tmp_path / "corpus.overgraph"))
    telemetry_store = open_telemetry_store(tmp_path / "telemetry.duck")
    telemetry = DuckDBTelemetryWriter(telemetry_store)

    class _FakeOrch(ComposeOrchestrator):
        async def retrieve(self, req: ComposeRequest) -> RetrievalResult:  # noqa: ARG002
            return RetrievalResult(
                candidates=[fake_fragment("f1"), fake_fragment("f2", skill="sk-b")],
                eligible_count=2,
                retrieval_ms=12,
            )

        async def retrieve_system(self, req: ComposeRequest) -> SystemRetrievalResult:  # noqa: ARG002
            return SystemRetrievalResult(candidates=[], applied_skill_ids=[], retrieval_ms=0)

    orch = _FakeOrch(
        source=None,  # type: ignore[arg-type]
        lm=StubLMClient(),
        vector_store=fragment_store,
        telemetry=telemetry,
        embedding_model="stub-embed",
    )

    result = asyncio.run(
        orch.compose(ComposeRequest(task="design a fastapi route", phase="design"))
    )
    assert result.result_type == "composed"

    traces = telemetry_store.query_traces(status="compose", limit=10)
    assert len(traces) == 1, "expected exactly one composition trace row"
    t = traces[0]
    assert t.status == "compose"
    assert t.task_prompt == "design a fastapi route"
    assert sorted(t.source_skill_ids) == ["sk-a", "sk-b"]
    assert sorted(t.selected_fragment_ids) == ["f1", "f2"]
    assert t.assembly_tier == "0"  # v5.4: no LLM tier
    assert t.retrieval_latency_ms == 12
    assert t.assembly_latency_ms == 0  # v5.4: no assembly latency
    assert t.total_latency_ms is not None and t.total_latency_ms >= 0


def test_step_7_embedding_model_not_loaded_returns_structured_503(tmp_path: Path) -> None:
    """Missing-embed-model requests surface as a structured 503 from the retrieve stage."""
    import asyncio

    from agentalloy.api.compose_models import ComposeRequest
    from agentalloy.orchestration.compose import (
        ComposeOrchestrator,
        RetrievalStageError,
    )
    from agentalloy.telemetry.writer import NullTelemetryWriter
    from tests.support import StubLMClient

    fragment_store = open_overgraph_skill_store(str(tmp_path / "corpus.overgraph"))
    # An empty (migrated) skill store as the retrieval source: the domain leg reads
    # deprecated skill ids from it before building the query embedding, so the
    # missing-model failure surfaces from the embed call (the path under test), not
    # from a None source.
    skill_store = open_overgraph_skill_store(str(tmp_path / "agentalloy.overgraph"))

    class _UnloadedEmbedLM(StubLMClient):
        def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:  # noqa: ARG002
            raise LMModelNotLoaded(model, ["some-other-embed-model"])

    orch = ComposeOrchestrator(
        source=skill_store,
        lm=_UnloadedEmbedLM(),
        vector_store=fragment_store,
        telemetry=NullTelemetryWriter(),
        embedding_model="missing-embed-model",
    )

    with pytest.raises(RetrievalStageError) as ei:
        asyncio.run(orch.retrieve(ComposeRequest(task="t", phase="design")))
    err = ei.value
    assert err.code == "embedding_model_unavailable"
    assert "missing-embed-model" in err.message


# ---------------------------------------------------------------------------
# De-Ollama guard: no legacy ollama references remain
# ---------------------------------------------------------------------------


def test_step_8_ollama_artifacts_removed() -> None:
    """Post de-Ollama sweep the legacy package, embed client, dep + config field are all gone.

    The original ``src/agentalloy/ollama/`` package was removed in NXS-801.
    The provider abstraction (cd73667) introduced ``src/agentalloy/ollama_embed.py``
    as a thin native-Ollama embed client; the de-Ollama sweep deleted it once
    llama-server became the sole runner (runtime embeds via ``openai_compat``).
    This test now bans both: no ``agentalloy.ollama`` reference of any kind in
    ``src/``, and no import of the upstream ``ollama`` PyPI client.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]

    # No legacy ollama package directory.
    assert not (repo_root / "src" / "agentalloy" / "ollama").exists(), (
        "src/agentalloy/ollama/ should be deleted"
    )

    # The native-Ollama embed client must be gone too.
    assert not (repo_root / "src" / "agentalloy" / "ollama_embed.py").exists(), (
        "src/agentalloy/ollama_embed.py should be deleted"
    )

    # No imports from the legacy package or the upstream `ollama` PyPI client.
    src_root = repo_root / "src"
    legacy_pkg_re = re.compile(r"agentalloy\.ollama")
    upstream_pypi_re = re.compile(r"^\s*(from ollama\b|import ollama\b)", re.MULTILINE)
    offending: list[str] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text()
        if legacy_pkg_re.search(text) or upstream_pypi_re.search(text):
            offending.append(str(py.relative_to(repo_root)))
    assert not offending, f"unexpected legacy-ollama references in src/: {offending}"

    # No ollama dep + no ollama_base_url in pyproject/config
    pyproject = (repo_root / "pyproject.toml").read_text()
    assert "ollama" not in pyproject.lower() or "# noqa-ollama" in pyproject, (
        "ollama should not appear in pyproject.toml"
    )

    config_text = (repo_root / "src" / "agentalloy" / "config.py").read_text()
    assert "ollama_base_url" not in config_text, "ollama_base_url should be removed from config"


# ---------------------------------------------------------------------------
# Quick sanity: embed runtime precheck plumbing is functional
# ---------------------------------------------------------------------------


def test_precheck_catches_missing_model(
    embed_runtime_required: None,
) -> None:
    """OpenAICompatClient.ensure_model_loaded raises LMModelNotLoaded for a
    nonexistent model id, and the error payload carries the loaded list.

    Doesn't require the embedding model specifically — proves the precheck
    plumbing works regardless of which models are resident."""
    with (
        OpenAICompatClient(EMBED_BASE_URL) as client,
        pytest.raises(LMModelNotLoaded) as exc_info,
    ):
        client.ensure_model_loaded("definitely-not-a-real-model-id-xyz")
    assert exc_info.value.model == "definitely-not-a-real-model-id-xyz"
    assert isinstance(exc_info.value.loaded, list)


def test_precheck_passes_for_loaded_model(
    embed_runtime_required: None,
    embedding_model_required: None,
) -> None:
    """A loaded model passes ensure_model_loaded without raising."""
    with OpenAICompatClient(EMBED_BASE_URL) as client:
        try:
            client.ensure_model_loaded(EMBEDDING_MODEL)
        except LMClientError as exc:  # pragma: no cover
            pytest.fail(f"ensure_model_loaded raised unexpectedly: {exc}")
