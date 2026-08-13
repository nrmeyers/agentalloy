"""Tests for agentalloy reset subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def profiles_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    return xdg


def _make_override(
    profile_name: str, skill_class: str, skill_name: str, content: str = "x" * 120
) -> Path:
    from agentalloy.profiles import profile_skills_dir

    d = profile_skills_dir(profile_name) / skill_class
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{skill_name}.yaml"
    f.write_text(
        yaml.dump({"skill_id": skill_name, "skill_class": skill_class, "raw_prose": content})
    )
    return f


def test_reset_requires_confirmation(profiles_tmp: Path, monkeypatch: pytest.MonkeyPatch):
    from agentalloy.install.subcommands.reset import reset
    from agentalloy.profiles import init_profile

    init_profile("testprofile")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "no")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    result = reset(profile="testprofile", yes=False)
    assert result.get("cancelled")


def test_reset_clears_profile_overrides(profiles_tmp: Path, monkeypatch: pytest.MonkeyPatch):
    from agentalloy.install.subcommands.reset import reset
    from agentalloy.profiles import init_profile

    init_profile("r1")
    override = _make_override("r1", "system", "my-skill")
    assert override.exists()

    result = reset(profile="r1", yes=True)
    assert not override.exists()
    assert "r1" in [r["profile"] for r in result["reset_profiles"]]


def test_reset_does_not_touch_domain(profiles_tmp: Path):
    from agentalloy.install.subcommands.reset import reset
    from agentalloy.profiles import domain_datastore_path, init_profile

    init_profile("r2")
    domain = domain_datastore_path()
    domain.parent.mkdir(parents=True, exist_ok=True)
    domain.write_bytes(b"fake-domain-data")

    reset(profile="r2", yes=True)

    assert domain.exists()
    assert domain.read_bytes() == b"fake-domain-data"


def test_reset_does_not_touch_other_profiles(profiles_tmp: Path):
    from agentalloy.install.subcommands.reset import reset
    from agentalloy.profiles import init_profile

    init_profile("rA")
    init_profile("rB")
    override_b = _make_override("rB", "system", "b-skill")

    reset(profile="rA", yes=True)
    # rB's override should be untouched
    assert override_b.exists()
