"""Authentication for the HTTP transport — P3 Stage 4's half of Decision 4.

``../Rag/reference/reference-security-model.md``: *"MCP stdio inherits local-user trust;
the HTTP transport always requires a key even on localhost."* Splitting `serve` (Stage 4)
from the real auth model (Stage 5) naively would mean shipping an unauthenticated port for
one PR, so this module is the **cheapest auth that satisfies the rule**: one bootstrap key
from the environment, required on every route but ``/healthz``, no scopes, no argon2, no
buckets.

What Stage 5 grows here — hashed at rest, per-project scopes crossed with read/write/admin,
per-key token buckets, ``vaultrag key add|list|revoke`` — replaces the *body* of
:func:`require_key` while leaving its position untouched. That is the point of doing it
this way round: the enforcement point exists, wired into every route, from the first
commit, so Stage 5 is a change to one function rather than a change to every route.

What this deliberately is **not**: a substitute for the network posture. The default bind
is loopback and remote access goes through Tailscale/WireGuard — a single shared bearer
token is a gate, not a security boundary, and calling it one would be the mistake.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Request

from vaultrag.api.errors import problem
from vaultrag.logging import get_logger

log = get_logger(__name__)

#: Bytes of entropy in a minted bootstrap key. 32 bytes → 43 url-safe characters, which is
#: past any brute-force concern for a loopback-bound port and still one copy-paste.
_KEY_BYTES = 32


class Unauthorized(Exception):
    """Raised by :func:`require_key` when a request carries no usable bearer key.

    A dedicated exception rather than an ``HTTPException`` so the 401 renders through the
    same ``problem+json`` handler as everything else — including the ``WWW-Authenticate``
    header, which RFC 9110 requires on a 401 and which a bare JSON body would omit.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def mint_key() -> str:
    """Generate an ephemeral bootstrap key.

    Called when ``VAULTRAG_API_KEY`` is unset. The alternative — refusing to start — would
    make the first ``vaultrag serve`` fail for a reason that has nothing to do with what
    the operator was trying to do, and the alternative to *that* (starting keyless) is the
    one thing the security model forbids. So: always a key, printed once, and a log line
    saying how to make it stable across restarts.
    """
    return secrets.token_urlsafe(_KEY_BYTES)


def require_key(request: Request) -> None:
    """Reject any request that does not present the server's bearer key.

    Constant-time comparison via :func:`secrets.compare_digest`: an ordinary ``==`` on a
    secret leaks its prefix through timing, and while that is a stretch against a loopback
    port, the fix costs one function call and this is the code Stage 5 will build on.
    """
    expected: str = request.app.state.api_key
    header = request.headers.get("authorization")
    if not header:
        raise Unauthorized("this endpoint requires `Authorization: Bearer <key>`")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise Unauthorized("authorization must be `Bearer <key>`")
    if not secrets.compare_digest(presented.strip(), expected):
        # Never echo the presented key, not even truncated — a rejected credential in a
        # log is still a credential, and it is often the *right* one for another server.
        log.warning("api.auth_rejected", path=request.url.path)
        raise Unauthorized("invalid API key")


#: The dependency every authenticated router declares. Named so a route reads as a
#: sentence — ``dependencies=[RequiresKey]`` — and so Stage 5 can swap what it resolves to
#: (a `Key` object with scopes) without touching the routes that declare it.
RequiresKey = Depends(require_key)

AuthenticatedRequest = Annotated[Request, RequiresKey]


def install_auth_handler(app: object) -> None:
    """Register the 401 renderer. Kept beside the check so the two cannot drift apart."""
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
