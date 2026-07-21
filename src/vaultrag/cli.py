"""``vaultrag`` command-line entrypoint.

Subcommands:

* ``reindex`` — build the index from a vault (P0 Step 8:
  :func:`~vaultrag.indexer.pipeline.reindex_vault`).
* ``watch``   — live-watch a vault and reindex each edit (P1 Stage 4).
* ``mcp``     — serve the MCP tools over stdio (P0 Step 10:
  :func:`~vaultrag.mcp.build_mcp_server`).
* ``eval``    — score retrieval against a golden set; the CI gate (P2 Stage 1).

The long-running roles (``watch``, ``mcp``) take ``--metrics`` to expose the standalone
Prometheus endpoint (P2 Stage 7: :mod:`vaultrag.metrics`).

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
from vaultrag.config import RerankMode, Settings, load_settings
from vaultrag.consistency import check_drift
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
    format_ceiling,
    format_comparison,
    format_misses,
    format_summary,
    live_stack,
    load_corpus_golden,
    load_design_golden,
    load_golden,
    load_overlay,
    overlay_label,
    rank_ceiling,
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
from vaultrag.metrics import set_build_info, start_metrics_server
from vaultrag.parsing.image import load_image_parser
from vaultrag.parsing.pdf import load_pdf_parser
from vaultrag.retrieval import (
    compress_params_for,
    graph_params_for,
    load_reranker,
    rerank_params_for,
)
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


def _start_metrics(settings: Settings, *, requested: bool) -> bool:
    """Bring up the standalone ``/metrics`` exporter if asked; report whether it bound.

    ``--metrics`` ORs with ``VAULTRAG_METRICS_ENABLED`` so the flag can turn it on for one
    run without editing the environment, and the env var can turn it on for a long-lived
    deployment without editing a command line.

    Only the **long-running** roles (``watch``, ``mcp``) call this. ``reindex`` finishes in
    seconds, so an exporter there would almost never be scraped before the process exited
    — its work still lands in the counters, it just has no window in which to be read.
    Metrics recording itself is unconditional, so nothing is lost by not exporting here.
    """
    if not (requested or settings.metrics_enabled):
        return False
    set_build_info(__version__)
    return start_metrics_server(settings.metrics_port, settings.metrics_addr)


@app.command()
def reindex(
    project: Annotated[
        str | None, typer.Option("--project", help="Project/tenant scope for the index.")
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault to index.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-ingest every file, ignoring the manifest — repairs a drifted vector store.",
        ),
    ] = False,
) -> None:
    """Build the index from a vault (scan → parse → chunk → embed → upsert).

    Idempotent by default: a file whose hash matches the manifest is skipped. Use
    ``--force`` when the vector store has lost data the manifest still records — an
    ordinary run cannot detect that and will report the missing documents as
    ``unchanged``. A drift warning is printed after every run.
    """
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
            force=force,
            # Composition root owns the optional parsers: None (no `pdf` extra / no OCR
            # engine) means those formats are skipped, not failed.
            pdf_parser=load_pdf_parser(),
            image_parser=load_image_parser(enabled=settings.ocr_enabled),
        )
        drift = check_drift(project_id, store=store, vector_store=vector_store)
    except (VectorStoreError, EmbeddingError, OSError, sqlite3.Error) as exc:
        log.error("reindex.failed", project=project_id, vault=str(vault_path), error=str(exc))
        typer.echo(f"reindex failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        for closer in (embedder, vector_store, store):
            if closer is not None:
                closer.close()

    # The graph backfill is silent when there's nothing to do (the steady state), but
    # worth surfacing on the one run that upgrades a pre-P2-Stage-2 index.
    graph = f", {stats.graph_backfilled} graph backfilled" if stats.graph_backfilled else ""
    # Surfaced only when non-zero: on a markdown-only vault this is always 0 and would
    # be noise, but "your PDFs were skipped" must never be silent.
    skipped = f" ?{stats.skipped}" if stats.skipped else ""
    typer.echo(
        f"reindex complete: +{stats.created} ~{stats.updated} -{stats.deleted} "
        f"→{stats.renamed} ={stats.unchanged} !{stats.failed}{skipped} "
        f"({stats.chunks_written} chunks{graph}, {stats.elapsed_ms:.0f} ms)"
    )
    # The drift this catches is *stable*: re-running plain reindex never converges,
    # because the skip decision reads the manifest the drift is invisible to. Say so
    # here rather than leaving retrieval to quietly serve a fraction of the vault.
    if drift is not None:
        typer.echo(f"warning: {drift.describe()} — re-run with --force to rebuild", err=True)
    # Same shape as the drift warning, different cause: here the manifest is *correct*
    # and still incomplete, because a hash-equal file is skipped even when today's
    # parser would extract more from it than the parser that indexed it did.
    if stats.stale_parser:
        typer.echo(
            "warning: this index was built by an older parser — re-run with --force to "
            "pick up newly-extracted data (e.g. Mermaid diagram edges, Excalidraw scenes)",
            err=True,
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
    metrics: Annotated[
        bool,
        typer.Option(
            "--metrics",
            help="Serve Prometheus metrics on VAULTRAG_METRICS_PORT (default 9464). "
            "Also settable with VAULTRAG_METRICS_ENABLED.",
        ),
    ] = False,
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

    # Started before the boot reconcile so that reconcile's embed-cache counters land in a
    # scrapeable registry — it is the largest batch of work this process will ever do.
    if _start_metrics(settings, requested=metrics):
        typer.echo(f"metrics: http://{settings.metrics_addr}:{settings.metrics_port}/metrics")

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
            pdf_parser=load_pdf_parser(),
            image_parser=load_image_parser(enabled=settings.ocr_enabled),
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
    metrics: Annotated[
        bool,
        typer.Option(
            "--metrics",
            help="Serve Prometheus metrics on VAULTRAG_METRICS_PORT (default 9464). "
            "Off by default: each agent session is its own process, so N sessions would "
            "contend for one port (a clash warns and continues).",
        ),
    ] = False,
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

    # No typer.echo here, unlike `watch`: stdout is the JSON-RPC channel and a stray line
    # corrupts the stream. The bind is announced by structlog on stderr instead.
    _start_metrics(settings, requested=metrics)

    # By default search reads only Qdrant and open reads only the vault, and neither client
    # connects at construction time, so a read-only server comes up instantly (a down
    # Qdrant/Ollama surfaces as an actionable per-call error). Three features need more: the
    # write tools (write_note/…) and --watch both drive the single-file pipeline, and graph
    # expand + `related` read the link graph — all of them want the SQLite store, so it is
    # opened once and shared (the write/watch paths additionally reuse the raw embedder).
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
                mode=settings.rerank_mode,
                repo_id=settings.rerank_model,
                model_file=settings.rerank_model_file,
            )
            # Obsidian display name for citation URIs (stage 8) + write-back note links:
            # configured value, else the vault directory name (a vault named after its folder).
            resolved_vault_name = settings.vault_name or vault_root.name

            # One store, shared by write-back, --watch and the graph features (SQLite
            # serializes its own writes; opened check_same_thread=False for exactly this
            # multi-thread reuse). The write/watch paths *create* it — they own the manifest
            # and jobs tables. The graph is only a reader, so it attaches to an existing DB
            # and never conjures one: a read-only server pointed at a state dir with no
            # index would otherwise leave an empty database behind, and an index that was
            # never built has no graph to serve anyway.
            graph_enabled = settings.graph_expand_enabled and settings.db_path.is_file()
            if write_enabled or watch or graph_enabled:
                store = SQLiteMetaStore.from_settings(settings)

            # Only the paths that *ingest* need it (write-back, --watch); a read-only
            # server never converts anything, so don't pay for the parser it won't use.
            ingests = write_enabled or watch
            pdf_parser = load_pdf_parser() if ingests else None
            image_parser = load_image_parser(enabled=settings.ocr_enabled) if ingests else None

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
                        pdf_parser=pdf_parser,
                        image_parser=image_parser,
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
                rerank_params=rerank_params_for(settings),
                parent_swap_enabled=settings.parent_swap_enabled,
                parent_max_tokens=settings.parent_max_tokens,
                # Stage 6 + the `related` tool: one flag drives both, so there is no half-on
                # state where `related` answers from a graph that search ignores. `store`
                # can be non-None here purely for write-back/--watch, hence the second term.
                graph_store=store if settings.graph_expand_enabled else None,
                graph_params=graph_params_for(settings),
                compress_params=compress_params_for(settings),
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
                        pdf_parser=pdf_parser,
                        image_parser=image_parser,
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
    rerank: Annotated[
        RerankMode | None,
        typer.Option(
            "--rerank",
            help="Rerank mode for this run (never|auto|always). Defaults to 'never' — the "
            "gate pins it rather than inheriting the product default, so the CI number "
            "stays hermetic and fast. A --config overlay setting rerank_mode still wins.",
        ),
    ] = None,
    graph: Annotated[
        bool | None,
        typer.Option(
            "--graph/--no-graph",
            help="Retrieval stage 6 (link-graph authority reorder) for this run. Unlike "
            "--rerank this is NOT pinned: stage 6 is pure SQLite with no model and no "
            "network, so the gate measures the shipped default. Use --no-graph for the A/B.",
        ),
    ] = None,
    record: Annotated[
        bool,
        typer.Option(
            "--record",
            help="Re-record the frozen vector fixture from live Ollama, then run. Use after "
            "changing the corpus, the golden queries, or the embedding model.",
        ),
    ] = False,
    ceiling: Annotated[
        bool,
        typer.Option(
            "--ceiling",
            help="Report how deep the reranker would have to look (the rerank_top_n ceiling) "
            "instead of scoring configs. Runs no cross-encoder, so it takes seconds.",
        ),
    ] = False,
) -> None:
    """Score retrieval quality against a golden set (P2 Stage 1).

    By default this is fully hermetic: it reindexes the committed fixture vault into an
    in-memory Qdrant using frozen Qwen3 vectors, so it needs no Docker, no Ollama and no
    network, and returns the same numbers on every machine. That is what CI runs::

        uv run vaultrag eval                          # the gate
        uv run vaultrag eval --golden design          # dogfood the live index
        uv run vaultrag eval --rerank always          # measure the cross-encoder
        uv run vaultrag eval --no-graph               # A/B retrieval stage 6
        uv run vaultrag eval --config a.yml --config b.yml   # A/B two configs

    **The rerank stage is pinned off unless asked for.** The gate inherits every other
    setting from the environment, but not this one: a cross-encoder run downloads a ~600 MB
    ONNX model and scores ~50 passages per query, which would turn a hermetic sub-minute
    check into a slow, network-dependent one. Measuring rerank is a deliberate act
    (``--rerank``/an overlay), not something a PR gate does by accident.
    """
    settings = load_settings()
    configure_logging(settings.log_level)

    # Pinned first so an A/B overlay can still opt in; --rerank is applied after the
    # overlays below, so precedence reads: --rerank > overlay > pinned-off.
    settings = settings.model_copy(update={"rerank_mode": RerankMode.never})

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
    if rerank is not None:
        overlays = [(label, s.model_copy(update={"rerank_mode": rerank})) for label, s in overlays]
    if graph is not None:
        overlays = [
            (label, s.model_copy(update={"graph_expand_enabled": graph})) for label, s in overlays
        ]

    if project:
        golden_set = replace(golden_set, project_id=project)

    if record:
        _record_fixture(golden_set, overlays[0][1])

    if ceiling:
        # Answers "how deep must rerank look?" without running one. Deliberately ignores
        # --config/--rerank: the ceiling is a property of *fusion* order, so no rerank
        # setting can move it, and honouring those flags would imply otherwise.
        _, ceiling_settings = overlays[0]
        stack_cm = (
            live_stack(golden_set, ceiling_settings)
            if use_live
            else replay_stack(golden_set, ceiling_settings, fixture_path=VECTOR_FIXTURE_PATH)
        )
        try:
            with stack_cm as stack:
                typer.echo(format_ceiling(rank_ceiling(golden_set, stack)))
        except (VectorStoreError, EmbeddingError, ValueError) as exc:
            typer.echo(f"\nceiling could not run: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        return

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
