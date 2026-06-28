"""Tests for token-savings telemetry: schema migration, trace write/readback,
aggregate_savings, CLI output, and compose-path counterfactual."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.storage.vector_store import (
    CompositionTrace,
    VectorStore,
    open_or_create,
)
from agentalloy.telemetry.writer import DuckDBTelemetryWriter, TelemetryRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_trace(
    trace_id: str,
    phase: str = "build",
    status: str = "proxy_composed",
    tokens_returned: int = 0,
    tokens_flat_equivalent: int = 0,
) -> CompositionTrace:
    return CompositionTrace(
        trace_id=trace_id,
        request_ts=int(time.time()),
        phase=phase,
        task_prompt="test task",
        status=status,
        tokens_returned=tokens_returned,
        tokens_flat_equivalent=tokens_flat_equivalent,
    )


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:  # type: ignore[misc]
    with open_or_create(tmp_path / "test.duck") as s:
        yield s


# ---------------------------------------------------------------------------
# Schema: new columns have correct defaults on fresh DB
# ---------------------------------------------------------------------------


def test_tokens_columns_default_zero_on_fresh_db(store: VectorStore) -> None:
    trace = CompositionTrace(
        trace_id="t-defaults",
        request_ts=int(time.time()),
        phase="spec",
        task_prompt="fresh db test",
        status="compose",
    )
    store.record_composition_trace(trace)
    results = store.query_traces(limit=1)
    assert len(results) == 1
    assert results[0].tokens_returned == 0
    assert results[0].tokens_flat_equivalent == 0


# ---------------------------------------------------------------------------
# Schema migration: columns added to pre-existing DB
# ---------------------------------------------------------------------------


def test_migration_adds_columns_to_preexisting_db(tmp_path: Path) -> None:
    """A DB opened before the migration should gain the new columns on reopen."""
    db_path = tmp_path / "migrate.duck"

    # Simulate a pre-existing DB by opening and then manually dropping the new columns.
    import duckdb

    conn = duckdb.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS composition_traces (
            trace_id VARCHAR PRIMARY KEY,
            correlation_id VARCHAR,
            request_ts BIGINT NOT NULL,
            phase VARCHAR NOT NULL,
            category VARCHAR,
            task_prompt VARCHAR NOT NULL,
            selected_fragment_ids VARCHAR[],
            source_skill_ids VARCHAR[],
            system_skill_ids VARCHAR[],
            assembly_tier VARCHAR,
            assembly_model VARCHAR,
            retrieval_latency_ms INTEGER,
            assembly_latency_ms INTEGER,
            total_latency_ms INTEGER,
            status VARCHAR NOT NULL,
            error_code VARCHAR,
            response_size_chars INTEGER,
            prompt_version VARCHAR,
            workflow_skill_ids VARCHAR[],
            event_type VARCHAR NOT NULL DEFAULT 'compose',
            pre_filter_matched VARCHAR,
            gates_met VARCHAR[],
            gates_unmet VARCHAR[],
            qwen_calls INTEGER NOT NULL DEFAULT 0,
            contract_path VARCHAR,
            contract_tags VARCHAR[],
            bm25_source VARCHAR NOT NULL DEFAULT 'rule-extracted',
            reranked BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    # Insert a legacy row (without the new columns) — they get the column default.
    conn.execute(
        """
        INSERT INTO composition_traces
            (trace_id, request_ts, phase, task_prompt, status)
        VALUES ('legacy-row', 0, 'build', 'legacy', 'compose')
        """
    )
    conn.close()

    # Reopen via open_or_create — migration should add the new columns.
    with open_or_create(db_path) as s:
        # Write a new row with values.
        s.record_composition_trace(
            _mk_trace("new-row", tokens_returned=50, tokens_flat_equivalent=200)
        )
        results = {r.trace_id: r for r in s.query_traces(limit=10)}

    assert "legacy-row" in results
    assert results["legacy-row"].tokens_returned == 0
    assert results["legacy-row"].tokens_flat_equivalent == 0
    assert results["new-row"].tokens_returned == 50
    assert results["new-row"].tokens_flat_equivalent == 200


# ---------------------------------------------------------------------------
# Write / readback round-trip
# ---------------------------------------------------------------------------


def test_tokens_roundtrip_via_record_and_query(store: VectorStore) -> None:
    trace = _mk_trace("t-rt", phase="qa", tokens_returned=120, tokens_flat_equivalent=480)
    store.record_composition_trace(trace)
    results = store.query_traces(limit=5)
    assert len(results) == 1
    r = results[0]
    assert r.tokens_returned == 120
    assert r.tokens_flat_equivalent == 480


def test_tokens_via_telemetry_record_writer(tmp_path: Path) -> None:
    """TelemetryRecord → DuckDBTelemetryWriter → VectorStore round-trip."""
    from datetime import UTC, datetime

    with open_or_create(tmp_path / "writer.duck") as vs:
        writer = DuckDBTelemetryWriter(vs)
        rec = TelemetryRecord(
            composition_id="tr-writer",
            timestamp=datetime.now(UTC),
            phase="build",
            task_prompt="writer test",
            result_type="compose",
            tokens_returned=75,
            tokens_flat_equivalent=300,
        )
        writer.write(rec)
        results = vs.query_traces(limit=5)

    assert len(results) == 1
    assert results[0].tokens_returned == 75
    assert results[0].tokens_flat_equivalent == 300


# ---------------------------------------------------------------------------
# aggregate_savings math
# ---------------------------------------------------------------------------


def test_aggregate_savings_empty_db(store: VectorStore) -> None:
    result = store.aggregate_savings()
    assert result["total_composes"] == 0
    assert result["tokens_returned"] == 0
    assert result["tokens_flat_equivalent"] == 0
    assert result["tokens_saved"] == 0
    assert result["savings_pct"] == 0.0
    assert result["per_phase"] == []


def test_aggregate_savings_zero_flat_equivalent(store: VectorStore) -> None:
    """If all flat_equivalents are 0 (legacy rows), savings_pct must be 0."""
    store.record_composition_trace(_mk_trace("t1", tokens_returned=50, tokens_flat_equivalent=0))
    result = store.aggregate_savings()
    assert result["tokens_saved"] == 0
    assert result["savings_pct"] == 0.0


def test_aggregate_savings_math(store: VectorStore) -> None:
    store.record_composition_trace(
        _mk_trace("t1", phase="build", tokens_returned=100, tokens_flat_equivalent=400)
    )
    store.record_composition_trace(
        _mk_trace("t2", phase="build", tokens_returned=150, tokens_flat_equivalent=600)
    )
    store.record_composition_trace(
        _mk_trace("t3", phase="qa", tokens_returned=80, tokens_flat_equivalent=200)
    )
    result = store.aggregate_savings()
    assert result["total_composes"] == 3
    assert result["tokens_returned"] == 330
    assert result["tokens_flat_equivalent"] == 1200
    assert result["tokens_saved"] == 870
    # 870 / 1200 * 100 = 72.5
    assert result["savings_pct"] == 72.5
    phases = {r["phase"]: r for r in result["per_phase"]}  # type: ignore[index]
    assert phases["build"]["composes"] == 2
    assert phases["build"]["tokens_saved"] == 750
    assert phases["qa"]["tokens_saved"] == 120


def test_aggregate_savings_excludes_non_compose_status(store: VectorStore) -> None:
    """Only status='proxy_composed' rows count. Passthroughs and legacy per-leg
    'compose'/'compose_empty' rows must not inflate totals."""
    store.record_composition_trace(
        _mk_trace("t1", status="proxy_composed", tokens_returned=100, tokens_flat_equivalent=400)
    )
    store.record_composition_trace(
        _mk_trace("t2", status="proxy_passthrough", tokens_returned=0, tokens_flat_equivalent=0)
    )
    # Legacy per-leg rows from the orchestrator/eval path are no longer counted.
    store.record_composition_trace(
        _mk_trace("t3", status="compose", tokens_returned=999, tokens_flat_equivalent=999)
    )
    store.record_composition_trace(
        _mk_trace("t4", status="compose_empty", tokens_returned=0, tokens_flat_equivalent=0)
    )
    result = store.aggregate_savings()
    assert result["total_composes"] == 1
    assert result["tokens_returned"] == 100


# ---------------------------------------------------------------------------
# CLI: savings sub-verb
# ---------------------------------------------------------------------------


def test_cli_savings_returns_zero_on_empty_db(tmp_path: Path) -> None:
    """_run_savings returns 0 (success) on an empty database."""
    from agentalloy.install.subcommands.telemetry import _run_savings

    with open_or_create(tmp_path / "cli.duck"):
        pass  # create empty DB

    settings_mock = MagicMock()
    settings_mock.duckdb_path = str(tmp_path / "cli.duck")
    args = MagicMock()
    args.json = False
    args.quiet = False

    # Force the direct-DB path (service down) so the test is independent of any
    # agentalloy service actually listening on the box.
    with (
        patch("agentalloy.install.server_proc.port_reachable", return_value=False),
        patch("agentalloy.config.get_settings", return_value=settings_mock),
    ):
        rc = _run_savings(args)

    assert rc == 0


def test_cli_savings_returns_zero_with_traces(tmp_path: Path) -> None:
    """_run_savings returns 0 (success) when traces exist."""
    from agentalloy.install.subcommands.telemetry import _run_savings

    db_path = tmp_path / "cli2.duck"
    with open_or_create(db_path) as vs:
        vs.record_composition_trace(
            _mk_trace("c1", phase="build", tokens_returned=200, tokens_flat_equivalent=800)
        )

    settings_mock = MagicMock()
    settings_mock.duckdb_path = str(db_path)
    args = MagicMock()
    args.json = False
    args.quiet = False

    # Force the direct-DB path (service down) so the test is independent of any
    # agentalloy service actually listening on the box.
    with (
        patch("agentalloy.install.server_proc.port_reachable", return_value=False),
        patch("agentalloy.config.get_settings", return_value=settings_mock),
    ):
        rc = _run_savings(args)

    assert rc == 0


def test_cli_savings_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json output must be valid JSON with expected top-level keys."""
    import json

    from agentalloy.install.subcommands.telemetry import _run_savings

    db_path = tmp_path / "cli3.duck"
    with open_or_create(db_path) as vs:
        vs.record_composition_trace(
            _mk_trace("j1", phase="spec", tokens_returned=50, tokens_flat_equivalent=250)
        )

    settings_mock = MagicMock()
    settings_mock.duckdb_path = str(db_path)
    args = MagicMock()
    args.json = True
    args.quiet = False

    # Force the direct-DB path (service down) so the test is independent of any
    # agentalloy service actually listening on the box.
    with (
        patch("agentalloy.install.server_proc.port_reachable", return_value=False),
        patch("agentalloy.config.get_settings", return_value=settings_mock),
    ):
        rc = _run_savings(args)

    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    for key in (
        "total_composes",
        "tokens_returned",
        "tokens_flat_equivalent",
        "tokens_saved",
        "savings_pct",
        "per_phase",
    ):
        assert key in data, f"missing key: {key}"
    assert data["total_composes"] == 1
    assert data["savings_pct"] == 80.0


# ---------------------------------------------------------------------------
# Compose path: counterfactual computed with RuntimeCache
# ---------------------------------------------------------------------------


async def test_compose_records_token_savings_with_runtime_cache() -> None:
    """compose() must record non-zero tokens_returned and tokens_flat_equivalent
    when source is a RuntimeCache and source skills have raw_prose."""

    from agentalloy.api.compose_models import ComposeRequest
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.reads.models import ActiveFragment, ActiveSkill
    from agentalloy.retrieval.domain import RetrievalResult
    from agentalloy.retrieval.system import SystemRetrievalResult
    from agentalloy.runtime_state import RuntimeCache, VersionDetail
    from agentalloy.storage.vector_store import VectorStore

    # Build a minimal RuntimeCache with one skill + version detail.
    RAW_PROSE = "x" * 400  # 400 chars → 100 tokens via len // 4
    skill = ActiveSkill(
        skill_id="sk-savings",
        canonical_name="savings skill",
        category="engineering",
        skill_class="domain",
        domain_tags=["python"],
        always_apply=False,
        phase_scope=None,
        category_scope=None,
        active_version_id="sk-savings-v1",
        tier=None,
    )
    vd = VersionDetail(
        version_id="sk-savings-v1",
        version_number=1,
        authored_at=None,
        author="test",
        change_summary="",
        raw_prose=RAW_PROSE,
    )
    fragment = ActiveFragment(
        fragment_id="f-savings-1",
        fragment_type="execution",
        sequence=1,
        content="y" * 80,  # 80 chars → 20 tokens
        skill_id="sk-savings",
        version_id="sk-savings-v1",
        skill_class="domain",
        category="engineering",
        domain_tags=["python"],
    )
    cache = RuntimeCache(
        skills={"sk-savings": skill},
        fragments=[fragment],
        version_details={"sk-savings-v1": vd},
    )

    # Capture what the telemetry writer receives.
    captured: list[TelemetryRecord] = []

    class _CapturingWriter:
        def write(self, rec: TelemetryRecord) -> None:
            captured.append(rec)

    # Fake embed client and vector store.
    lm_mock = MagicMock()
    vs_mock = MagicMock(spec=VectorStore)

    orch = ComposeOrchestrator(
        source=cache,
        lm=lm_mock,
        vector_store=vs_mock,
        telemetry=_CapturingWriter(),
        embedding_model="fake-embed",
    )

    # Override retrieve methods directly.
    async def _fake_retrieve(req: ComposeRequest) -> RetrievalResult:
        return RetrievalResult(
            candidates=[fragment], retrieval_ms=1, reranked=False, eligible_count=1
        )

    async def _fake_retrieve_system(req: ComposeRequest) -> SystemRetrievalResult:
        return SystemRetrievalResult(candidates=[], applied_skill_ids=[], retrieval_ms=0)

    orch.retrieve = _fake_retrieve  # type: ignore[method-assign]
    orch.retrieve_system = _fake_retrieve_system  # type: ignore[method-assign]

    req = ComposeRequest(task="test savings", phase="build")
    await orch.compose(req)

    assert len(captured) == 1
    rec = captured[0]
    # tokens_returned = len(composed output) // 4, must be > 0
    assert rec.tokens_returned > 0
    # tokens_flat_equivalent = len(raw_prose) // 4 = 100
    assert rec.tokens_flat_equivalent == 100
    # composed output is fragments only, should be less than full prose
    assert rec.tokens_returned < rec.tokens_flat_equivalent


async def test_system_only_compose_counts_system_skills_in_flat_baseline() -> None:
    """P4: a system-only (Tier-1) compose — e.g. ship, or any phase with no work-item
    contract — must include its system skills' raw_prose in tokens_flat_equivalent.
    Previously the flat baseline summed only domain skills, so a system-only compose
    reported flat=0 → savings_pct=0 despite injecting real system prose."""
    from agentalloy.api.compose_models import ComposeRequest
    from agentalloy.orchestration.compose import ComposeOrchestrator
    from agentalloy.reads.models import ActiveFragment, ActiveSkill
    from agentalloy.retrieval.system import SystemRetrievalResult
    from agentalloy.runtime_state import RuntimeCache, VersionDetail
    from agentalloy.storage.vector_store import VectorStore

    RAW_PROSE = "s" * 400  # 400 chars → 100 tokens via len // 4
    sys_skill = ActiveSkill(
        skill_id="sk-sys",
        canonical_name="system skill",
        category="engineering",
        skill_class="system",
        domain_tags=[],
        always_apply=True,
        phase_scope=None,
        category_scope=None,
        active_version_id="sk-sys-v1",
        tier=None,
    )
    vd = VersionDetail(
        version_id="sk-sys-v1",
        version_number=1,
        authored_at=None,
        author="test",
        change_summary="",
        raw_prose=RAW_PROSE,
    )
    sys_fragment = ActiveFragment(
        fragment_id="f-sys-1",
        fragment_type="system",
        sequence=1,
        content="z" * 80,
        skill_id="sk-sys",
        version_id="sk-sys-v1",
        skill_class="system",
        category="engineering",
        domain_tags=[],
    )
    cache = RuntimeCache(
        skills={"sk-sys": sys_skill},
        fragments=[sys_fragment],
        version_details={"sk-sys-v1": vd},
    )

    captured: list[TelemetryRecord] = []

    class _CapturingWriter:
        def write(self, rec: TelemetryRecord) -> None:
            captured.append(rec)

    orch = ComposeOrchestrator(
        source=cache,
        lm=MagicMock(),
        vector_store=MagicMock(spec=VectorStore),
        telemetry=_CapturingWriter(),
        embedding_model="fake-embed",
    )

    async def _fake_retrieve_system(req: ComposeRequest) -> SystemRetrievalResult:
        return SystemRetrievalResult(
            candidates=[sys_fragment], applied_skill_ids=["sk-sys"], retrieval_ms=0
        )

    orch.retrieve_system = _fake_retrieve_system  # type: ignore[method-assign]

    # legs="system" → Tier 1 only; the domain leg is skipped entirely.
    req = ComposeRequest(task="entering ship", phase="ship", legs="system")
    await orch.compose(req)

    assert len(captured) == 1
    rec = captured[0]
    assert rec.tokens_returned > 0  # real injected system prose
    assert rec.tokens_flat_equivalent == 100  # system skill counted (was 0 before P4)
