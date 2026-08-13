"""Task 03: lesson -> domain-skill pack generator.

The key gate (AC 4): a pack generated from a docs/solutions/<slug>.md lesson
passes ``validate_pack_skills`` under ``strict=True`` with zero errors — i.e. it
is drop-in installable via the existing rail.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agentalloy.install.lesson_pack import generate_lesson_pack
from agentalloy.pack_validation import validate_pack_skills

LESSON = """# Rate-limit retries with jittered backoff

Tags: http-clients, retry-backoff

## Problem

Our webhook sender hammered a partner API and got 429s in bursts; naive fixed
retries synchronized every worker and made the storm worse instead of better.

## Approach that worked

Wrap the client in an exponential backoff with full jitter: on a 429 or 503,
sleep ``random(0, base * 2**attempt)`` capped at 30s, and honor a ``Retry-After``
header when present. Cap attempts at six and surface the final failure to the
caller instead of blocking forever. This spread the retries out and the 429s
cleared within one cycle.

## What didn't work

A fixed 1s retry and a naive doubling without jitter both kept the workers in
lockstep, so every retry wave hit the API at the same instant and re-triggered
the limit. Removing the cap also let a genuinely-down partner hang the queue.

## Verification

Point the client at a stub that returns 429 for the first three calls, then 200,
and assert the call succeeds within the attempt budget and that the sleep values
are non-decreasing and jittered. Re-run the load test and confirm no synchronized
retry spikes appear in the metrics.
"""


def _write_lesson(root: Path, slug: str, body: str = LESSON) -> Path:
    p = root / "docs" / "solutions" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_ac4_generated_pack_validates_strict(tmp_path: Path):
    lesson = _write_lesson(tmp_path, "rate-limit-retry")
    dest = tmp_path / ".agentalloy" / "custom-skills"
    res = generate_lesson_pack(lesson, dest)
    assert res["action"] == "generated", res
    pack_dir = Path(res["pack_dir"])
    assert pack_dir == dest / "rate-limit-retry-lesson"

    manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    result = validate_pack_skills(pack_dir, manifest["skills"], strict=True)
    assert result.ok is True, result.format_errors()
    assert result.errors == []


def test_fragment_taxonomy_and_raw_prose(tmp_path: Path):
    lesson = _write_lesson(tmp_path, "rate-limit-retry")
    res = generate_lesson_pack(lesson, tmp_path / ".agentalloy" / "custom-skills")
    doc = yaml.safe_load((Path(res["pack_dir"]) / f"{res['skill_id']}.yaml").read_text())
    frags = doc["fragments"]
    assert [f["fragment_type"] for f in frags] == ["execution", "verification", "rationale"]
    # raw_prose == concatenation of fragment contents (the drift lint)
    assert doc["raw_prose"] == "\n\n".join(f["content"] for f in frags)
    # the lesson's approach text landed in the execution fragment
    assert "full jitter" in frags[0]["content"]
    # explicit Tags line is honored
    assert "http-clients" in doc["domain_tags"]


def test_thin_lesson_still_validates_strict(tmp_path: Path):
    """A sparse lesson with no named sections still yields a strict-clean pack
    (fragments are padded to clear the word floor)."""
    thin = "# Quick note\n\nUse a connection pool for the reporting DB; a fresh connection per query was the bottleneck and the pool fixed the p99 latency immediately.\n"
    lesson = _write_lesson(tmp_path, "db-pool", thin)
    res = generate_lesson_pack(lesson, tmp_path / ".agentalloy" / "custom-skills")
    assert res["action"] == "generated"
    manifest = yaml.safe_load((Path(res["pack_dir"]) / "pack.yaml").read_text())
    result = validate_pack_skills(Path(res["pack_dir"]), manifest["skills"], strict=True)
    assert result.ok is True, result.format_errors()


def test_missing_lesson_is_reported(tmp_path: Path):
    res = generate_lesson_pack(tmp_path / "docs" / "solutions" / "nope.md", tmp_path / "dest")
    assert res["action"] == "lesson_not_found"
