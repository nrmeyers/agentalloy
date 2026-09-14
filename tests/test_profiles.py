"""Profile resolver tests (M2)."""

import tempfile
from pathlib import Path

import yaml

from agentalloy.profiles import (
    DEFAULT_PROFILE_NAME,
    Profile,
    detect_profile,
    list_profiles,
    load_profiles_config,
)


def test_default_profile() -> None:
    """Default profile is returned when no config exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        profile = detect_profile(Path(tmpdir))
        assert profile.name == DEFAULT_PROFILE_NAME
        assert profile.is_default is True
        assert profile.packs == []
        assert profile.domain_tags == []


def test_project_marker_detection(tmp_path: Path) -> None:
    """Explicit project marker selects the named profile."""
    # Create a profiles.yaml with a "work" profile
    profiles_dir = tmp_path / "profiles_root"
    profiles_dir.mkdir()
    profiles_yaml = profiles_dir / "profiles.yaml"
    profiles_yaml.write_text(
        yaml.dump(
            {
                "profiles": {
                    "work": {
                        "packs": ["python", "fastapi"],
                        "domain_tags": ["backend"],
                    }
                },
                "default_profile": "default",
            }
        )
    )

    # Create project with marker
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    marker_dir = project_dir / ".agentalloy"
    marker_dir.mkdir()
    (marker_dir / "profile").write_text(yaml.dump({"profile": "work"}))

    # Monkeypatch profiles_root to use our temp dir
    import agentalloy.profiles as profiles_mod

    original_root = profiles_mod.profiles_root
    profiles_mod.profiles_root = lambda: profiles_dir  # type: ignore[assignment]
    try:
        profile = detect_profile(project_dir)
        assert profile.name == "work"
        assert profile.packs == ["python", "fastapi"]
        assert profile.domain_tags == ["backend"]
        assert profile.is_default is False
    finally:
        profiles_mod.profiles_root = original_root  # type: ignore[assignment]


def test_list_profiles_includes_default(tmp_path: Path) -> None:
    """list_profiles always includes the default profile."""
    import agentalloy.profiles as profiles_mod

    profiles_dir = tmp_path / "profiles_root"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yaml").write_text(
        yaml.dump(
            {
                "profiles": {"work": {"packs": ["python"]}},
                "default_profile": "default",
            }
        )
    )

    original_root = profiles_mod.profiles_root
    profiles_mod.profiles_root = lambda: profiles_dir  # type: ignore[assignment]
    try:
        result = list_profiles()
        names = [p["name"] for p in result]
        assert DEFAULT_PROFILE_NAME in names
        assert "work" in names
    finally:
        profiles_mod.profiles_root = original_root  # type: ignore[assignment]


def test_profile_dataclass() -> None:
    """Profile dataclass fields."""
    p = Profile(
        name="test",
        skills_dir=Path("/tmp/test-skills"),
        datastore_path=Path("/tmp/test-datastore"),
        packs=["python"],
        domain_tags=["backend"],
    )
    assert p.name == "test"
    assert p.packs == ["python"]
    assert p.domain_tags == ["backend"]
    assert p.is_default is False

    p2 = Profile(
        name="default",
        skills_dir=Path("/tmp/default-skills"),
        datastore_path=Path("/tmp/default-datastore"),
        is_default=True,
    )
    assert p2.is_default is True
    assert p2.packs == []


def test_load_profiles_config_missing() -> None:
    """Missing profiles.yaml returns default config."""
    import agentalloy.profiles as profiles_mod

    original_root = profiles_mod.profiles_root
    profiles_mod.profiles_root = lambda: Path("/nonexistent/path")  # type: ignore[assignment]
    try:
        config = load_profiles_config()
        assert config.default_profile == DEFAULT_PROFILE_NAME
        assert isinstance(config.profiles, dict)
    finally:
        profiles_mod.profiles_root = original_root  # type: ignore[assignment]


class TestDetectRepoTags:
    """Repo technology detection feeding the skill-catalog filter."""

    def test_python_fastapi_repo(self, tmp_path):
        from agentalloy.profiles import detect_repo_tags

        (tmp_path / "app").mkdir()
        for i in range(4):
            (tmp_path / "app" / f"m{i}.py").write_text("x = 1\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["fastapi", "pytest"]\n'
        )
        tags = detect_repo_tags(tmp_path)
        assert "python" in tags
        assert "fastapi" in tags
        assert "typescript" not in tags
        assert "fastify" not in tags

    def test_node_repo_manifest_deps(self, tmp_path):
        import json

        from agentalloy.profiles import detect_repo_tags

        (tmp_path / "index.ts").write_text("export {}\n")
        (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"fastify": "^4.0.0"}}))
        tags = detect_repo_tags(tmp_path)
        assert "typescript" in tags  # small repo → threshold 1
        assert "fastify" in tags

    def test_skip_dirs_ignored(self, tmp_path):
        from agentalloy.profiles import detect_repo_tags

        nm = tmp_path / "node_modules" / "lib"
        nm.mkdir(parents=True)
        (nm / "big.js").write_text("x")
        (tmp_path / "main.py").write_text("x")
        tags = detect_repo_tags(tmp_path)
        assert tags == {"python"}
