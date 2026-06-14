"""End-to-end integration tests for the v1.5 migration (NXS-802).

Exercises the dual-store architecture against live LM Studio + real DuckDB
and LadybugDB (at tmp paths — does not touch ``data/``). Steps mirror the
acceptance criteria in the NXS-802 Linear ticket.

Skipped gracefully if LM Studio is unreachable. The steps that depend on
still-blocked migration tickets (NXS-797 schema change, NXS-798 compose
wiring, NXS-801 Ollama removal) skip with a clear "pending X" message so
running this harness tells you exactly what's still outstanding.

Run locally::

    uv run pytest tests/test_v1_5_integration.py -v

All tests are marked ``integration`` per the marker registered in
pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest

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
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import EMBEDDING_DIM, VectorStore, open_or_create

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LM_STUDIO_BASE_URL = "http://localhost:11434"
LM_STUDIO_MODELS_URL = f"{LM_STUDIO_BASE_URL}/v1/models"
EMBEDDING_MODEL = "qwen3-embedding:0.6b"

REPO_ROOT = Path(__file__).resolve().parent.parent
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
def lm_studio_available() -> bool:
    """One probe per test module. Used by the ``lm_studio_required`` fixture."""
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)) as c:
            resp = c.get(LM_STUDIO_MODELS_URL)
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def lm_studio_models(lm_studio_available: bool) -> list[str]:
    """The set of models currently loaded. Used to verify embedding model presence."""
    if not lm_studio_available:
        return []
    with httpx.Client(timeout=5.0) as c:
        body = cast("dict[str, object]", c.get(LM_STUDIO_MODELS_URL).json())
    raw = body.get("data")
    if not isinstance(raw, list):
        return []
    return [str(e["id"]) for e in cast("list[dict[str, object]]", raw) if "id" in e]


@pytest.fixture
def lm_studio_required(lm_studio_available: bool) -> None:
    if not lm_studio_available:
        pytest.skip("LM Studio not reachable at " + LM_STUDIO_MODELS_URL)


@pytest.fixture
def embedding_model_required(lm_studio_models: list[str]) -> None:
    if EMBEDDING_MODEL not in lm_studio_models:
        pytest.skip(f"{EMBEDDING_MODEL} not loaded in LM Studio (have: {lm_studio_models})")


@pytest.fixture
def fresh_ladybug(tmp_path: Path):
    """A migrated, empty LadybugDB at a tmp path."""
    path = tmp_path / "ladybug"
    with LadybugStore(str(path)) as store:
        store.migrate()
        yield store


@pytest.fixture
def fresh_duckdb(tmp_path: Path):
    """An open, empty DuckDB vector store at a tmp path."""
    with open_or_create(tmp_path / "skills.duck") as vs:
        yield vs


# ---------------------------------------------------------------------------
# NXS-802 Step 1: fresh state has no data
# ---------------------------------------------------------------------------


def test_step_1_fresh_stores_are_empty(
    fresh_ladybug: LadybugStore, fresh_duckdb: VectorStore
) -> None:
    """Fresh LadybugDB + DuckDB carry no skills, fragments, or embeddings."""
    skill_count = fresh_ladybug.scalar("MATCH (s:Skill) RETURN count(s)")
    assert skill_count == 0
    frag_count = fresh_ladybug.scalar("MATCH (f:Fragment) RETURN count(f)")
    assert frag_count == 0
    assert fresh_duckdb.count_embeddings() == 0
    assert fresh_duckdb.count_traces() == 0


# ---------------------------------------------------------------------------
# NXS-802 Step 2+3: ingest populates LadybugDB, DuckDB remains empty
# ---------------------------------------------------------------------------


def test_step_2_ingest_populates_ladybug_only(
    fresh_ladybug: LadybugStore, fresh_duckdb: VectorStore
) -> None:
    """Ingest writes graph data to Ladybug. DuckDB fragment_embeddings stays empty
    (the v1.5 model separates ingest from embedding)."""
    record = _load_yaml(FIXTURE_SKILL)
    assert _validate(record) == []
    _insert(fresh_ladybug, record, force=False)

    assert (
        fresh_ladybug.scalar(
            "MATCH (s:Skill {skill_id: $id}) RETURN count(s)", {"id": record.skill_id}
        )
        == 1
    )
    assert fresh_ladybug.scalar("MATCH (v:SkillVersion) RETURN count(v)") == 1
    fragment_count = fresh_ladybug.scalar("MATCH (f:Fragment) RETURN count(f)")
    assert fragment_count == len(record.fragments) > 0

    # Critically: DuckDB has nothing yet — ingest is graph-only in the v1.5 model.
    assert fresh_duckdb.count_embeddings() == 0


# ---------------------------------------------------------------------------
# NXS-802 Step 4+5: reembed populates DuckDB with L2-normalized vectors
# ---------------------------------------------------------------------------


def test_step_4_reembed_populates_duckdb(
    fresh_ladybug: LadybugStore,
    fresh_duckdb: VectorStore,
    lm_studio_required: None,
    embedding_model_required: None,
) -> None:
    """After reembed: DuckDB has one row per Fragment, vectors are L2-normalized,
    denormalized columns are populated correctly."""
    record = _load_yaml(FIXTURE_SKILL)
    _insert(fresh_ladybug, record, force=False)

    fragments = discover_unembedded_fragments(fresh_ladybug, fresh_duckdb)
    assert len(fragments) == len(record.fragments)
    assert all(f.category == record.category for f in fragments)

    with OpenAICompatClient(LM_STUDIO_BASE_URL) as client:

        def embed(text: str) -> list[float]:
            return client.embed(model=EMBEDDING_MODEL, texts=[text])[0]

        stats = reembed_fragments(
            fragments,
            embed_fn=embed,
            vector_store=fresh_duckdb,
            embedding_model=EMBEDDING_MODEL,
        )

    assert stats.failed == 0
    assert stats.embedded == len(fragments)
    assert fresh_duckdb.count_embeddings() == len(fragments)

    # Verify the stored vectors are L2-normalized (||v|| ≈ 1).
    raw_vec_rows: list[Any] = fresh_duckdb._conn.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT embedding FROM fragment_embeddings LIMIT 3"
    ).fetchall()
    assert len(raw_vec_rows) > 0
    for row in raw_vec_rows:
        vec = row[0]
        assert len(vec) == EMBEDDING_DIM
        norm_sq = sum(float(x) * float(x) for x in vec)
        assert abs(norm_sq - 1.0) < 1e-4, f"vector not unit-normalized: ||v||² = {norm_sq}"


def test_step_5_search_roundtrip_returns_exact_match(
    fresh_ladybug: LadybugStore,
    fresh_duckdb: VectorStore,
    lm_studio_required: None,
    embedding_model_required: None,
) -> None:
    """Embedding the same content twice should yield a near-zero-distance
    search result — proves the full write/query path is coherent."""
    record = _load_yaml(FIXTURE_SKILL)
    _insert(fresh_ladybug, record, force=False)

    fragments = discover_unembedded_fragments(fresh_ladybug, fresh_duckdb)

    with OpenAICompatClient(LM_STUDIO_BASE_URL) as client:

        def embed(text: str) -> list[float]:
            return client.embed(model=EMBEDDING_MODEL, texts=[text])[0]

        reembed_fragments(
            fragments,
            embed_fn=embed,
            vector_store=fresh_duckdb,
            embedding_model=EMBEDDING_MODEL,
        )

        # Re-embed the first fragment's content and search — it should be its own top hit.
        target = fragments[0]
        query_vec = client.embed(model=EMBEDDING_MODEL, texts=[target.content])[0]

    hits = fresh_duckdb.search_similar(query_vec, k=5)
    assert hits, "expected at least one hit"
    assert hits[0].fragment_id == target.fragment_id
    # Distance should be essentially zero — same content, same model, same norm.
    assert hits[0].distance < 1e-4


def test_step_5b_category_filter_narrows_search(
    fresh_ladybug: LadybugStore,
    fresh_duckdb: VectorStore,
    lm_studio_required: None,
    embedding_model_required: None,
) -> None:
    """Denormalized-column filters should restrict results — a filter for a
    category the skill doesn't belong to returns nothing."""
    record = _load_yaml(FIXTURE_SKILL)
    _insert(fresh_ladybug, record, force=False)
    fragments = discover_unembedded_fragments(fresh_ladybug, fresh_duckdb)

    with OpenAICompatClient(LM_STUDIO_BASE_URL) as client:

        def embed(text: str) -> list[float]:
            return client.embed(model=EMBEDDING_MODEL, texts=[text])[0]

        reembed_fragments(
            fragments,
            embed_fn=embed,
            vector_store=fresh_duckdb,
            embedding_model=EMBEDDING_MODEL,
        )
        q = client.embed(model=EMBEDDING_MODEL, texts=[fragments[0].content])[0]

    # Filter on a category this skill doesn't have.
    other = "safety" if record.category != "safety" else "governance"
    hits = fresh_duckdb.search_similar(q, k=10, categories=[other])
    assert hits == []


# ---------------------------------------------------------------------------
# NXS-802 Step 6+7: compose flow (pending)
# ---------------------------------------------------------------------------


def test_step_6_compose_writes_composition_trace(tmp_path: Path) -> None:
    """End-to-end /compose call writes a composition_traces row to DuckDB."""
    import asyncio

    from agentalloy.api.compose_models import ComposeRequest
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.retrieval.domain import RetrievalResult
    from agentalloy.retrieval.system import SystemRetrievalResult
    from agentalloy.telemetry import DuckDBTelemetryWriter
    from tests.support import StubLMClient, fake_fragment

    duck_path = tmp_path / "skills.duck"
    vector_store = open_or_create(duck_path)
    telemetry = DuckDBTelemetryWriter(vector_store)

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
        vector_store=vector_store,
        telemetry=telemetry,
        embedding_model="stub-embed",
    )

    result = asyncio.run(
        orch.compose(ComposeRequest(task="design a fastapi route", phase="design"))
    )
    assert result.result_type == "composed"

    rows = vector_store._conn.execute(  # pyright: ignore[reportPrivateUsage]
        """
        SELECT status, task_prompt, source_skill_ids, selected_fragment_ids,
               assembly_tier, retrieval_latency_ms, assembly_latency_ms, total_latency_ms
        FROM composition_traces
        WHERE status = 'compose'
        """
    ).fetchall()
    assert len(rows) == 1, "expected exactly one composition trace row"
    status, task, source_ids, selected_ids, tier, ret_ms, asm_ms, tot_ms = rows[0]
    assert status == "compose"
    assert task == "design a fastapi route"
    assert sorted(source_ids) == ["sk-a", "sk-b"]
    assert sorted(selected_ids) == ["f1", "f2"]
    assert tier == "0"  # v5.4: no LLM tier
    assert ret_ms == 12
    assert asm_ms == 0  # v5.4: no assembly latency
    assert tot_ms is not None and tot_ms >= 0


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

    duck_path = tmp_path / "skills.duck"
    vector_store = open_or_create(duck_path)

    class _UnloadedEmbedLM(StubLMClient):
        def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:  # noqa: ARG002
            raise LMModelNotLoaded(model, ["some-other-embed-model"])

    orch = ComposeOrchestrator(
        source=None,  # type: ignore[arg-type]
        lm=_UnloadedEmbedLM(),
        vector_store=vector_store,
        telemetry=NullTelemetryWriter(),
        embedding_model="missing-embed-model",
    )

    with pytest.raises(RetrievalStageError) as ei:
        asyncio.run(orch.retrieve(ComposeRequest(task="t", phase="design")))
    err = ei.value
    assert err.code == "embedding_model_unavailable"
    assert "missing-embed-model" in err.message


# ---------------------------------------------------------------------------
# NXS-802 cleanup verification: Ollama is gone (pending)
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

    repo_root = Path(__file__).resolve().parents[1]

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
# Quick sanity: LM Studio precheck plumbing is functional
# ---------------------------------------------------------------------------


def test_precheck_catches_missing_model(
    lm_studio_required: None,
) -> None:
    """OpenAICompatClient.ensure_model_loaded raises LMModelNotLoaded for a
    nonexistent model id, and the error payload carries the loaded list.

    Doesn't require the embedding model specifically — proves the precheck
    plumbing works regardless of which models are resident."""
    with (
        OpenAICompatClient(LM_STUDIO_BASE_URL) as client,
        pytest.raises(LMModelNotLoaded) as exc_info,
    ):
        client.ensure_model_loaded("definitely-not-a-real-model-id-xyz")
    assert exc_info.value.model == "definitely-not-a-real-model-id-xyz"
    assert isinstance(exc_info.value.loaded, list)


def test_precheck_passes_for_loaded_model(
    lm_studio_required: None,
    embedding_model_required: None,
) -> None:
    """A loaded model passes ensure_model_loaded without raising."""
    with OpenAICompatClient(LM_STUDIO_BASE_URL) as client:
        try:
            client.ensure_model_loaded(EMBEDDING_MODEL)
        except LMClientError as exc:  # pragma: no cover
            pytest.fail(f"ensure_model_loaded raised unexpectedly: {exc}")
