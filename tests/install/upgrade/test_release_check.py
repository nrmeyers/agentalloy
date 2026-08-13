"""Unit tests for the release-update check (src/agentalloy/install/release_check.py).

Fully offline: the GitHub API (urllib) and the clock are mocked, and the cache is
redirected into a tmp dir via ``XDG_DATA_HOME`` so nothing touches real state.
Covers the version helpers, the throttled producer, the ``notice()`` decision
matrix, dismissal, and the one-time opt-out gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install import release_check as rc


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect user_data_dir() → tmp so the cache file never touches real state,
    # and clear any inherited toggle so "enabled" is the default.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTALLOY_RELEASE_CHECK", raising=False)


def _urlopen_for(body: dict[str, object]) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda *a: False
    return MagicMock(return_value=resp)


def _seed_cache(**kw: object) -> None:
    base: dict[str, object] = {"enabled": True, "latest_tag": None, "dismissed_version": None}
    base.update(kw)
    rc._write_cache(base)


# --- version helpers --------------------------------------------------------


def test_parse_semver_strips_v_and_extras() -> None:
    assert rc.parse_semver("v3.7.0") == (3, 7, 0)
    assert rc.parse_semver("3.7") == (3, 7, 0)
    assert rc.parse_semver("3.7.1-rc2") == (3, 7, 1)


def test_bump_type_classifies() -> None:
    assert rc.bump_type("3.7.0", "4.0.0") == "major"
    assert rc.bump_type("3.7.0", "3.8.0") == "minor"
    assert rc.bump_type("3.7.0", "3.7.1") == "patch"
    assert rc.bump_type("3.7.0", "3.7.0") == ""
    assert rc.bump_type("3.7.0", "3.6.0") == ""  # behind → not a bump


# --- fetch (relocated from test_upgrade) ------------------------------------


def test_fetch_latest_tag_parses_tag_name() -> None:
    with patch.object(rc.urllib.request, "urlopen", _urlopen_for({"tag_name": "v3.8.0"})):
        assert rc.fetch_latest_tag() == "v3.8.0"


def test_fetch_latest_tag_offline_returns_none() -> None:
    with patch.object(rc.urllib.request, "urlopen", side_effect=OSError("offline")):
        assert rc.fetch_latest_tag() is None


def test_fetch_release_info_returns_fields() -> None:
    body = {
        "tag_name": "v3.8.0",
        "name": "Big release",
        "html_url": "https://example/r",
        "body": "notes",
        "published_at": "2026-06-26T00:00:00Z",
    }
    with patch.object(rc.urllib.request, "urlopen", _urlopen_for(body)):
        info = rc.fetch_release_info()
    assert info == {
        "tag": "v3.8.0",
        "name": "Big release",
        "html_url": "https://example/r",
        "body": "notes",
        "published_at": "2026-06-26T00:00:00Z",
    }


def test_fetch_release_info_offline_returns_none() -> None:
    with patch.object(rc.urllib.request, "urlopen", side_effect=OSError):
        assert rc.fetch_release_info(ref="v3.8.0") is None


# --- refresh: throttle / force / disabled / offline -------------------------


def test_refresh_fetches_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "fetch_latest_tag", lambda timeout=10.0: "v3.8.0")
    monkeypatch.setattr(rc.time, "time", lambda: 1000.0)
    out = rc.refresh()
    assert out["latest_tag"] == "v3.8.0"
    assert out["checked_at"] == 1000.0
    assert out["enabled"] is True
    assert rc.read_cache()["latest_tag"] == "v3.8.0"


def test_refresh_throttles_within_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_fetch(timeout: float = 10.0) -> str:
        calls.append(1)
        return "v3.8.0"

    now = [1000.0]
    monkeypatch.setattr(rc, "fetch_latest_tag", fake_fetch)
    monkeypatch.setattr(rc.time, "time", lambda: now[0])
    rc.refresh()
    now[0] = 1000.0 + rc.CHECK_INTERVAL_SECONDS - 1  # still within the window
    rc.refresh()
    assert calls == [1]  # second call served from cache
    rc.refresh(force=True)
    assert calls == [1, 1]  # force bypasses the throttle


def test_refresh_disabled_skips_network_and_marks_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_RELEASE_CHECK", "0")
    fetched: list[int] = []
    monkeypatch.setattr(rc, "fetch_latest_tag", lambda timeout=10.0: fetched.append(1) or "v9.9.9")
    out = rc.refresh()
    assert out.get("enabled") is False
    assert fetched == []  # never hit the network
    assert rc.notice() is None


def test_refresh_offline_keeps_prior_tag_but_stamps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "fetch_latest_tag", lambda timeout=10.0: "v3.8.0")
    monkeypatch.setattr(rc.time, "time", lambda: 1000.0)
    rc.refresh()
    # Go offline and force a re-check past the interval.
    later = 1000.0 + rc.CHECK_INTERVAL_SECONDS + 1
    monkeypatch.setattr(rc, "fetch_latest_tag", lambda timeout=10.0: None)
    monkeypatch.setattr(rc.time, "time", lambda: later)
    out = rc.refresh()
    assert out["latest_tag"] == "v3.8.0"  # prior value retained
    assert out["checked_at"] == later  # throttle still advances (no busy-loop)


# --- notice(): decision matrix ----------------------------------------------


def test_notice_update_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "current_version", lambda: "3.7.0")
    _seed_cache(latest_tag="v3.8.0")
    assert rc.notice() == {"current": "3.7.0", "latest": "v3.8.0", "bump_type": "minor"}


def test_notice_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "current_version", lambda: "3.8.0")
    _seed_cache(latest_tag="v3.8.0")
    assert rc.notice() is None


def test_notice_dismissed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "current_version", lambda: "3.7.0")
    _seed_cache(latest_tag="v3.8.0", dismissed_version="v3.8.0")
    assert rc.notice() is None


def test_notice_disabled_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "current_version", lambda: "3.7.0")
    _seed_cache(latest_tag="v3.8.0", enabled=False)
    assert rc.notice() is None


def test_notice_unknown_source_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "current_version", lambda: "0.0.0+unknown")
    _seed_cache(latest_tag="v3.8.0")
    assert rc.notice() is None


def test_notice_no_cache_is_none() -> None:
    assert rc.notice() is None


# --- dismiss + opt-out ------------------------------------------------------


def test_dismiss_persists_and_silences(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "current_version", lambda: "3.7.0")
    _seed_cache(latest_tag="v3.8.0")
    rc.dismiss("v3.8.0")
    assert rc.read_cache()["dismissed_version"] == "v3.8.0"
    assert rc.notice() is None


def test_take_optout_notice_fires_once() -> None:
    assert rc.take_optout_notice() is True
    assert rc.take_optout_notice() is False


def test_take_optout_notice_silent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_RELEASE_CHECK", "off")
    assert rc.take_optout_notice() is False
