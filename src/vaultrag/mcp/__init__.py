"""vaultrag.mcp — the FastMCP transport. Step 10 (the P0 finish line), thinned in P3 Stage 4.

:func:`build_mcp_server` assembles the server from injected dependencies and
:func:`mcp_server_for` wraps an already-built :class:`~vaultrag.service.VaultRAGService` —
the form the composition root uses, so MCP and REST share one service instance rather than
two equivalent ones. The operations themselves live in :mod:`vaultrag.service`; what is
here is tool schemas and error rendering. See ``server.py`` and
``../Rag/reference/reference-mcp-tools.md``.

``OverviewStore`` and ``ServedProject`` are re-exported from :mod:`vaultrag.service`, where
they now live: they describe the *service*, not this transport.
"""

from __future__ import annotations

from vaultrag.mcp.server import (
    OverviewStore,
    SearchFiltersParam,
    ServedProject,
    build_mcp_server,
    mcp_server_for,
)

__all__ = [
    "OverviewStore",
    "SearchFiltersParam",
    "ServedProject",
    "build_mcp_server",
    "mcp_server_for",
]
