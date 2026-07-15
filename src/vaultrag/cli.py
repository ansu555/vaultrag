"""``vaultrag`` command-line entrypoint.

P0 exposes two subcommands:

* ``reindex`` — build the index from a vault (wired at Step 8:
  :func:`~vaultrag.indexer.pipeline.reindex_vault`).
* ``mcp``     — serve the MCP tools (search/open) over stdio (wired at Step 10:
  :func:`~vaultrag.mcp.build_mcp_server`).

Later phases add ``serve`` (HTTP) and ``eval``. See
``../Rag/projects/vaultrag/p0-step-plan.md``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from vaultrag import __version__
from vaultrag.config import load_settings
from vaultrag.embedding.base import EmbeddingError
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.embedding.ollama import OllamaEmbedder
from vaultrag.index.base import VectorStoreError
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.index.sparse import Bm25Embedder
from vaultrag.indexer.pipeline import reindex_vault
from vaultrag.logging import configure_logging, get_logger
from vaultrag.mcp import build_mcp_server
from vaultrag.store.sqlite import SQLiteMetaStore

app = typer.Typer(
    name="vaultrag",
    help="Self-hosted, MCP-first RAG backend over Obsidian vaults.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"vaultrag {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """VaultRAG CLI."""


@app.command()
def reindex(
    project: Annotated[
        str | None, typer.Option("--project", help="Project/tenant scope for the index.")
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault to index.")
    ] = None,
) -> None:
    """Build the index from a vault (scan → parse → chunk → embed → upsert)."""
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")

    project_id = project or settings.project_id
    vault_path = vault or settings.vault_path

    # Constructed inside the guarded region: opening the SQLite store can itself fail
    # (unwritable VAULTRAG_STATE_DIR, unopenable db file) and must exit 1 with a message,
    # not a traceback. sqlite3.Error is caught here because the CLI is the composition
    # root — the one place that knows the concrete MetaStore is SQLite.
    store: SQLiteMetaStore | None = None
    vector_store: QdrantVectorStore | None = None
    embedder: OllamaEmbedder | None = None
    try:
        store = SQLiteMetaStore.from_settings(settings)
        vector_store = QdrantVectorStore.from_settings(settings)
        embedder = OllamaEmbedder.from_settings(settings)
        stats = reindex_vault(
            project_id=project_id,
            vault_path=vault_path,
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
    except (VectorStoreError, EmbeddingError, OSError, sqlite3.Error) as exc:
        log.error("reindex.failed", project=project_id, vault=str(vault_path), error=str(exc))
        typer.echo(f"reindex failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        for closer in (embedder, vector_store, store):
            if closer is not None:
                closer.close()

    typer.echo(
        f"reindex complete: +{stats.created} ~{stats.updated} -{stats.deleted} "
        f"→{stats.renamed} ={stats.unchanged} !{stats.failed} "
        f"({stats.chunks_written} chunks, {stats.elapsed_ms:.0f} ms)"
    )
    if stats.failed:
        raise typer.Exit(code=1)


@app.command()
def mcp(
    project: Annotated[
        str | None, typer.Option("--project", help="Project/tenant scope to serve.")
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault `open` reads from.")
    ] = None,
) -> None:
    """Serve the MCP tools (search/open) over stdio.

    stdout is the JSON-RPC channel, so nothing here may print to it — logs go to
    stderr (see ``vaultrag.logging``) and the FastMCP banner is disabled. Register
    with: ``claude mcp add vaultrag -- uv run vaultrag mcp --project X``.
    """
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")

    project_id = project or settings.project_id
    vault_path = vault or settings.vault_path
    if not vault_path.is_dir():
        typer.echo(f"mcp failed: vault path is not a directory: {vault_path}", err=True)
        raise typer.Exit(code=1)

    # No MetaStore here: search reads only Qdrant, open reads only the vault. Neither
    # client connects at construction time, so the server comes up instantly and a
    # down Qdrant/Ollama surfaces as an actionable per-call tool error instead.
    vector_store: QdrantVectorStore | None = None
    embedder: OllamaEmbedder | None = None
    try:
        try:
            vector_store = QdrantVectorStore.from_settings(settings)
            embedder = OllamaEmbedder.from_settings(settings)
            server = build_mcp_server(
                project_id=project_id,
                vault_path=vault_path,
                vector_store=vector_store,
                embedder=QueryEmbedder(embedder),
                sparse=Bm25Embedder(),
                default_k=settings.default_top_k,
                default_budget_tokens=settings.budget_tokens,
            )
        # ValueError = misconfiguration (e.g. VAULTRAG_EMBED_DIMS <= 0); OSError = a
        # client that resolves/binds something at construction. Either way the fix is
        # the operator's, so fail with a readable line rather than a stdio traceback.
        except (ValueError, OSError) as exc:
            log.error("mcp.failed", project=project_id, error=str(exc))
            typer.echo(f"mcp failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        log.info("mcp.serve", project=project_id, vault=str(vault_path), transport="stdio")
        try:
            server.run(transport="stdio", show_banner=False)  # blocks until client detaches
        except KeyboardInterrupt:
            log.info("mcp.stopped", project=project_id)
    finally:
        for closer in (embedder, vector_store):
            if closer is not None:
                closer.close()


def main() -> None:
    """Console-script entrypoint (``vaultrag`` → this)."""
    app()


if __name__ == "__main__":
    main()
