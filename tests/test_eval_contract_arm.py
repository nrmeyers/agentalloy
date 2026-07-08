"""composed-contract arm + paired comparator (build contract eval-contract-arm-and-comparator)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval import run_poc
from eval.compare_runs import load_run, paired_deltas
from eval.domain_tasks import DOMAIN_CONTRACT_TAGS, DOMAIN_TASKS

from agentalloy.contracts import parse_contract

_FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "contracts"


# -------- AC2.2: fixture set --------


def test_fixture_set_covers_every_domain_task() -> None:
    task_ids = {t.task_id for t in DOMAIN_TASKS}
    fixture_ids = {p.stem for p in _FIXTURES.glob("*.md")}
    assert fixture_ids == task_ids
    assert set(DOMAIN_CONTRACT_TAGS) == task_ids


def test_fixtures_parse_and_match_the_tag_map() -> None:
    by_id = {t.task_id: t for t in DOMAIN_TASKS}
    for path in sorted(_FIXTURES.glob("*.md")):
        contract = parse_contract(path)
        task = by_id[path.stem]
        assert contract.task_slug == task.task_id
        assert contract.phase == task.phase
        assert contract.domain_tags == DOMAIN_CONTRACT_TAGS[task.task_id]
        assert len(contract.domain_tags) <= 2  # tag-focus posture
        assert contract.body.strip() == task.spec.strip()


def test_contract_payload_matches_proxy_tier2_shape() -> None:
    """The arm's payload must be what compose_request_from_contract produces —
    contract_tags + contract_path present, legs=domain, task = fixture body."""
    from agentalloy.api.compose_models import compose_request_from_contract

    task = DOMAIN_TASKS[0]
    req = compose_request_from_contract(
        parse_contract(_FIXTURES / f"{task.task_id}.md"), legs="domain", k=4
    )
    payload = req.model_dump(mode="json")
    assert payload["legs"] == "domain"
    assert payload["contract_tags"] == DOMAIN_CONTRACT_TAGS[task.task_id]
    assert payload["contract_path"].endswith(f"{task.task_id}.md")
    assert payload["task"].strip() == task.spec.strip()
    assert payload["phase"] == task.phase


class _Resp:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


class _CaptureClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> _Resp:  # noqa: A002
        self.payloads.append(json)
        return _Resp({"output": "x", "result_type": "hit", "source_skills": ["s1"]})


def test_call_compose_from_contract_posts_contract_fields() -> None:
    client = _CaptureClient()
    task = DOMAIN_TASKS[0]
    out, rtype, _ms, skills = run_poc.call_compose_from_contract(client, task, k=2)  # type: ignore[arg-type]
    assert out == "x" and rtype == "hit" and skills == ["s1"]
    payload = client.payloads[0]
    assert payload["contract_tags"] == DOMAIN_CONTRACT_TAGS[task.task_id]
    assert payload["legs"] == "domain" and payload["k"] == 2


# -------- AC2.6: paired comparator --------


def _write_cell(
    run_dir: Path, task_id: str, cond: str, idx: int, *, score: float, seed: int, tokens: int
) -> None:
    cell = run_dir / task_id / cond
    cell.mkdir(parents=True, exist_ok=True)
    (cell / f"run-{idx}.meta.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "condition": cond,
                "run_index": idx,
                "seed": seed,
                "score": score,
                "input_tokens": tokens,
            }
        )
    )


def test_paired_deltas_per_task_and_totals(tmp_path: Path) -> None:
    base, cand = tmp_path / "base", tmp_path / "cand"
    for idx, (b_score, c_score) in enumerate([(0.5, 0.75), (0.5, 0.65)]):
        _write_cell(base, "t1", "composed", idx, score=b_score, seed=idx, tokens=800)
        _write_cell(cand, "t1", "composed", idx, score=c_score, seed=idx, tokens=600)
    _write_cell(base, "t2", "composed", 0, score=1.0, seed=7, tokens=900)
    _write_cell(cand, "t2", "composed", 0, score=0.9, seed=7, tokens=650)

    report = paired_deltas(load_run(base), load_run(cand))
    assert report["paired_cells"] == 3
    assert report["per_task"]["t1"]["composed"] == 0.2  # mean of +0.25, +0.15
    assert report["per_task"]["t2"]["composed"] == -0.1
    totals = report["totals"]["composed"]
    assert totals["n_pairs"] == 3
    assert totals["mean_input_tokens_baseline"] > totals["mean_input_tokens_candidate"]


def test_paired_deltas_excludes_and_flags_seed_mismatch(tmp_path: Path) -> None:
    base, cand = tmp_path / "base", tmp_path / "cand"
    _write_cell(base, "t1", "composed", 0, score=0.5, seed=1, tokens=100)
    _write_cell(cand, "t1", "composed", 0, score=0.9, seed=2, tokens=100)  # diverged
    _write_cell(base, "t1", "composed", 1, score=0.5, seed=3, tokens=100)
    _write_cell(cand, "t1", "composed", 1, score=0.6, seed=3, tokens=100)

    report = paired_deltas(load_run(base), load_run(cand))
    assert report["seed_mismatches"] == ["t1/composed/run-0"]
    assert report["paired_cells"] == 1
    assert report["per_task"]["t1"]["composed"] == 0.1


def test_paired_deltas_condition_filter_and_unshared_cells(tmp_path: Path) -> None:
    base, cand = tmp_path / "base", tmp_path / "cand"
    _write_cell(base, "t1", "composed", 0, score=0.5, seed=1, tokens=100)
    _write_cell(cand, "t1", "composed", 0, score=0.7, seed=1, tokens=100)
    _write_cell(base, "t1", "none", 0, score=0.2, seed=1, tokens=100)
    _write_cell(cand, "t1", "none", 0, score=0.4, seed=1, tokens=100)
    _write_cell(base, "t9", "flat", 0, score=0.9, seed=9, tokens=100)  # baseline-only

    report = paired_deltas(load_run(base), load_run(cand), condition="composed")
    assert list(report["totals"]) == ["composed"]
    assert report["per_task"]["t1"] == {"composed": 0.2}
