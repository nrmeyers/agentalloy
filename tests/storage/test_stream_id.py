from pathlib import Path

from agentalloy.storage.stream_id import bind_stream_id, resolve_stream_id


def test_resolve_stream_id_falls_back_to_path_hash(tmp_path: Path) -> None:
    stream_id = resolve_stream_id(tmp_path)
    assert stream_id == resolve_stream_id(tmp_path)
    assert len(stream_id) == 16


def test_resolve_stream_id_distinguishes_worktrees(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert resolve_stream_id(tmp_path) != resolve_stream_id(other)


def test_resolve_stream_id_env_var_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "from-env")
    assert resolve_stream_id(tmp_path) == "from-env"


def test_bind_stream_id_takes_priority_over_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "from-env")
    bind_stream_id(tmp_path, "from-file")
    assert resolve_stream_id(tmp_path) == "from-file"


def test_bind_stream_id_writes_binding_file(tmp_path: Path) -> None:
    bind_stream_id(tmp_path, "pinned")
    assert (tmp_path / ".agentalloy" / ".stream").read_text() == "pinned\n"
