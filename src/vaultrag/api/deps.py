"""Authentication + authorization for the HTTP transport — P3 Stage 5's enforcement point.

Stage 4 built this module around the cheapest auth that satisfied
``../Rag/reference/reference-security-model.md``'s *"the HTTP transport always requires a
key even on localhost"*: one shared secret, compared with :func:`secrets.compare_digest`,
wired into every route via :data:`RequiresKey` on the router as a whole. That wiring is
untouched here — the point of building it that way round was that Stage 5 only had to
replace what :func:`require_key` *does*, not where it sits.

What changed is the body, and the shape grew a second dependency alongside it:

* :func:`require_key` — **authentication**. Delegates to
  :class:`~vaultrag.auth.KeyAuthenticator`, which looks a presented key up by its (public)
  id, verifies its (secret) half against the stored argon2 hash, and enforces its
  token-bucket rate limit. Still router-wide, so a new route is authenticated by default.
* :func:`require_scope` — **authorization**, new this stage. A key that authenticates is
  not automatically allowed everywhere: each route declares the level it needs
  (:data:`RequiresRead`/:data:`RequiresWrite`/:data:`RequiresAdmin`), and this dependency
  checks the authenticated key's scopes against the ``{project}`` path parameter the same
  route handler will use — reading the *same* value, so there is no path by which the
  check and the operation could disagree about which project is in play. A route with no
  ``{project}`` segment (``/v1/projects``) is checked against "holds this level on *any*
  served project", since there is no specific tenant to name yet. A ``{project}`` that
  names no project this process serves at all is let through *un*refused here on purpose:
  a scope table has nothing truthful to say about a project it was never offered a chance
  to grant access to, so the request reaches :meth:`~vaultrag.service.VaultRAGService.select`
  and gets the same ``UnknownProject`` 404 MCP gives for the identical typo — a 403 would
  have turned "no such project" into "you can't see this project", a less honest answer.

Three failure modes now render as three status codes, each with its own handler below:
:class:`~vaultrag.auth.Unauthorized` (401, not this key),
:class:`~vaultrag.auth.InsufficientScope` (403, this key but not for this) and
:class:`~vaultrag.auth.RateLimited` (429, this key but not right now).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from vaultrag.api.errors import problem
from vaultrag.auth import (
    AuthenticatedKey,
    InsufficientScope,
    KeyAuthenticator,
    RateLimited,
    Scope,
    Unauthorized,
    scope_at_least,
)
from vaultrag.logging import get_logger

log = get_logger(__name__)


def _authenticator(request: Request) -> KeyAuthenticator:
    auth: KeyAuthenticator = request.app.state.api_keys
    return auth


def require_key(request: Request) -> AuthenticatedKey:
    """Authenticate the request's bearer key. Raises :class:`Unauthorized`/
    :class:`RateLimited`; returns the key's scopes for :func:`require_scope` to check."""
    header = request.headers.get("authorization")
    if not header:
        raise Unauthorized("this endpoint requires `Authorization: Bearer <key>`")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise Unauthorized("authorization must be `Bearer <key>`")
    return _authenticator(request).authenticate(presented.strip())


#: The dependency every authenticated router declares. Named so a route reads as a
#: sentence — ``dependencies=[RequiresKey]`` — and attached to the whole router in
#: ``api/app.py`` so a new endpoint is authenticated by default; per-route scope
#: dependencies (below) layer *authorization* on top, not authentication itself.
RequiresKey = Depends(require_key)

CurrentKey = Annotated[AuthenticatedKey, Depends(require_key)]


def _require_scope(level: Scope) -> Any:
    def _check(request: Request, key: CurrentKey) -> AuthenticatedKey:
        project = request.path_params.get("project")
        if project is None:
            # No project in the path (`/v1/projects`): there is nothing narrower to name
            # yet, so the bar is "holds this level somewhere" rather than "holds it here".
            if not key.has_any(level):
                raise InsufficientScope(
                    f"key {key.id!r} does not hold {level.value!r} on any served project"
                )
            return key
        served_ids = {p.project_id for p in request.app.state.service.projects}
        if project not in served_ids:
            # Not a project this process serves at all — a scope table has nothing
            # truthful to say about a project it was never given the chance to grant
            # access to, so let it through to the service's own tenancy check, which
            # answers UnknownProject (404) exactly as it does for the same mistake over
            # MCP. Refusing here first would turn "no such project" into "you can't see
            # this project", which is a worse and less honest answer to a typo.
            return key
        granted = key.scope_for(project)
        if granted is None or not scope_at_least(granted, level):
            raise InsufficientScope(
                f"key {key.id!r} does not hold {level.value!r} on project {project!r}"
            )
        return key

    return Depends(_check)


RequiresRead = _require_scope(Scope.read)
RequiresWrite = _require_scope(Scope.write)
RequiresAdmin = _require_scope(Scope.admin)


def install_auth_handler(app: object) -> None:
    """Register the 401/403/429 renderers. Kept beside the checks so they cannot drift."""
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)

    @app.exception_handler(Unauthorized)
    async def _on_unauthorized(request: Request, exc: Exception) -> object:
        assert isinstance(exc, Unauthorized)
        response = problem(
            status=401,
            slug="unauthorized",
            title="Unauthorized",
            detail=exc.detail,
            instance=request.url.path,
        )
        response.headers["WWW-Authenticate"] = 'Bearer realm="vaultrag"'
        return response

    @app.exception_handler(InsufficientScope)
    async def _on_insufficient_scope(request: Request, exc: Exception) -> object:
        assert isinstance(exc, InsufficientScope)
        # 403, not 401: the caller authenticated fine — re-sending the same header would
        # not help, which is the RFC 9110 line between the two codes.
        return problem(
            status=403,
            slug="insufficient-scope",
            title="Insufficient scope",
            detail=exc.detail,
            instance=request.url.path,
        )

    @app.exception_handler(RateLimited)
    async def _on_rate_limited(request: Request, exc: Exception) -> object:
        assert isinstance(exc, RateLimited)
        response = problem(
            status=429,
            slug="rate-limited",
            title="Too many requests",
            detail=exc.detail,
            instance=request.url.path,
        )
        response.headers["Retry-After"] = str(max(1, round(exc.retry_after)))
        return response
