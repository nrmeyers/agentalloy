"""Task 05: boundary guards for the compound-engineering bridge.

AC 6 (read-path reused, not rebuilt): docs/solutions/*.md is retrievable via the
existing code-index markdown ingest with no new code.
AC 7 (opt-out parity): the codify gate cannot fire outside lifecycle-mode full —
it is confined to a workflow skill's exit_gates, and off-mode composes nothing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import agentalloy


def _load_markdown_module():
    """Load code_index/ingest/markdown.py WITHOUT triggering the package __init__,
    which eagerly imports the tree-sitter engine (the optional ``[code-index]``
    extra). markdown.py is pure stdlib, so a standalone load works whether or not
    the extra is installed — the AC 6 property holds either way."""
    try:
        from agentalloy.code_index.ingest import markdown as md  # type: ignore

        return md
    except ModuleNotFoundError:
        path = Path(agentalloy.__file__).resolve().parent / "code_index" / "ingest" / "markdown.py"
        spec = importlib.util.spec_from_file_location("aa_markdown_standalone", path)
        assert spec and spec.loader
        md = importlib.util.module_from_spec(spec)
        # Register before exec so @dataclass can resolve cls.__module__ in sys.modules.
        sys.modules[spec.name] = md
        spec.loader.exec_module(md)
        return md


# --- AC 6: docs/solutions is indexed by the existing code index -------------


def test_ac6_solutions_markdown_is_discovered_by_code_index(tmp_path: Path):
    md = _load_markdown_module()

    # docs/ (and docs/solutions/) must NOT be excluded — that's the whole
    # "retrievable the moment it's written, zero new code" property.
    assert "docs" not in md.EXCLUDED_DIRS
    assert "solutions" not in md.EXCLUDED_DIRS

    (tmp_path / "docs" / "solutions").mkdir(parents=True)
    lesson = tmp_path / "docs" / "solutions" / "rate-limit-retry.md"
    lesson.write_text("# Lesson\n\nWhat worked.\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")

    found = md.discover_markdown_files(tmp_path)
    assert lesson.resolve() in {p.resolve() for p in found}


# --- AC 6: the feature reuses the read-path rather than forking it -----------


def test_ac6_markdown_ingest_surface_intact():
    """The bridge depends on the code index's markdown ingest; assert the entry
    points it relies on still exist (we consume them, we don't fork them)."""
    md = _load_markdown_module()
    assert hasattr(md, "discover_markdown_files")
    assert hasattr(md, "collect_markdown_chunks")


# --- AC 7: opt-out parity — off mode composes nothing -----------------------


def test_ac7_off_mode_composes_nothing_even_at_qa(tmp_path: Path):
    from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
    from agentalloy.api.proxy_signal import evaluate_signal

    d = tmp_path / ".agentalloy"
    (d / "contracts" / "qa").mkdir(parents=True)
    (d / "phase").write_text("phase: qa\n")
    (d / "config").write_text("lifecycle_mode: off\n")
    (d / "contracts" / "qa" / "feat-x.md").write_text("---\nphase: qa\n---\n")
    # No docs/solutions/feat-x.md — in full mode the codify gate would block, but
    # in off mode the whole workflow layer (gate + prose) must stay dark.

    req = ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content="ready to ship")],
        tools=[{"name": "Read", "description": "", "input_schema": {}}],
    )
    result = asyncio.run(evaluate_signal(req, tmp_path))
    assert result.should_compose is False


def test_ac7_codify_gate_confined_to_the_qa_workflow_skill():
    """The lessons_recorded leaf must live ONLY in the qa workflow skill's
    exit_gates. Workflow exit_gates are evaluated only on a full-lifecycle forward
    transition, so a leaf confined there can never fire under off / flow-free.
    Assert it appears in no other packaged skill (esp. no always-apply system skill)."""
    import yaml

    packs = Path(agentalloy.__file__).resolve().parent / "_packs"
    hosts: list[str] = []
    for f in packs.rglob("*.yaml"):
        if f.name == "pack.yaml":
            continue
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if "lessons_recorded" in f.read_text(encoding="utf-8"):
            hosts.append(f.name)
            # where it appears, it must be a workflow skill's exit_gates
            assert data.get("skill_class") == "workflow"
            assert "lessons_recorded" in str(data.get("exit_gates"))
    assert hosts == ["sdd-verify-and-review.yaml"], hosts
