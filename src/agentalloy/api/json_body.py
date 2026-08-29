"""Content-type tolerance for the service's JSON-body endpoints.

Every body-bearing endpoint on this service takes JSON, but its primary
clients are LLM agents hand-rolling requests: ``curl --data`` tags the body
``application/x-www-form-urlencoded`` by default, and Python ``urllib`` /
``requests`` (without ``json=``) do the same. FastAPI honors the tag and
form-parses a perfectly good JSON body, returning a 422 whose detail gives
the agent no usable path forward.

Two mechanisms make the API tolerant:

- ``JsonBodyNormalizer`` rewrites a missing/mistagged content-type to
  ``application/json`` on body-bearing methods so valid JSON wins regardless
  of the client's tag. Multipart requests are never touched.
- The ``RequestValidationError`` handler turns a genuinely unparseable body
  into a 422 that names the exact fix (the ``artifact put`` CLI for
  artifacts, a JSON-header curl for everything else), so a blocked agent
  self-recovers in one step. Field-level validation errors keep FastAPI's
  default response shape.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_BODY_PARSE_ERROR_TYPES = {
    "json_invalid",
    "model_attributes_type",
    "dict_type",
    "list_type",
}
_METHODS_WITH_BODY = {"POST", "PUT", "PATCH"}


class JsonBodyNormalizer:
    """Rewrite non-JSON body content-types to ``application/json``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in _METHODS_WITH_BODY:
            headers = scope["headers"]
            content_type = next((v for k, v in headers if k == b"content-type"), b"")
            is_json = content_type.startswith(b"application/json")
            is_multipart = content_type.startswith(b"multipart/")
            if not is_json and not is_multipart:
                scope = dict(scope)
                scope["headers"] = [(k, v) for k, v in headers if k != b"content-type"] + [
                    (b"content-type", b"application/json")
                ]
        await self.app(scope, receive, send)


def install_json_body_tolerances(app: FastAPI) -> None:
    """Mount the content-type normalizer and the actionable body-422 handler."""
    app.add_middleware(JsonBodyNormalizer)

    @app.exception_handler(RequestValidationError)
    async def _body_parse_help(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        body_parse = any(
            tuple(err.get("loc") or ())[:1] == ("body",)
            and err.get("type") in _BODY_PARSE_ERROR_TYPES
            for err in errors
        )
        if not body_parse:
            return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    "The request body must be a JSON object sent as "
                    "Content-Type: application/json. To record a deliverable "
                    "artifact, pipe its body straight to the store: "
                    "agentalloy artifact put --phase <phase> --slug <slug> "
                    "--name <name> (content on stdin — nothing on disk). For "
                    "other endpoints, send JSON directly: "
                    f"curl -X {request.method} -H 'Content-Type: application/json' "
                    f"-d '<json>' '{target}'"
                )
            },
        )
