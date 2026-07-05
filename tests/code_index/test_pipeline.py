"""ingest.pipeline — full run, incremental skip, force, cancellation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.code_index.ingest.embed_text import DOCUMENT_PREFIX
from agentalloy.code_index.ingest.pipeline import IndexResult, run_index_job
from agentalloy.code_index.store import CodeIndexJobsStore, open_code_index, open_jobs
from agentalloy.config import Settings

from .conftest import PY_UTIL, FakeEmbedClient

SLUG = "demo"

# The fixture repo's embeddable code symbols (functions only; modules skipped).
CODE_QNS = {"demo.pkg.util.helper", "demo.pkg.util.caller", "demo.pkg.main.main"}
# README.md chunks: preamble + "# Overview" + "## Usage".
MD_QNS = {
    "README.md::readme-preamble",
    "README.md::overview",
    "README.md::usage",
}


async def run(
    settings: Settings,
    embed: FakeEmbedClient,
    jobs: CodeIndexJobsStore,
    repo: Path,
    *,
    force: bool = False,
    index_markdown: bool = True,
    job_id: str | None = None,
) -> IndexResult:
    return await run_index_job(
        settings,
        embed,
        jobs,
        repo_path=repo,
        slug=SLUG,
        force=force,
        index_markdown=index_markdown,
        job_id=job_id,
    )


async def test_full_pipeline_first_run(settings: Settings, fixture_repo: Path) -> None:
    embed = FakeEmbedClient()
    jobs = open_jobs(settings)
    try:
        result = await run(settings, embed, jobs, fixture_repo)
        assert result.status == "done"
        assert result.symbols_embedded == len(CODE_QNS)
        assert result.markdown_embedded == len(MD_QNS)
        assert result.edges_total > 0
        assert result.duration_s > 0

        # Every embedded text carried the nomic document prefix.
        assert embed.embedded_texts
        assert all(t.startswith(DOCUMENT_PREFIX) for t in embed.embedded_texts)

        # Job row reached done with counts.
        job = jobs.get_job(result.job_id)
        assert job is not None and job.status == "done"
        assert job.embedding_count == len(CODE_QNS) + len(MD_QNS)

        # Registry row exists (repo is a plain dir — no git — so head_sha None).
        repo = jobs.get_repo(SLUG)
        assert repo is not None
        assert repo.last_indexed_at is not None
        assert repo.head_sha is None

        handles = open_code_index(settings, SLUG, role="reader")
        try:
            counts = handles.graph.counts_by_kind()
            assert counts.get("Function", 0) == 3
            assert counts.get("MarkdownDoc", 0) == len(MD_QNS)
            # CALLS edges drove centrality: callee ranks above a leaf caller.
            top = dict(handles.graph.top_centrality(20))
            assert "demo.pkg.util.helper" in top
            # Vector rows: one per embeddable symbol + one per markdown chunk.
            assert handles.vectors.count() == len(CODE_QNS) + len(MD_QNS)
            # FTS index was built: BM25 finds the markdown chunk by its content.
            hits = dict(handles.vectors.search_bm25("zanzibar", k=5))
            assert "README.md::overview" in hits
        finally:
            handles.close()
    finally:
        jobs.close()


async def test_second_run_skips_everything(settings: Settings, fixture_repo: Path) -> None:
    embed = FakeEmbedClient()
    jobs = open_jobs(settings)
    try:
        await run(settings, embed, jobs, fixture_repo)
        calls_after_first = len(embed.calls)

        result = await run(settings, embed, jobs, fixture_repo)
        assert result.status == "done"
        assert result.symbols_embedded == 0
        assert result.markdown_embedded == 0
        assert len(embed.calls) == calls_after_first, "unchanged repo must not re-embed"
    finally:
        jobs.close()


async def test_incremental_reembeds_only_changed_file(
    settings: Settings, fixture_repo: Path
) -> None:
    embed = FakeEmbedClient()
    jobs = open_jobs(settings)
    try:
        await run(settings, embed, jobs, fixture_repo)
        before = len(embed.embedded_texts)

        (fixture_repo / "pkg" / "util.py").write_text(
            PY_UTIL.replace("return x + 1", "return x + 100")
        )
        result = await run(settings, embed, jobs, fixture_repo)
        assert result.status == "done"

        new_texts = embed.embedded_texts[before:]
        # helper's source changed; caller's source is unchanged (hash match)
        # and main.py / README.md are untouched.
        assert len(new_texts) == 1
        assert "demo.pkg.util.helper" in new_texts[0]
        assert result.symbols_embedded == 1
        assert result.markdown_embedded == 0

        # Graph reflects the new source; the untouched file's symbols survive.
        handles = open_code_index(settings, SLUG, role="reader")
        try:
            helper = handles.graph.symbol("demo.pkg.util.helper")
            assert helper is not None and helper.source_code is not None
            assert "x + 100" in helper.source_code
            assert handles.graph.symbol("demo.pkg.main.main") is not None
            # caller was deleted + re-upserted with the file; still present.
            assert handles.graph.symbol("demo.pkg.util.caller") is not None
            assert handles.vectors.count() == len(CODE_QNS) + len(MD_QNS)
        finally:
            handles.close()
    finally:
        jobs.close()


async def test_force_takes_bulk_replace_path(settings: Settings, fixture_repo: Path) -> None:
    embed = FakeEmbedClient()
    jobs = open_jobs(settings)
    try:
        await run(settings, embed, jobs, fixture_repo)

        result = await run(settings, embed, jobs, fixture_repo, force=True)
        assert result.status == "done"
        # Force ignores stored hashes: everything re-embeds.
        assert result.symbols_embedded == len(CODE_QNS)
        assert result.markdown_embedded == len(MD_QNS)

        handles = open_code_index(settings, SLUG, role="reader")
        try:
            # bulk_replace rebuilt the dataset — no duplicate rows.
            assert handles.vectors.count() == len(CODE_QNS) + len(MD_QNS)
        finally:
            handles.close()
    finally:
        jobs.close()


async def test_cancel_before_embed_phase(settings: Settings, fixture_repo: Path) -> None:
    embed = FakeEmbedClient()
    jobs = open_jobs(settings)
    try:
        job = jobs.create_job(slug=SLUG, repo_path=str(fixture_repo))
        assert jobs.request_cancel(job.job_id)

        result = await run(settings, embed, jobs, fixture_repo, job_id=job.job_id)
        assert result.status == "cancelled"
        assert embed.calls == [], "cancelled job must not embed"

        row = jobs.get_job(job.job_id)
        assert row is not None
        assert row.status == "cancelled"
        assert row.error == "cancelled by request"
    finally:
        jobs.close()


async def test_failure_marks_job_failed(settings: Settings, tmp_path: Path) -> None:
    class ExplodingEmbedClient(FakeEmbedClient):
        def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embed server down")

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 1\n")

    jobs = open_jobs(settings)
    try:
        result = await run(settings, ExplodingEmbedClient(), jobs, repo, index_markdown=False)
        assert result.status == "failed"
        assert result.error is not None and "embed server down" in result.error
        row = jobs.get_job(result.job_id)
        assert row is not None and row.status == "failed"
        assert row.error is not None and "embed server down" in row.error
    finally:
        jobs.close()


async def test_markdown_disabled(settings: Settings, fixture_repo: Path) -> None:
    embed = FakeEmbedClient()
    jobs = open_jobs(settings)
    try:
        result = await run(settings, embed, jobs, fixture_repo, index_markdown=False)
        assert result.status == "done"
        assert result.markdown_embedded == 0
        handles = open_code_index(settings, SLUG, role="reader")
        try:
            assert handles.vectors.count() == len(CODE_QNS)
            assert "MarkdownDoc" not in handles.graph.counts_by_kind()
        finally:
            handles.close()
    finally:
        jobs.close()


async def test_head_sha_recorded_for_git_repo(settings: Settings, fixture_repo: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=fixture_repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=fixture_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=fixture_repo,
        check=True,
    )
    jobs = open_jobs(settings)
    try:
        result = await run(settings, FakeEmbedClient(), jobs, fixture_repo, index_markdown=False)
        assert result.status == "done"
        repo = jobs.get_repo(SLUG)
        assert repo is not None
        assert repo.head_sha is not None and len(repo.head_sha) == 40
    finally:
        jobs.close()


async def test_embed_batches_halving_fallback() -> None:
    """A server-rejected batch degrades to per-item halving, not job failure.

    Found live: a 6000-char code body reached 2489 tokens (code tokenizes
    ~2.4 chars/token) and 500'd llama-server, killing the whole index job.
    """
    from agentalloy.code_index.ingest.pipeline import _embed_batches

    class SizeLimitedEmbedClient:
        """Rejects any request containing a text over the limit (like llama-server)."""

        def __init__(self, limit: int) -> None:
            self.limit = limit
            self.calls: list[list[str]] = []

        def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            if any(len(t) > self.limit for t in texts):
                raise RuntimeError("input is too large to process")
            return [[1.0] + [0.0] * 767 for _ in texts]

        def close(self) -> None:  # pragma: no cover - protocol completeness
            pass

    client = SizeLimitedEmbedClient(limit=1000)
    texts = ["ok " * 10, "x" * 3000, "fine"]  # middle one beats the server limit
    vectors = await _embed_batches(client, "m", texts, heartbeat=lambda: None)

    assert len(vectors) == 3
    assert all(len(v) == 768 for v in vectors)
    # The oversized text was halved until acceptable (3000 -> 1500 -> 750).
    assert any(len(t[0]) == 750 for t in client.calls if len(t) == 1)


async def test_embed_one_halving_gives_up_at_floor() -> None:
    from agentalloy.code_index.ingest.pipeline import _embed_one_with_halving

    class AlwaysRejects:
        def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("input is too large to process")

    with pytest.raises(RuntimeError):
        await _embed_one_with_halving(AlwaysRejects(), "m", "y" * 4000, min_chars=256)
