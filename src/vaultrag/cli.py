"""``vaultrag`` command-line entrypoint.

P0 exposes two subcommands:

* ``reindex`` — build the index from a vault (wired at Step 8:
  :func:`~vaultrag.indexer.pipeline.reindex_vault`).
* ``mcp``     — serve the MCP tools over stdio (wired at Step 10, still stubbed).

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
from vaultrag.embedding.ollama import OllamaEmbedder
from vaultrag.index.base import VectorStoreError
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.indexer.pipeline import reindex_vault
from vaultrag.logging import configure_logging, get_logger
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
) -> None:
    """Serve the MCP tools (search/open) over stdio.

    Stubbed until Step 10 of the P0 plan.
    """
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")
    log.info(
        "mcp.stub",
        project=project or settings.project_id,
        status="not_implemented",
        step="P0 Step 10",
    )
    typer.echo("mcp is not implemented yet (P0 Step 10).")


def main() -> None:
    """Console-script entrypoint (``vaultrag`` → this)."""
    app()


if __name__ == "__main__":
    main()
