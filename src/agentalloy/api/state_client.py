"""Thin HTTP client for the local phase state service.

All state reads and writes go through the service's HTTP API.  When the
service is unreachable, reads return ``None`` and writes raise
``StateClientError`` — there is no file-mirror fallback.  This ensures
fail-loud behaviour (spec A4): a mutation attempt with the service down
exits non-zero with a message naming the service, and nothing is written.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StateClientError(Exception):
    """Raised when an HTTP call to the state service fails."""

    message: str
    status: int | None = None


@dataclass
class StateClient:
    """Thin HTTP client for the local phase state service.

    Methods that perform a POST return a dict parsed from the JSON
    response.  When the service is unreachable they raise
    ``StateClientError`` — there is no file-mirror fallback.

    The base URL is configured via the ``STATE_SERVICE_URL`` environment
    variable (useful for tests that spin up a fake service).  When the
    ``base_url`` dataclass field is passed explicitly it takes priority.
    """

    base_url: str | None = None
    _timeout: float = 5.0

    def __post_init__(self) -> None:
        if self.base_url is None:
            object.__setattr__(
                self,
                "base_url",
                os.environ.get("STATE_SERVICE_URL", "http://localhost:8400"),
            )

    def is_running(self) -> bool:
        """Return True if the service responds to a health check."""
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            urllib.request.urlopen(req, timeout=1.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # -- write operations ------------------------------------------------

    def set_phase(self, value: str) -> dict[str, Any]:
        """Set the current phase via the service."""
        return self._post("/state/phase", {"value": value})

    def set_phase_with_contract(self, value: str, contract: dict[str, Any]) -> dict[str, Any]:
        """Advance phase and store a contract in a single transaction.

        Both writes commit or roll back together.  Raises ``StateClientError``
        if the service rejects the payload or rolls back.
        """
        return self._post("/state/phase", {"value": value, "contract": contract})

    def approve(self, phase: str) -> dict[str, Any]:
        """Record an approval for the given phase."""
        return self._post("/state/approve", {"value": phase})

    def set_cursor(self, value: str) -> dict[str, Any]:
        """Set the work-item cursor via the service."""
        return self._post("/state/cursor", {"value": value})

    # -- read operations -------------------------------------------------

    def get_state(self, kind: str) -> str | None:
        """Read a state kind (phase, cursor, approved) from the service.

        Returns the raw string body on success, or ``None`` when the
        service is down.
        """
        try:
            resp = urllib.request.urlopen(f"{self.base_url}/state/{kind}", timeout=self._timeout)
            return resp.read().decode()
        except (urllib.error.URLError, OSError):
            return None

    # -- contract operations ------------------------------------------------

    def create_contract(self, contract: dict[str, Any]) -> dict[str, Any]:
        """Create a contract via the service."""
        return self._post("/contracts", contract)

    def get_contract(self, contract_id: str) -> dict[str, Any] | None:
        """Read a contract by ID.  Returns None if not found."""
        try:
            resp = urllib.request.urlopen(
                f"{self.base_url}/contracts/{contract_id}", timeout=self._timeout
            )
            return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def list_contracts(
        self,
        *,
        phase: str | None = None,
        slug: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List contracts with optional filters."""
        params: list[tuple[str, str]] = []
        if phase is not None:
            params.append(("phase", phase))
        if slug is not None:
            params.append(("slug", slug))
        if status is not None:
            params.append(("status", status))

        qs = urllib.parse.urlencode(params) if params else ""
        url = f"{self.base_url}/contracts?{qs}" if qs else f"{self.base_url}/contracts"
        try:
            resp = urllib.request.urlopen(url, timeout=self._timeout)
            data = json.loads(resp.read().decode())
            return data.get("contracts", [])
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def patch_contract(self, contract_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """In-place correction of a contract."""
        req = urllib.request.Request(
            f"{self.base_url}/contracts/{contract_id}",
            data=json.dumps(updates).encode("utf-8"),
            method="PATCH",
        )
        req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
            return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def archive_contract(self, contract_id: str) -> dict[str, Any]:
        """Archive a contract."""
        return self._post(f"/contracts/{contract_id}/archive", {})

    def supersede_contract(self, contract_id: str, new_contract: dict[str, Any]) -> dict[str, Any]:
        """Supersede a contract with a new revision."""
        return self._post(f"/contracts/{contract_id}/supersede", new_contract)

    # -- internal helpers ------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
            raw = resp.read().decode()
            # Some endpoints return a plain result string; wrap in a dict.
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return {"result": raw}
        except urllib.error.HTTPError as exc:
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def _get(self, path: str) -> str:
        """Return the raw response body for a GET request."""
        resp = urllib.request.urlopen(f"{self.base_url}{path}", timeout=self._timeout)
        return resp.read().decode()
