"""Unit tests for the review-gated ingest CLI."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.ingest import (
    EXIT_DUPLICATE,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    _lint,  # type: ignore[reportPrivateUsage]
    _load_yaml,  # type: ignore[reportPrivateUsage]
    main,
)
from agentalloy.storage.ladybug import LadybugStore

_DOMAIN_YAML = textwrap.dedent("""\
    skill_type: domain
    skill_id: test-domain-skill
    canonical_name: Test Domain Skill
    category: engineering
    skill_class: domain
    domain_tags: [testing, pytest]
    always_apply: false
    phase_scope: null
    category_scope: null
    author: test
    change_summary: unit test
    raw_prose: |
      Run pytest with the -x flag to stop on first failure. This is the
      fastest way to get useful feedback during a debug loop because the
      stack trace from the very first failing assertion is rarely buried
      under cascading downstream failures.

      All tests pass with exit code 0; non-zero indicates at least one
      failure or collection error. Wire this exit code into your CI step
      so a regression blocks the merge rather than emitting a green check.
    fragments:
      - sequence: 1
        fragment_type: execution
        content: |
          Run pytest with the -x flag to stop on first failure. This is the
          fastest way to get useful feedback during a debug loop because the
          stack trace from the very first failing assertion is rarely buried
          under cascading downstream failures.
      - sequence: 2
        fragment_type: verification
        content: |
          All tests pass with exit code 0; non-zero indicates at least one
          failure or collection error. Wire this exit code into your CI step
          so a regression blocks the merge rather than emitting a green check.
""")

_SYSTEM_YAML = textwrap.dedent("""\
    skill_type: system
    skill_id: sys-test-governance
    canonical_name: Test Governance Rule
    category: governance
    skill_class: system
    domain_tags: []
    always_apply: true
    phase_scope: null
    category_scope: null
    author: test
    change_summary: unit test
    raw_prose: |
      Always cite your sources.
""")


def _make_settings(db_path: str) -> object:
    class FakeSettings:
        ladybug_db_path = db_path

    return FakeSettings()


@pytest.fixture
def seeded_db(tmp_path: Path) -> tuple[str, LadybugStore]:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()
    return db_path, store


def test_insert_domain_skill(tmp_path: Path, seeded_db: tuple[str, LadybugStore]) -> None:
    db_path, store = seeded_db
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        code = main([str(yaml_file), "--yes"])

    assert code == EXIT_OK

    store.open()
    name = store.scalar("MATCH (s:Skill {skill_id: 'test-domain-skill'}) RETURN s.canonical_name")
    assert name == "Test Domain Skill"
    fragment_count = store.scalar(
        """
        MATCH (:Skill {skill_id: 'test-domain-skill'})-[:HAS_VERSION]->(v)-[:DECOMPOSES_TO]->(f)
        RETURN count(f)
        """
    )
    assert fragment_count == 2
    current = store.scalar(
        "MATCH (:Skill {skill_id: 'test-domain-skill'})-[:CURRENT_VERSION]->(v) RETURN v.version_id"
    )
    assert current == "test-domain-skill-v1"
    store.close()


def test_insert_system_skill(tmp_path: Path, seeded_db: tuple[str, LadybugStore]) -> None:
    db_path, store = seeded_db
    yaml_file = tmp_path / "system.yaml"
    yaml_file.write_text(_SYSTEM_YAML)

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        code = main([str(yaml_file), "--yes"])

    assert code == EXIT_OK

    store.open()
    fragment_count = store.scalar(
        """
        MATCH (:Skill {skill_id: 'sys-test-governance'})-[:HAS_VERSION]->(v)-[:DECOMPOSES_TO]->(f)
        RETURN count(f)
        """
    )
    assert fragment_count == 1
    store.close()


def test_duplicate_skill_id_without_force_fails(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    db_path, _ = seeded_db
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        main([str(yaml_file), "--yes"])
        code = main([str(yaml_file), "--yes"])

    # Re-running ingest on an already-loaded skill returns EXIT_DUPLICATE
    # so re-running setup / install-pack on a populated DB is a clean no-op
    # rather than a "validation failure". Real validation errors still use
    # EXIT_VALIDATION.
    assert code == EXIT_DUPLICATE


def test_canonical_name_collision_without_force_fails(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    db_path, _ = seeded_db
    first = tmp_path / "first.yaml"
    first.write_text(_DOMAIN_YAML)

    second = tmp_path / "second.yaml"
    second.write_text(_DOMAIN_YAML.replace("test-domain-skill", "test-domain-skill-b"))

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        assert main([str(first), "--yes"]) == EXIT_OK
        code = main([str(second), "--yes"])

    # Same exit-code semantics for canonical_name collisions.
    assert code == EXIT_DUPLICATE


def test_force_overwrites(tmp_path: Path, seeded_db: tuple[str, LadybugStore]) -> None:
    db_path, store = seeded_db
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        main([str(yaml_file), "--yes"])
        code = main([str(yaml_file), "--force", "--yes"])

    assert code == EXIT_OK

    store.open()
    count = store.scalar("MATCH (s:Skill {skill_id: 'test-domain-skill'}) RETURN count(s)")
    assert count == 1
    store.close()


def test_file_not_found_returns_usage_error() -> None:
    code = main(["/nonexistent/review.yaml", "--yes"])
    assert code == EXIT_USAGE


def test_missing_execution_fragment_is_validation_error(tmp_path: Path) -> None:
    bad = tmp_path / "no_exec.yaml"
    bad.write_text(
        textwrap.dedent("""\
        skill_type: domain
        skill_id: test-no-exec
        canonical_name: No Exec
        category: engineering
        skill_class: domain
        domain_tags: []
        always_apply: false
        raw_prose: Content.
        fragments:
          - sequence: 1
            fragment_type: guardrail
            content: Do not do bad things.
    """)
    )
    code = main([str(bad), "--yes"])
    assert code == EXIT_VALIDATION


def test_system_skill_with_fragments_is_validation_error(tmp_path: Path) -> None:
    bad = tmp_path / "sys_with_frags.yaml"
    bad.write_text(
        textwrap.dedent("""\
        skill_type: system
        skill_id: sys-bad
        canonical_name: Bad System
        category: governance
        skill_class: system
        domain_tags: []
        always_apply: true
        raw_prose: Content.
        fragments:
          - sequence: 1
            fragment_type: guardrail
            content: Extra fragment.
    """)
    )
    code = main([str(bad), "--yes"])
    assert code == EXIT_VALIDATION


def test_system_skill_no_applicability_is_validation_error(tmp_path: Path) -> None:
    bad = tmp_path / "sys_no_apply.yaml"
    bad.write_text(
        textwrap.dedent("""\
        skill_type: system
        skill_id: sys-no-apply
        canonical_name: No Applicability
        category: governance
        skill_class: system
        domain_tags: []
        always_apply: false
        phase_scope: null
        category_scope: null
        raw_prose: Content.
    """)
    )
    code = main([str(bad), "--yes"])
    assert code == EXIT_VALIDATION


def test_invalid_fragment_type_is_validation_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad_frag.yaml"
    bad.write_text(
        textwrap.dedent("""\
        skill_type: domain
        skill_id: test-bad-frag
        canonical_name: Bad Fragment
        category: engineering
        skill_class: domain
        domain_tags: []
        always_apply: false
        raw_prose: Content.
        fragments:
          - sequence: 1
            fragment_type: execution
            content: Core steps.
          - sequence: 2
            fragment_type: unknown_type
            content: What is this?
    """)
    )
    code = main([str(bad), "--yes"])
    assert code == EXIT_VALIDATION


def test_non_contiguous_sequences_is_validation_error(tmp_path: Path) -> None:
    bad = tmp_path / "gap.yaml"
    bad.write_text(
        textwrap.dedent("""\
        skill_type: domain
        skill_id: test-gap
        canonical_name: Gap Sequences
        category: engineering
        skill_class: domain
        domain_tags: []
        always_apply: false
        raw_prose: Content.
        fragments:
          - sequence: 1
            fragment_type: execution
            content: Step one.
          - sequence: 3
            fragment_type: verification
            content: Step three (gap!).
    """)
    )
    code = main([str(bad), "--yes"])
    assert code == EXIT_VALIDATION


# ---------------------------------------------------------------------------
# Batch mode tests
# ---------------------------------------------------------------------------


def _write_domain(path: Path, skill_id: str, canonical_name: str) -> None:
    body = (
        f"Run the canonical workflow for {skill_id} end-to-end. Start by "
        f"checking out a fresh branch, install dependencies with the project's "
        f"package manager, and run the smoke test suite to confirm the local "
        f"environment matches CI before making any changes."
    )
    path.write_text(
        textwrap.dedent(f"""\
        skill_type: domain
        skill_id: {skill_id}
        canonical_name: {canonical_name}
        category: engineering
        skill_class: domain
        domain_tags: [testing]
        always_apply: false
        raw_prose: |
          {body}
        fragments:
          - sequence: 1
            fragment_type: execution
            content: |
              {body}
    """)
    )


def test_batch_loads_all_valid_files(tmp_path: Path, seeded_db: tuple[str, LadybugStore]) -> None:
    db_path, store = seeded_db
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()

    _write_domain(batch_dir / "skill_a.yaml", "batch-skill-a", "Batch Skill A")
    _write_domain(batch_dir / "skill_b.yaml", "batch-skill-b", "Batch Skill B")

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        code = main([str(batch_dir), "--yes"])

    assert code == EXIT_OK

    store.open()
    count = store.scalar("MATCH (s:Skill) RETURN count(s)")
    assert count == 2
    store.close()


def test_batch_empty_directory_returns_usage_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code = main([str(empty), "--yes"])
    assert code == EXIT_USAGE


def test_batch_skips_invalid_and_loads_valid(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    db_path, store = seeded_db
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()

    _write_domain(batch_dir / "good.yaml", "batch-good", "Batch Good")

    bad = batch_dir / "bad.yaml"
    bad.write_text(
        textwrap.dedent("""\
        skill_type: domain
        skill_id: batch-bad
        canonical_name: Batch Bad
        category: engineering
        skill_class: domain
        always_apply: false
        raw_prose: Content.
        fragments:
          - sequence: 1
            fragment_type: guardrail
            content: Missing execution fragment.
    """)
    )

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        code = main([str(batch_dir), "--yes"])

    assert code == EXIT_OK

    store.open()
    count = store.scalar("MATCH (s:Skill) RETURN count(s)")
    assert count == 1
    store.close()


def test_batch_blocks_duplicates_without_force(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    db_path, _ = seeded_db
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()

    _write_domain(batch_dir / "skill_a.yaml", "batch-dup-a", "Batch Dup A")

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        main([str(batch_dir), "--yes"])
        code = main([str(batch_dir), "--yes"])

    assert code == EXIT_VALIDATION


def test_batch_force_overwrites_duplicates(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    db_path, store = seeded_db
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()

    _write_domain(batch_dir / "skill_a.yaml", "batch-force-a", "Batch Force A")

    with patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)):
        main([str(batch_dir), "--yes"])
        code = main([str(batch_dir), "--force", "--yes"])

    assert code == EXIT_OK

    store.open()
    count = store.scalar("MATCH (s:Skill {skill_id: 'batch-force-a'}) RETURN count(s)")
    assert count == 1
    store.close()


# ---------------------------------------------------------------------------
# B.4 — Mechanical tag lint warning shape tests
# ---------------------------------------------------------------------------

_WORKFLOW_YAML = textwrap.dedent("""\
    skill_type: domain
    skill_id: sdd-spec-authoring
    canonical_name: SDD Spec Authoring
    category: sdd
    skill_class: workflow
    domain_tags: [design, planning]
    always_apply: false
    phase_scope: null
    category_scope: null
    author: test
    change_summary: test
    raw_prose: |
      The spec phase captures user intent into a structured specification document.
      Authors write the spec before any design or planning begins. The spec is
      the single source of truth for what the feature should do, not how.
      Verification: spec document exists, stakeholder has reviewed it, no open
      questions remain unresolved.
    fragments:
      - sequence: 1
        fragment_type: rationale
        content: |
          The spec phase captures user intent into a structured specification
          document. Authors write the spec before any design or planning begins.
      - sequence: 2
        fragment_type: verification
        content: |
          Spec document exists, stakeholder has reviewed it, no open questions
          remain unresolved.
""")


def _parse_yaml_inline(content: str, tmp_path: Path) -> object:
    """Write content to a tmp file and parse it via _load_yaml."""
    f = tmp_path / "skill.yaml"
    f.write_text(content)
    return _load_yaml(f)


def test_system_skill_with_tags_warns_system_empty(tmp_path: Path) -> None:
    """system skills with non-empty domain_tags should get system-empty verdicts."""
    yaml_content = textwrap.dedent("""\
        skill_type: system
        skill_id: sys-tagged
        canonical_name: Tagged System Skill
        category: governance
        skill_class: system
        domain_tags: [some-tag, another-tag]
        always_apply: true
        phase_scope: null
        category_scope: null
        author: test
        change_summary: test
        raw_prose: |
          Always do the right thing.
    """)
    f = tmp_path / "sys_tagged.yaml"
    f.write_text(yaml_content)
    record = _load_yaml(f)
    warns = _lint(record, yaml_path=f)
    assert any("system_has_tags" in w for w in warns), (
        f"Expected system_has_tags warning, got: {warns}"
    )


def test_domain_skill_title_overlapping_tags_do_not_warn(tmp_path: Path) -> None:
    """Title-overlapping tags are the skill's natural filter keys — no warning.

    Inverted from the retired R2 rule: domain_tags is an exact-match filter,
    so 'prisma' on "Prisma Schema Design" is required for
    domain_tags=["prisma"] queries, not redundant.
    """
    yaml_content = textwrap.dedent("""\
        skill_type: domain
        skill_id: prisma-schema-design
        canonical_name: Prisma Schema Design
        category: engineering
        skill_class: domain
        domain_tags: [prisma, schema]
        always_apply: false
        phase_scope: null
        category_scope: null
        author: test
        change_summary: test
        raw_prose: |
          Use Prisma schema to define your database models. The schema file
          declares all models and their relations. Run prisma generate to
          sync the client with your schema. Verify the schema compiles
          without errors by running prisma validate.
        fragments:
          - sequence: 1
            fragment_type: execution
            content: |
              Use Prisma schema to define your database models. The schema file
              declares all models and their relations. Run prisma generate to
              sync the client with your schema.
          - sequence: 2
            fragment_type: verification
            content: |
              Verify the schema compiles without errors by running prisma validate.
    """)
    f = tmp_path / "prisma.yaml"
    f.write_text(yaml_content)
    record = _load_yaml(f)
    warns = _lint(record, yaml_path=f)
    assert not any("redundant_with_title" in w for w in warns), f"R2 is retired, got: {warns}"


def test_workflow_skill_without_position_marker_warns_w1(tmp_path: Path) -> None:
    """Workflow skills lacking a position marker tag should trigger W1."""
    f = tmp_path / "workflow.yaml"
    f.write_text(_WORKFLOW_YAML)
    record = _load_yaml(f)
    warns = _lint(record, yaml_path=f)
    assert any("missing_position_marker" in w for w in warns), (
        f"Expected W1/missing_position_marker warning, got: {warns}"
    )


def test_workflow_skill_with_position_marker_no_w1(tmp_path: Path) -> None:
    """Workflow skills that include a position marker tag must NOT trigger W1."""
    yaml_with_marker = _WORKFLOW_YAML.replace(
        "domain_tags: [design, planning]",
        "domain_tags: [phase:spec, design]",
    )
    f = tmp_path / "workflow_marked.yaml"
    f.write_text(yaml_with_marker)
    record = _load_yaml(f)
    warns = _lint(record, yaml_path=f)
    assert not any("missing_position_marker" in w for w in warns), (
        f"Unexpected W1 warning when position marker present: {warns}"
    )


# ---------------------------------------------------------------------------
# TASK-4: --no-restart flag for container stop/restart
# ---------------------------------------------------------------------------


def test_no_restart_skips_container_stop_restart_in_container(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    """When --no-restart is passed and we're in a container,
    stop_service_in_container and restart_service_in_container must NOT be called."""
    db_path, _ = seeded_db
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)

    with (
        patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)),
        patch("agentalloy.ingest.is_in_container", return_value=True),
        patch(
            "agentalloy.ingest.stop_service_in_container",
            autospec=True,
        ) as mock_stop,
        patch(
            "agentalloy.ingest.restart_service_in_container",
            autospec=True,
        ) as mock_restart,
    ):
        code = main([str(yaml_file), "--yes", "--no-restart"])

    assert code == EXIT_OK
    mock_stop.assert_not_called()
    mock_restart.assert_not_called()


def test_without_no_restart_calls_container_stop_restart_in_container(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    """Without --no-restart, when in a container, stop_service_in_container
    and restart_service_in_container MUST be called."""
    db_path, _ = seeded_db
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)

    with (
        patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)),
        patch("agentalloy.ingest.is_in_container", return_value=True),
        patch(
            "agentalloy.ingest.stop_service_in_container",
            return_value=True,
            autospec=True,
        ) as mock_stop,
        patch(
            "agentalloy.ingest.restart_service_in_container",
            return_value=True,
            autospec=True,
        ) as mock_restart,
    ):
        code = main([str(yaml_file), "--yes"])

    assert code == EXIT_OK
    mock_stop.assert_called_once_with(no_restart=False)
    mock_restart.assert_called_once_with(no_restart=False)


def test_no_restart_skips_in_container_batch_mode(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    """--no-restart in batch mode must also skip container stop/restart."""
    db_path, _ = seeded_db
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    _write_domain(batch_dir / "skill_a.yaml", "batch-skill-a", "Batch Skill A")

    with (
        patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)),
        patch("agentalloy.ingest.is_in_container", return_value=True),
        patch(
            "agentalloy.ingest.stop_service_in_container",
            autospec=True,
        ) as mock_stop,
        patch(
            "agentalloy.ingest.restart_service_in_container",
            autospec=True,
        ) as mock_restart,
    ):
        code = main([str(batch_dir), "--yes", "--no-restart"])

    assert code == EXIT_OK
    mock_stop.assert_not_called()
    mock_restart.assert_not_called()


def test_not_in_container_noops_container_functions(
    tmp_path: Path, seeded_db: tuple[str, LadybugStore]
) -> None:
    """When not in a container, container stop/restart functions must NOT be called."""
    db_path, _ = seeded_db
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)

    with (
        patch("agentalloy.ingest.get_settings", return_value=_make_settings(db_path)),
        patch("agentalloy.ingest.is_in_container", return_value=False),
        patch(
            "agentalloy.ingest.stop_service_in_container",
            autospec=True,
        ) as mock_stop,
        patch(
            "agentalloy.ingest.restart_service_in_container",
            autospec=True,
        ) as mock_restart,
    ):
        code = main([str(yaml_file), "--yes"])

    assert code == EXIT_OK
    mock_stop.assert_not_called()
    mock_restart.assert_not_called()
