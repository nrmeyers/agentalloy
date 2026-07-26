"""Thin HTTP client for the local phase state service.

Provides a simple check-then-route pattern for CLI subcommands:
when the service is running, state mutations go through the HTTP API;
when it is down, the caller falls back to direct file writes.
"""

from __future__ import annotations

import json
import os
import urllib.error
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
    ``StateClientError`` so the caller can fall back to file-mirror
    writes.

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

    def approve(self, phase: str) -> dict[str, Any]:
        """Record an approval for the given phase."""
        return self._post("/state/approved", {"value": phase})

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
            raise StateClientError(f"HTTP {exc.code}: {exc.reason}", status=exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(str(exc)) from exc

    def _get(self, path: str) -> str:
        """Return the raw response body for a GET request."""
        resp = urllib.request.urlopen(f"{self.base_url}{path}", timeout=self._timeout)
        return resp.read().decode()

    # -- state-mirror helpers (file fallback) ----------------------------

    def _read_phase_file(self, root: Any) -> dict[str, Any] | None:
        """Read the phase file as a fallback when the service is down.

        Mirrors ``phase._read_phase`` so the client can serve reads
        without importing the phase module directly.
        """
        from agentalloy.install.subcommands.phase import (  # noqa: PLC0415
            _read_phase,  # pyright: ignore[reportPrivateUsage]
        )

        return _read_phase(root)  # type: ignore[no-any-return]

    def _write_phase_file(
        self, root: Any, data: dict[str, Any], *, force: bool = False
    ) -> dict[str, Any]:
        """Write the phase file as a fallback when the service is down.

        Mirrors ``phase.run_phase_set`` so the client can serve
        writes without importing the phase module directly.
        """
        from agentalloy.install.subcommands.phase import (  # noqa: PLC0415
            run_phase_set,  # pyright: ignore[reportPrivateUsage]
        )

        return run_phase_set(data["value"], root=root, force=force)  # type: ignore[no-any-return]
