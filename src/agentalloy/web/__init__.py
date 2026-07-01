"""Web UI backend — config endpoints and SPA static serving.

The browser-facing surface of the service: ``/api/config`` (read/edit the
user-scoped ``.env``) and the built React SPA out of ``frontend/dist``.
Read-side pages (telemetry, diagnostics, health) ride the existing routers.
"""
