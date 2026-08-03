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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

DEFAULT_PORT_FALLBACK = 47950


def _configured_port() -> int:
    """Return the port the local service binds, per install state.

    Imported lazily: ``agentalloy.install`` pulls in the whole CLI surface,
    and this module is imported by the service itself.  Any failure to read
    state falls back to the shipped default rather than raising — a client
    that cannot read config should still point somewhere plausible.
    """
    try:
        from agentalloy.install import state as install_state

        st = install_state.load_state()
        return install_state.validate_port(st.get("port", DEFAULT_PORT_FALLBACK))
    except Exception:  # noqa: BLE001 — config read must never break client construction
        return DEFAULT_PORT_FALLBACK


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

    Resolution order for the base URL:

    1. the ``base_url`` dataclass field, when passed explicitly;
    2. the ``STATE_SERVICE_URL`` environment variable (useful for tests
       that spin up a fake service);
    3. the port recorded in install state — the same value the service
       itself binds — falling back to 47950.

    Step 3 matters: the default used to be a hard-coded ``:8400``, which no
    deployment has ever listened on.  Every ``is_running()`` therefore
    returned False and every store-aware CLI silently took its file-mirror
    path, so CLI writes and service writes stopped seeing each other.
    """

    base_url: str | None = None
    repo_root: str | None = None
    _timeout: float = 5.0

    def __post_init__(self) -> None:
        if self.base_url is None:
            object.__setattr__(
                self,
                "base_url",
                os.environ.get("STATE_SERVICE_URL") or f"http://127.0.0.1:{_configured_port()}",
            )
        if self.repo_root is None:
            object.__setattr__(self, "repo_root", str(Path.cwd()))

    def is_running(self) -> bool:
        """Return True if the service responds to a health check."""
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            urllib.request.urlopen(req, timeout=1.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # -- write operations ------------------------------------------------

    def set_phase(
        self,
        value: str,
        *,
        repo_root: str | None = None,
        actor: str | None = None,
        mode: str | None = None,
        free_since: str | None = None,
        override: bool = False,
    ) -> dict[str, Any]:
        """Set the current phase via the service.

        When *repo_root* is provided, it is forwarded as a query parameter
        so the server can rewrite the enforcement posture for wired Tier A
        harnesses (D1–D9) as part of the phase advance.

        ``mode``/``free_since`` are the free-flow pair.  Omitting them carries
        the stored values forward; passing ``""`` clears them, which is how
        ``flow resume`` leaves free-flow.  They are real fields rather than
        something smuggled inside *value*: ``flow free`` used to POST
        ``"free-flow:<phase>"`` as the phase name, which wrote a phase nothing
        recognises and had to skip the posture rewrite to avoid clearing the
        deny rules.
        """
        body: dict[str, Any] = {"value": value}
        if actor is not None:
            body["actor"] = actor
        if mode is not None:
            body["mode"] = mode
        if override:
            body["override"] = True
        if free_since is not None:
            body["free_since"] = free_since
        return self._post("/state/phase", body, repo_root=repo_root)

    def set_phase_with_contract(
        self, value: str, contract: dict[str, Any], *, repo_root: str | None = None
    ) -> dict[str, Any]:
        """Advance phase and store a contract in a single transaction.

        Both writes commit or roll back together.  Raises ``StateClientError``
        if the service rejects the payload or rolls back.

        When *repo_root* is provided, it is forwarded as a query parameter
        so the server can rewrite the enforcement posture for wired Tier A
        harnesses (D1–D9) as part of the phase advance.
        """
        return self._post(
            "/state/phase", {"value": value, "contract": contract}, repo_root=repo_root
        )

    def import_files(self, repo_root: str | None = None) -> dict[str, str]:
        """Migrate a repo's ``.agentalloy`` file mirror into the store.

        Returns the kinds imported by this call — empty when the repo has no
        file mirror left, which is the steady state after the first run.
        """
        resp = self._post("/state/import-files", {}, repo_root=repo_root)
        imported = resp.get("imported") or {}
        return imported if isinstance(imported, dict) else {}

    def approve(self, phase: str) -> dict[str, Any]:
        """Record an approval for the given phase."""
        return self._post("/state/approve", {"value": phase})

    def set_cursor(self, value: str) -> dict[str, Any]:
        """Set the work-item cursor via the service."""
        return self._post("/state/cursor", {"value": value})

    # -- read operations -------------------------------------------------

    def get_state(self, kind: str) -> str | None:
        """Read a state kind (phase, cursor, approved) from the service.

        Returns the **value**, or ``None`` when the service is down or the kind
        is unset.  It used to return the raw response body — that is, the whole
        ``{"kind": ..., "value": ...}`` envelope as a string — so every caller
        got JSON where it expected a bare token and rendered it verbatim.  A
        body that is not that envelope is returned as-is rather than discarded,
        so an older service still answers something usable.
        """
        try:
            resp = urllib.request.urlopen(self._url(f"/state/{kind}"), timeout=self._timeout)
            raw = resp.read().decode()
        except (urllib.error.URLError, OSError):
            return None
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
        if isinstance(body, dict) and "value" in body:
            value = body["value"]
            return None if value is None else str(value)
        return raw

    def get_phase(self, *, repo_root: str | None = None) -> dict[str, Any] | None:
        """Read the decoded phase row, or ``None`` when no phase is recorded.

        Distinct from ``get_state("phase")``, which returns only the bare name:
        the CLI renders ``mode``, ``free_since`` and the timestamps too, and
        reaching them was the last thing keeping the file mirror alive.

        Raises ``StateClientError`` when the service is unreachable.  A down
        service must never read as "this repo has no phase" — that is exactly
        how an outage used to look like a fresh repo and reset the workflow.
        """
        try:
            resp = urllib.request.urlopen(
                self._url("/state/phase", repo_root=repo_root), timeout=self._timeout
            )
            body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc
        if not isinstance(body, dict) or body.get("value") is None:
            return None
        return cast("dict[str, Any]", body)

    def delete_repo_rows(self, *, repo_root: str | None = None) -> int:
        """Drop all state + contract rows for a repo via the service.

        Idempotent — deleting an already-empty repo returns 0.
        """
        req = urllib.request.Request(self._url("/state/repo", repo_root=repo_root), method="DELETE")
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
            body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc
        return int(body.get("deleted_rows", 0)) if isinstance(body, dict) else 0

    def clear_phase(self, *, repo_root: str | None = None) -> None:
        """Delete the phase row.  Idempotent — clearing an absent phase is fine."""
        req = urllib.request.Request(
            self._url("/state/phase", repo_root=repo_root), method="DELETE"
        )
        try:
            urllib.request.urlopen(req, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    # -- contract operations ------------------------------------------------

    def create_contract(self, contract: dict[str, Any]) -> dict[str, Any]:
        """Create a contract via the service."""
        return self._post("/contracts", contract)

    def get_contract(self, contract_id: str) -> dict[str, Any] | None:
        """Read a contract by ID.  Returns None if not found."""
        try:
            resp = urllib.request.urlopen(
                self._url(f"/contracts/{contract_id}"), timeout=self._timeout
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

        url = self._url("/contracts", params)
        try:
            resp = urllib.request.urlopen(url, timeout=self._timeout)
            data = json.loads(resp.read().decode())
            return data.get("contracts", [])
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def patch_contract(self, contract_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """In-place correction of a contract."""
        req = urllib.request.Request(
            self._url(f"/contracts/{contract_id}"),
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

    def archive_all(self) -> dict[str, Any]:
        """Archive all active contracts and artifacts."""
        return self._post("/state/archive-all", {})

    def supersede_contract(self, contract_id: str, new_contract: dict[str, Any]) -> dict[str, Any]:
        """Supersede a contract with a new revision."""
        return self._post(f"/contracts/{contract_id}/supersede", new_contract)

    # -- artifact operations -------------------------------------------------

    def set_artifact(self, phase: str, slug: str, name: str, content: str) -> dict[str, Any]:
        """Upsert a deliverable artifact body via the service."""
        return self._put(
            "/state/artifact", {"phase": phase, "slug": slug, "name": name, "content": content}
        )

    def list_artifacts(
        self,
        phase: str,
        *,
        slug: str | None = None,
        name_glob: str | None = None,
    ) -> list[dict[str, Any]]:
        """List deliverable artifacts for a phase, optionally filtered."""
        params: list[tuple[str, str]] = [("phase", phase)]
        if slug is not None:
            params.append(("slug", slug))
        if name_glob is not None:
            params.append(("name_glob", name_glob))
        try:
            resp = urllib.request.urlopen(
                self._url("/state/artifact", params), timeout=self._timeout
            )
            data = json.loads(resp.read().decode())
            return data.get("artifacts", [])
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def get_artifact(self, phase: str, slug: str, name: str) -> dict[str, Any] | None:
        """Fetch a single artifact by (phase, slug, name), or None if absent."""
        path = f"/state/artifact/{urllib.parse.quote(phase, safe='')}/{urllib.parse.quote(slug, safe='')}/{urllib.parse.quote(name, safe='')}"
        req = urllib.request.Request(
            f"{self.base_url}{path}", headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise StateClientError(exc.read().decode()) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def set_approval(
        self, phase: str, artifact_digest: str, *, approver: str | None = None
    ) -> dict[str, Any]:
        """Record human approval for *phase* with the artifact digest it covers.

        Reuses the existing ``POST /state/approve`` write path (session_key
        scopes the "approved" kind's row to this phase) with a JSON value
        carrying the digest + timestamp — mirrors
        ``DuckDBStateStore.set_approval`` so in-process and HTTP callers agree
        on the value's shape.
        """
        value = json.dumps(
            {
                "artifact_digest": artifact_digest,
                "approver": approver,
                "approved_at": datetime.now(UTC).isoformat(),
            }
        )
        return self._post("/state/approve", {"value": value, "session_key": phase})

    def get_approval(self, phase: str) -> dict[str, Any] | None:
        """Fetch the recorded approval for *phase*, or None if never approved."""
        try:
            resp = urllib.request.urlopen(
                self._url("/state/approved", [("session_key", phase)]), timeout=self._timeout
            )
            data = json.loads(resp.read().decode())
            raw = data.get("value")
            if raw is None:
                return None
            try:
                return cast(dict[str, Any], json.loads(raw))
            except json.JSONDecodeError:
                return None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    def get_resume(self) -> dict[str, Any]:
        """Get assembled resume data for cold-session bootstrap.

        Returns a dict with phase, cursor_contract, owed_artifacts, and
        governing_decisions.  Raises ``StateClientError`` if the service
        is unreachable.
        """
        try:
            resp = urllib.request.urlopen(self._url("/state/resume"), timeout=self._timeout)
            return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise StateClientError(
                f"agentalloy service returned HTTP {exc.code}: {exc.reason}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StateClientError(f"agentalloy service is not running ({exc})") from exc

    # -- internal helpers ------------------------------------------------

    def _url(
        self,
        path: str,
        params: list[tuple[str, str]] | None = None,
        *,
        repo_root: str | None = None,
    ) -> str:
        """Build a service URL carrying the repo this client speaks for.

        ``repo_root`` rides on *every* call, not just the phase advance: the
        service serves every repo from one store, so a call without it lands in
        whichever repo the service happens to be deployed against.
        """
        query = list(params or [])
        root = repo_root or self.repo_root
        if root:
            query.append(("repo_root", root))
        qs = urllib.parse.urlencode(query)
        return f"{self.base_url}{path}?{qs}" if qs else f"{self.base_url}{path}"

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self._url(path, repo_root=repo_root), data=data, method="POST")
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

    def _put(
        self,
        path: str,
        body: dict[str, Any],
        *,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self._url(path, repo_root=repo_root), data=data, method="PUT")
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

    def _get(self, path: str) -> str:
        """Return the raw response body for a GET request."""
        resp = urllib.request.urlopen(self._url(path), timeout=self._timeout)
        return resp.read().decode()
