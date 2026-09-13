"""M3 Scale & Ops tests: SSE streaming, compound engineering, telemetry analytics."""

import tempfile
from pathlib import Path

from agentalloy.compound import CompoundEngine
from agentalloy.skill_engine import SkillEngine
from agentalloy.state_store import StateStore
from agentalloy.telemetry_store import TelemetryStore


def _make_store(tmpdir: str) -> StateStore:
    return StateStore(str(Path(tmpdir) / "state.duck"))


def _make_telemetry(tmpdir: str) -> TelemetryStore:
    return TelemetryStore(str(Path(tmpdir) / "telemetry.duck"))


# ─── Compound Engineering ────────────────────────────────────────────


def test_capture_lesson() -> None:
    """Capture a QA lesson."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        engine = SkillEngine()
        compound = CompoundEngine(store, engine)

        lesson = compound.capture_lesson(
            slug="null-pointer-auth",
            title="Null pointer in auth middleware",
            body="Always check for None before accessing user.session",
            domain_tags=["python", "security"],
        )
        assert lesson.slug == "null-pointer-auth"
        assert lesson.reference_count == 0
        assert not lesson.promoted
        store.close()


def test_reference_and_promote() -> None:
    """Lessons auto-promote after enough references."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        engine = SkillEngine()
        compound = CompoundEngine(store, engine)

        compound.capture_lesson(
            slug="off-by-one",
            title="Off-by-one in pagination",
            body="Use range(0, limit) not range(1, limit+1)",
        )

        # Reference 3 times (threshold)
        for _ in range(3):
            lesson = compound.reference_lesson("off-by-one")
            assert lesson is not None

        # Should be promoted now
        assert lesson.promoted is True
        assert lesson.reference_count == 3

        # Skill should exist in engine
        assert "lesson-off-by-one" in engine.skills
        store.close()


def test_promotion_dedup_gate() -> None:
    """Promotion is dedup-gated — won't add duplicate skill."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        engine = SkillEngine()
        compound = CompoundEngine(store, engine)

        compound.capture_lesson("dup-lesson", "Dup", "Body")

        # Manually add the skill first
        from agentalloy.skill_engine import Skill

        engine.add_skill(Skill(id="lesson-dup-lesson", name="Existing", body="Already exists"))

        # Promote should return None (dedup)
        result = compound.promote_lesson("dup-lesson")
        assert result is None
        store.close()


def test_list_lessons() -> None:
    """List all captured lessons."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _make_store(tmpdir)
        engine = SkillEngine()
        compound = CompoundEngine(store, engine)

        compound.capture_lesson("l1", "Lesson 1", "Body 1")
        compound.capture_lesson("l2", "Lesson 2", "Body 2")

        lessons = compound.list_lessons()
        assert len(lessons) == 2
        slugs = {les.slug for les in lessons}
        assert slugs == {"l1", "l2"}
        store.close()


# ─── Telemetry Analytics ─────────────────────────────────────────────


def test_tool_usage_summary() -> None:
    """Tool usage analytics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ts = _make_telemetry(tmpdir)

        ts.record_trace("s1", 0, "code_search", '{"q": "auth"}', '{"count": 5}', phase="build")
        ts.record_trace("s1", 1, "code_search", '{"q": "api"}', '{"count": 3}', phase="build")
        ts.record_trace("s2", 0, "contract_add", '{"slug": "x"}', '{"status": "ok"}', phase="plan")

        summary = ts.tool_usage_summary()
        assert summary["total_calls"] == 3
        tools = {t["name"]: t["count"] for t in summary["tools"]}
        assert tools["code_search"] == 2
        assert tools["contract_add"] == 1
        ts.close()


def test_phase_activity() -> None:
    """Phase activity analytics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ts = _make_telemetry(tmpdir)

        ts.record_trace("s1", 0, "code_search", "{}", "{}", phase="build")
        ts.record_trace("s1", 1, "code_search", "{}", "{}", phase="build")
        ts.record_trace("s2", 0, "contract_add", "{}", "{}", phase="plan")

        activity = ts.phase_activity()
        phases = {p["phase"]: p["traces"] for p in activity["phases"]}
        assert phases["build"] == 2
        assert phases["plan"] == 1
        ts.close()


def test_stop_reason_distribution() -> None:
    """Stop reason analytics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ts = _make_telemetry(tmpdir)

        ts.record_trace("s1", 0, "code_search", "{}", "{}", stop_reason="answer")
        ts.record_trace("s2", 0, "bad_tool", "{}", "{}", stop_reason="tool_failed")
        ts.record_trace("s3", 0, "x", "{}", "{}", stop_reason="answer")

        dist = ts.stop_reason_distribution()
        reasons = {r["reason"]: r["count"] for r in dist["reasons"]}
        assert reasons["answer"] == 2
        assert reasons["tool_failed"] == 1
        ts.close()


def test_error_rate() -> None:
    """Error rate calculation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ts = _make_telemetry(tmpdir)

        ts.record_trace("s1", 0, "x", "{}", "{}", stop_reason="answer")
        ts.record_trace("s2", 0, "x", "{}", "{}", stop_reason="tool_failed")
        ts.record_trace("s3", 0, "x", "{}", "{}", stop_reason="answer")
        ts.record_trace("s4", 0, "x", "{}", "{}", stop_reason="validation_failed")

        rate = ts.error_rate()
        assert rate["total_sessions"] == 4
        assert rate["error_sessions"] == 2
        assert rate["error_rate"] == 0.5
        ts.close()


def test_session_summary() -> None:
    """Per-session analytics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ts = _make_telemetry(tmpdir)

        ts.record_trace("session-abc", 0, "code_search", "{}", "{}", phase="build")
        ts.record_trace("session-abc", 1, "contract_add", "{}", "{}", phase="build")

        summary = ts.session_summary("session-abc")
        assert summary["session_id"] == "session-abc"
        assert summary["total_traces"] == 2
        assert len(summary["tools_used"]) == 2
        assert "build" in summary["phases_visited"]
        ts.close()


# ─── SSE Streaming ───────────────────────────────────────────────────


def test_sse_stream_endpoint_exists() -> None:
    """SSE streaming endpoint is registered."""
    from agentalloy.server import app

    routes = [r.path for r in app.routes]
    assert "/chat/stream" in routes


def test_interpreter_streaming_method_exists() -> None:
    """Interpreter has run_streaming method."""
    from agentalloy.interpreter import Interpreter

    assert hasattr(Interpreter, "run_streaming")


# ─── Multi-repo config ───────────────────────────────────────────────


def test_extra_repos_config() -> None:
    """Config supports extra_repos."""
    from agentalloy.config import Config

    config = Config(extra_repos="/path/to/repo1,/path/to/repo2")
    repos = [r.strip() for r in config.extra_repos.split(",") if r.strip()]
    assert len(repos) == 2
    assert repos[0] == "/path/to/repo1"


def test_extra_repos_from_env() -> None:
    """extra_repos loaded from environment."""
    import os

    os.environ["AGENTALLOY_EXTRA_REPOS"] = "/repo/a,/repo/b"
    try:
        from agentalloy.config import Config

        config = Config.from_env()
        assert config.extra_repos == "/repo/a,/repo/b"
    finally:
        del os.environ["AGENTALLOY_EXTRA_REPOS"]


# ─── Dockerfile ──────────────────────────────────────────────────────


def test_dockerfile_exists() -> None:
    """Dockerfile exists and has required directives."""
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    assert dockerfile.exists()
    content = dockerfile.read_text()
    assert "FROM python:3.11" in content
    assert "EXPOSE 48950" in content
    assert "HEALTHCHECK" in content


def test_docker_compose_exists() -> None:
    """docker-compose.yml exists."""
    compose = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose.exists()
    content = compose.read_text()
    assert "agentalloy" in content
    assert "48950:48950" in content
