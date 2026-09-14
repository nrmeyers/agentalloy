"""instance_env — env.sh read/rewrite tests."""

from pathlib import Path

from agentalloy.instance_env import (
    instance_env_path,
    read_env_vars,
    update_env_vars,
)


def _write(path: Path, *lines: str) -> None:
    path.write_text("\n".join(lines) + "\n")


def test_instance_env_path_is_sibling_of_state_duck() -> None:
    assert instance_env_path("/a/b/state.duck") == Path("/a/b/env.sh")


def test_read_env_vars_parses_export_lines(tmp_path: Path) -> None:
    path = tmp_path / "env.sh"
    _write(
        path,
        "export AGENTALLOY_MODEL=Qwen3.8-27B-FP8",
        "# a comment",
        "AGENTALLOY_NOT_EXPORTED=skipped",
        "export AGENTALLOY_SERVICE_PORT=48950",
    )
    assert read_env_vars(path) == {
        "AGENTALLOY_MODEL": "Qwen3.8-27B-FP8",
        "AGENTALLOY_SERVICE_PORT": "48950",
    }


def test_read_env_vars_missing_file(tmp_path: Path) -> None:
    assert read_env_vars(tmp_path / "nope.sh") == {}


def test_update_env_vars_replaces_in_place(tmp_path: Path) -> None:
    path = tmp_path / "env.sh"
    _write(path, "export AGENTALLOY_A=1", "export AGENTALLOY_B=2")
    assert update_env_vars(path, {"AGENTALLOY_B": "20"}) is True
    assert path.read_text() == "export AGENTALLOY_A=1\nexport AGENTALLOY_B=20\n"


def test_update_env_vars_appends_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "env.sh"
    _write(path, "export AGENTALLOY_A=1")
    assert update_env_vars(path, {"AGENTALLOY_C": "3"}) is True
    assert path.read_text() == "export AGENTALLOY_A=1\nexport AGENTALLOY_C=3\n"


def test_update_env_vars_missing_file_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "nope.sh"
    assert update_env_vars(path, {"AGENTALLOY_A": "1"}) is False
    assert not path.exists()
