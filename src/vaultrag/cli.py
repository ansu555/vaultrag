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
import threading
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
from vaultrag.indexer.pipeline import IngestContext, reindex_vault
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.logging import configure_logging, get_logger
from vaultrag.mcp import build_mcp_server
from vaultrag.retrieval import RerankParams, load_reranker
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
def watch(
    project: Annotated[
        str | None, typer.Option("--project", help="Project/tenant scope to index.")
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault to watch.")
    ] = None,
) -> None:
    """Live-watch a vault: reconcile once, then (re)index each edit as it happens.

    First runs a boot reconcile (catching anything changed while the watcher was down),
    then starts the watchdog observer and blocks until Ctrl-C. Owned by the indexer role
    (p1-step-plan Decisions). Requires Qdrant + Ollama, like ``reindex``.
    """
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")

    project_id = project or settings.project_id
    vault_path = vault or settings.vault_path
    if not vault_path.is_dir():
        typer.echo(f"watch failed: vault path is not a directory: {vault_path}", err=True)
        raise typer.Exit(code=1)

    store: SQLiteMetaStore | None = None
    vector_store: QdrantVectorStore | None = None
    embedder: OllamaEmbedder | None = None
    try:
        store = SQLiteMetaStore.from_settings(settings)
        vector_store = QdrantVectorStore.from_settings(settings)
        embedder = OllamaEmbedder.from_settings(settings)
        # Boot reconcile first — a watcher that starts on a stale index would miss every
        # edit made while it was down. reindex is idempotent, so this is cheap when clean.
        stats = reindex_vault(
            project_id=project_id,
            vault_path=vault_path,
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
        typer.echo(
            f"reconciled: +{stats.created} ~{stats.updated} -{stats.deleted} "
            f"→{stats.renamed} ={stats.unchanged} !{stats.failed}"
        )
        ctx = IngestContext(
            project_id=project_id,
            vault_path=vault_path.resolve(),
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
        watcher = VaultWatcher(
            ctx,
            debounce_seconds=settings.watch_debounce_seconds,
            workers=settings.watch_workers,
        )
        watcher.start()
        typer.echo(f"watching {vault_path} (Ctrl-C to stop)…")
        try:
            threading.Event().wait()  # block main thread; interruptible by Ctrl-C
        except KeyboardInterrupt:
            typer.echo("stopping…")
        finally:
            watcher.stop()
    except (VectorStoreError, EmbeddingError, OSError, sqlite3.Error) as exc:
        log.error("watch.failed", project=project_id, vault=str(vault_path), error=str(exc))
        typer.echo(f"watch failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        for closer in (embedder, vector_store, store):
            if closer is not None:
                closer.close()


@app.command()
def mcp(
    project: Annotated[
        str | None, typer.Option("--project", help="Project/tenant scope to serve.")
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault `open` reads from.")
    ] = None,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Also live-watch the vault in a background daemon thread (opt-in; "
            "solo dogfooding). Off by default — serve/indexer own the watcher.",
        ),
    ] = False,
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

    # No MetaStore here by default: search reads only Qdrant, open reads only the vault.
    # Neither client connects at construction time, so the server comes up instantly and a
    # down Qdrant/Ollama surfaces as an actionable per-call tool error instead. --watch is
    # the one exception: live-watch needs the SQLite manifest/jobs + an embedder, so it
    # opens a store and runs one VaultWatcher in a background daemon thread (opt-in, solo).
    vector_store: QdrantVectorStore | None = None
    embedder: OllamaEmbedder | None = None
    store: SQLiteMetaStore | None = None
    watcher: VaultWatcher | None = None
    try:
        try:
            vector_store = QdrantVectorStore.from_settings(settings)
            embedder = OllamaEmbedder.from_settings(settings)
            # Built at startup so the (possibly first-run) model download blocks here,
            # visibly, not mid-query. load_reranker never raises: a fetch/load failure
            # degrades to a no-op reranker so the server still serves (rerank-off).
            reranker = load_reranker(
                enabled=settings.rerank_enabled,
                repo_id=settings.rerank_model,
                model_file=settings.rerank_model_file,
            )
            server = build_mcp_server(
                project_id=project_id,
                vault_path=vault_path,
                vector_store=vector_store,
                embedder=QueryEmbedder(embedder),
                sparse=Bm25Embedder(),
                default_k=settings.default_top_k,
                default_budget_tokens=settings.budget_tokens,
                reranker=reranker,
                rerank_params=RerankParams(
                    top_n=settings.rerank_top_n,
                    threshold=settings.rerank_threshold,
                    dominant_ratio=settings.rerank_dominant_ratio,
                ),
                parent_swap_enabled=settings.parent_swap_enabled,
                parent_max_tokens=settings.parent_max_tokens,
                # Obsidian display name for citation URIs (stage 8): configured value, else
                # the vault directory name — the common case (a vault named after its folder).
                # resolve() so a relative/'.'-style --vault still yields a real folder name.
                vault_name=settings.vault_name or vault_path.resolve().name,
            )
            if watch:
                # Reuse the raw OllamaEmbedder (not the QueryEmbedder wrapper) for document
                # ingest. The observer + dispatcher are daemon threads that die with this
                # stdio process; stop() in the finally drains them on a clean detach.
                store = SQLiteMetaStore.from_settings(settings)
                watcher = VaultWatcher(
                    IngestContext(
                        project_id=project_id,
                        vault_path=vault_path.resolve(),
                        store=store,
                        vector_store=vector_store,
                        embedder=embedder,
                    ),
                    debounce_seconds=settings.watch_debounce_seconds,
                    workers=settings.watch_workers,
                )
                watcher.start()
        # ValueError = misconfiguration (e.g. VAULTRAG_EMBED_DIMS <= 0); OSError = a
        # client that resolves/binds something at construction; sqlite3.Error = an
        # unopenable --watch store. Either way the fix is the operator's, so fail with a
        # readable line rather than a stdio traceback.
        except (ValueError, OSError, sqlite3.Error) as exc:
            log.error("mcp.failed", project=project_id, error=str(exc))
            typer.echo(f"mcp failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        log.info(
            "mcp.serve",
            project=project_id,
            vault=str(vault_path),
            transport="stdio",
            watching=watch,
        )
        try:
            server.run(transport="stdio", show_banner=False)  # blocks until client detaches
        except KeyboardInterrupt:
            log.info("mcp.stopped", project=project_id)
    finally:
        if watcher is not None:
            watcher.stop()
        for closer in (embedder, vector_store, store):
            if closer is not None:
                closer.close()


def main() -> None:
    """Console-script entrypoint (``vaultrag`` → this)."""
    app()


if __name__ == "__main__":
    main()
