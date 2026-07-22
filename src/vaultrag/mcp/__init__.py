"""vaultrag.mcp — FastMCP ``search``/``open`` served over stdio. Step 10, the P0 finish line.

:func:`build_mcp_server` assembles the server from injected dependencies; the CLI's
``vaultrag mcp`` command is the composition root. See ``server.py`` and
``../Rag/reference/reference-mcp-tools.md``.
"""

from __future__ import annotations

from vaultrag.mcp.server import (
    OverviewStore,
    SearchFiltersParam,
    ServedProject,
    build_mcp_server,
)

__all__ = ["OverviewStore", "SearchFiltersParam", "ServedProject", "build_mcp_server"]
