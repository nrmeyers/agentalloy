"""Tests for agentalloy.profiles — resolver, management, and datastore helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def profiles_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect XDG_DATA_HOME to a temp dir so tests don't touch ~/.agentalloy."""
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    return xdg


# ---------------------------------------------------------------------------
# profiles_root
# ---------------------------------------------------------------------------


def test_profiles_root_honors_xdg(profiles_tmp: Path):
    from agentalloy.profiles import profiles_root

    root = profiles_root()
    assert root == profiles_tmp / "agentalloy"


# ---------------------------------------------------------------------------
# load_profiles_config
# ---------------------------------------------------------------------------


def test_load_profiles_config_missing_file(profiles_tmp: Path):
    from agentalloy.profiles import DEFAULT_PROFILE_NAME, load_profiles_config

    cfg = load_profiles_config()
    assert cfg.default_profile == DEFAULT_PROFILE_NAME
    assert isinstance(cfg.profiles, dict)


def test_load_profiles_config_reads_yaml(profiles_tmp: Path):
    from agentalloy.profiles import load_profiles_config, profiles_yaml_path

    path = profiles_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "default_profile: work\nprofiles:\n  work:\n    match_remote:\n      - '*github.com/acme/*'\n"
    )
    cfg = load_profiles_config()
    assert cfg.default_profile == "work"
    assert "work" in cfg.profiles


# ---------------------------------------------------------------------------
# detect_profile
# ---------------------------------------------------------------------------


def test_detect_profile_explicit_marker(profiles_tmp: Path, tmp_path: Path):
    from agentalloy.profiles import detect_profile, init_profile, profiles_yaml_path

    # Create the profile
    profiles_yaml_path().parent.mkdir(parents=True, exist_ok=True)
    init_profile("work")

    project = tmp_path / "myrepo"
    project.mkdir()
    marker = project / ".agentalloy" / "profile"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("profile: work\n")

    p = detect_profile(project)
    assert p.name == "work"


def test_detect_profile_remote_match(profiles_tmp: Path, tmp_path: Path):
    from agentalloy.profiles import detect_profile, init_profile

    init_profile("acme", match_remote=["*github.com/acme/*"])

    project = tmp_path / "myrepo"
    project.mkdir()

    with patch(
        "agentalloy.profiles._git_remote_url",
        return_value="https://github.com/acme/foo.git",
    ):
        p = detect_profile(project)

    assert p.name == "acme"


def test_detect_profile_path_match(profiles_tmp: Path, tmp_path: Path):
    from agentalloy.profiles import detect_profile, init_profile

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    init_profile("workprofile", match_path=[str(work_dir / "*")])

    project = work_dir / "myrepo"
    project.mkdir()

    with patch("agentalloy.profiles._git_remote_url", return_value=None):
        p = detect_profile(project)

    assert p.name == "workprofile"


def test_detect_profile_path_match_deep(profiles_tmp: Path, tmp_path: Path):
    """A '<dir>/*' pattern matches repos nested deeper than one level (prefix)."""
    from agentalloy.profiles import detect_profile, init_profile

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    init_profile("workprofile", match_path=[str(work_dir / "*")])

    deep = work_dir / "team" / "myrepo" / "sub"
    deep.mkdir(parents=True)

    with patch("agentalloy.profiles._git_remote_url", return_value=None):
        p = detect_profile(deep)

    assert p.name == "workprofile"


def test_detect_profile_path_prefix_no_glob(profiles_tmp: Path, tmp_path: Path):
    """A bare '<dir>' prefix pattern (no glob) matches a repo nested under it."""
    from agentalloy.profiles import detect_profile, init_profile

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    init_profile("workprofile", match_path=[str(work_dir)])

    project = work_dir / "myrepo"
    project.mkdir()

    with patch("agentalloy.profiles._git_remote_url", return_value=None):
        p = detect_profile(project)

    assert p.name == "workprofile"


def test_detect_profile_path_no_false_match(profiles_tmp: Path, tmp_path: Path):
    """A sibling dir sharing a string prefix must NOT match (path-component aware)."""
    from agentalloy.profiles import detect_profile, init_profile

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    init_profile("workprofile", match_path=[str(work_dir)])

    sibling = tmp_path / "work2" / "repo"
    sibling.mkdir(parents=True)

    with patch("agentalloy.profiles._git_remote_url", return_value=None):
        p = detect_profile(sibling)

    assert p.name != "workprofile"


def test_detect_profile_fallback_to_default(profiles_tmp: Path, tmp_path: Path):
    from agentalloy.profiles import detect_profile

    with patch("agentalloy.profiles._git_remote_url", return_value=None):
        p = detect_profile(tmp_path / "norepo")

    assert p.name == "default"
    assert p.is_default


# ---------------------------------------------------------------------------
# init_profile
# ---------------------------------------------------------------------------


def test_init_profile_creates_structure(profiles_tmp: Path):
    from agentalloy.profiles import init_profile, profile_skills_dir

    p = init_profile("mywork", match_remote=["*github.com/myorg/*"])

    assert p.name == "mywork"
    assert (profile_skills_dir("mywork") / "system").is_dir()
    assert (profile_skills_dir("mywork") / "workflow").is_dir()
    # profiles.yaml should have the entry
    from agentalloy.profiles import load_profiles_config

    cfg = load_profiles_config()
    assert "mywork" in cfg.profiles
    assert cfg.profiles["mywork"]["match_remote"] == ["*github.com/myorg/*"]


def test_init_profile_invalid_name(profiles_tmp: Path):
    from agentalloy.profiles import init_profile

    with pytest.raises(ValueError):
        init_profile("bad name!")


def test_init_profile_refuses_default(profiles_tmp: Path):
    from agentalloy.profiles import init_profile

    with pytest.raises(ValueError, match="reserved"):
        init_profile("default")


# ---------------------------------------------------------------------------
# delete_profile
# ---------------------------------------------------------------------------


def test_delete_default_refused(profiles_tmp: Path):
    from agentalloy.profiles import delete_profile

    with pytest.raises(ValueError, match="default"):
        delete_profile("default")


def test_delete_profile_removes_dir_and_config(profiles_tmp: Path):
    from agentalloy.profiles import delete_profile, init_profile, load_profiles_config, profile_dir

    init_profile("temp")
    assert profile_dir("temp").exists()

    delete_profile("temp")
    assert not profile_dir("temp").exists()
    cfg = load_profiles_config()
    assert "temp" not in cfg.profiles


# ---------------------------------------------------------------------------
# domain_datastore_path
# ---------------------------------------------------------------------------


def test_domain_datastore_independent_of_profile(profiles_tmp: Path):
    from agentalloy.profiles import domain_datastore_path, init_profile

    init_profile("p1")
    init_profile("p2")

    path1 = domain_datastore_path()
    # Should not contain "profiles" — it's shared
    assert "profiles" not in str(path1)
    # Same path regardless of which profile is "active"
    assert domain_datastore_path() == path1


# ---------------------------------------------------------------------------
# list_profiles
# ---------------------------------------------------------------------------


def test_list_profiles_includes_default(profiles_tmp: Path):
    from agentalloy.profiles import list_profiles

    profiles = list_profiles()
    names = [p["name"] for p in profiles]
    assert "default" in names


def test_list_profiles_round_trip(profiles_tmp: Path, tmp_path: Path):
    from agentalloy.profiles import init_profile, list_profiles

    init_profile("alpha")
    init_profile("beta")
    profiles = list_profiles()
    names = [p["name"] for p in profiles]
    assert "alpha" in names
    assert "beta" in names
