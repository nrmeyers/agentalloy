"""Tests for build_retrieval_query — strip injected noise, then head-cap."""

from __future__ import annotations

from agentalloy.retrieval.query_bounds import build_retrieval_query


def test_strips_system_reminder_block():
    task = (
        "Refactor the auth middleware to use JWT.\n"
        "<system-reminder>\n" + ("blah " * 4000) + "\n</system-reminder>"
    )
    q = build_retrieval_query(task)
    assert "Refactor the auth middleware to use JWT." in q
    assert "blah" not in q


def test_strips_fenced_code_and_indented_blobs():
    task = (
        "Add retry logic to the client.\n"
        "```python\n" + ("x = 1\n" * 500) + "```\n"
        "    pasted log line\n    another pasted line\n"
    )
    q = build_retrieval_query(task)
    assert "Add retry logic to the client." in q
    assert "x = 1" not in q
    assert "pasted log line" not in q


def test_head_caps_to_budget_char_proxy():
    task = "word " * 5000  # ~25k chars, no noise to strip
    q = build_retrieval_query(task, token_budget=512)
    assert len(q) <= 512 * 4
    assert q.startswith("word")


def test_strip_then_cap_preserves_instruction_after_boilerplate():
    # Cap-then-strip would have kept only boilerplate; strip-then-cap keeps intent.
    task = (
        "<system-reminder>"
        + ("noise " * 3000)
        + "</system-reminder>\nFix the N+1 query in the report builder."
    )
    q = build_retrieval_query(task)
    assert "Fix the N+1 query in the report builder." in q


def test_respects_real_token_counter():
    task = "one two three four five six seven eight nine ten"
    # crude word counter: cap to 4 tokens
    q = build_retrieval_query(task, token_budget=4, count_tokens=lambda s: len(s.split()))
    assert q == "one two three four"


def test_empty_and_noise_only_return_empty():
    assert build_retrieval_query(None) == ""
    assert build_retrieval_query("") == ""
    assert build_retrieval_query("<system-reminder>only noise</system-reminder>") == ""
