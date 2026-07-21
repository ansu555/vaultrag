"""``vaultrag`` command-line entrypoint.

Subcommands:

* ``reindex`` — build the index from a vault (P0 Step 8:
  :func:`~vaultrag.indexer.pipeline.reindex_vault`).
* ``watch``   — live-watch a vault and reindex each edit (P1 Stage 4).
* ``mcp``     — serve the MCP tools over stdio (P0 Step 10:
  :func:`~vaultrag.mcp.build_mcp_server`).
* ``eval``    — score retrieval against a golden set; the CI gate (P2 Stage 1).

Later phases add ``serve`` (HTTP). See ``../Rag/projects/vaultrag/p0-step-plan.md``.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from vaultrag import __version__
from vaultrag.config import Settings, load_settings
from vaultrag.embedding.base import EmbeddingError
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.embedding.ollama import OllamaEmbedder
from vaultrag.eval import (
    VECTOR_FIXTURE_PATH,
    EvalReport,
    GoldenSet,
    GoldenSetError,
    OverlayError,
    RecordingEmbedder,
    VectorFixture,
    apply_overlay,
    format_cases,
    format_comparison,
    format_misses,
    format_summary,
    live_stack,
    load_corpus_golden,
    load_design_golden,
    load_golden,
    load_overlay,
    overlay_label,
    replay_stack,
    run_eval,
)
from vaultrag.index.base import VectorStoreError
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.index.sparse import Bm25Embedder
from vaultrag.indexer.pipeline import IngestContext, reindex_vault
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.logging import configure_logging, get_logger
from vaultrag.mcp import build_mcp_server
from vaultrag.retrieval import RerankParams, load_reranker
from vaultrag.store.sqlite import SQLiteMetaStore
from vaultrag.writeback import NoteWriter

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
    write: Annotated[
        bool,
        typer.Option(
            "--write/--no-write",
            help="Expose the write tools (write_note/append_section/patch_frontmatter). "
            "On by default (the memory loop); --no-write serves read-only. Also gated by "
            "VAULTRAG_WRITEBACK_ENABLED.",
        ),
    ] = True,
) -> None:
    """Serve the MCP tools (search/open, and write_note/… unless --no-write) over stdio.

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

    # By default search reads only Qdrant and open reads only the vault, and neither client
    # connects at construction time, so a read-only server comes up instantly (a down
    # Qdrant/Ollama surfaces as an actionable per-call error). Two features need more: the
    # write tools (write_note/…) and --watch both drive the single-file pipeline, so they
    # open the SQLite manifest/jobs store and reuse the raw embedder for document ingest.
    write_enabled = write and settings.writeback_enabled
    vault_root = vault_path.resolve()
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
            # Obsidian display name for citation URIs (stage 8) + write-back note links:
            # configured value, else the vault directory name (a vault named after its folder).
            resolved_vault_name = settings.vault_name or vault_root.name

            # One store, shared by write-back and --watch (SQLite serializes its own writes;
            # opened check_same_thread=False for exactly this multi-thread reuse).
            if write_enabled or watch:
                store = SQLiteMetaStore.from_settings(settings)

            writer: NoteWriter | None = None
            if write_enabled:
                assert store is not None  # opened just above when write_enabled
                writer = NoteWriter(
                    IngestContext(
                        project_id=project_id,
                        vault_path=vault_root,
                        store=store,
                        vector_store=vector_store,
                        embedder=embedder,
                    ),
                    author=settings.writeback_author,
                    allow_roots=settings.writeback_allow_roots,
                    max_bytes=settings.writeback_max_bytes,
                    max_writes_per_minute=settings.writeback_max_writes_per_minute,
                    git_enabled=settings.writeback_git_enabled,
                    vault_name=resolved_vault_name,
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
                vault_name=resolved_vault_name,
                writer=writer,
            )
            if watch:
                # The observer + dispatcher are daemon threads that die with this stdio
                # process; stop() in the finally drains them on a clean detach.
                assert store is not None  # opened above when watch is set
                watcher = VaultWatcher(
                    IngestContext(
                        project_id=project_id,
                        vault_path=vault_root,
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
        # unopenable write/watch store. Either way the fix is the operator's, so fail with a
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
            writing=write_enabled,
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


@app.command()
def eval(  # shadows the builtin: `eval` is the documented subcommand name
    golden: Annotated[
        str,
        typer.Option(
            "--golden",
            help="Golden set: 'corpus' (hermetic gate), 'design' (live dogfooding vault), "
            "or a path to a golden JSON file.",
        ),
    ] = "corpus",
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Score the running index (Qdrant + Ollama from settings) instead of "
            "building the fixture corpus in memory. Implied by --golden design.",
        ),
    ] = False,
    config: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            help="Settings-override file (YAML or JSON) applied on top of the environment. "
            "Repeat to A/B two or more configs over the same golden set; the first is the "
            "baseline the deltas are measured against.",
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Override the project scope named in the golden set."),
    ] = None,
    gate: Annotated[
        bool,
        typer.Option(
            "--gate/--no-gate",
            help="Exit non-zero when a config misses recall@10 >= 0.9 or MRR@5 >= 0.75. "
            "On by default — this is what makes the CI job merge-blocking.",
        ),
    ] = True,
    show_cases: Annotated[
        bool,
        typer.Option("--show-cases", help="Print the per-case rank table, not just the summary."),
    ] = False,
    record: Annotated[
        bool,
        typer.Option(
            "--record",
            help="Re-record the frozen vector fixture from live Ollama, then run. Use after "
            "changing the corpus, the golden queries, or the embedding model.",
        ),
    ] = False,
) -> None:
    """Score retrieval quality against a golden set (P2 Stage 1).

    By default this is fully hermetic: it reindexes the committed fixture vault into an
    in-memory Qdrant using frozen Qwen3 vectors, so it needs no Docker, no Ollama and no
    network, and returns the same numbers on every machine. That is what CI runs::

        uv run vaultrag eval                      # the gate
        uv run vaultrag eval --golden design      # dogfood the live index
        uv run vaultrag eval --config base.yaml --config rerank-on.yaml   # A/B
    """
    settings = load_settings()
    configure_logging(settings.log_level)

    try:
        golden_set = _resolve_golden(golden)
    except GoldenSetError as exc:
        typer.echo(f"eval failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    use_live = live or golden_set.vault_path is None
    if record and use_live:
        typer.echo("eval failed: --record only applies to the hermetic corpus run", err=True)
        raise typer.Exit(code=2)

    overlays: list[tuple[str, Settings]] = []
    try:
        for path in config or []:
            overrides = load_overlay(path)
            overlays.append((overlay_label(path, overrides), apply_overlay(settings, overrides)))
    except OverlayError as exc:
        typer.echo(f"eval failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not overlays:
        overlays = [("current settings", settings)]

    if project:
        golden_set = replace(golden_set, project_id=project)

    if record:
        _record_fixture(golden_set, overlays[0][1])

    reports: list[EvalReport] = []
    try:
        for label, run_settings in overlays:
            stack_cm = (
                live_stack(golden_set, run_settings)
                if use_live
                else replay_stack(golden_set, run_settings, fixture_path=VECTOR_FIXTURE_PATH)
            )
            with stack_cm as stack:
                reports.append(run_eval(golden_set, stack, label=label))
    except (VectorStoreError, EmbeddingError, ValueError) as exc:
        typer.echo(f"\neval could not run: {exc}", err=True)
        if use_live:
            typer.echo(
                "live mode needs Qdrant up (`docker compose up -d qdrant`), Ollama serving, "
                "and the project indexed (`uv run vaultrag reindex --project … --vault …`).",
                err=True,
            )
        raise typer.Exit(code=2) from exc

    for report in reports:
        if show_cases:
            typer.echo(format_cases(report))
        typer.echo(format_summary(report))
    if len(reports) > 1:
        typer.echo(format_comparison(reports))

    failed = [r for r in reports if not r.metrics.meets_targets()]
    if failed:
        typer.echo("")
        for report in failed:
            typer.echo(f"{report.label}: {'; '.join(report.metrics.failures())}", err=True)
            typer.echo(format_misses(report), err=True)
        if gate:
            typer.echo("\nquality gate FAILED — see the misses above.", err=True)
            raise typer.Exit(code=1)


def _resolve_golden(name: str) -> GoldenSet:
    """Map the ``--golden`` value to a set: the two shorthands, or a path."""
    if name == "corpus":
        return load_corpus_golden()
    if name == "design":
        return load_design_golden()
    return load_golden(Path(name))


def _record_fixture(golden_set: GoldenSet, settings: Settings) -> None:
    """Refresh ``tests/eval/vectors.db`` from live Ollama by running the eval through it.

    Recording *by running* is deliberate: it guarantees the fixture covers exactly the
    texts the gate will ask for — every chunk the current chunker produces from the corpus,
    plus every golden query — rather than a hand-maintained list that silently drifts out
    of sync with the pipeline.
    """
    typer.echo(f"recording vectors from {settings.ollama_url} ({settings.embed_model})…")
    fixture = VectorFixture(VECTOR_FIXTURE_PATH, create=True)
    inner = OllamaEmbedder.from_settings(settings)
    recorder = RecordingEmbedder(inner, fixture)
    try:
        with replay_stack(
            golden_set, settings, fixture_path=VECTOR_FIXTURE_PATH, embedder=recorder
        ) as stack:
            run_eval(golden_set, stack, label="record")
        fixture.vacuum()
        typer.echo(
            f"recorded {recorder.recorded} new vectors ({recorder.reused} already cached); "
            f"fixture now holds {fixture.count()} → {VECTOR_FIXTURE_PATH}"
        )
    except (EmbeddingError, VectorStoreError) as exc:
        typer.echo(f"recording failed: {exc}", err=True)
        typer.echo("is Ollama serving the embedding model?", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        recorder.close()
        fixture.close()


def main() -> None:
    """Console-script entrypoint (``vaultrag`` → this)."""
    app()


if __name__ == "__main__":
    main()
