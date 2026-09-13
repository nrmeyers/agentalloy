"""Executors (M1c) — in-process action execution and failure containment.

Executors run against the lifespan stores and return *transcript text* (the
2.6B's only consumer is the next prompt). Availability guards fail open into
an :class:`ExecutorError` the loop records as an honest transcript line — a
missing dependency, an unresolvable repo, or a closed store never fails the
request.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agentalloy.api.state_router as state_router
from agentalloy.local_agent.executors import NO_RESULTS, ExecutorError, Executors
from agentalloy.local_agent.protocol import Action

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeIndexed:
    slug: str
    repo_path: str
    head_sha: str = "abc123"


class _FakeJobs:
    def __init__(self, repos: list[_FakeIndexed]) -> None:
        self._repos = repos

    def list_repos(self) -> list[_FakeIndexed]:
        return self._repos

    def get_repo(self, slug: str) -> _FakeIndexed | None:
        for repo in self._repos:
            if repo.slug == slug:
                return repo
        return None


class _FakeCodeIndexState:
    def __init__(self, repos: list[_FakeIndexed]) -> None:
        self.jobs = _FakeJobs(repos)


@dataclass
class _FakeHit:
    qualified_name: str | None = None
    heading: str | None = None
    symbol: str | None = None
    file_path: str | None = None
    start_line: int | None = None
    source: str | None = None
    snippet: str | None = None


def _stub_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def _state(repo: str | None = "main-repo", *, fail: bool = False) -> _FakeCodeIndexState:
    repos = [_FakeIndexed(repo, "/data/main")] if repo else []
    return _FakeCodeIndexState(repos)


class _FakeStateStore:
    def __init__(self, *, artifacts: dict | None = None, contracts: dict | None = None) -> None:
        self._artifacts = artifacts or {}
        self._contracts = contracts or {}
        self.get_artifact_calls: list[tuple[Any, ...]] = []

    def get_artifact(self, phase: str, slug: str, name: str, *, status: str | None = None):
        self.get_artifact_calls.append((phase, slug, name, status))
        return self._artifacts.get((phase, slug, name))

    def get_contract(self, slug: str):
        return self._contracts.get(slug)


class _FakeQuerier:
    def __init__(self, traces: list[Any]) -> None:
        self._traces = traces
        self.calls: list[dict[str, Any]] = []

    async def query(self, **kwargs: Any):
        self.calls.append(kwargs)
        return SimpleNamespace(traces=self._traces)


class _FakeOrchestrator:
    def __init__(self, output: str = "") -> None:
        self._output = output
        self.calls: list[dict[str, Any]] = []

    async def compose(self, request: Any, *, repo: str | None, record_trace: bool):
        self.calls.append({"request": request, "repo": repo, "record_trace": record_trace})
        return SimpleNamespace(output=self._output, status="ok")


class _FakeComposeRequest:
    def __init__(self, *, task: str, phase: str, requesting_agent: str) -> None:
        self.task = task
        self.phase = phase
        self.requesting_agent = requesting_agent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


def _executors(repo_root: Path, **deps: Any) -> Executors:
    return Executors(repo_root=repo_root, **deps)


def _override_scoped_store(monkeypatch: pytest.MonkeyPatch, store: _FakeStateStore) -> None:
    monkeypatch.setattr(state_router, "scoped_state_store", lambda s, root: store)


# ---------------------------------------------------------------------------
# Repo resolution guards (shared by every code-index action)
# ---------------------------------------------------------------------------


class TestCodeIndexGuards:
    async def test_missing_code_index_raises(self, repo_root: Path) -> None:
        with pytest.raises(ExecutorError) as exc:
            await _executors(repo_root).execute(Action.CODE_SEARCH, {"query": "auth bug"})
        assert "code index is not available" in str(exc.value)

    async def test_no_repo_indexed_raises(self, repo_root: Path) -> None:
        ex = _executors(repo_root, code_index_state=_state(None))
        with pytest.raises(ExecutorError) as exc:
            await ex.execute(Action.CODE_SEARCH, {"query": "auth bug"})
        assert "no repo is indexed yet" in str(exc.value)

    async def test_ambiguous_repos_raise(self, repo_root: Path) -> None:
        ex = _executors(
            repo_root,
            code_index_state=_FakeCodeIndexState(
                [_FakeIndexed("alpha", "/data/a"), _FakeIndexed("beta", "/data/b")]
            ),
        )
        with pytest.raises(ExecutorError) as exc:
            await ex.execute(Action.CODE_SEARCH, {"query": "auth bug"})
        assert "several repos are indexed (alpha, beta)" in str(exc.value)

    async def test_unknown_slug_raises(self, repo_root: Path) -> None:
        ex = _executors(repo_root, code_index_state=_state("main-repo"))
        with pytest.raises(ExecutorError) as exc:
            await ex.execute(Action.CODE_SEARCH, {"query": "auth bug"}, repo_slug="ghost")
        assert "repo 'ghost' is not indexed" in str(exc.value)

    async def test_single_repo_resolved_from_registry(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_semantic_search(state, slug, query, *, k, repo_path, indexed_head):
            seen.update(slug=slug, repo_path=repo_path, indexed_head=indexed_head)
            return []

        _stub_module(
            monkeypatch,
            "agentalloy.code_index.retrieval.hybrid",
            semantic_search=fake_semantic_search,
        )
        await _executors(repo_root, code_index_state=_state("main-repo")).execute(
            Action.CODE_SEARCH, {"query": "auth bug"}
        )
        assert seen["slug"] == "main-repo"
        assert seen["repo_path"] == "/data/main"
        assert seen["indexed_head"] == "abc123"


# ---------------------------------------------------------------------------
# code_search / knowledge_related
# ---------------------------------------------------------------------------


class TestCodeSearch:
    async def test_results_formatted_for_the_transcript(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        seen: dict[str, Any] = {}
        hits = [
            _FakeHit(
                qualified_name="pkg.auth.login",
                file_path="src/pkg/auth.py",
                start_line=12,
                source="code",
                snippet="def login(user):",
            ),
            _FakeHit(heading="Auth fix", file_path="docs/adr/001.md", start_line=3),
        ]

        async def fake_semantic_search(state, slug, query, *, k, repo_path, indexed_head):
            seen.update(query=query, k=k)
            return hits

        _stub_module(
            monkeypatch,
            "agentalloy.code_index.retrieval.hybrid",
            semantic_search=fake_semantic_search,
        )
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.CODE_SEARCH, {"query": "auth bug", "k": 3}
        )
        assert seen == {"query": "auth bug", "k": 3}
        assert text == (
            "2 result(s):\n"
            "1. pkg.auth.login — src/pkg/auth.py:12 [code]\n"
            "   def login(user):\n"
            "2. Auth fix — docs/adr/001.md:3"
        )

    async def test_k_defaults_to_ten(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_semantic_search(state, slug, query, *, k, repo_path, indexed_head):
            seen["k"] = k
            return []

        _stub_module(
            monkeypatch,
            "agentalloy.code_index.retrieval.hybrid",
            semantic_search=fake_semantic_search,
        )
        await _executors(repo_root, code_index_state=_state()).execute(
            Action.CODE_SEARCH, {"query": "auth bug"}
        )
        assert seen["k"] == 10

    async def test_empty_results_is_no_results(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _stub_module(
            monkeypatch,
            "agentalloy.code_index.retrieval.hybrid",
            semantic_search=lambda *a, **k: _none(),
        )
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.CODE_SEARCH, {"query": "nothing"}
        )
        assert text == NO_RESULTS

    async def test_related_decisions_same_surface(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        async def fake_related_decisions(state, slug, query, *, k, repo_path, indexed_head):
            return [
                _FakeHit(heading="Why we use tokens", file_path="docs/adr/002.md", start_line=9)
            ]

        _stub_module(
            monkeypatch,
            "agentalloy.code_index.retrieval.hybrid",
            related_decisions=fake_related_decisions,
        )
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.KNOWLEDGE_RELATED, {"query": "auth token choice"}
        )
        assert "1 result(s):" in text
        assert "Why we use tokens — docs/adr/002.md:9" in text


async def _none() -> list[Any]:
    return []


# ---------------------------------------------------------------------------
# symbols / knowledge_why / knowledge_entities (handle-based)
# ---------------------------------------------------------------------------


class TestSymbolActions:
    async def test_symbols(self, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
        sym = SimpleNamespace(
            qualified_name="pkg.auth.login",
            kind="function",
            file_path="src/pkg/auth.py",
            start_line=12,
            docstring="  Log a user in.  ",
        )

        def _stub(handles: SimpleNamespace) -> Any:
            async def fake_with_handles(state, slug, fn, **kwargs):
                return fn(handles)

            _stub_module(
                monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles
            )

        _stub(SimpleNamespace(graph=SimpleNamespace(symbol=lambda fqn: sym)))
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.SYMBOLS, {"query": "pkg.auth.login"}
        )
        assert text == (
            'Symbol: pkg.auth.login (function)\nsrc/pkg/auth.py:12\n"""Log a user in."""'
        )

    async def test_symbols_not_found(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        async def fake_with_handles(state, slug, fn, **kwargs):
            return fn(SimpleNamespace(graph=SimpleNamespace(symbol=lambda fqn: None)))

        _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles)
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.SYMBOLS, {"query": "pkg.auth.missing"}
        )
        assert text == NO_RESULTS

    async def test_knowledge_why(self, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
        decision = SimpleNamespace(
            heading="Use opaque tokens",
            file_path="docs/adr/002.md",
            start_line=4,
            snippet="  tokens are revocable  ",
        )

        async def fake_with_handles(state, slug, fn, **kwargs):
            return fn(
                SimpleNamespace(graph=SimpleNamespace(governing_decisions=lambda fqn: [decision]))
            )

        _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles)
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.KNOWLEDGE_WHY, {"query": "pkg.auth.login"}
        )
        assert text == (
            "Decisions governing pkg.auth.login:\n"
            "1. Use opaque tokens — docs/adr/002.md:4\n"
            "   tokens are revocable"
        )

    async def test_knowledge_entities_short_name_fallback(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        edge = SimpleNamespace(
            src="pkg.auth.login", dst="httpx", kind="calls", file_path="src/pkg/auth.py"
        )
        graph = SimpleNamespace(
            typed_edges_for_fqn=lambda fqn: [edge] if fqn == "login" else [],
            symbols_by_name=lambda name: [("login",)] if name == "login" else [],
        )

        async def fake_with_handles(state, slug, fn, **kwargs):
            return fn(SimpleNamespace(graph=graph))

        _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles)
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.KNOWLEDGE_ENTITIES, {"query": "login"}
        )
        assert text == (
            "Entity edges touching login:\n- pkg.auth.login --calls--> httpx  (src/pkg/auth.py)"
        )

    async def test_knowledge_entities_kind_filter(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        edges = [
            SimpleNamespace(src="a", dst="b", kind="calls", file_path=None),
            SimpleNamespace(src="a", dst="c", kind="imports", file_path=None),
        ]
        graph = SimpleNamespace(
            typed_edges_for_fqn=lambda fqn: edges, symbols_by_name=lambda name: []
        )

        async def fake_with_handles(state, slug, fn, **kwargs):
            return fn(SimpleNamespace(graph=graph))

        _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles)
        text = await _executors(repo_root, code_index_state=_state()).execute(
            Action.KNOWLEDGE_ENTITIES, {"query": "a", "kind": "calls"}
        )
        assert text == "Entity edges touching a:\n- a --calls--> b"


# ---------------------------------------------------------------------------
# State actions (artifact_body / contract_detail)
# ---------------------------------------------------------------------------


class TestStateActions:
    async def test_no_state_store_raises(self, repo_root: Path) -> None:
        with pytest.raises(ExecutorError) as exc:
            await _executors(repo_root).execute(
                Action.ARTIFACT_BODY, {"phase": "build", "slug": "s", "query": "design.md"}
            )
        assert "state store is not available" in str(exc.value)

    async def test_artifact_with_phase(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        store = _FakeStateStore(
            artifacts={
                ("build", "auth-fix", "design.md"): {
                    "name": "design.md",
                    "phase": "build",
                    "slug": "auth-fix",
                    "content": "the design body",
                }
            }
        )
        _override_scoped_store(monkeypatch, store)
        text = await _executors(repo_root, state_store=object()).execute(
            Action.ARTIFACT_BODY, {"phase": "build", "slug": "auth-fix", "query": "design.md"}
        )
        assert text == "Artifact: design.md (phase build, contract auth-fix)\nthe design body"
        assert store.get_artifact_calls == [("build", "auth-fix", "design.md", "active")]

    async def test_artifact_without_phase_scans_lifecycle(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        store = _FakeStateStore(
            artifacts={
                ("qa", "auth-fix", "spec.md"): {
                    "name": "spec.md",
                    "phase": "qa",
                    "slug": "auth-fix",
                    "content": "qa notes",
                }
            }
        )
        _override_scoped_store(monkeypatch, store)
        text = await _executors(repo_root, state_store=object()).execute(
            Action.ARTIFACT_BODY, {"slug": "auth-fix", "query": "spec.md"}
        )
        assert "Artifact: spec.md (phase qa, contract auth-fix)" in text
        # The scan walks the lifecycle in order and stops at the first hit.
        assert store.get_artifact_calls[-1] == ("qa", "auth-fix", "spec.md", "active")

    async def test_artifact_not_found(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _override_scoped_store(monkeypatch, _FakeStateStore())
        text = await _executors(repo_root, state_store=object()).execute(
            Action.ARTIFACT_BODY, {"phase": "build", "slug": "s", "query": "missing.md"}
        )
        assert text == NO_RESULTS

    async def test_contract_detail(self, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
        store = _FakeStateStore(
            contracts={
                "auth-fix": {
                    "contract_id": "auth-fix",
                    "status": "approved",
                    "work_item": "wi-1",
                    "domain_tags": ["auth", "web"],
                    "body": "issue tokens on login",
                }
            }
        )
        _override_scoped_store(monkeypatch, store)
        text = await _executors(repo_root, state_store=object()).execute(
            Action.CONTRACT_DETAIL, {"slug": "auth-fix"}
        )
        assert text == (
            "Contract: auth-fix [approved]\nwork item: wi-1\ntags: auth, web\nissue tokens on login"
        )

    async def test_contract_missing(self, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
        _override_scoped_store(monkeypatch, _FakeStateStore())
        text = await _executors(repo_root, state_store=object()).execute(
            Action.CONTRACT_DETAIL, {"slug": "ghost"}
        )
        assert text == NO_RESULTS


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class TestTelemetry:
    @staticmethod
    def _trace(**kw: Any) -> Any:
        return SimpleNamespace(
            task_prompt=kw.get("task_prompt", "refactor the auth flow"),
            phase=kw.get("phase", "build"),
            status=kw.get("status", "ok"),
            tokens_returned=kw.get("tokens_returned"),
        )

    async def test_no_querier_raises(self, repo_root: Path) -> None:
        with pytest.raises(ExecutorError) as exc:
            await _executors(repo_root).execute(Action.TELEMETRY, {"k": 5})
        assert "telemetry is not available" in str(exc.value)

    async def test_traces_formatted(self, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
        monkeypatch.setattr(state_router, "_repo_key_for", lambda root: "hls")
        querier = _FakeQuerier(
            [
                self._trace(task_prompt="refactor the auth flow", tokens_returned=120),
                self._trace(phase="qa", status="degraded"),
            ]
        )
        text = await _executors(repo_root, telemetry_querier=querier).execute(
            Action.TELEMETRY, {"k": 5}
        )
        assert text == (
            "2 recent composition trace(s):\n"
            "- [build] ok: refactor the auth flow (+120 tokens)\n"
            "- [qa] degraded: refactor the auth flow"
        )

    async def test_query_filters_passed_through(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        monkeypatch.setattr(state_router, "_repo_key_for", lambda root: "hls")
        querier = _FakeQuerier([])
        await _executors(repo_root, telemetry_querier=querier).execute(
            Action.TELEMETRY, {"k": 7, "phase": "qa"}
        )
        assert querier.calls[0] == {
            "phase": "qa",
            "status": None,
            "since": None,
            "until": None,
            "repo": "hls",
            "limit": 7,
            "offset": 0,
        }

    async def test_prompt_truncated_at_eighty_chars(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        monkeypatch.setattr(state_router, "_repo_key_for", lambda root: "hls")
        prompt = "x" * 90
        querier = _FakeQuerier([self._trace(task_prompt=prompt)])
        text = await _executors(repo_root, telemetry_querier=querier).execute(
            Action.TELEMETRY, {"k": 3}
        )
        assert f"- [build] ok: {'x' * 77}..." in text

    async def test_no_traces(self, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
        monkeypatch.setattr(state_router, "_repo_key_for", lambda root: "hls")
        text = await _executors(repo_root, telemetry_querier=_FakeQuerier([])).execute(
            Action.TELEMETRY, {"k": 3}
        )
        assert text == NO_RESULTS

    async def test_repo_attribution_only_when_root_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(state_router, "_repo_key_for", lambda root: "hls")
        querier = _FakeQuerier([])
        await _executors(Path.cwd(), telemetry_querier=querier).execute(Action.TELEMETRY, {"k": 3})
        assert querier.calls[0]["repo"] is None


# ---------------------------------------------------------------------------
# get_skill_for (compose)
# ---------------------------------------------------------------------------


class TestGetSkillFor:
    async def test_no_orchestrator_raises(self, repo_root: Path) -> None:
        with pytest.raises(ExecutorError) as exc:
            await _executors(repo_root).execute(Action.GET_SKILL_FOR, {"task": "implement auth"})
        assert "compose orchestrator is not available" in str(exc.value)

    async def test_skill_output_returned(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        monkeypatch.setattr(state_router, "_repo_key_for", lambda root: "hls")
        _stub_module(
            monkeypatch, "agentalloy.api.compose_models", ComposeRequest=_FakeComposeRequest
        )
        orch = _FakeOrchestrator(output="use the token refresh helper")
        text = await _executors(repo_root, compose_orchestrator=orch).execute(
            Action.GET_SKILL_FOR, {"task": "implement auth", "phase": "qa"}
        )
        assert text == "use the token refresh helper"
        call = orch.calls[0]
        assert call["request"].task == "implement auth"
        assert call["request"].phase == "qa"
        assert call["request"].requesting_agent == "local-agent"
        assert call["repo"] == "hls"
        assert call["record_trace"] is True

    async def test_null_phase_defaults_to_build(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _stub_module(
            monkeypatch, "agentalloy.api.compose_models", ComposeRequest=_FakeComposeRequest
        )
        orch = _FakeOrchestrator(output="skill")
        await _executors(repo_root, compose_orchestrator=orch).execute(
            Action.GET_SKILL_FOR, {"task": "implement auth"}
        )
        assert orch.calls[0]["request"].phase == "build"

    async def test_unknown_phase_raises(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _stub_module(
            monkeypatch, "agentalloy.api.compose_models", ComposeRequest=_FakeComposeRequest
        )
        ex = _executors(repo_root, compose_orchestrator=_FakeOrchestrator())
        with pytest.raises(ExecutorError) as exc:
            await ex.execute(Action.GET_SKILL_FOR, {"task": "x", "phase": "vibecode"})
        assert "unknown phase" in str(exc.value)

    async def test_empty_output_is_no_results(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _stub_module(
            monkeypatch, "agentalloy.api.compose_models", ComposeRequest=_FakeComposeRequest
        )
        text = await _executors(
            repo_root, compose_orchestrator=_FakeOrchestrator(output="")
        ).execute(Action.GET_SKILL_FOR, {"task": "implement auth"})
        assert text == NO_RESULTS
