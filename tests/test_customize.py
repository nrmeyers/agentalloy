"""Tests for agentalloy customize CLI — three-layer resolution and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def profiles_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    return xdg


def _make_skill_yaml(
    skill_id: str = "test-skill",
    skill_class: str = "system",
    raw_prose: str = "A" * 120,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "skill_id": skill_id,
        "canonical_name": skill_id,
        "skill_class": skill_class,
        "raw_prose": raw_prose,
    }
    if skill_class == "workflow":
        data["applies_to_phases"] = ["build"]
        data["exit_gates"] = {"tests_pass": "all tests green"}
        data["contract_template"] = "---\nphase: build\n---\n\nbody\n"
    if skill_class == "system":
        data["applies_when"] = {"always": True}
    if extra:
        data.update(extra)
    return data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_rejects_domain_skill():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data = _make_skill_yaml(skill_class="domain")
    errors = _validate_skill_data(data, "some-domain-skill")
    assert any("domain skill" in e for e in errors)


def test_validate_rejects_short_prose():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data = _make_skill_yaml(skill_class="system", raw_prose="short")
    errors = _validate_skill_data(data, "myskill")
    assert any("80 characters" in e for e in errors)


def test_validate_workflow_missing_exit_gates():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data = _make_skill_yaml(skill_class="workflow")
    del data["exit_gates"]
    errors = _validate_skill_data(data, "wf-skill")
    assert any("exit_gates" in e for e in errors)


def test_validate_workflow_missing_applies_to_phases():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data = _make_skill_yaml(skill_class="workflow")
    del data["applies_to_phases"]
    errors = _validate_skill_data(data, "wf-skill")
    assert any("applies_to_phases" in e for e in errors)


def test_validate_system_missing_applies_when():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data = _make_skill_yaml(skill_class="system")
    del data["applies_when"]
    errors = _validate_skill_data(data, "sys-skill")
    assert any("applies_when" in e for e in errors)


def test_validate_valid_system_skill():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data = _make_skill_yaml(skill_class="system")
    errors = _validate_skill_data(data, "sys-skill")
    assert errors == []


def test_validate_valid_workflow_skill():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data = _make_skill_yaml(skill_class="workflow")
    errors = _validate_skill_data(data, "wf-skill")
    assert errors == []


# ---------------------------------------------------------------------------
# Profile store (unit-level ingest)
# ---------------------------------------------------------------------------


def test_customize_update_ingests_into_profile(profiles_tmp: Path, tmp_path: Path):
    from agentalloy.install.subcommands.customize import (
        _ingest_skill,  # pyright: ignore[reportPrivateUsage]
        _skill_in_store,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.profiles import init_profile

    init_profile("testprofile")

    data = _make_skill_yaml(skill_id="my-system-skill", skill_class="system")
    _ingest_skill("testprofile", data)

    assert _skill_in_store("testprofile", "my-system-skill")


def test_customize_update_reverts_to_inherited(profiles_tmp: Path, tmp_path: Path):
    from agentalloy.install.subcommands.customize import (
        _delete_from_store,  # pyright: ignore[reportPrivateUsage]
        _ingest_skill,  # pyright: ignore[reportPrivateUsage]
        _skill_in_store,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.profiles import init_profile

    init_profile("p2")
    data = _make_skill_yaml(skill_id="rev-skill")
    _ingest_skill("p2", data)
    assert _skill_in_store("p2", "rev-skill")

    _delete_from_store("p2", "rev-skill")
    assert not _skill_in_store("p2", "rev-skill")


# ---------------------------------------------------------------------------
# Validate domain skill blocked from customize
# ---------------------------------------------------------------------------


def test_customize_validate_rejects_domain(
    profiles_tmp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """validate subcommand returns error for domain-class skill."""
    import argparse

    from agentalloy.install.subcommands.customize import (
        _validate_skill,  # pyright: ignore[reportPrivateUsage]
    )

    skill_file = tmp_path / "domain-skill.yaml"
    data = {
        "skill_id": "domain-skill",
        "skill_class": "domain",
        "raw_prose": "A" * 120,
    }
    skill_file.write_text(yaml.dump(data))

    # Patch _active_layer to return our file
    from agentalloy.install.subcommands import customize as cmod

    monkeypatch.setattr(
        cmod,
        "_resolve_skill_layers",
        lambda name, profile_name, **kw: {  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
            "project": None,
            "profile": None,
            "default": skill_file,
            "active_profile_name": "default",
            "active_profile": type("P", (), {"name": "default", "skills_dir": tmp_path})(),
            "skill_class": "domain",
        },
    )
    monkeypatch.setattr(cmod, "_active_layer", lambda layers: ("default", skill_file))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    args = argparse.Namespace(name="domain-skill", profile=None, project=False)
    rc = _validate_skill(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# Workflow skill schema (Phase 2 additions to customize validator)
# ---------------------------------------------------------------------------


def test_validate_workflow_requires_contract_template():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data: dict[str, Any] = {
        "skill_id": "wf",
        "skill_class": "workflow",
        "raw_prose": "A" * 120,
        "applies_to_phases": ["build"],
        "exit_gates": {"all_of": []},
        # missing contract_template
    }
    errors = _validate_skill_data(data, "wf")
    assert any("contract_template" in e for e in errors)


def test_workflow_schema_accepts_phase2_minimal_gates():
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    data: dict[str, Any] = {
        "skill_id": "wf",
        "skill_class": "workflow",
        "raw_prose": "A" * 120,
        "applies_to_phases": ["build"],
        "exit_gates": {"all_of": [{"artifact_exists": {"path": "src/**"}}]},
        "contract_template": "---\nphase: build\n---\n\nbody\n",
    }
    errors = _validate_skill_data(data, "wf")
    assert errors == []


def test_sdd_workflow_skills_pass_validation():
    """All shipped sdd-*.yaml files must pass the Phase 2 validator."""
    import yaml as _yaml

    import agentalloy
    from agentalloy.install.subcommands.customize import (
        _validate_skill_data,  # pyright: ignore[reportPrivateUsage]
    )

    packs_root = Path(agentalloy.__file__).resolve().parent / "_packs" / "sdd"
    failures: list[str] = []
    for f in sorted(packs_root.glob("sdd-*.yaml")):
        data: dict[str, Any] = _yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if data.get("skill_class") == "workflow":
            errors = _validate_skill_data(data, f.stem)
            if errors:
                failures.append(f"{f.name}: {errors}")

    assert not failures, "SDD workflow skill validation failures:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Three-layer list output includes provenance
# ---------------------------------------------------------------------------


def test_customize_list_has_layer_field(profiles_tmp: Path):
    import argparse
    import io
    import json
    import sys

    from agentalloy.install.subcommands.customize import (
        _list_skills,  # pyright: ignore[reportPrivateUsage]
    )

    args = argparse.Namespace(profile=None, json=True)
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        _list_skills(args)
    finally:
        sys.stdout = old_stdout

    rows = json.loads(captured.getvalue())
    assert isinstance(rows, list)
    if rows:
        assert "layer" in rows[0]
        assert "skill_class" in rows[0]
