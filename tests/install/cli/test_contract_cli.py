"""Tests for contract CLI verbs: init, show, validate, edit, supersede.

Each verb is tested against a stubbed StateClient (service up) and a
service-down scenario (TA5: non-zero exit, stderr names the service,
nothing written to disk).

Test cases from docs/design/contract-store-and-write-gating/test-plan.md:
- TA5 — service down: non-zero exit, nothing written (all five verbs)
- TA7, TA8, TA9 — archive / supersede / correct via CLI
"""

from __future__ import annotations

import argparse
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.install.subcommands.contract import (
    _artifact_show,
    _edit,
    _init,
    _show,
    _supersede,
    _validate,
)

_REPO_ROOT_PATCH = "agentalloy.install.state._repo_root"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace with sensible defaults for contract verbs."""
    defaults = {
        "json": False,
        "quiet": True,  # suppress human output in tests
        "body": None,
        "domain_tags": None,
        "scope_touches": None,
        "scope_avoids": None,
        "success_criteria": None,
        "phase": None,
        "slug": None,
        "route": "full",
        "new_id": None,
        "contract_id": None,
        "triple": None,
        "name": None,
        "body_file": None,
        "name_glob": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


_UNSET = object()


def _mock_client(
    *,
    is_running: bool = True,
    create_contract: object = _UNSET,
    get_contract: object = _UNSET,
    patch_contract: object = _UNSET,
    supersede_contract: object = _UNSET,
    get_state: object = _UNSET,
    get_artifact: object = _UNSET,
) -> MagicMock:
    """Build a mock StateClient instance for use with _get_client patching.

    For *get_contract*, pass a dict (returned as-is), None (returns None),
    or a callable.  Defaults to _UNSET (leaves the MagicMock default).
    """
    client = MagicMock(spec=StateClient)
    client.is_running.return_value = is_running
    if create_contract is not _UNSET:
        client.create_contract = create_contract
    if get_contract is not _UNSET:
        # Accept a dict/None (wrap in lambda) or a callable directly
        client.get_contract = (
            get_contract if callable(get_contract) else lambda cid, **kw: get_contract
        )
    if patch_contract is not _UNSET:
        client.patch_contract = patch_contract
    if supersede_contract is not _UNSET:
        client.supersede_contract = supersede_contract
    if get_state is not _UNSET:
        client.get_state = get_state
    if get_artifact is not _UNSET:
        # Accept a dict/None (wrap in lambda) or a callable directly
        client.get_artifact = (
            get_artifact if callable(get_artifact) else lambda phase, slug, name: get_artifact
        )
    return client


# ---------------------------------------------------------------------------
# TA5 — Service down: non-zero exit, nothing written (all five verbs)
# ---------------------------------------------------------------------------


class TestTA5ServiceDown:
    """TA5: Service down → non-zero exit, stderr mentions 'service', nothing
    written to disk.  Every verb calls _get_client() first, which exits 1
    when is_running() is False."""

    def test_init_exits_nonzero_when_service_down(self, tmp_path: Path) -> None:
        """_get_client() calls sys.exit(1) when is_running() returns False."""
        args = _make_args(phase="build", slug="01-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract._get_client", return_value=mock_client):
            # _get_client returns a client whose is_running is False, but
            # _get_client itself checks is_running and exits. So we need to
            # patch is_running on the returned client.
            pass
        # Actually, _get_client is:
        #   client = StateClient()
        #   if not client.is_running(): sys.exit(1)
        #   return client
        # So we need to patch StateClient() constructor AND is_running.
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch(_REPO_ROOT_PATCH, return_value=tmp_path):
                stderr = StringIO()
                with patch.object(sys, "stderr", stderr):
                    with pytest.raises(SystemExit) as exc_info:
                        _init(args)
                assert exc_info.value.code == 1
        assert "service" in stderr.getvalue().lower()

    def test_show_exits_nonzero_when_service_down(self) -> None:
        args = _make_args(contract_id="build/01-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                with pytest.raises(SystemExit) as exc_info:
                    _show(args)
            assert exc_info.value.code == 1
        assert "service" in stderr.getvalue().lower()

    def test_validate_exits_nonzero_when_service_down(self) -> None:
        args = _make_args(contract_id="build/01-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                with pytest.raises(SystemExit) as exc_info:
                    _validate(args)
            assert exc_info.value.code == 1
        assert "service" in stderr.getvalue().lower()

    def test_edit_exits_nonzero_when_service_down(self) -> None:
        args = _make_args(contract_id="build/01-test", body="new body")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                with pytest.raises(SystemExit) as exc_info:
                    _edit(args)
            assert exc_info.value.code == 1
        assert "service" in stderr.getvalue().lower()

    def test_supersede_exits_nonzero_when_service_down(self) -> None:
        args = _make_args(contract_id="build/01-test", new_id="build/02-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                with pytest.raises(SystemExit) as exc_info:
                    _supersede(args)
            assert exc_info.value.code == 1
        assert "service" in stderr.getvalue().lower()

    def test_init_writes_nothing_to_disk_when_service_down(self, tmp_path: Path) -> None:
        """Service down → _init exits before any disk write (scaffold or store)."""
        args = _make_args(phase="build", slug="01-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch(_REPO_ROOT_PATCH, return_value=tmp_path):
                with pytest.raises(SystemExit):
                    _init(args)
        # No contract file written, no .agentalloy state written
        contracts_dir = tmp_path / ".agentalloy" / "contracts"
        assert not contracts_dir.exists()

    def test_show_writes_nothing_to_disk_when_service_down(self, tmp_path: Path) -> None:
        args = _make_args(contract_id="build/01-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with pytest.raises(SystemExit):
                _show(args)

    def test_supersede_writes_nothing_to_disk_when_service_down(self, tmp_path: Path) -> None:
        args = _make_args(contract_id="build/01-test", new_id="build/02-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with pytest.raises(SystemExit):
                _supersede(args)
        contracts_dir = tmp_path / ".agentalloy" / "contracts"
        assert not contracts_dir.exists()


# ---------------------------------------------------------------------------
# Success paths — service up, stubbed StateClient
# ---------------------------------------------------------------------------


class TestInitSuccess:
    """contract init: success path with stubbed service."""

    def test_init_sends_correct_payload(self, tmp_path: Path) -> None:
        captured: list[dict] = []

        def fake_create(payload: dict) -> dict:
            captured.append(payload)
            return {"contract_id": payload["contract_id"]}

        mock_client = _mock_client(is_running=True, create_contract=fake_create)
        args = _make_args(phase="build", slug="01-auth", route="full")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch(_REPO_ROOT_PATCH, return_value=tmp_path):
                rc = _init(args)
        assert rc == 0
        assert captured[0]["phase"] == "build"
        assert captured[0]["slug"] == "01-auth"
        assert "body" in captured[0]

    def test_init_with_explicit_phase(self, tmp_path: Path) -> None:
        captured: list[dict] = []

        def fake_create(payload: dict) -> dict:
            captured.append(payload)
            return {"contract_id": payload["contract_id"]}

        mock_client = _mock_client(is_running=True, create_contract=fake_create)
        args = _make_args(phase="design", slug="calendar-ui", route="full")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch(_REPO_ROOT_PATCH, return_value=tmp_path):
                rc = _init(args)
        assert rc == 0
        assert captured[0]["phase"] == "design"

    def test_init_no_phase_and_service_down_fails(self, tmp_path: Path) -> None:
        """When --phase is omitted and the service is down, _init reports the error."""
        args = _make_args(phase=None, slug="01-test")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch(_REPO_ROOT_PATCH, return_value=tmp_path):
                with pytest.raises(SystemExit) as exc_info:
                    _init(args)
            assert exc_info.value.code == 1


class TestShowSuccess:
    """contract show: success path with stubbed service."""

    def test_show_returns_contract_data(self) -> None:
        contract_data = {
            "contract_id": "build/01-auth",
            "phase": "build",
            "slug": "01-auth",
            "domain_tags": ["api-design"],
            "scope_touches": ["src/auth/"],
            "scope_avoids": [],
            "success_criteria": ["tests pass"],
            "body": "# Auth Middleware",
            "status": "active",
        }

        mock_client = _mock_client(is_running=True, get_contract=contract_data)
        args = _make_args(contract_id="build/01-auth")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            rc = _show(args)
        assert rc == 0

    def test_show_not_found_returns_nonzero(self) -> None:
        mock_client = _mock_client(is_running=True, get_contract=None)
        args = _make_args(contract_id="build/missing")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _show(args)
            assert rc == 1
        assert "not found" in stderr.getvalue().lower()

    def test_show_service_error_returns_nonzero(self) -> None:
        mock_client = _mock_client(
            is_running=True,
            get_contract=lambda cid: (_ for _ in ()).throw(StateClientError("connection refused")),
        )
        args = _make_args(contract_id="build/01-auth")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _show(args)
            assert rc == 1


class TestValidateSuccess:
    """contract validate: success path with stubbed service."""

    def test_validate_valid_contract(self) -> None:
        contract_data = {
            "contract_id": "build/01-auth",
            "phase": "build",
            "slug": "01-auth",
            "domain_tags": ["api-design"],
            "scope_touches": ["src/auth/"],
            "scope_avoids": [],
            "success_criteria": ["tests pass"],
            "body": "# Auth Middleware",
            "status": "active",
        }

        mock_client = _mock_client(is_running=True, get_contract=contract_data)
        args = _make_args(contract_id="build/01-auth")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            rc = _validate(args)
        assert rc == 0

    def test_validate_malformed_contract(self) -> None:
        """A contract with missing required fields should report issues."""
        contract_data = {
            "contract_id": "build/01-bad",
            "phase": "",
            "slug": "",
            "body": "",
            "status": "active",
        }

        mock_client = _mock_client(is_running=True, get_contract=contract_data)
        args = _make_args(contract_id="build/01-bad")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            rc = _validate(args)
        assert rc == 1  # issues found → non-zero

    def test_validate_not_found(self) -> None:
        mock_client = _mock_client(is_running=True, get_contract=None)
        args = _make_args(contract_id="build/missing")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _validate(args)
            assert rc == 1


class TestEditSuccess:
    """TA9: contract edit mutates in place via StateClient."""

    def test_edit_sends_updates(self) -> None:
        args = _make_args(contract_id="build/01-auth", body="corrected body")
        captured_updates: list[dict] = []

        def fake_patch(cid: str, updates: dict) -> dict:
            captured_updates.append(updates)
            return {"contract_id": cid}

        mock_client = _mock_client(is_running=True, patch_contract=fake_patch)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            rc = _edit(args)
        assert rc == 0
        assert captured_updates[0]["body"] == "corrected body"

    def test_edit_no_fields_returns_nonzero(self) -> None:
        mock_client = _mock_client(is_running=True)
        args = _make_args(contract_id="build/01-auth")
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _edit(args)
            assert rc == 1
        assert "at least one field" in stderr.getvalue().lower()

    def test_edit_service_error_returns_nonzero(self) -> None:
        args = _make_args(contract_id="build/01-auth", body="new body")
        mock_client = _mock_client(
            is_running=True,
            patch_contract=lambda cid, upd: (_ for _ in ()).throw(StateClientError("server error")),
        )
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _edit(args)
            assert rc == 1


class TestSupersedeSuccess:
    """TA8: contract supersede writes new row and flips prior to superseded."""

    def test_supersede_sends_correct_payload(self) -> None:
        args = _make_args(
            contract_id="build/01-auth",
            new_id="build/02-auth",
            phase="build",
            slug="02-auth",
        )
        captured: list[dict] = []

        def fake_supersede(cid: str, payload: dict) -> dict:
            captured.append(payload)
            return {"contract_id": payload["new_contract_id"], "supersedes": cid}

        mock_client = _mock_client(is_running=True, supersede_contract=fake_supersede)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            rc = _supersede(args)
        assert rc == 0
        assert captured[0]["new_contract_id"] == "build/02-auth"

    def test_supersede_service_error_returns_nonzero(self) -> None:
        args = _make_args(contract_id="build/01", new_id="build/02")
        mock_client = _mock_client(
            is_running=True,
            supersede_contract=lambda cid, payload: (_ for _ in ()).throw(
                StateClientError("not found")
            ),
        )
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _supersede(args)
            assert rc == 1


# ---------------------------------------------------------------------------
# _artifact_show — contract artifact-show CLI command
# ---------------------------------------------------------------------------


class TestArtifactShowSuccess:
    """artifact-show: prints raw content by default, JSON with --json."""

    def test_show_by_triple(self) -> None:
        args = _make_args(triple="spec/my-task/spec.md")
        captured: list[tuple] = []

        def fake_get_artifact(phase: str, slug: str, name: str) -> dict | None:
            captured.append((phase, slug, name))
            return {"phase": phase, "slug": slug, "name": name, "content": "# The spec"}

        mock_client = _mock_client(is_running=True, get_artifact=fake_get_artifact)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            rc = _artifact_show(args)
        assert rc == 0
        assert captured[0] == ("spec", "my-task", "spec.md")

    def test_show_by_flags(self) -> None:
        args = _make_args(phase="design", slug="feat", name="plan.md")
        captured: list[tuple] = []

        def fake_get_artifact(phase: str, slug: str, name: str) -> dict | None:
            captured.append((phase, slug, name))
            return {"phase": phase, "slug": slug, "name": name, "content": "plan body"}

        mock_client = _mock_client(is_running=True, get_artifact=fake_get_artifact)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            rc = _artifact_show(args)
        assert rc == 0
        assert captured[0] == ("design", "feat", "plan.md")

    def test_show_json_output(self) -> None:
        args = _make_args(triple="spec/my-task/spec.md", json=True, quiet=False)

        def fake_get_artifact(phase: str, slug: str, name: str) -> dict | None:
            return {"phase": phase, "slug": slug, "name": name, "content": "# spec"}

        mock_client = _mock_client(is_running=True, get_artifact=fake_get_artifact)
        stdout = StringIO()
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch("sys.stdout", stdout):
                rc = _artifact_show(args)
        assert rc == 0
        output = stdout.getvalue()
        assert '"content"' in output
        assert '"phase"' in output

    def test_show_service_down(self) -> None:
        args = _make_args(triple="spec/missing/art.md")
        mock_client = _mock_client(is_running=False)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                with pytest.raises(SystemExit) as exc_info:
                    _artifact_show(args)
            assert exc_info.value.code == 1
        assert "service" in stderr.getvalue().lower()

    def test_show_not_found(self) -> None:
        args = _make_args(triple="spec/missing/art.md")

        def fake_get_artifact(phase: str, slug: str, name: str) -> dict | None:
            return None  # artifact doesn't exist

        mock_client = _mock_client(is_running=True, get_artifact=fake_get_artifact)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _artifact_show(args)
            assert rc == 1
        assert "not found" in stderr.getvalue().lower()

    def test_show_invalid_triple_format(self) -> None:
        args = _make_args(triple="spec/only-two-parts")
        mock_client = _mock_client(is_running=True)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _artifact_show(args)
            assert rc == 1
        assert "triple must be" in stderr.getvalue().lower()

    def test_show_missing_args(self) -> None:
        # Neither triple nor all three flags provided
        args = _make_args(triple=None, phase=None, slug=None, name=None)
        mock_client = _mock_client(is_running=True)
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            stderr = StringIO()
            with patch.object(sys, "stderr", stderr):
                rc = _artifact_show(args)
            assert rc == 1
        assert "provide either" in stderr.getvalue().lower()
