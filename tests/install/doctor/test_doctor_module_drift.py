"""Doctor module-drift check (spec AC 7, container-module-env-propagation).

Forwarded env binds at container create; a later .env edit is inert until a
recreate. `_check_module_drift` compares host .env intent against the running
container's /health modules block and names both sides + the exact fix.
"""

from agentalloy.install.subcommands.doctor import _check_module_drift


def _health(**modules: str) -> dict:
    return {"status": "ok", "modules": modules}


def test_flags_env_enabled_container_disabled():
    check = _check_module_drift(
        {"CODE_INDEX_ENABLED": "1"}, _health(compose="enabled", code_index="disabled")
    )
    assert check["passed"] is False
    assert "CODE_INDEX_ENABLED=1" in check["error"]
    assert "modules.code_index=disabled" in check["error"]
    assert "upgrade --recreate-only" in check["remediation"]


def test_flags_env_disabled_container_enabled():
    check = _check_module_drift(
        {"CODE_INDEX_ENABLED": "0"}, _health(compose="enabled", code_index="enabled")
    )
    assert check["passed"] is False
    assert "CODE_INDEX_ENABLED=0" in check["error"]
    assert "modules.code_index=enabled" in check["error"]


def test_absent_toggle_uses_module_default():
    """No CODE_INDEX_ENABLED in .env → default off; an enabled container drifts."""
    check = _check_module_drift({}, _health(compose="enabled", code_index="enabled"))
    assert check["passed"] is False
    assert "unset" in check["error"]


def test_silent_when_agreeing():
    check = _check_module_drift(
        {"CODE_INDEX_ENABLED": "1"}, _health(compose="enabled", code_index="enabled")
    )
    assert check["passed"] is True
    assert "error" not in check
    assert "agree" in check["detail"]


def test_compose_drift_detected_symmetrically():
    check = _check_module_drift(
        {"COMPOSE_ENABLED": "0"}, _health(compose="enabled", code_index="disabled")
    )
    assert check["passed"] is False
    assert "modules.compose=enabled" in check["error"]


def test_unavailable_is_not_drift():
    """Packaging failure (extra missing) is the code_index check's finding."""
    check = _check_module_drift(
        {"CODE_INDEX_ENABLED": "1"}, _health(compose="enabled", code_index="unavailable")
    )
    assert check["passed"] is True


def test_older_image_without_modules_block():
    check = _check_module_drift({"CODE_INDEX_ENABLED": "1"}, {"status": "ok"})
    assert check["passed"] is True


def test_unreachable_service_warns_not_fails():
    check = _check_module_drift({"CODE_INDEX_ENABLED": "1"}, None)
    assert check["passed"] is True
    assert check["severity"] == "warn"
