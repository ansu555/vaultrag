"""``create_app`` — the FastAPI application, assembled around one service.

The composition root (:mod:`vaultrag.composition`) builds a
:class:`~vaultrag.service.VaultRAGService` and hands the **same instance** to this app and
to the MCP server. That sharing is the structural half of the stage's parity claim: the two
transports cannot answer differently because there is only one thing answering.

Layout of the surface (``../Rag/reference/reference-rest-api.md``):

* ``/v1/...`` — everything vault-related, bearer-key required.
* ``/metrics`` — the same registry ``vaultrag.metrics`` exports standalone, key required.
* ``/healthz`` — liveness, the one unauthenticated route, and contentless because of it.
"""

from __future__ import annotations

from fastapi import FastAPI, Response

from vaultrag import __version__
from vaultrag.api.deps import RequiresKey, install_auth_handler
from vaultrag.api.errors import install_error_handlers
from vaultrag.api.routes import HealthResponse, router
from vaultrag.auth import KeyAuthenticator
from vaultrag.logging import get_logger
from vaultrag.metrics import registry, set_build_info
from vaultrag.service import VaultRAGService

log = get_logger(__name__)

_DESCRIPTION = """\
Cited-snippet retrieval over one or more Obsidian vaults — the REST twin of VaultRAG's
MCP tools, over the same service core. Responses are **snippets, not answers**: the
caller is expected to be the LLM.

Every `/v1` route and `/metrics` requires `Authorization: Bearer <key>` (P3 Stage 5: an
argon2-hashed, per-project-scoped, rate-limited key — see `vaultrag key add`). Errors are
RFC 7807 `application/problem+json`.
"""


def create_app(
    service: VaultRAGService,
    *,
    api_keys: KeyAuthenticator,
) -> FastAPI:
    """Build the ASGI app for ``service``, authenticated by ``api_keys``.

    Key material itself is never on this object: ``api_keys`` looks a presented key up in
    the ``api_keys`` table and verifies it per request, so revoking a key takes effect on
    the very next request rather than requiring an app rebuild. The caller (the ``serve``
    command) owns bootstrapping the first key and printing it — this function only wires
    the authenticator in, the same "no stdout concerns here" split Stage 4 drew.
    """
    app = FastAPI(
        title="VaultRAG",
        version=__version__,
        description=_DESCRIPTION,
        # The whole surface is versioned under /v1 (reference-rest-api), so the docs and
        # schema live at the root rather than inside a version that would date them.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.service = service
    app.state.api_keys = api_keys

    install_error_handlers(app)
    install_auth_handler(app)
    set_build_info(__version__)

    # Auth is attached to the router as a whole rather than route by route: a new endpoint
    # is then authenticated by default, and forgetting to add a decorator cannot open one.
    # The two unauthenticated/differently-handled routes below are declared outside it, so
    # the exemption is visible in one place instead of implied by an absence.
    app.include_router(router, dependencies=[RequiresKey])

    @app.get(
        "/healthz",
        response_model=HealthResponse,
        summary="Liveness probe (unauthenticated)",
        tags=["ops"],
    )
    def healthz() -> HealthResponse:
        """The one route that answers without a key.

        It exists for a container orchestrator, which cannot hold a credential, and it
        therefore says the least that still answers the question: this process is up, and
        this is its build. No project ids, no counts, no vault paths.
        """
        return HealthResponse(version=__version__)

    @app.get(
        "/metrics",
        summary="Prometheus exposition (same registry as the standalone exporter)",
        tags=["ops"],
        dependencies=[RequiresKey],
        response_class=Response,
    )
    def metrics() -> Response:
        """Mount P2 Stage 7's registry — not a second set of counters, the same objects.

        The standalone exporter on ``VAULTRAG_METRICS_PORT`` stays exactly as it was (P2
        Decision 5): it is a second *front-end* onto this registry, which is what lets a
        Prometheus scrape a keyless loopback port while the HTTP surface stays behind a
        key. Two front doors, one set of numbers — a series that appears on one and not the
        other would be a bug in this line, not a difference between deployments.
        """
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(registry()), media_type=CONTENT_TYPE_LATEST)

    log.info(
        "api.app_built",
        projects=[p.project_id for p in service.projects],
        writable=service.has_writes,
        graph=service.has_graph,
    )
    return app
