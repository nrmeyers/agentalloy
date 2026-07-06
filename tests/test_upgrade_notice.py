"""New-module upgrade notice (spec AC 6, container-module-env-propagation).

`_module_notices` diffs MODULE_TOGGLES against the user .env: a default-off
module whose toggle is absent gets exactly one non-interactive line naming
the module and the enable command; a toggle present (either value) means the
user has decided; a default-on module is already running — no notice.
"""

from pathlib import Path

import pytest

from agentalloy.install.subcommands.upgrade import _module_notices


@pytest.fixture
def user_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg_dir = tmp_path / "agentalloy"
    cfg_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def write(content: str | None) -> None:
        if content is not None:
            (cfg_dir / ".env").write_text(content)

    return write


def test_notice_when_code_index_toggle_absent(user_env):
    user_env("LOG_LEVEL=info\n")
    notices = _module_notices()
    assert len(notices) == 1
    assert "codebase indexer" in notices[0]
    assert "CODE_INDEX_ENABLED=1" in notices[0]


def test_notice_when_env_missing_entirely(user_env):
    user_env(None)
    assert len(_module_notices()) == 1


def test_no_notice_when_toggle_present_enabled(user_env):
    user_env("CODE_INDEX_ENABLED=1\n")
    assert _module_notices() == []


def test_no_notice_when_toggle_present_disabled(user_env):
    """Either value counts as a decision — don't nag a deliberate opt-out."""
    user_env("CODE_INDEX_ENABLED=0\n")
    assert _module_notices() == []


def test_no_notice_for_default_on_module(user_env):
    """COMPOSE_ENABLED absent → module already running; nothing to announce."""
    user_env("CODE_INDEX_ENABLED=1\n")  # only the default-off toggle present
    assert _module_notices() == []
