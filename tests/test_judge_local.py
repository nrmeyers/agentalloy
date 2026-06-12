"""Unit tests for the local LLM-judge (eval/judge_local.py).

Everything here is offline. The model server is faked with an
``httpx.MockTransport`` that returns canned ``/v1/chat/completions`` payloads,
so we exercise the real client + parsing + retry + checkpoint code paths
without a GPU. (Live behaviour — actual AceReason-Nemotron output, latency — is
deferred to a post-download smoke test; see the PR.)
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from eval.judge_common import coerce_verdict, extract_json_object, strip_think
from eval.judge_local import (
    LocalJudgeClient,
    _record,
    _rows_from_verdicts,
    _select_work,
    _stratified_sample,
    _verdict_key,
    judge_once,
    load_done_keys,
)


def _chat_response(content: str = "", reasoning: str = "") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content, "reasoning_content": reasoning}}]},
    )


def _client_from_replies(replies: list[httpx.Response]) -> LocalJudgeClient:
    """Build a client whose transport yields ``replies`` in order."""
    it = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(it)

    return LocalJudgeClient(transport=httpx.MockTransport(handler))


# --- pure parsing helpers ---------------------------------------------------


def test_strip_think_removes_balanced_block():
    text = '<think>reasoning here</think>\n{"correctness": 5}'
    assert strip_think(text).strip() == '{"correctness": 5}'


def test_strip_think_leaves_unmatched_tag():
    text = '<think>dangling {"a": 1}'
    assert "<think>" in strip_think(text)


def test_extract_json_object_ignores_braces_in_strings():
    text = 'prefix {"rationale": "use {curly} braces", "correctness": 4} suffix'
    obj = extract_json_object(text)
    assert obj is not None
    assert obj["rationale"] == "use {curly} braces"
    assert obj["correctness"] == 4


def test_extract_json_object_from_markdown_fence():
    text = 'Here you go:\n```json\n{"coverage": 3}\n```'
    assert extract_json_object(text) == {"coverage": 3}


def test_extract_json_object_skips_first_unbalanced_brace():
    # leading "{" belongs to prose; the real object comes later
    text = 'score is {high} actually {"precision": 2}'
    assert extract_json_object(text) == {"precision": 2}


def test_coerce_verdict_computes_score():
    v = coerce_verdict({"correctness": 5, "coverage": 4, "precision": 3, "rationale": "ok"})
    assert v is not None
    assert v["judge_score"] == pytest.approx((5 + 4 + 3) / 15)


def test_coerce_verdict_rejects_out_of_range():
    assert coerce_verdict({"correctness": 6, "coverage": 0, "precision": 0}) is None


def test_coerce_verdict_rejects_missing_dimension():
    assert coerce_verdict({"correctness": 5, "coverage": 5}) is None


def test_coerce_verdict_tolerates_float_strings():
    v = coerce_verdict({"correctness": 4.0, "coverage": "3", "precision": 2, "rationale": ""})
    assert v is not None
    assert v["correctness"] == 4 and v["coverage"] == 3


# --- judge_once: content / reasoning / retry --------------------------------


def test_judge_once_json_from_content_with_think_block():
    payload = (
        "<think>The response is solid.</think>\n"
        '{"correctness": 5, "coverage": 4, "precision": 4, "rationale": "good"}'
    )
    client = _client_from_replies([_chat_response(content=payload)])
    verdict, raw = judge_once(client, "spec", "candidate")
    assert verdict is not None
    assert verdict["correctness"] == 5
    assert "<think>" not in raw


def test_judge_once_falls_back_to_reasoning_content():
    # content is empty; the JSON leaked into reasoning_content
    client = _client_from_replies(
        [
            _chat_response(
                content="",
                reasoning='{"correctness": 3, "coverage": 3, "precision": 3, "rationale": "meh"}',
            )
        ]
    )
    verdict, _ = judge_once(client, "spec", "candidate")
    assert verdict is not None
    assert verdict["judge_score"] == pytest.approx(9 / 15)


def test_judge_once_retries_once_on_malformed_then_succeeds():
    good = '{"correctness": 2, "coverage": 2, "precision": 2, "rationale": "x"}'
    client = _client_from_replies(
        [
            _chat_response(content="sorry, no JSON for you"),
            _chat_response(content=good),
        ]
    )
    verdict, _ = judge_once(client, "spec", "candidate")
    assert verdict is not None
    assert verdict["correctness"] == 2


def test_judge_once_returns_none_after_two_failures():
    client = _client_from_replies(
        [_chat_response(content="nope"), _chat_response(content="still nope")]
    )
    verdict, raw = judge_once(client, "spec", "candidate")
    assert verdict is None
    assert raw == "still nope"


# --- checkpoint resume ------------------------------------------------------


def test_load_done_keys_skips_truncated_last_line(tmp_path: Path):
    p = tmp_path / "verdicts.jsonl"
    p.write_text(
        json.dumps({"key": "a"})
        + "\n"
        + json.dumps({"key": "b"})
        + "\n"
        + '{"key": "c", "trunc'  # interrupted append, no newline
    )
    assert load_done_keys(p) == {"a", "b"}


def test_load_done_keys_missing_file(tmp_path: Path):
    assert load_done_keys(tmp_path / "nope.jsonl") == set()


def _write_run(run_dir: Path, task: str, cond: str, idx: int, *, score: float = 0.5) -> Path:
    d = run_dir / task / cond
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"run-{idx}.txt"
    txt.write_text(f"output for {task}/{cond}/{idx}")
    (d / f"run-{idx}.meta.json").write_text(
        json.dumps(
            {
                "task_id": task,
                "condition": cond,
                "run_index": idx,
                "score": score,
                "output_tokens": 100 + idx,
                "model": "qwen3-4b",
            }
        )
    )
    return txt


def test_resume_skips_already_judged_keys(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    txt = _write_run(run_dir, "taskA", "composed", 0)
    key = _verdict_key(txt.parents[2], "taskA", "composed", 0)

    verdicts = tmp_path / "verdicts.jsonl"
    verdicts.write_text(json.dumps({"key": key}) + "\n")

    done = load_done_keys(verdicts)
    assert key in done
    # the run command filters pending with exactly this key construction
    assert _verdict_key(txt.parents[2], "taskA", "composed", 0) in done


# --- sample stratification --------------------------------------------------


def _ns(**kw):
    import argparse

    base = dict(conditions=None, models=None, limit=None, sample=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_stratified_sample_keeps_k_per_stratum(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    work = []
    for cond in ("composed", "none"):
        for idx in range(5):
            txt = _write_run(run_dir, "taskA", cond, idx)
            work.append((txt, json.loads((txt.with_name(f"run-{idx}.meta.json")).read_text())))

    sampled = _stratified_sample(work, 2)
    # 2 conditions x 2 kept = 4
    assert len(sampled) == 4
    # the kept run_indexes are the lowest 2 per stratum (deterministic resume)
    kept_idx = sorted({m["run_index"] for _, m in sampled})
    assert kept_idx == [0, 1]


def test_select_work_applies_condition_and_sample_filters(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    for cond in ("composed", "none", "flat"):
        for idx in range(5):
            _write_run(run_dir, "taskA", cond, idx)

    args = _ns(run_dirs=[str(run_dir)], conditions="composed,none", sample=2)
    work = _select_work(args)
    conds = {m["condition"] for _, m in work}
    assert conds == {"composed", "none"}
    assert len(work) == 4  # 2 conditions x 2 sampled


def test_select_work_limit_caps_total(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    for idx in range(5):
        _write_run(run_dir, "taskA", "composed", idx)
    args = _ns(run_dirs=[str(run_dir)], limit=3)
    assert len(_select_work(args)) == 3


# --- verdict record + report rows -------------------------------------------


def test_record_parse_error_has_no_judge_score(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    txt = _write_run(run_dir, "taskA", "composed", 0)
    meta = json.loads((txt.with_name("run-0.meta.json")).read_text())
    key = _verdict_key(txt.parents[2], "taskA", "composed", 0)
    rec = _record(key, meta, txt.parents[2], None, error="parse_error", raw="garbage")
    assert "judge_score" not in rec
    assert rec["parse_error"] == "parse_error"
    assert rec["heuristic"] == 0.5


def test_rows_from_verdicts_excludes_parse_errors_and_dedupes(tmp_path: Path):
    p = tmp_path / "verdicts.jsonl"
    good = {
        "key": "k1",
        "run_dir": str(tmp_path / "leg"),
        "task_id": "taskA",
        "condition": "composed",
        "run_index": 0,
        "judge_score": 0.6,
        "heuristic": 0.5,
        "output_tokens": 120,
    }
    bad = {"key": "k2", "parse_error": "parse_error", "heuristic": 0.4, "task_id": "t"}
    # duplicate key k1 with a higher score should win (later record)
    superseded = dict(good, judge_score=0.99)
    p.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n" + json.dumps(superseded) + "\n")
    rows = _rows_from_verdicts([p])
    assert len(rows) == 1
    assert rows[0][4] == 0.99  # later record wins


def test_client_does_not_send_temperature(tmp_path: Path):
    """The server pins temp/top_p; the client must not override them."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _chat_response(
            content='{"correctness": 1, "coverage": 1, "precision": 1, "rationale": "x"}'
        )

    client = LocalJudgeClient(transport=httpx.MockTransport(handler))
    client.chat([{"role": "user", "content": "hi"}])
    assert "temperature" not in captured
    assert "top_p" not in captured
