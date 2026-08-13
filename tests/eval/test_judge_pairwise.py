"""Unit tests for the pairwise local LLM-judge (eval/judge_pairwise.py).

Offline only. The model server is faked with ``httpx.MockTransport`` (reusing
the scalar judge's :class:`LocalJudgeClient`), so we exercise prompt building +
order construction + blinding + reconciliation + checkpoint resume + the report
math without a GPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from eval.judge_local import LocalJudgeClient
from eval.judge_pairwise import (
    PairOutcome,
    _heuristic_delta_signs,
    _judge_order,
    _select_units,
    _sign_test_p,
    _stratified_units,
    _tally,
    build_pairwise_prompt,
    build_units,
    coerce_pairwise,
    judge_pair_once,
    load_done_orders,
    order_key,
    parse_pairs,
    reconcile,
    reconcile_records,
    render_pairwise_report,
)


def _chat_response(content: str = "", reasoning: str = "") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content, "reasoning_content": reasoning}}]},
    )


def _client_from_replies(replies: list[httpx.Response]) -> LocalJudgeClient:
    it = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(it)

    return LocalJudgeClient(transport=httpx.MockTransport(handler))


def _win(w: str) -> httpx.Response:
    return _chat_response(content=json.dumps({"winner": w, "reason": "because"}))


# --- pair parsing -----------------------------------------------------------


def test_parse_pairs_basic():
    assert parse_pairs("composed:none,composed:flat") == [
        ("composed", "none"),
        ("composed", "flat"),
    ]


def test_parse_pairs_rejects_bad_shapes():
    for bad in ("composed", "a:b:c", "x:x", ":none"):
        with pytest.raises(ValueError):
            parse_pairs(bad)


# --- coercion ---------------------------------------------------------------


def test_coerce_pairwise_normalizes_case_and_space():
    assert coerce_pairwise({"winner": " a "})["winner"] == "A"
    assert coerce_pairwise({"winner": "TIE"})["winner"] == "tie"


def test_coerce_pairwise_rejects_junk():
    assert coerce_pairwise({"winner": "neither"}) is None
    assert coerce_pairwise({"reason": "no winner"}) is None


# --- prompt blinding --------------------------------------------------------


def test_prompt_never_leaks_condition_names():
    prompt = build_pairwise_prompt("the spec", "first candidate", "second candidate")
    assert "RESPONSE A" in prompt and "RESPONSE B" in prompt
    # condition vocabulary must never reach the model
    for word in ("composed", "flat", "external", "condition"):
        assert word not in prompt.lower()


def test_judge_order_maps_files_to_positional_slots(tmp_path: Path):
    """AB order: left->A, right->B. BA order swaps them in the prompt."""
    left = tmp_path / "L.txt"
    right = tmp_path / "R.txt"
    left.write_text("LEFTCONTENT")
    right.write_text("RIGHTCONTENT")
    unit = _make_unit(tmp_path, "taskA", ("composed", "none"), 0, left, right)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["messages"][0]["content"])
        return _win("A")

    client = LocalJudgeClient(transport=httpx.MockTransport(handler))
    _judge_order(client, "spec", unit, "AB")
    _judge_order(client, "spec", unit, "BA")

    ab_prompt, ba_prompt = seen
    # In AB, LEFT precedes RIGHT; in BA, RIGHT precedes LEFT.
    assert ab_prompt.index("LEFTCONTENT") < ab_prompt.index("RIGHTCONTENT")
    assert ba_prompt.index("RIGHTCONTENT") < ba_prompt.index("LEFTCONTENT")


# --- judge_pair_once: retry path --------------------------------------------


def test_judge_pair_once_retries_once_then_succeeds():
    client = _client_from_replies([_chat_response(content="not json"), _win("B")])
    verdict, _ = judge_pair_once(client, "spec", "a", "b")
    assert verdict is not None and verdict["winner"] == "B"


def test_judge_pair_once_none_after_two_failures():
    client = _client_from_replies([_chat_response(content="x"), _chat_response(content="y")])
    verdict, raw = judge_pair_once(client, "spec", "a", "b")
    assert verdict is None and raw == "y"


def test_judge_pair_once_reads_think_and_reasoning_fallback():
    payload = '<think>weighing</think>{"winner": "A", "reason": "r"}'
    client = _client_from_replies([_chat_response(reasoning=payload)])
    verdict, raw = judge_pair_once(client, "spec", "a", "b")
    assert verdict is not None and verdict["winner"] == "A"
    assert "<think>" not in raw


# --- reconciliation ---------------------------------------------------------


def test_reconcile_agreement_left_wins():
    # AB: A wins -> left. BA: B wins -> left (A is the right cond under BA).
    assert reconcile("A", "B") == "left"


def test_reconcile_agreement_right_wins():
    # AB: B wins -> right. BA: A wins -> right.
    assert reconcile("B", "A") == "right"


def test_reconcile_position_flip_both_say_a_slot():
    # The positional A wins regardless of which condition sits there.
    assert reconcile("A", "A") == "position_flip"


def test_reconcile_position_flip_both_say_b_slot():
    assert reconcile("B", "B") == "position_flip"


def test_reconcile_tie_when_either_order_ties():
    assert reconcile("tie", "A") == "tie"
    assert reconcile("A", "tie") == "tie"
    assert reconcile("tie", "tie") == "tie"


# --- checkpoint keys + resume -----------------------------------------------


def test_order_key_includes_order_so_orders_resume_independently(tmp_path: Path):
    k_ab = order_key(tmp_path, "taskA", ("composed", "none"), 0, "AB")
    k_ba = order_key(tmp_path, "taskA", ("composed", "none"), 0, "BA")
    assert k_ab != k_ba
    assert k_ab.endswith("|AB") and k_ba.endswith("|BA")


def test_load_done_orders_skips_truncated_last_line(tmp_path: Path):
    p = tmp_path / "pairwise.jsonl"
    p.write_text(
        json.dumps({"key": "k1", "winner": "A"})
        + "\n"
        + json.dumps({"key": "k2", "winner": "B"})
        + "\n"
        + '{"key": "k3", "wi'  # interrupted append
    )
    done = load_done_orders(p)
    assert set(done) == {"k1", "k2"}


def test_resume_skips_completed_order_judges_only_missing(tmp_path: Path):
    """A unit with AB already checkpointed should only re-judge BA."""
    run_dir = _build_pair(tmp_path, "taskA", ("composed", "none"), [0])
    units = build_units([run_dir], [("composed", "none")])
    assert len(units) == 1
    u = units[0]

    verdicts = tmp_path / "pairwise.jsonl"
    ab_key = order_key(u.run_dir, u.task_id, u.pair, u.run_index, "AB")
    verdicts.write_text(json.dumps({"key": ab_key, "winner": "A"}) + "\n")

    done = load_done_orders(verdicts)
    pending = [
        (u, order)
        for order in ("AB", "BA")
        if order_key(u.run_dir, u.task_id, u.pair, u.run_index, order) not in done
    ]
    assert [o for _, o in pending] == ["BA"]


# --- build_units: only pairs present in BOTH legs ---------------------------


def test_build_units_only_when_both_legs_present(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    # task with both legs at runs 0,1; right-only run 2 must be dropped.
    _write_output(run_dir, "taskA", "composed", 0)
    _write_output(run_dir, "taskA", "composed", 1)
    _write_output(run_dir, "taskA", "none", 0)
    _write_output(run_dir, "taskA", "none", 1)
    _write_output(run_dir, "taskA", "none", 2)  # no composed/run-2
    # taskB has only the left leg -> no units.
    _write_output(run_dir, "taskB", "composed", 0)

    units = build_units([run_dir], [("composed", "none")])
    keys = sorted((u.task_id, u.run_index) for u in units)
    assert keys == [("taskA", 0), ("taskA", 1)]


def test_build_units_reads_agent_model(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    _write_output(run_dir, "taskA", "composed", 0, agent_model="gemma-4-12b-it")
    _write_output(run_dir, "taskA", "none", 0, agent_model="gemma-4-12b-it")
    units = build_units([run_dir], [("composed", "none")])
    assert units[0].model == "gemma-4-12b-it"


def test_stratified_units_keeps_k_lowest_run_indexes(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    for idx in range(5):
        _write_output(run_dir, "taskA", "composed", idx)
        _write_output(run_dir, "taskA", "none", idx)
    units = build_units([run_dir], [("composed", "none")])
    kept = _stratified_units(units, 2)
    assert sorted(u.run_index for u in kept) == [0, 1]


def test_select_units_applies_sample_and_limit(tmp_path: Path):
    import argparse

    run_dir = tmp_path / "leg__ts"
    for idx in range(5):
        _write_output(run_dir, "taskA", "composed", idx)
        _write_output(run_dir, "taskA", "none", idx)
    ns = argparse.Namespace(run_dirs=[str(run_dir)], pairs="composed:none", sample=3, limit=2)
    assert len(_select_units(ns)) == 2


# --- reconcile_records: grouping + flip accounting --------------------------


def _order_rec(task: str, pair: str, idx: int, order: str, winner: str, model="m") -> dict:
    return {
        "key": f"d|{task}|{pair}|{idx}|{order}",
        "run_dir": "d",
        "task_id": task,
        "pair": pair,
        "run_index": idx,
        "order": order,
        "model": model,
        "winner": winner,
    }


def test_reconcile_records_requires_both_orders(tmp_path: Path):
    recs = [_order_rec("taskA", "composed:none", 0, "AB", "A")]  # BA missing
    assert reconcile_records(recs) == []


def test_reconcile_records_flip_and_win_accounting():
    recs = [
        # unit 0: left wins (A,B)
        _order_rec("t", "composed:none", 0, "AB", "A"),
        _order_rec("t", "composed:none", 0, "BA", "B"),
        # unit 1: position flip (A,A)
        _order_rec("t", "composed:none", 1, "AB", "A"),
        _order_rec("t", "composed:none", 1, "BA", "A"),
        # unit 2: right wins (B,A)
        _order_rec("t", "composed:none", 2, "AB", "B"),
        _order_rec("t", "composed:none", 2, "BA", "A"),
        # unit 3: tie
        _order_rec("t", "composed:none", 3, "AB", "tie"),
        _order_rec("t", "composed:none", 3, "BA", "B"),
    ]
    outcomes = reconcile_records(recs)
    t = _tally(outcomes)
    assert t == {"left": 1, "right": 1, "tie": 1, "position_flip": 1}


def test_reconcile_records_parse_error_order_drops_unit():
    recs = [
        _order_rec("t", "composed:none", 0, "AB", "A"),
        {"key": "d|t|composed:none|0|BA", "task_id": "t", "parse_error": "parse_error"},
    ]
    assert reconcile_records(recs) == []


# --- sign test + heuristic agreement ----------------------------------------


def test_sign_test_p_values():
    assert _sign_test_p(0, 0) is None
    # all wins, n=5: two-sided p = 2 * (1/32) = 0.0625
    assert _sign_test_p(5, 0) == pytest.approx(2 / 32)
    # even split is p=1.0
    assert _sign_test_p(3, 3) == pytest.approx(1.0)


def test_heuristic_delta_signs_from_summary(tmp_path: Path):
    run_dir = tmp_path / "leg__ts"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "by_task": {
                    "taskA": {
                        "composed": {"mean_score": 0.8},
                        "none": {"mean_score": 0.5},
                    },
                    "taskB": {
                        "composed": {"mean_score": 0.4},
                        "none": {"mean_score": 0.9},
                    },
                }
            }
        )
    )
    signs = _heuristic_delta_signs([run_dir], ("composed", "none"))
    assert signs[(str(run_dir), "taskA")] == 1
    assert signs[(str(run_dir), "taskB")] == -1


def test_render_report_sign_agreement(tmp_path: Path, capsys):
    run_dir = tmp_path / "leg__ts"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "by_task": {
                    # heuristic: composed > none  (sign +1)
                    "taskA": {"composed": {"mean_score": 0.9}, "none": {"mean_score": 0.1}},
                }
            }
        )
    )
    # judge agrees: left (composed) wins
    outcomes = [PairOutcome(str(run_dir), "taskA", "composed:none", 0, "m", "left")]
    render_pairwise_report([run_dir], ("composed", "none"), outcomes)
    out = capsys.readouterr().out
    assert "heuristic sign-agreement: 1/1 = 100.00%" in out
    assert "position-flip rate" in out


# --- helpers ----------------------------------------------------------------


def _write_output(
    run_dir: Path, task: str, cond: str, idx: int, *, agent_model: str | None = None
) -> Path:
    d = run_dir / task / cond
    d.mkdir(parents=True, exist_ok=True)
    txt = d / f"run-{idx}.txt"
    txt.write_text(f"output {task}/{cond}/{idx}")
    meta: dict = {"task_id": task, "condition": cond, "run_index": idx, "score": 0.5}
    if agent_model is not None:
        meta["agent_model"] = agent_model
    (d / f"run-{idx}.meta.json").write_text(json.dumps(meta))
    return txt


def _build_pair(tmp_path: Path, task: str, pair: tuple[str, str], idxs: list[int]) -> Path:
    run_dir = tmp_path / "leg__ts"
    for idx in idxs:
        _write_output(run_dir, task, pair[0], idx)
        _write_output(run_dir, task, pair[1], idx)
    return run_dir


def _make_unit(tmp_path, task, pair, idx, left_txt, right_txt):
    from eval.judge_pairwise import Unit

    return Unit(tmp_path, task, pair, idx, left_txt, right_txt, "m")
