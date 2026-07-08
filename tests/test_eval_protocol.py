"""Layer-2 protocol fidelity seams (build contract eval-protocol-fidelity).

Unit coverage for the harness request shape (legs=domain), provenance
fetchers, the gold-skill preflight, and source_skills persistence — all via a
stubbed httpx client; no live service or model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from eval import run_poc
from eval.tasks import Task


class _Resp:
    def __init__(self, status_code: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeClient:
    """Routes .post/.get by URL substring; records request payloads."""

    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def _match(self, url: str) -> _Resp:
        for fragment, resp in self.routes.items():
            if fragment in url:
                return resp
        raise AssertionError(f"unrouted url: {url}")

    def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> _Resp:  # noqa: A002
        self.posts.append((url, json))
        return self._match(url)

    def get(self, url: str, timeout: Any = None) -> _Resp:
        return self._match(url)


_TASK = Task(
    task_id="probe_task",
    spec="verify webhook signatures",
    phase="build",
    gold_skills=("webhooks-signature-verification",),
)


# -------- AC2.1: composed arm requests the shipped Tier-2 shape --------


def test_call_compose_sends_legs_domain() -> None:
    client = _FakeClient(
        {"/compose": _Resp(body={"output": "x", "result_type": "hit", "source_skills": ["s1"]})}
    )
    out, rtype, _ms, skills = run_poc.call_compose(client, _TASK, k=4)  # type: ignore[arg-type]
    assert out == "x" and rtype == "hit" and skills == ["s1"]
    _url, payload = client.posts[0]
    assert payload["legs"] == "domain"
    assert payload["k"] == 4


# -------- AC2.3: provenance fetchers --------


def test_fetch_service_provenance_reads_service_block() -> None:
    client = _FakeClient(
        {"/health": _Resp(body={"service": {"version": "9.9.9", "corpus_stamp": "ab" * 32}})}
    )
    prov = run_poc.fetch_service_provenance(client)  # type: ignore[arg-type]
    assert prov == {"service_version": "9.9.9", "corpus_stamp": "ab" * 32}


def test_fetch_service_provenance_null_tolerant_on_old_service() -> None:
    client = _FakeClient({"/health": _Resp(body={"status": "healthy"})})
    prov = run_poc.fetch_service_provenance(client)  # type: ignore[arg-type]
    assert prov == {"service_version": None, "corpus_stamp": None}


def test_fetch_serving_backend_records_model_ids() -> None:
    client = _FakeClient(
        {"/v1/models": _Resp(body={"data": [{"id": "lfm-2.5-8b"}, {"id": "other"}]})}
    )
    backend = run_poc.fetch_serving_backend(client)  # type: ignore[arg-type]
    assert backend["models"] == ["lfm-2.5-8b", "other"]
    assert backend["url"]


# -------- AC2.4: gold preflight --------


def _diag(skill_ids: list[str]) -> _Resp:
    return _Resp(body={"store_state": [{"skill_id": s} for s in skill_ids]})


def _fake_pack_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, deprecated: bool) -> None:
    y = tmp_path / "webhooks-signature-verification.yaml"
    doc = "skill_id: webhooks-signature-verification\n"
    if deprecated:
        doc += "deprecated: true\nsuperseded_by: newer-skill\n"
    y.write_text(doc)
    monkeypatch.setattr(
        run_poc, "_pack_skill_index", lambda: {"webhooks-signature-verification": y}
    )


def test_preflight_clean_corpus_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_pack_index(monkeypatch, tmp_path, deprecated=False)
    client = _FakeClient({"/diagnostics/runtime": _diag(["webhooks-signature-verification"])})
    assert run_poc.preflight_gold_skills(client, [_TASK], require_live=True) == []  # type: ignore[arg-type]


def test_preflight_flags_missing_live_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_pack_index(monkeypatch, tmp_path, deprecated=False)
    client = _FakeClient({"/diagnostics/runtime": _diag(["some-other-skill"])})
    violations = run_poc.preflight_gold_skills(client, [_TASK], require_live=True)  # type: ignore[arg-type]
    assert violations and "not in the live corpus" in violations[0]


def test_preflight_flags_deprecated_gold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_pack_index(monkeypatch, tmp_path, deprecated=True)
    client = _FakeClient({"/diagnostics/runtime": _diag(["webhooks-signature-verification"])})
    violations = run_poc.preflight_gold_skills(client, [_TASK], require_live=True)  # type: ignore[arg-type]
    assert violations and "deprecated in pack source" in violations[0]
    assert "newer-skill" in violations[0]


def test_preflight_skips_live_check_without_composed_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_pack_index(monkeypatch, tmp_path, deprecated=False)
    client = _FakeClient({})  # any HTTP call would raise "unrouted url"
    assert run_poc.preflight_gold_skills(client, [_TASK], require_live=False) == []  # type: ignore[arg-type]


# -------- AC2.5: source_skills persisted in run meta --------


def test_run_one_persists_source_skills(tmp_path: Path) -> None:
    client = _FakeClient(
        {
            "/compose": _Resp(
                body={"output": "guidance", "result_type": "hit", "source_skills": ["s1", "s2"]}
            ),
            "/chat/completions": _Resp(
                body={
                    "choices": [{"message": {"content": "answer"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ),
        }
    )
    graders = {"probe_task": lambda out: {"answered": bool(out)}}
    run_poc.run_one(client, _TASK, "composed", 0, tmp_path, k=4, graders=graders)  # type: ignore[arg-type]
    meta = json.loads((tmp_path / "probe_task" / "composed" / "run-0.meta.json").read_text())
    assert meta["source_skills"] == ["s1", "s2"]


def test_run_one_source_skills_empty_for_none_arm(tmp_path: Path) -> None:
    client = _FakeClient(
        {
            "/chat/completions": _Resp(
                body={
                    "choices": [{"message": {"content": "answer"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ),
        }
    )
    graders = {"probe_task": lambda out: {"answered": bool(out)}}
    run_poc.run_one(client, _TASK, "none", 0, tmp_path, k=4, graders=graders)  # type: ignore[arg-type]
    meta = json.loads((tmp_path / "probe_task" / "none" / "run-0.meta.json").read_text())
    assert meta["source_skills"] == []
