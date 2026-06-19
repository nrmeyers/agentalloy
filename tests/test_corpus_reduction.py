"""Tests for Phase 1 / Duplicate Consolidation — A1: Python Packaging.

Merges python/python-packaging-and-pyproject (deprecated) into
python/python-packaging-pyproject (keeper).  Verifies:

  - Deprecated skill has deprecated:true + superseded_by.
  - Keeper skill has absorbed all unique content from deprecated.
  - Fragment sequences remain contiguous after merge.
  - Both skills ingest cleanly (valid YAML, required fields).
"""

from __future__ import annotations

from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PACKS_ROOT = Path(__file__).resolve().parent.parent / "src" / "agentalloy" / "_packs"


def _load_skill(skill_id: str, category: str = "python") -> dict:
    """Load a skill YAML from the packs directory."""
    path = _PACKS_ROOT / category / f"{skill_id}.yaml"
    assert path.exists(), f"Skill file not found: {path}"
    with open(path) as f:
        return yaml.safe_load(f)


def _fragment_sequences(skill: dict) -> list[int]:
    """Return the fragment sequence numbers in order."""
    frags = skill.get("fragments", [])
    return sorted([fr["sequence"] for fr in frags])


def _fragment_types(skill: dict) -> list[str]:
    """Return the fragment types in sequence order."""
    frags = skill.get("fragments", [])
    sorted_frags = sorted(frags, key=lambda f: f["sequence"])
    return [fr["fragment_type"] for fr in sorted_frags]


def _fragment_content_concat(skill: dict) -> str:
    """Concatenate all fragment content in sequence order."""
    frags = skill.get("fragments", [])
    sorted_frags = sorted(frags, key=lambda f: f["sequence"])
    return "\n\n".join(fr["content"].strip() for fr in sorted_frags)


def _raw_prose(skill: dict) -> str:
    """Return the raw_prose content."""
    return (skill.get("raw_prose") or "").strip()


# ---------------------------------------------------------------------------
# A7: FastAPI Background Tasks helpers
# ---------------------------------------------------------------------------

_A7_KEEPER_ID = "fastapi-async-and-background-tasks"
_A7_DEPRECATED_ID = "fastapi-background-tasks-deep"


def _load_a7_skill(skill_id: str) -> dict:
    """Load a FastAPI skill YAML from the packs directory."""
    path = _PACKS_ROOT / "fastapi" / f"{skill_id}.yaml"
    assert path.exists(), f"Skill file not found: {path}"
    with open(path) as f:
        return yaml.safe_load(f)


# These are the fragment contents unique to the deprecated A7 skill that
# must appear in the keeper's combined content after consolidation.
_A7_UNIQUE_DEPRECATED_SECTIONS = [
    # Multiple tasks example
    "bg.add_task(send_confirmation_email, saved)",
    # Async/sync distinction
    "Async tasks are awaited on the event loop",
    "Sync tasks run on the thread pool",
    # Dependency-injected background tasks
    "dependencies=[Depends(log_request)]",
    # Limits and when to outgrow
    "No durability",
    "No retry",
    "No isolation",
    "No scheduling",
    # When to use
    "send an email",
    # DB session pitfalls
    "request-scoped",
    "Pass IDs into background tasks, not ORM objects",
    # Common pitfalls
    "Returning task results to the client",
]


# ---------------------------------------------------------------------------
# A7: Deprecated flags
# ---------------------------------------------------------------------------


def test_a7_fastapi_background_tasks_deprecated_flags() -> None:
    """The deprecated skill must have deprecated:true and superseded_by set."""
    dep = _load_a7_skill(_A7_DEPRECATED_ID)

    # Must have deprecated flag
    assert dep.get("deprecated") is True, f"{_A7_DEPRECATED_ID} must have deprecated: true"

    # Must have superseded_by pointing to the keeper skill_id
    superseded = dep.get("superseded_by")
    assert superseded is not None, f"{_A7_DEPRECATED_ID} must have superseded_by set"
    assert superseded == _A7_KEEPER_ID, (
        f"superseded_by should point to '{_A7_KEEPER_ID}', got '{superseded}'"
    )


# ---------------------------------------------------------------------------
# A7: Keeper has absorbed deprecated content
# ---------------------------------------------------------------------------


def test_a7_fastapi_background_tasks_keeper_has_absorbed_content() -> None:
    """Keeper must contain all unique content from the deprecated skill."""
    keeper = _load_a7_skill(_A7_KEEPER_ID)
    _load_a7_skill(_A7_DEPRECATED_ID)

    # Combine raw_prose + all fragment content for the keeper
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    # Each unique deprecated section must appear somewhere in the keeper
    missing = []
    for section in _A7_UNIQUE_DEPRECATED_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper is missing absorbed content from deprecated skill: {missing}"


# ---------------------------------------------------------------------------
# A7: Fragment sequences are contiguous
# ---------------------------------------------------------------------------


def test_a7_fastapi_background_tasks_deprecated_sequences_contiguous() -> None:
    """Deprecated skill fragment sequences must be contiguous (no gaps)."""
    dep = _load_a7_skill(_A7_DEPRECATED_ID)
    seqs = _fragment_sequences(dep)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Deprecated skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


def test_a7_fastapi_background_tasks_keeper_sequences_contiguous() -> None:
    """Keeper skill fragment sequences must be contiguous (no gaps)."""
    keeper = _load_a7_skill(_A7_KEEPER_ID)
    seqs = _fragment_sequences(keeper)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Keeper skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# A7: Both skills ingest cleanly
# ---------------------------------------------------------------------------


def test_a7_fastapi_background_tasks_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass all ingest validation."""
    dep = _load_a7_skill(_A7_DEPRECATED_ID)
    errors = _validate_skill_ingest(dep, _A7_DEPRECATED_ID)
    assert not errors, f"Deprecated skill has ingest errors: {errors}"


def test_a7_fastapi_background_tasks_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass all ingest validation."""
    keeper = _load_a7_skill(_A7_KEEPER_ID)
    errors = _validate_skill_ingest(keeper, _A7_KEEPER_ID)
    assert not errors, f"Keeper skill has ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A7: Keeper still has its own unique content
# ---------------------------------------------------------------------------

_A7_KEEPER_UNIQUE_SECTIONS = [
    # Keeper-specific topics (with backticks as they appear in YAML)
    "`async def` vs `def` handlers",
    "blocking-the-loop trap",
    "`lifespan` is the modern startup/shutdown hook",
    "app.state",
    "Async DB sessions",
    "Concurrency model",
    "Async tests",
    "Anti-patterns",
]


def test_a7_fastapi_background_tasks_keeper_retains_unique_content() -> None:
    """Keeper must still contain its own unique content after merge."""
    keeper = _load_a7_skill(_A7_KEEPER_ID)
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A7_KEEPER_UNIQUE_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper lost its own unique content: {missing}"


# ---------------------------------------------------------------------------
# A7: Deprecated skill retains its content (just marked deprecated)
# ---------------------------------------------------------------------------


def test_a7_fastapi_background_tasks_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content."""
    dep = _load_a7_skill(_A7_DEPRECATED_ID)
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    assert "bg.add_task(send_confirmation_email, saved)" in dep_text, (
        "Deprecated skill should retain multiple tasks content"
    )
    assert "No durability" in dep_text, "Deprecated skill should retain limits content"
    assert "Pass IDs into background tasks, not ORM objects" in dep_text, (
        "Deprecated skill should retain DB session pitfalls"
    )


# ---------------------------------------------------------------------------
# A1: Deprecated flags
# ---------------------------------------------------------------------------


def test_a1_python_packaging_deprecated_flags() -> None:
    """The deprecated skill must have deprecated:true and superseded_by set."""
    dep = _load_skill("python-packaging-and-pyproject")

    # Must have deprecated flag
    assert dep.get("deprecated") is True, (
        "python-packaging-and-pyproject must have deprecated: true"
    )

    # Must have superseded_by pointing to the keeper skill_id
    superseded = dep.get("superseded_by")
    assert superseded is not None, "python-packaging-and-pyproject must have superseded_by set"
    assert superseded == "python-packaging-pyproject", (
        f"superseded_by should point to 'python-packaging-pyproject', got '{superseded}'"
    )


# ---------------------------------------------------------------------------
# A1: Keeper has absorbed deprecated content
# ---------------------------------------------------------------------------

# These are the fragment contents unique to the deprecated skill that
# must appear in the keeper's combined content after consolidation.
_UNIQUE_DEPRECATED_SECTIONS = [
    # Section: src layout
    "Use the src layout",
    # Section: editable install
    "Editable install for local development",
    # Section: dependencies and lockfiles
    "Dependencies and lockfiles",
    # Section: virtual environments
    "Virtual environments",
    # Section: versioning
    "Versioning",
    # Content: plugin ecosystem detail (extra prose in deprecated fragment 8)
    "importlib.metadata.entry_points",
]


def test_a1_python_packaging_keeper_has_absorbed_content() -> None:
    """Keeper must contain all unique content from the deprecated skill."""
    keeper = _load_skill("python-packaging-pyproject")
    _load_skill("python-packaging-and-pyproject")

    # Combine raw_prose + all fragment content for the keeper
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    # Each unique deprecated section must appear somewhere in the keeper
    missing = []
    for section in _UNIQUE_DEPRECATED_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper is missing absorbed content from deprecated skill: {missing}"


# ---------------------------------------------------------------------------
# A1: Fragment sequences are contiguous
# ---------------------------------------------------------------------------


def test_a1_python_packaging_deprecated_sequences_contiguous() -> None:
    """Deprecated skill fragment sequences must be contiguous (no gaps)."""
    dep = _load_skill("python-packaging-and-pyproject")
    seqs = _fragment_sequences(dep)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Deprecated skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


def test_a1_python_packaging_keeper_sequences_contiguous() -> None:
    """Keeper skill fragment sequences must be contiguous (no gaps)."""
    keeper = _load_skill("python-packaging-pyproject")
    seqs = _fragment_sequences(keeper)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Keeper skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# A1: Both skills ingest cleanly
# ---------------------------------------------------------------------------

_VALID_FRAGMENT_TYPES = {"execution", "rationale", "example", "guardrail", "verification", "setup"}
_REQUIRED_DOMAIN_FIELDS = {
    "skill_id",
    "canonical_name",
    "category",
    "skill_class",
    "domain_tags",
    "always_apply",
    "author",
    "change_summary",
    "raw_prose",
    "fragments",
}


def _validate_skill_ingest(skill: dict, skill_id: str) -> list[str]:
    """Return a list of validation errors (empty = clean)."""
    errors = []

    # Required fields
    for field in _REQUIRED_DOMAIN_FIELDS:
        if field not in skill:
            errors.append(f"Missing required field: {field}")

    # skill_class must be domain
    if skill.get("skill_class") != "domain":
        errors.append(f"skill_class must be 'domain', got '{skill.get('skill_class')}'")

    # Must have at least one execution fragment
    frag_types = _fragment_types(skill)
    if "execution" not in frag_types:
        errors.append("Domain skill must have at least one execution fragment")

    # Fragment types must be valid
    for ft in frag_types:
        if ft not in _VALID_FRAGMENT_TYPES:
            errors.append(f"Invalid fragment_type: '{ft}'")

    # Sequences must be contiguous
    seqs = _fragment_sequences(skill)
    expected = list(range(1, len(seqs) + 1))
    if seqs != expected:
        errors.append(f"Fragment sequences not contiguous: {seqs}")

    return errors


def test_a1_python_packaging_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass all ingest validation."""
    dep = _load_skill("python-packaging-and-pyproject")
    errors = _validate_skill_ingest(dep, "python-packaging-and-pyproject")
    assert not errors, f"Deprecated skill has ingest errors: {errors}"


def test_a1_python_packaging_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass all ingest validation."""
    keeper = _load_skill("python-packaging-pyproject")
    errors = _validate_skill_ingest(keeper, "python-packaging-pyproject")
    assert not errors, f"Keeper skill has ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A1: Keeper still has its own unique content
# ---------------------------------------------------------------------------

_KEEPER_UNIQUE_SECTIONS = [
    # [tool.*] Sections
    "[tool.*] Sections",
    # Dynamic Fields
    "Dynamic Fields",
    # PEP 735
    "PEP 735",
    # maturin
    "maturin",
    # flit-core
    "flit-core",
    # classifiers
    "classifiers",
    # keywords
    "keywords",
    # urls
    "urls",
    # PEP 639
    "PEP 639",
    # GUI scripts
    "gui-scripts",
]


def test_a1_python_packaging_keeper_retains_unique_content() -> None:
    """Keeper must still contain its own unique content after merge."""
    keeper = _load_skill("python-packaging-pyproject")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _KEEPER_UNIQUE_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper lost its own unique content: {missing}"


# ---------------------------------------------------------------------------
# A1: Deprecated skill retains its content (just marked deprecated)
# ---------------------------------------------------------------------------


def test_a1_python_packaging_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content."""
    dep = _load_skill("python-packaging-and-pyproject")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    # Must still have the src layout section
    assert "Use the src layout" in dep_text, "Deprecated skill should retain src layout content"

    # Must still have the versioning section
    assert "Versioning" in dep_text, "Deprecated skill should retain versioning content"

    # Must still have virtual environments
    assert "Virtual environments" in dep_text, (
        "Deprecated skill should retain virtual environments content"
    )


# ---------------------------------------------------------------------------
# A4: Fastify Error Handling — Deprecated flags
# ---------------------------------------------------------------------------


def test_a4_fastify_error_handling_deprecated_flags() -> None:
    """The deprecated skill must have deprecated:true and superseded_by set."""
    dep = _load_skill("fastify-error-handling", category="fastify")

    # Must have deprecated flag
    assert dep.get("deprecated") is True, "fastify-error-handling must have deprecated: true"

    # Must have superseded_by pointing to the keeper skill_id
    superseded = dep.get("superseded_by")
    assert superseded is not None, "fastify-error-handling must have superseded_by set"
    assert superseded == "fastify-error-handling-deep", (
        f"superseded_by should point to 'fastify-error-handling-deep', got '{superseded}'"
    )


# ---------------------------------------------------------------------------
# A4: Fastify Error Handling — Keeper has absorbed deprecated content
# ---------------------------------------------------------------------------

# These are the fragment contents unique to the deprecated skill that
# must appear in the keeper's combined content after consolidation.
_A4_UNIQUE_DEPRECATED_SECTIONS = [
    # The error code list from the deprecated skill
    "FST_ERR_NOT_FOUND",
    "FST_ERR_OPTIONS_NOT_OBJ",
    "FST_ERR_CTP_INVALID_JSON_BODY",
    "FST_ERR_REP_ALREADY_SENT",
    "FST_ERR_SEND_INSIDE_ONERR",
    "FST_ERR_HANDLER_TIMEOUT",
    "FST_ERR_DUPLICATED_ROUTE",
    "FST_ERR_PLUGIN_VERSION_MISMATCH",
]


def test_a4_fastify_error_handling_keeper_has_absorbed_content() -> None:
    """Keeper must contain all unique content from the deprecated skill."""
    keeper = _load_skill("fastify-error-handling-deep", category="fastify")
    _load_skill("fastify-error-handling", category="fastify")

    # Combine raw_prose + all fragment content for the keeper
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    # Each unique deprecated section must appear somewhere in the keeper
    missing = []
    for section in _A4_UNIQUE_DEPRECATED_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper is missing absorbed content from deprecated skill: {missing}"


# ---------------------------------------------------------------------------
# A4: Fragment sequences are contiguous
# ---------------------------------------------------------------------------


def test_a4_fastify_error_handling_deprecated_sequences_contiguous() -> None:
    """Deprecated skill fragment sequences must be contiguous (no gaps)."""
    dep = _load_skill("fastify-error-handling", category="fastify")
    seqs = _fragment_sequences(dep)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Deprecated skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


def test_a4_fastify_error_handling_keeper_sequences_contiguous() -> None:
    """Keeper skill fragment sequences must be contiguous (no gaps)."""
    keeper = _load_skill("fastify-error-handling-deep", category="fastify")
    seqs = _fragment_sequences(keeper)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Keeper skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# A4: Both skills ingest cleanly
# ---------------------------------------------------------------------------


def test_a4_fastify_error_handling_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass all ingest validation."""
    dep = _load_skill("fastify-error-handling", category="fastify")
    errors = _validate_skill_ingest(dep, "fastify-error-handling")
    assert not errors, f"Deprecated skill has ingest errors: {errors}"


def test_a4_fastify_error_handling_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass all ingest validation."""
    keeper = _load_skill("fastify-error-handling-deep", category="fastify")
    errors = _validate_skill_ingest(keeper, "fastify-error-handling-deep")
    assert not errors, f"Keeper skill has ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A4: Keeper still has its own unique content
# ---------------------------------------------------------------------------

_A4_KEEPER_UNIQUE_SECTIONS = [
    "setErrorHandler",
    "Validation Errors",
    "Error Codes vs Status",
    "404 and 405 Handlers",
    "onError Hook",
    "Encapsulation",
    "Error Logging Levels",
    "Async Errors",
    "Common Pitfalls",
    "Verification",
]


def test_a4_fastify_error_handling_keeper_retains_unique_content() -> None:
    """Keeper must still contain its own unique content after merge."""
    keeper = _load_skill("fastify-error-handling-deep", category="fastify")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A4_KEEPER_UNIQUE_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper lost its own unique content: {missing}"


# ---------------------------------------------------------------------------
# A4: Deprecated skill retains its content (just marked deprecated)
# ---------------------------------------------------------------------------


def test_a4_fastify_error_handling_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content."""
    dep = _load_skill("fastify-error-handling", category="fastify")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    # Must still have the error code list
    assert "FST_ERR_NOT_FOUND" in dep_text, "Deprecated skill should retain error code content"

    # Must still have the overview
    assert "Fastify routes errors through a layered pipeline" in dep_text, (
        "Deprecated skill should retain overview content"
    )


# ---------------------------------------------------------------------------
# A3: Node.js Test Runner — Deprecated flags
# ---------------------------------------------------------------------------


def test_a3_node_test_runner_deprecated_flags() -> None:
    """The deprecated skill must have deprecated:true and superseded_by set."""
    dep = _load_skill("nodejs-native-test-runner", category="nodejs")

    # Must have deprecated flag
    assert dep.get("deprecated") is True, "nodejs-native-test-runner must have deprecated: true"

    # Must have superseded_by pointing to the keeper skill_id
    superseded = dep.get("superseded_by")
    assert superseded is not None, "nodejs-native-test-runner must have superseded_by set"
    assert superseded == "node-built-in-test-runner", (
        f"superseded_by should point to 'node-built-in-test-runner', got '{superseded}'"
    )


# ---------------------------------------------------------------------------
# A3: Node.js Test Runner — Keeper has absorbed deprecated content
# ---------------------------------------------------------------------------

# These are the fragment contents unique to the deprecated skill that
# must appear in the keeper's combined content after consolidation.
_A3_UNIQUE_DEPRECATED_SECTIONS = [
    # --test-rerun-failures
    "--test-rerun-failures",
    # describe/it aliases
    "describe()",
    "it()",
    "describe('A thing'",
    # skip tests
    "skip option",
    "t.skip()",
    # todo tests
    "todo option",
    "t.todo()",
]


def test_a3_node_test_runner_keeper_has_absorbed_content() -> None:
    """Keeper must contain all unique content from the deprecated skill."""
    keeper = _load_skill("node-built-in-test-runner", category="nodejs")
    _load_skill("nodejs-native-test-runner", category="nodejs")

    # Combine raw_prose + all fragment content for the keeper
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    # Each unique deprecated section must appear somewhere in the keeper
    missing = []
    for section in _A3_UNIQUE_DEPRECATED_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper is missing absorbed content from deprecated skill: {missing}"


# ---------------------------------------------------------------------------
# A3: Fragment sequences are contiguous
# ---------------------------------------------------------------------------


def test_a3_node_test_runner_deprecated_sequences_contiguous() -> None:
    """Deprecated skill fragment sequences must be contiguous (no gaps)."""
    dep = _load_skill("nodejs-native-test-runner", category="nodejs")
    seqs = _fragment_sequences(dep)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Deprecated skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


def test_a3_node_test_runner_keeper_sequences_contiguous() -> None:
    """Keeper skill fragment sequences must be contiguous (no gaps)."""
    keeper = _load_skill("node-built-in-test-runner", category="nodejs")
    seqs = _fragment_sequences(keeper)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Keeper skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# A3: Both skills ingest cleanly
# ---------------------------------------------------------------------------


def test_a3_node_test_runner_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass all ingest validation."""
    dep = _load_skill("nodejs-native-test-runner", category="nodejs")
    errors = _validate_skill_ingest(dep, "nodejs-native-test-runner")
    assert not errors, f"Deprecated skill has ingest errors: {errors}"


def test_a3_node_test_runner_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass all ingest validation."""
    keeper = _load_skill("node-built-in-test-runner", category="nodejs")
    errors = _validate_skill_ingest(keeper, "node-built-in-test-runner")
    assert not errors, f"Keeper skill has ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A3: Keeper still has its own unique content
# ---------------------------------------------------------------------------

_A3_KEEPER_UNIQUE_SECTIONS = [
    # Keeper-specific topics
    "Zero dependencies",
    "Native ESM and TypeScript-friendly",
    "t.mock.method",
    "t.mock.fn",
    "t.mock.timers",
    "assert.deepEqual",
    "assert.rejects",
    "--experimental-strip-types",
]


def test_a3_node_test_runner_keeper_retains_unique_content() -> None:
    """Keeper must still contain its own unique content after merge."""
    keeper = _load_skill("node-built-in-test-runner", category="nodejs")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A3_KEEPER_UNIQUE_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper lost its own unique content: {missing}"


# ---------------------------------------------------------------------------
# A3: Deprecated skill retains its content (just marked deprecated)
# ---------------------------------------------------------------------------


def test_a3_node_test_runner_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content."""
    dep = _load_skill("nodejs-native-test-runner", category="nodejs")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    # Must still have the skip content
    assert "t.skip()" in dep_text, "Deprecated skill should retain skip content"

    # Must still have the todo content
    assert "t.todo()" in dep_text, "Deprecated skill should retain todo content"

    # Must still have the rerun-failures content
    assert "--test-rerun-failures" in dep_text, (
        "Deprecated skill should retain rerun-failures content"
    )


# ---------------------------------------------------------------------------
# A5: Next.js Forms and Server Actions merge
# ---------------------------------------------------------------------------


def test_a5_nextjs_forms_deprecated_is_marked() -> None:
    """Deprecated skill (nextjs-forms-and-server-actions) must have
    deprecated=True and superseded_by pointing to the keeper."""
    dep = _load_skill("nextjs-forms-and-server-actions", category="nextjs")

    assert dep.get("deprecated") is True, (
        "nextjs-forms-and-server-actions must have deprecated=True"
    )
    assert dep.get("superseded_by") == "nextjs-server-actions-and-mutations", (
        "superseded_by must point to nextjs-server-actions-and-mutations"
    )


def test_a5_nextjs_forms_deprecated_has_no_jsx_artifacts() -> None:
    """Deprecated skill must not contain JSX/MDX artifacts from Next.js docs."""
    dep = _load_skill("nextjs-forms-and-server-actions", category="nextjs")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    artifacts = [
        "filename=",
        "switcher",
        "highlight={",
        "> [!WARNING]",
        "> [!NOTE]",
        "> [!IMPORTANT]",
        "> [!CAUTION]",
    ]
    for artifact in artifacts:
        assert artifact not in dep_text, (
            f"Deprecated skill still contains JSX artifact: {artifact!r}"
        )


def test_a5_nextjs_forms_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content after merge."""
    dep = _load_skill("nextjs-forms-and-server-actions", category="nextjs")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    # Must still have the core server action content
    assert "use server" in dep_text, "Deprecated skill should retain 'use server' content"
    assert "formData" in dep_text, "Deprecated skill should retain formData content"
    assert "useActionState" in dep_text, "Deprecated skill should retain useActionState content"
    assert "Object.fromEntries" in dep_text, (
        "Deprecated skill should retain Object.fromEntries content"
    )
    assert "bind" in dep_text, "Deprecated skill should retain bind content"

    # Must still have validation content
    assert "zod" in dep_text, "Deprecated skill should retain zod validation content"
    assert "safeParse" in dep_text, "Deprecated skill should retain safeParse content"


def test_a5_nextjs_forms_deprecated_has_category_scope() -> None:
    """Deprecated skill must have category_scope set."""
    dep = _load_skill("nextjs-forms-and-server-actions", category="nextjs")
    assert dep.get("category_scope") == "framework", (
        "Deprecated skill must have category_scope='framework'"
    )


def test_a5_nextjs_forms_deprecated_has_merged_domain_tags() -> None:
    """Deprecated skill must have its original domain_tags preserved."""
    dep = _load_skill("nextjs-forms-and-server-actions", category="nextjs")
    tags = dep.get("domain_tags", [])
    assert "nextjs" in tags, "Must have 'nextjs' domain tag"
    assert "forms" in tags, "Must have 'forms' domain tag"
    assert "server-actions" in tags, "Must have 'server-actions' domain tag"
    assert "progressive-enhancement" in tags, "Must have 'progressive-enhancement' domain tag"
    assert "validation" in tags, "Must have 'validation' domain tag"


def test_a5_nextjs_forms_deprecated_change_summary_mentions_merge() -> None:
    """Deprecated skill change_summary must mention the merge."""
    dep = _load_skill("nextjs-forms-and-server-actions", category="nextjs")
    summary = dep.get("change_summary", "")
    assert "merged" in summary.lower() or "superseded" in summary.lower(), (
        "change_summary must mention merge or supersession"
    )


def test_a5_nextjs_forms_keeper_has_merged_domain_tags() -> None:
    """Keeper skill must have domain_tags from both merged skills."""
    keeper = _load_skill("nextjs-server-actions-and-mutations", category="nextjs")
    tags = keeper.get("domain_tags", [])

    # Original keeper tags
    assert "use-server-directive" in tags, "Keeper must retain original 'use-server-directive' tag"
    assert "form-action-prop" in tags, "Keeper must retain original 'form-action-prop' tag"
    assert "useactionstate-pending" in tags, (
        "Keeper must retain original 'useactionstate-pending' tag"
    )
    assert "revalidatepath-tag" in tags, "Keeper must retain original 'revalidatepath-tag' tag"
    assert "csrf-authorization" in tags, "Keeper must retain original 'csrf-authorization' tag"

    # Merged tags from deprecated skill
    assert "forms" in tags, "Keeper must have merged 'forms' tag from deprecated skill"
    assert "progressive-enhancement" in tags, (
        "Keeper must have merged 'progressive-enhancement' tag"
    )
    assert "validation" in tags, "Keeper must have merged 'validation' tag"


def test_a5_nextjs_forms_keeper_change_summary_mentions_merge() -> None:
    """Keeper change_summary must mention the merge."""
    keeper = _load_skill("nextjs-server-actions-and-mutations", category="nextjs")
    summary = keeper.get("change_summary", "")
    assert "merged" in summary.lower() or "merge" in summary.lower(), (
        "Keeper change_summary must mention the merge"
    )


def test_a5_nextjs_forms_keeper_retains_all_content() -> None:
    """Keeper skill must retain all content from both merged skills."""
    keeper = _load_skill("nextjs-server-actions-and-mutations", category="nextjs")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    # Content from keeper (original)
    assert "use server" in keeper_text, "Keeper must retain 'use server' content"
    assert "useOptimistic" in keeper_text, "Keeper must retain useOptimistic content"
    assert "redirect" in keeper_text, "Keeper must retain redirect content"
    assert "useActionState" in keeper_text, "Keeper must retain useActionState content"
    assert "revalidatePath" in keeper_text, "Keeper must retain revalidatePath content"
    assert "revalidateTag" in keeper_text, "Keeper must retain revalidateTag content"
    assert "useFormStatus" in keeper_text, "Keeper must retain useFormStatus content"

    # Content from deprecated (forms) skill
    assert "Object.fromEntries" in keeper_text, (
        "Keeper must retain Object.fromEntries from deprecated skill"
    )
    assert "bind" in keeper_text, "Keeper must retain bind content from deprecated skill"
    assert "zod" in keeper_text, "Keeper must retain zod validation content from deprecated skill"
    assert "safeParse" in keeper_text, "Keeper must retain safeParse content from deprecated skill"
    assert "progressive enhancement" in keeper_text, (
        "Keeper must retain progressive enhancement content"
    )


def test_a5_nextjs_forms_keeper_has_no_jsx_artifacts() -> None:
    """Keeper skill must not contain JSX/MDX artifacts."""
    keeper = _load_skill("nextjs-server-actions-and-mutations", category="nextjs")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    artifacts = [
        "filename=",
        "switcher",
        "highlight={",
        "> [!WARNING]",
        "> [!NOTE]",
        "> [!IMPORTANT]",
        "> [!CAUTION]",
    ]
    for artifact in artifacts:
        assert artifact not in keeper_text, (
            f"Keeper skill still contains JSX artifact: {artifact!r}"
        )


def test_a5_nextjs_forms_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass ingest validation."""
    dep = _load_skill("nextjs-forms-and-server-actions", category="nextjs")
    errors = _validate_skill_ingest(dep, "nextjs-forms-and-server-actions")
    assert not errors, f"Deprecated skill ingest errors: {errors}"


def test_a5_nextjs_forms_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass ingest validation after merge."""
    keeper = _load_skill("nextjs-server-actions-and-mutations", category="nextjs")
    errors = _validate_skill_ingest(keeper, "nextjs-server-actions-and-mutations")
    assert not errors, f"Keeper skill ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A8: FastAPI OAuth2 Scopes — Deprecated flags
# ---------------------------------------------------------------------------


def test_a8_fastapi_oauth2_scopes_deprecated_flags() -> None:
    """The deprecated skill must have deprecated:true and superseded_by set."""
    dep = _load_a7_skill("fastapi-oauth2-scopes")

    # Must have deprecated flag
    assert dep.get("deprecated") is True, "fastapi-oauth2-scopes must have deprecated: true"

    # Must have superseded_by pointing to the keeper skill_id
    superseded = dep.get("superseded_by")
    assert superseded is not None, "fastapi-oauth2-scopes must have superseded_by set"
    assert superseded == "fastapi-auth-and-security", (
        f"superseded_by should point to 'fastapi-auth-and-security', got '{superseded}'"
    )


# ---------------------------------------------------------------------------
# A8: FastAPI OAuth2 Scopes — Keeper has absorbed deprecated content
# ---------------------------------------------------------------------------

# These are the fragment contents unique to the deprecated skill that
# must appear in the keeper's combined content after consolidation.
_A8_UNIQUE_DEPRECATED_SECTIONS = [
    # Fragment 4: Global view
    "Global view",
    # Fragment 5: OAuth2 Security scheme
    "declaring the OAuth2 security scheme with two available scopes, `me` and `items`",
    "The `scopes` parameter receives a `dict`",
    # Fragment 9: Use SecurityScopes
    "Use `SecurityScopes`",
    # Fragment 10: Use the scopes
    "Use the `scopes`",
    "scope_str",
    # Fragment 12: Verify the scopes
    "verify that all the scopes required",
    "security_scopes.scopes",
]


def test_a8_fastapi_oauth2_scopes_keeper_has_absorbed_content() -> None:
    """Keeper must contain all unique content from the deprecated skill."""
    keeper = _load_a7_skill("fastapi-auth-and-security")
    _load_a7_skill("fastapi-oauth2-scopes")

    # Combine raw_prose + all fragment content for the keeper
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    # Each unique deprecated section must appear somewhere in the keeper
    missing = []
    for section in _A8_UNIQUE_DEPRECATED_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper is missing absorbed content from deprecated skill: {missing}"


# ---------------------------------------------------------------------------
# A8: Fragment sequences are contiguous
# ---------------------------------------------------------------------------


def test_a8_fastapi_oauth2_scopes_deprecated_sequences_contiguous() -> None:
    """Deprecated skill fragment sequences must be contiguous (no gaps)."""
    dep = _load_a7_skill("fastapi-oauth2-scopes")
    seqs = _fragment_sequences(dep)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Deprecated skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


def test_a8_fastapi_oauth2_scopes_keeper_sequences_contiguous() -> None:
    """Keeper skill fragment sequences must be contiguous (no gaps)."""
    keeper = _load_a7_skill("fastapi-auth-and-security")
    seqs = _fragment_sequences(keeper)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Keeper skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# A8: Both skills ingest cleanly
# ---------------------------------------------------------------------------


def test_a8_fastapi_oauth2_scopes_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass all ingest validation."""
    dep = _load_a7_skill("fastapi-oauth2-scopes")
    errors = _validate_skill_ingest(dep, "fastapi-oauth2-scopes")
    assert not errors, f"Deprecated skill has ingest errors: {errors}"


def test_a8_fastapi_oauth2_scopes_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass all ingest validation."""
    keeper = _load_a7_skill("fastapi-auth-and-security")
    errors = _validate_skill_ingest(keeper, "fastapi-auth-and-security")
    assert not errors, f"Keeper skill has ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A8: Keeper still has its own unique content
# ---------------------------------------------------------------------------

_A8_KEEPER_UNIQUE_SECTIONS = [
    # Keeper-specific topics from raw_prose and fragments
    "OAuth2 Password Bearer scheme",
    "The /token endpoint with OAuth2PasswordRequestForm",
    "JWT encoding, decoding, and claims",
    "get_current_user dependency",
    "OAuth2 scopes via Security()",
    "API key auth for service-to-service traffic",
    "CORS and refresh tokens",
    "Anti-patterns and the weak-secret edge case",
    "Verification",
]


def test_a8_fastapi_oauth2_scopes_keeper_retains_unique_content() -> None:
    """Keeper must still contain its own unique content after merge."""
    keeper = _load_a7_skill("fastapi-auth-and-security")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A8_KEEPER_UNIQUE_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper lost its own unique content: {missing}"


# ---------------------------------------------------------------------------
# A8: Deprecated skill retains its content (just marked deprecated)
# ---------------------------------------------------------------------------


def test_a8_fastapi_oauth2_scopes_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content."""
    dep = _load_a7_skill("fastapi-oauth2-scopes")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    # Must still have the SecurityScopes content
    assert "SecurityScopes" in dep_text, "Deprecated skill should retain SecurityScopes content"

    # Must still have the verify scopes content
    assert "verify that all the scopes required" in dep_text, (
        "Deprecated skill should retain verify scopes content"
    )

    # Must still have the overview
    assert "FastAPI extends OAuth2 password flow with scopes" in dep_text, (
        "Deprecated skill should retain overview content"
    )


# ---------------------------------------------------------------------------
# A6: Testing TDD Cycle — Deprecated flags
# ---------------------------------------------------------------------------


def test_a6_testing_tdd_cycle_deprecated_flags() -> None:
    """The deprecated skill must have deprecated:true and superseded_by set."""
    dep = _load_skill("testing-tdd-cycle", category="testing")

    assert dep.get("deprecated") is True, "testing-tdd-cycle must have deprecated: true"
    superseded = dep.get("superseded_by")
    assert superseded is not None, "testing-tdd-cycle must have superseded_by set"
    assert superseded == "test-driven-development", (
        f"superseded_by should point to 'test-driven-development', got '{superseded}'"
    )


# ---------------------------------------------------------------------------
# A6: Testing TDD Cycle — Keeper has absorbed deprecated content
# ---------------------------------------------------------------------------

_A6_UNIQUE_DEPRECATED_SECTIONS = [
    # Cycle length guidance
    "30 seconds",
    "2 minutes",
    "smallest change to pass",
    # Triangulation
    "Triangulation",
    "passed by hardcoded values",
    "test_add_two_numbers",
    "test_add_other_numbers",
    # Failing for the right reason
    "TypeError: undefined is not a function",
    # When TDD helps most
    "Adding behavior to existing code",
    "Bug fixes",
    "Refactors of business logic",
]


def test_a6_testing_tdd_cycle_keeper_has_absorbed_content() -> None:
    """Keeper must contain all unique content from the deprecated skill."""
    keeper = _load_skill("test-driven-development", category="core")

    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A6_UNIQUE_DEPRECATED_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper is missing absorbed content from deprecated skill: {missing}"


# ---------------------------------------------------------------------------
# A6: Fragment sequences are contiguous
# ---------------------------------------------------------------------------


def test_a6_testing_tdd_cycle_deprecated_sequences_contiguous() -> None:
    """Deprecated skill fragment sequences must be contiguous (no gaps)."""
    dep = _load_skill("testing-tdd-cycle", category="testing")
    seqs = _fragment_sequences(dep)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Deprecated skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


def test_a6_testing_tdd_cycle_keeper_sequences_contiguous() -> None:
    """Keeper skill fragment sequences must be contiguous (no gaps)."""
    keeper = _load_skill("test-driven-development", category="core")
    seqs = _fragment_sequences(keeper)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Keeper skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# A6: Both skills ingest cleanly
# ---------------------------------------------------------------------------


def test_a6_testing_tdd_cycle_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass all ingest validation."""
    dep = _load_skill("testing-tdd-cycle", category="testing")
    errors = _validate_skill_ingest(dep, "testing-tdd-cycle")
    assert not errors, f"Deprecated skill has ingest errors: {errors}"


def test_a6_testing_tdd_cycle_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass all ingest validation."""
    keeper = _load_skill("test-driven-development", category="core")
    errors = _validate_skill_ingest(keeper, "test-driven-development")
    assert not errors, f"Keeper skill has ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A6: Keeper retains its own unique content
# ---------------------------------------------------------------------------

_A6_KEEPER_UNIQUE_SECTIONS = [
    "Test-Driven Development (Red, Green, Refactor)",
    "regression-trap",
    "over-design",
    "fake-it-till-you-make-it",
    "Mock the boundary",
    "test_user_with_invalid_email_is_rejected",
    "pytest",
    "vitest",
]


def test_a6_testing_tdd_cycle_keeper_retains_unique_content() -> None:
    """Keeper must still contain its own unique content after merge."""
    keeper = _load_skill("test-driven-development", category="core")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A6_KEEPER_UNIQUE_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper lost its own unique content: {missing}"


# ---------------------------------------------------------------------------
# A6: Deprecated skill retains its content
# ---------------------------------------------------------------------------


def test_a6_testing_tdd_cycle_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content."""
    dep = _load_skill("testing-tdd-cycle", category="testing")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    assert "TypeError: undefined is not a function" in dep_text, (
        "Deprecated skill should retain 'failing for the right reason' content"
    )
    assert "test_add_two_numbers" in dep_text, (
        "Deprecated skill should retain triangulation content"
    )
    assert "30 seconds" in dep_text, "Deprecated skill should retain cycle length content"
    assert "When TDD Helps Most" in dep_text, (
        "Deprecated skill should retain 'when TDD helps most' content"
    )


# ---------------------------------------------------------------------------
# A6: Deprecated skill has merged domain_tags
# ---------------------------------------------------------------------------


def test_a6_testing_tdd_cycle_keeper_has_merged_domain_tags() -> None:
    """Keeper skill must have domain_tags from both merged skills."""
    keeper = _load_skill("test-driven-development", category="core")
    tags = keeper.get("domain_tags", [])

    # Original keeper tags
    assert "failing-test-first" in tags, "Keeper must retain original 'failing-test-first' tag"
    assert "pytest" in tags, "Keeper must retain original 'pytest' tag"
    assert "vitest" in tags, "Keeper must retain original 'vitest' tag"
    assert "red-green-refactor" in tags, "Keeper must retain original 'red-green-refactor' tag"
    assert "regression-trap" in tags, "Keeper must retain original 'regression-trap' tag"

    # Merged tags from deprecated skill
    assert "tdd" in tags, "Keeper must have merged 'tdd' tag"
    assert "test-driven-development" in tags, (
        "Keeper must have merged 'test-driven-development' tag"
    )
    assert "software-testing" in tags, "Keeper must have merged 'software-testing' tag"
    assert "agile-methodology" in tags, "Keeper must have merged 'agile-methodology' tag"


# ---------------------------------------------------------------------------
# A6: Deprecated skill change_summary mentions merge
# ---------------------------------------------------------------------------


def test_a6_testing_tdd_cycle_deprecated_change_summary_mentions_merge() -> None:
    """Deprecated skill change_summary must mention the merge."""
    dep = _load_skill("testing-tdd-cycle", category="testing")
    summary = dep.get("change_summary", "")
    assert "merged" in summary.lower() or "superseded" in summary.lower(), (
        "change_summary must mention merge or supersession"
    )


# ---------------------------------------------------------------------------
# A6: Keeper change_summary mentions merge
# ---------------------------------------------------------------------------


def test_a6_testing_tdd_cycle_keeper_change_summary_mentions_merge() -> None:
    """Keeper change_summary must mention the merge."""
    keeper = _load_skill("test-driven-development", category="core")
    summary = keeper.get("change_summary", "")
    assert "merged" in summary.lower() or "merge" in summary.lower(), (
        "Keeper change_summary must mention the merge"
    )


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Deprecated flags
# ---------------------------------------------------------------------------


def test_a2_typescript_narrowing_deprecated_flags() -> None:
    """The deprecated skill must have deprecated:true and superseded_by set."""
    dep = _load_skill("typescript-narrowing-and-control-flow", category="typescript")

    assert dep.get("deprecated") is True, (
        "typescript-narrowing-and-control-flow must have deprecated: true"
    )

    superseded = dep.get("superseded_by")
    assert superseded is not None, (
        "typescript-narrowing-and-control-flow must have superseded_by set"
    )
    assert superseded == "typescript-narrowing-patterns", (
        f"superseded_by should point to 'typescript-narrowing-patterns', got '{superseded}'"
    )


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Keeper has absorbed deprecated content
# ---------------------------------------------------------------------------

_A2_UNIQUE_DEPRECATED_SECTIONS = [
    # typeof quirks (null === 'object' edge case with printAll example)
    'typeof null === "object"',
    "string[] | null",
    # Truthiness narrowing (falsy values list, guards, caveats)
    "0n",
    "bigint",
    "getUsersOnlineMessage",
    # Boolean negations (multiplyAll example)
    "multiplyAll",
    # typeof return values list
    '"string"',
    '"bigint"',
    '"symbol"',
]


def test_a2_typescript_narrowing_keeper_has_absorbed_content() -> None:
    """Keeper must contain all unique content from the deprecated skill."""
    keeper = _load_skill("typescript-narrowing-patterns", category="typescript")
    _load_skill("typescript-narrowing-and-control-flow", category="typescript")

    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A2_UNIQUE_DEPRECATED_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper is missing absorbed content from deprecated skill: {missing}"


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Fragment sequences are contiguous
# ---------------------------------------------------------------------------


def test_a2_typescript_narrowing_deprecated_sequences_contiguous() -> None:
    """Deprecated skill fragment sequences must be contiguous (no gaps)."""
    dep = _load_skill("typescript-narrowing-and-control-flow", category="typescript")
    seqs = _fragment_sequences(dep)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Deprecated skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


def test_a2_typescript_narrowing_keeper_sequences_contiguous() -> None:
    """Keeper skill fragment sequences must be contiguous (no gaps)."""
    keeper = _load_skill("typescript-narrowing-patterns", category="typescript")
    seqs = _fragment_sequences(keeper)
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f"Keeper skill fragment sequences are not contiguous: got {seqs}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Both skills ingest cleanly
# ---------------------------------------------------------------------------


def test_a2_typescript_narrowing_deprecated_ingests_cleanly() -> None:
    """Deprecated skill must pass all ingest validation."""
    dep = _load_skill("typescript-narrowing-and-control-flow", category="typescript")
    errors = _validate_skill_ingest(dep, "typescript-narrowing-and-control-flow")
    assert not errors, f"Deprecated skill has ingest errors: {errors}"


def test_a2_typescript_narrowing_keeper_ingests_cleanly() -> None:
    """Keeper skill must pass all ingest validation."""
    keeper = _load_skill("typescript-narrowing-patterns", category="typescript")
    errors = _validate_skill_ingest(keeper, "typescript-narrowing-patterns")
    assert not errors, f"Keeper skill has ingest errors: {errors}"


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Keeper retains its own unique content
# ---------------------------------------------------------------------------

_A2_KEEPER_UNIQUE_SECTIONS = [
    # Keeper-specific topics
    "User-Defined Type Guards",
    "Assertion Functions",
    "Discriminated Unions",
    "Exhaustiveness Checking",
    "satisfies",
    "Narrowing on Optional Properties",
    "Array.isArray",
    "Common Pitfalls",
    "Verification",
    "x is string",
    "asserts x is string",
]


def test_a2_typescript_narrowing_keeper_retains_unique_content() -> None:
    """Keeper must still contain its own unique content after merge."""
    keeper = _load_skill("typescript-narrowing-patterns", category="typescript")
    keeper_text = _raw_prose(keeper) + "\n\n" + _fragment_content_concat(keeper)

    missing = []
    for section in _A2_KEEPER_UNIQUE_SECTIONS:
        if section not in keeper_text:
            missing.append(section)

    assert not missing, f"Keeper lost its own unique content: {missing}"


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Deprecated skill retains its content
# ---------------------------------------------------------------------------


def test_a2_typescript_narrowing_deprecated_retains_content() -> None:
    """Deprecated skill must retain all its original content."""
    dep = _load_skill("typescript-narrowing-and-control-flow", category="typescript")
    dep_text = _raw_prose(dep) + "\n\n" + _fragment_content_concat(dep)

    assert "padLeft" in dep_text, "Deprecated skill should retain padLeft walkthrough content"
    assert "multiplyAll" in dep_text, "Deprecated skill should retain boolean negations content"
    assert "typeof null" in dep_text, "Deprecated skill should retain typeof quirks content"


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Keeper has merged domain tags
# ---------------------------------------------------------------------------


def test_a2_typescript_narrowing_keeper_has_merged_domain_tags() -> None:
    """Keeper must have merged domain tags from deprecated skill."""
    keeper = _load_skill("typescript-narrowing-patterns", category="typescript")
    dep = _load_skill("typescript-narrowing-and-control-flow", category="typescript")

    keeper_tags = keeper.get("domain_tags", [])
    dep.get("domain_tags", [])

    # Original keeper tags
    assert "typescript" in keeper_tags, "Keeper must retain original 'typescript' tag"
    assert "narrowing" in keeper_tags, "Keeper must retain original 'narrowing' tag"
    assert "type-guards" in keeper_tags, "Keeper must retain original 'type-guards' tag"
    assert "discriminated-unions" in keeper_tags, (
        "Keeper must retain original 'discriminated-unions' tag"
    )
    assert "assertions" in keeper_tags, "Keeper must retain original 'assertions' tag"

    # Merged tags from deprecated skill
    assert "control-flow-analysis" in keeper_tags, (
        "Keeper must have merged 'control-flow-analysis' tag"
    )
    assert "union-types" in keeper_tags, "Keeper must have merged 'union-types' tag"


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Deprecated skill change_summary mentions merge
# ---------------------------------------------------------------------------


def test_a2_typescript_narrowing_deprecated_change_summary_mentions_merge() -> None:
    """Deprecated skill change_summary must mention the merge."""
    dep = _load_skill("typescript-narrowing-and-control-flow", category="typescript")
    summary = dep.get("change_summary", "")
    assert "merged" in summary.lower() or "superseded" in summary.lower(), (
        "change_summary must mention merge or supersession"
    )


# ---------------------------------------------------------------------------
# A2: TypeScript Narrowing — Keeper change_summary mentions merge
# ---------------------------------------------------------------------------


def test_a2_typescript_narrowing_keeper_change_summary_mentions_merge() -> None:
    """Keeper change_summary must mention the merge."""
    keeper = _load_skill("typescript-narrowing-patterns", category="typescript")
    summary = keeper.get("change_summary", "")
    assert "merged" in summary.lower() or "merge" in summary.lower(), (
        "Keeper change_summary must mention the merge"
    )
