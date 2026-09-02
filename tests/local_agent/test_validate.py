"""Validator (M1b) — index-grounded checks for filled arguments.

Schema conformance lives in ``protocol.parse_fill``; this leg answers the
sharper question — does the argument name something the live index actually
contains? — with a fail-open posture in the *opposite* direction from the LM
stages: a broken store lookup passes, so a flaky dependency never blocks a
question the schema check already accepted.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentalloy.local_agent.protocol import Action
from agentalloy.local_agent.validate import Validator

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeRepo:
    slug: str
    repo_path: str


class _FakeJobs:
    def __init__(self, repos: list[_FakeRepo]) -> None:
        self._repos = repos

    def list_repos(self) -> list[_FakeRepo]:
        return self._repos

    def get_repo(self, slug: str) -> _FakeRepo | None:
        for repo in self._repos:
            if repo.slug == slug:
                return repo
        return None


class _FakeCodeIndexState:
    def __init__(self, repos: list[_FakeRepo]) -> None:
        self.jobs = _FakeJobs(repos)


class _FakeGraph:
    def __init__(self, known: set[str] = ()) -> None:
        self._known = known

    def symbols_by_name(self, fqn: str) -> list[Any]:
        return [object()] if fqn in self._known else []

    def symbol(self, fqn: str) -> Any:
        return object() if fqn in self._known else None


@dataclass
class _FakeHandles:
    graph: _FakeGraph


def _with_handles_stub(monkeypatch: pytest.MonkeyPatch, known: set[str] = ()):
    async def fake_with_handles(state: Any, slug: str, fn, **kwargs: Any) -> Any:
        return fn(_FakeHandles(_FakeGraph(known)))

    _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles)


def _with_handles_raises(monkeypatch: pytest.MonkeyPatch):
    async def fake_with_handles(state: Any, slug: str, fn, **kwargs: Any) -> Any:
        raise RuntimeError("backend offline")

    _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles)


def _stub_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


class _FakeStore:
    def __init__(self, known: set[str] | None = None, *, fail: bool = False) -> None:
        self._known = known or set()
        self._fail = fail

    def get_contract(self, slug: str) -> dict[str, Any] | None:
        if self._fail:
            raise RuntimeError("store closed")
        return {"contract_id": slug} if slug in self._known else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


def _validator(
    repo_root: Path,
    *,
    code_index_state: Any = None,
    state_store: Any = None,
    repo_slug: str | None = None,
) -> Validator:
    return Validator(
        code_index_state=code_index_state,
        state_store=state_store,
        repo_slug=repo_slug,
        repo_root=repo_root,
    )


# ---------------------------------------------------------------------------
# Symbol leg (knowledge_why / symbols / knowledge_entities)
# ---------------------------------------------------------------------------


class TestSymbolLeg:
    @pytest.mark.parametrize(
        "action", [Action.KNOWLEDGE_WHY, Action.SYMBOLS, Action.KNOWLEDGE_ENTITIES]
    )
    async def test_blank_query_rejected(self, action: Action, repo_root: Path) -> None:
        check = await _validator(repo_root).validate(action, {"query": "   "})
        assert not check.ok
        assert check.error == "argument 'query' must be a fully-qualified symbol name"

    async def test_missing_query_rejected(self, repo_root: Path) -> None:
        check = await _validator(repo_root).validate(Action.SYMBOLS, {})
        assert not check.ok
        assert check.error == "argument 'query' must be a fully-qualified symbol name"

    async def test_no_index_on_service_passes_fail_open(self, repo_root: Path) -> None:
        check = await _validator(repo_root).validate(Action.SYMBOLS, {"query": "pkg.mod.Fn"})
        assert check.ok and check.error is None

    async def test_symbol_not_in_index_rejected(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _with_handles_stub(monkeypatch)  # no known symbols
        state = _FakeCodeIndexState([_FakeRepo("main-repo", "/data/main")])
        check = await _validator(repo_root, code_index_state=state, repo_slug="main-repo").validate(
            Action.SYMBOLS, {"query": "pkg.mod.Missing"}
        )
        assert not check.ok
        assert check.error == "argument 'query'='pkg.mod.Missing' does not exist in the code index"

    async def test_symbol_in_index_ok(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _with_handles_stub(monkeypatch, known={"pkg.mod.Fn"})
        state = _FakeCodeIndexState([_FakeRepo("main-repo", "/data/main")])
        check = await _validator(repo_root, code_index_state=state, repo_slug="main-repo").validate(
            Action.KNOWLEDGE_WHY, {"query": "pkg.mod.Fn"}
        )
        assert check.ok and check.error is None

    async def test_uses_short_name_resolver_first(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        # Only symbols_by_name knows the symbol; symbol() alone would miss it.
        _with_handles_stub(monkeypatch, known={"Fn"})
        state = _FakeCodeIndexState([_FakeRepo("main-repo", "/data/main")])
        check = await _validator(repo_root, code_index_state=state, repo_slug="main-repo").validate(
            Action.SYMBOLS, {"query": "Fn"}
        )
        assert check.ok

    async def test_repo_not_indexed_passes(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        # get_repo misses before any handle lookup — the executor owns the report.
        def _no_stub(*a: object, **k: object) -> None:  # pragma: no cover
            raise AssertionError("with_handles must not be called for an unindexed repo")

        _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=_no_stub)
        state = _FakeCodeIndexState([_FakeRepo("main-repo", "/data/main")])
        check = await _validator(repo_root, code_index_state=state, repo_slug="ghost").validate(
            Action.SYMBOLS, {"query": "pkg.mod.Fn"}
        )
        assert check.ok

    async def test_multiple_repos_unspecified_passes(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        def _no_stub(*a: object, **k: object) -> None:  # pragma: no cover
            raise AssertionError("with_handles must not be called with ambiguous scope")

        _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=_no_stub)
        state = _FakeCodeIndexState([_FakeRepo("alpha", "/data/a"), _FakeRepo("beta", "/data/b")])
        check = await _validator(repo_root, code_index_state=state).validate(
            Action.SYMBOLS, {"query": "pkg.mod.Fn"}
        )
        assert check.ok

    async def test_single_repo_resolved_from_registry(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        seen: dict[str, str] = {}

        async def fake_with_handles(state: Any, slug: str, fn, **kwargs: Any) -> Any:
            seen["slug"] = slug
            return fn(_FakeHandles(_FakeGraph({"pkg.mod.Fn"})))

        _stub_module(monkeypatch, "agentalloy.code_index.api.deps", with_handles=fake_with_handles)
        state = _FakeCodeIndexState([_FakeRepo("solo-repo", "/data/solo")])
        check = await _validator(repo_root, code_index_state=state).validate(
            Action.SYMBOLS, {"query": "pkg.mod.Fn"}
        )
        assert check.ok
        assert seen["slug"] == "solo-repo"

    async def test_store_lookup_failure_passes_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        _with_handles_raises(monkeypatch)
        state = _FakeCodeIndexState([_FakeRepo("main-repo", "/data/main")])
        check = await _validator(repo_root, code_index_state=state, repo_slug="main-repo").validate(
            Action.SYMBOLS, {"query": "pkg.mod.Fn"}
        )
        assert check.ok and check.error is None


# ---------------------------------------------------------------------------
# Contract leg (artifact_body / contract_detail)
# ---------------------------------------------------------------------------


class TestContractLeg:
    @pytest.mark.parametrize("action", [Action.ARTIFACT_BODY, Action.CONTRACT_DETAIL])
    async def test_blank_slug_rejected(self, action: Action, repo_root: Path) -> None:
        check = await _validator(repo_root, state_store=object()).validate(action, {"slug": ""})
        assert not check.ok
        assert check.error == "argument 'slug' must be a contract slug"

    async def test_no_state_store_on_service_passes(self, repo_root: Path) -> None:
        check = await _validator(repo_root).validate(Action.ARTIFACT_BODY, {"slug": "auth-fix"})
        assert check.ok and check.error is None

    async def test_unknown_contract_rejected(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        import agentalloy.api.state_router as state_router  # noqa: PLC0415

        monkeypatch.setattr(state_router, "scoped_state_store", lambda store, root: _FakeStore())
        check = await _validator(repo_root, state_store=object()).validate(
            Action.ARTIFACT_BODY, {"slug": "auth-fix"}
        )
        assert not check.ok
        assert check.error == "argument 'slug'='auth-fix' is not a known contract"

    async def test_known_contract_ok(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        import agentalloy.api.state_router as state_router  # noqa: PLC0415

        monkeypatch.setattr(
            state_router, "scoped_state_store", lambda store, root: _FakeStore({"auth-fix"})
        )
        check = await _validator(repo_root, state_store=object()).validate(
            Action.CONTRACT_DETAIL, {"slug": "auth-fix"}
        )
        assert check.ok and check.error is None

    async def test_scoped_store_used_for_the_request_root(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        import agentalloy.api.state_router as state_router  # noqa: PLC0415

        seen: dict[str, Any] = {}

        def fake_scoped(store: Any, root: Path) -> _FakeStore:
            seen["store"] = store
            seen["root"] = root
            return _FakeStore({"auth-fix"})

        monkeypatch.setattr(state_router, "scoped_state_store", fake_scoped)
        store = object()
        await _validator(repo_root, state_store=store).validate(
            Action.CONTRACT_DETAIL, {"slug": "auth-fix"}
        )
        assert seen["store"] is store
        assert seen["root"] == repo_root

    async def test_contract_lookup_failure_passes_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, repo_root: Path
    ) -> None:
        import agentalloy.api.state_router as state_router  # noqa: PLC0415

        monkeypatch.setattr(
            state_router, "scoped_state_store", lambda store, root: _FakeStore(fail=True)
        )
        check = await _validator(repo_root, state_store=object()).validate(
            Action.CONTRACT_DETAIL, {"slug": "auth-fix"}
        )
        assert check.ok and check.error is None


# ---------------------------------------------------------------------------
# Free-text / numeric / none — the schema check was enough
# ---------------------------------------------------------------------------


class TestFreeTextLeg:
    @pytest.mark.parametrize(
        "action",
        [
            Action.CODE_SEARCH,
            Action.KNOWLEDGE_RELATED,
            Action.TELEMETRY,
            Action.GET_SKILL_FOR,
            Action.NONE,
        ],
    )
    async def test_trivially_valid(self, action: Action, repo_root: Path) -> None:
        check = await _validator(repo_root).validate(action, {})
        assert check.ok and check.error is None
