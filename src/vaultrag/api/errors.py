"""RFC 7807 ``application/problem+json`` — the HTTP rendering of a service refusal.

Every error this API returns has the same shape, whether it came from the service, from
request validation, from a route that does not exist, or from a bug. That uniformity is
the point: a client should never have to guess whether an error body is
``{"detail": ...}``, ``{"error": ...}`` or an HTML traceback
(``../Rag/reference/reference-rest-api.md``).

The status mapping lives **here**, not in :mod:`vaultrag.service`, because "which HTTP
code" is a transport question — the same :class:`~vaultrag.service.Refused` that becomes a
403 here becomes a plain ``ToolError`` string over MCP, and neither transport should have
to know about the other's failure vocabulary.

``type`` is a URN rather than an ``https://`` URL: a URI is required to identify the
problem kind, but inventing a documentation domain would promise a page that does not
exist. A URN identifies without promising to dereference.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from vaultrag.logging import get_logger
from vaultrag.service import (
    BackendUnavailable,
    Conflict,
    InvalidRequest,
    NotFound,
    Refused,
    ServiceError,
    UnknownProject,
)

log = get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"

#: How much of a conflicting note's content a 409 body echoes back, so a client can rebase
#: without a second read — the same bound, and the same reason, as the MCP conflict text.
_CONFLICT_PREVIEW_CHARS = 4_000

#: Service error → (status, problem slug, title). Ordered most-specific first and matched
#: by ``isinstance``, so a future subclass inherits its parent's mapping rather than
#: silently falling through to 500.
_MAPPING: tuple[tuple[type[ServiceError], int, str, str], ...] = (
    (Conflict, 409, "write-conflict", "Write conflict"),
    (UnknownProject, 404, "unknown-project", "Unknown project"),
    (NotFound, 404, "not-found", "Not found"),
    (InvalidRequest, 400, "invalid-request", "Invalid request"),
    # 403, not 401: the caller authenticated fine (a real, unrevoked key), and the request
    # is understood — a guardrail declines it. Re-authenticating would not help, which is
    # exactly the distinction RFC 9110 draws between the two codes. Insufficient *scope* is
    # the same distinction one layer up (auth.InsufficientScope, also 403) — this mapping
    # only covers refusals the service raises, after a key has already cleared that gate.
    (Refused, 403, "refused", "Refused"),
    (BackendUnavailable, 503, "backend-unavailable", "Backend unavailable"),
)


def problem(
    *,
    status: int,
    slug: str,
    title: str,
    detail: str,
    instance: str | None = None,
    **extra: Any,
) -> JSONResponse:
    """Build one ``problem+json`` response.

    ``extra`` becomes RFC 7807 *extension members* — the mechanism that lets a 409 carry
    the live hash a client needs to rebase, without inventing a second error envelope.
    """
    body: dict[str, Any] = {
        "type": f"urn:vaultrag:problem:{slug}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if instance is not None:
        body["instance"] = instance
    body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)


def _service_error_response(request: Request, exc: ServiceError) -> JSONResponse:
    for kind, status, slug, title in _MAPPING:
        if isinstance(exc, kind):
            extra: dict[str, Any] = {}
            if isinstance(exc, Conflict):
                # The 409's whole job: hand back what is needed to merge and retry, so a
                # conflict costs one round-trip rather than two.
                extra = {
                    "path": exc.path,
                    "current_hash": exc.current_hash,
                    "current_text": exc.current_text[:_CONFLICT_PREVIEW_CHARS],
                    "truncated": len(exc.current_text) > _CONFLICT_PREVIEW_CHARS,
                }
            return problem(
                status=status,
                slug=slug,
                title=title,
                detail=str(exc),
                instance=request.url.path,
                **extra,
            )
    # An unmapped ServiceError subclass is a bug in this table, not a client error — say
    # so honestly rather than picking a plausible 4xx and hiding it.
    log.error("api.unmapped_service_error", kind=type(exc).__name__, error=str(exc))
    return problem(
        status=500,
        slug="internal",
        title="Internal error",
        detail="the server refused this request in a way it does not know how to report",
        instance=request.url.path,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Route every failure mode through ``problem+json``.

    Four handlers, because FastAPI has four ways to fail and only one of them is ours:
    a service refusal, a body/query that failed validation, an HTTP-level error raised by
    the framework (404 on an unknown path, 405 on a wrong verb), and an unhandled
    exception. The last one is the reason this is not just an exception handler on
    ``ServiceError``: without it a bug returns HTML, and an HTML body from a JSON API is
    the error that costs a client the most to debug.
    """

    @app.exception_handler(ServiceError)
    async def _on_service_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ServiceError)  # registered for exactly this type
        return _service_error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return problem(
            status=422,
            slug="validation",
            title="Request validation failed",
            detail="the request body or query parameters did not match the schema",
            instance=request.url.path,
            # pydantic's own per-field report, kept — it is precise, and re-summarizing it
            # into prose would lose which field was wrong.
            errors=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return problem(
            status=exc.status_code,
            slug="http-error",
            title=detail if exc.status_code < 500 else "Server error",
            detail=detail,
            instance=request.url.path,
        )

    @app.exception_handler(Exception)
    async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Logged with the type, returned without it: an exception message can carry a
        # filesystem path or a query fragment, and this body goes to the client.
        log.error("api.unhandled", kind=type(exc).__name__, path=request.url.path)
        return problem(
            status=500,
            slug="internal",
            title="Internal error",
            detail="the server hit an unexpected error; check its logs",
            instance=request.url.path,
        )
