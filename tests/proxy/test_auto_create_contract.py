"""Auto-create next-phase contract tests.

Tests the ``_auto_create_next_contract`` helper that creates a contract in the
target phase after a phase transition, carrying forward slug and scope from
the current contract.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from agentalloy.api.proxy_signal import _auto_create_next_contract


class TestAutoCreateNextContract:
    def test_skips_non_auto_create_phases(self) -> None:
        store = MagicMock()
        _auto_create_next_contract(Path("/tmp"), "build", "spec/auth", store)
        store.get_contract.assert_not_called()

    def test_skips_when_no_current_contract(self) -> None:
        store = MagicMock()
        _auto_create_next_contract(Path("/tmp"), "design", None, store)
        store.get_contract.assert_not_called()

    def test_skips_when_no_store(self) -> None:
        _auto_create_next_contract(Path("/tmp"), "design", "spec/auth", None)

    def test_creates_contract_for_design_phase(self) -> None:
        store = MagicMock()
        store.get_contract.return_value = {
            "contract_id": "auth-refactor",
            "slug": "auth-refactor",
            "phase": "spec",
            "domain_tags": '["fastapi"]',
            "scope_touches": '["src/auth/**"]',
            "scope_avoids": "[]",
            "body": "Spec body",
        }
        store.list_contracts.return_value = []  # no existing contract

        with patch("agentalloy.signals.skill_loader._write_cursor_atomic") as mock_cursor:
            _auto_create_next_contract(Path("/tmp"), "design", "auth-refactor", store)

        store.put_contract.assert_called_once()
        call_kwargs = store.put_contract.call_args
        # Phase-scoped id: a bare slug would upsert over the same work-item's
        # contract in other phases (put_contract keys on contract_id alone).
        assert call_kwargs[0][0] == "design/auth-refactor"  # contract_id
        assert call_kwargs[1]["phase"] == "design"
        assert call_kwargs[1]["slug"] == "auth-refactor"
        assert call_kwargs[1]["domain_tags"] == ["fastapi"]
        assert call_kwargs[1]["scope_touches"] == ["src/auth/**"]
        mock_cursor.assert_called_once()

    def test_skips_when_contract_already_exists(self) -> None:
        store = MagicMock()
        store.get_contract.return_value = {
            "contract_id": "auth-refactor",
            "slug": "auth-refactor",
            "phase": "spec",
        }
        store.list_contracts.return_value = [{"contract_id": "auth-refactor"}]

        _auto_create_next_contract(Path("/tmp"), "design", "auth-refactor", store)
        store.put_contract.assert_not_called()

    def test_skips_when_current_contract_not_found(self) -> None:
        store = MagicMock()
        store.get_contract.return_value = None

        _auto_create_next_contract(Path("/tmp"), "design", "nonexistent", store)
        store.put_contract.assert_not_called()

    def test_handles_json_parse_errors_softly(self) -> None:
        store = MagicMock()
        store.get_contract.return_value = {
            "contract_id": "auth",
            "slug": "auth",
            "phase": "spec",
            "domain_tags": "not-valid-json",
            "scope_touches": "also-bad",
            "scope_avoids": "[]",
        }
        store.list_contracts.return_value = []

        with patch("agentalloy.signals.skill_loader._write_cursor_atomic"):
            _auto_create_next_contract(Path("/tmp"), "design", "auth", store)

        # Should still create the contract with empty lists for unparseable fields
        store.put_contract.assert_called_once()
        call_kwargs = store.put_contract.call_args
        assert call_kwargs[1]["domain_tags"] == []
        assert call_kwargs[1]["scope_touches"] == []

    def test_store_failure_is_soft(self) -> None:
        store = MagicMock()
        store.get_contract.side_effect = RuntimeError("db error")

        # Should not raise
        _auto_create_next_contract(Path("/tmp"), "design", "auth", store)

    def test_creates_for_qa_phase(self) -> None:
        store = MagicMock()
        store.get_contract.return_value = {
            "contract_id": "task-01",
            "slug": "task-01",
            "phase": "build",
            "domain_tags": '["testing"]',
            "scope_touches": '["src/auth/**"]',
            "scope_avoids": "[]",
        }
        store.list_contracts.return_value = []

        with patch("agentalloy.signals.skill_loader._write_cursor_atomic"):
            _auto_create_next_contract(Path("/tmp"), "qa", "task-01", store)

        store.put_contract.assert_called_once()
        call_kwargs = store.put_contract.call_args
        assert call_kwargs[1]["phase"] == "qa"
