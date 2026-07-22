"""vaultrag.api — the REST transport (P3 Stage 4).

A thin wrapper over :class:`~vaultrag.service.VaultRAGService`, the same core the MCP tools
call. :func:`create_app` builds the ASGI app; ``vaultrag serve`` runs it under uvicorn.
See ``../Rag/reference/reference-rest-api.md`` for the endpoint table and
``../Rag/decisions/mcp-first-not-rest-first.md`` for why this is a wrapper and not a peer.
"""

from __future__ import annotations

from vaultrag.api.app import create_app
from vaultrag.api.deps import require_key
from vaultrag.api.errors import PROBLEM_MEDIA_TYPE, install_error_handlers, problem
from vaultrag.auth import InsufficientScope, RateLimited, Unauthorized

__all__ = [
    "PROBLEM_MEDIA_TYPE",
    "InsufficientScope",
    "RateLimited",
    "Unauthorized",
    "create_app",
    "install_error_handlers",
    "problem",
    "require_key",
]
