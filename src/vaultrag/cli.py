"""``vaultrag`` command-line entrypoint.

Subcommands:

* ``reindex`` — build the index from a vault (P0 Step 8:
  :func:`~vaultrag.indexer.pipeline.reindex_vault`).
* ``watch``   — live-watch a vault and reindex each edit (P1 Stage 4).
* ``mcp``     — serve the MCP tools over stdio (P0 Step 10:
  :func:`~vaultrag.mcp.build_mcp_server`).
* ``serve``   — serve REST ``/v1`` + ``/metrics`` over HTTP, or run the indexer role
  (P3 Stage 4: :mod:`vaultrag.api`, ``VAULTRAG_ROLE``).
* ``project`` — register/inspect/remove the vaults one process serves (P3 Stage 2).
* ``queries`` — report on the query log and mine it for golden-set cases (P3 Stage 3).
* ``key``     — mint/list/revoke API keys for the HTTP transport (P3 Stage 5).
* ``eval``    — score retrieval against a golden set; the CI gate (P2 Stage 1).

The long-running roles (``watch``, ``mcp``) take ``--metrics`` to expose the standalone
Prometheus endpoint (P2 Stage 7: :mod:`vaultrag.metrics`); ``serve`` mounts the same
registry on its own port instead.

``mcp`` and ``serve`` build their stack through the *same* composition root
(:func:`~vaultrag.composition.build_stack`) and differ only in which transport they hand
the finished service to — see that module for why that matters.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from structlog.typing import FilteringBoundLogger

from vaultrag import __version__
from vaultrag.auth import DEFAULT_RATE_LIMIT, KeyAuthenticator, Scope, generate_key, hash_secret
from vaultrag.composition import ServedStack, build_stack, check_startup_drift
from vaultrag.config import RerankMode, Role, Settings, load_settings
from vaultrag.consistency import DriftError, check_drift
from vaultrag.embedding.base import EmbeddingError
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
    write_candidates,
)
from vaultrag.index.base import VectorStoreError
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.indexer.pipeline import IngestContext, reindex_vault
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.logging import configure_logging, get_logger
from vaultrag.mcp import mcp_server_for
from vaultrag.metrics import set_build_info, start_metrics_server
from vaultrag.parsing.image import load_image_parser
from vaultrag.parsing.pdf import load_pdf_parser
from vaultrag.projects import ProjectRegistry, UnknownProjectError
from vaultrag.store.base import QueryStat, QueryStatOrder
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


@contextmanager
def _registry(base: Settings) -> Iterator[ProjectRegistry | None]:
    """Yield a registry over the shared state db, or ``None`` when there isn't one to read.

    Two ways to get ``None``, both deliberate. **No db file:** a read-only
    ``mcp --project X --vault Y`` must not conjure an empty state db just to discover the
    project isn't registered (the same invariant the graph reader keeps) — and with no db
    there are no registered projects anyway. **An unopenable db** (unwritable state dir, a
    corrupt file): fall through silently so the *command's* own guarded store-open reports
    the real error with a message, rather than this helper raising a traceback out of
    argument resolution.
    """
    if not base.db_path.is_file():
        yield None
        return
    try:
        store = SQLiteMetaStore.from_settings(base)
    except (OSError, sqlite3.Error):
        yield None
        return
    try:
        yield ProjectRegistry(store, base)
    finally:
        store.close()


def _resolve_settings(project: str | None, vault: Path | None) -> Settings:
    """Resolve the :class:`Settings` a command runs under, via the project registry.

    A **registered** ``--project`` supplies its own vault path and any per-project
    overrides (P3 Stage 2), so ``--vault`` stops being mandatory. An **unregistered**
    ``--project`` keeps the P0 single-project shorthand (base settings with the id set), and
    an explicit ``--vault`` always wins — run a registered project against a different tree,
    or point at a vault before registering it.
    """
    base = load_settings()
    resolved: Settings | None = None
    if project is not None:
        with _registry(base) as registry:
            if registry is not None and registry.get(project) is not None:
                resolved = registry.resolve(project)

    settings = resolved or base
    updates: dict[str, object] = {}
    if project is not None and resolved is None:
        updates["project_id"] = project  # unregistered shorthand
    if vault is not None:
        updates["vault_path"] = vault  # explicit --vault overrides the registry
    return settings.model_copy(update=updates) if updates else settings


def _resolve_served_settings(projects: list[str] | None, vault: Path | None) -> list[Settings]:
    """Resolve the set of projects one ``vaultrag mcp`` process serves (P3 Stage 2).

    Three cases, in the order they are checked:

    * **``--project`` given (once or several times)** — serve exactly those. More than one
      must all be registered, because there is no per-project vault path to fall back on;
      one keeps the P0 shorthand.
    * **no ``--project``, projects registered** — serve *all* of them. This is the case the
      stage exists for: ``claude mcp add vaultrag -- uv run vaultrag mcp`` picks up every
      vault the operator registered, and adding a project later needs no re-registration of
      the MCP server.
    * **no ``--project``, nothing registered** — the P0 single-project path, base settings
      as-is (with ``--vault`` applied if given).

    ``--vault`` names one tree, so it is only meaningful for a single project; combining it
    with several is a contradiction rather than a thing to guess at.
    """
    base = load_settings()
    ids = list(projects or [])
    if vault is not None and len(ids) > 1:
        raise typer.BadParameter(
            "--vault names one tree and cannot apply to several --project values; "
            "register each project with `vaultrag project add` instead"
        )
    if not ids and vault is None:
        with _registry(base) as registry:
            ids = [row.id for row in registry.list()] if registry is not None else []
    if len(ids) <= 1:
        return [_resolve_settings(ids[0] if ids else None, vault)]

    with _registry(base) as registry:
        if registry is None:
            raise typer.BadParameter(
                "serving several projects needs the registry, and no state database exists "
                "yet — run `vaultrag project add <id> --vault <path>` first"
            )
        try:
            return [registry.resolve(pid) for pid in ids]
        except UnknownProjectError as exc:
            raise typer.BadParameter(str(exc)) from exc


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
    settings = _resolve_settings(project, vault)
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")

    project_id = settings.project_id
    vault_path = settings.vault_path

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
    settings = _resolve_settings(project, vault)
    configure_logging(settings.log_level)
    log = get_logger("vaultrag.cli")

    project_id = settings.project_id
    vault_path = settings.vault_path
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
        list[str] | None,
        typer.Option(
            "--project",
            help="Project/tenant to serve; repeat to serve several. Omit to serve every "
            "registered project (P3 Stage 2).",
        ),
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault `open` reads from.")
    ] = None,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Also live-watch the vault(s) in background daemon threads (opt-in; "
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
    allow_drift: Annotated[
        bool,
        typer.Option(
            "--allow-drift",
            help="Start even if the manifest and the vector store disagree. The drift is "
            "still measured and logged — only the refusal is waived.",
        ),
    ] = False,
) -> None:
    """Serve the MCP tools (search/open, and write_note/… unless --no-write) over stdio.

    stdout is the JSON-RPC channel, so nothing here may print to it — logs go to
    stderr (see ``vaultrag.logging``) and the FastMCP banner is disabled. Register
    with: ``claude mcp add vaultrag -- uv run vaultrag mcp`` (every registered project), or
    ``… mcp --project X`` for one.
    """
    served_settings = _resolve_served_settings(project, vault)
    # Shared infrastructure is reserved per-project (projects._RESERVED_SETTINGS), so the
    # first project's view of it is every project's view of it.
    base = served_settings[0]
    configure_logging(base.log_level)
    log = get_logger("vaultrag.cli")

    _require_vault_dirs(served_settings, command="mcp")

    # No typer.echo here, unlike `watch`: stdout is the JSON-RPC channel and a stray line
    # corrupts the stream. The bind is announced by structlog on stderr instead.
    _start_metrics(base, requested=metrics)

    project_ids = [s.project_id for s in served_settings]
    try:
        with build_stack(served_settings, write=write, watch=watch) as stack:
            _startup_drift_gate(stack, allow_drift=allow_drift, command="mcp")
            log.info(
                "mcp.serve",
                projects=project_ids,
                transport="stdio",
                watching=watch,
                writing=[p.project_id for p in stack.service.projects if p.writer is not None],
            )
            server = mcp_server_for(stack.service)
            try:
                # blocks until the client detaches
                server.run(transport="stdio", show_banner=False)
            except KeyboardInterrupt:
                log.info("mcp.stopped", projects=project_ids)
    # ValueError = misconfiguration (e.g. VAULTRAG_EMBED_DIMS <= 0); OSError = a
    # client that resolves/binds something at construction; sqlite3.Error = an
    # unopenable write/watch store. Either way the fix is the operator's, so fail with a
    # readable line rather than a stdio traceback.
    except (ValueError, OSError, sqlite3.Error) as exc:
        log.error("mcp.failed", projects=project_ids, error=str(exc))
        typer.echo(f"mcp failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --- serve (P3 Stage 4) ------------------------------------------------------
def _require_vault_dirs(served_settings: list[Settings], *, command: str) -> None:
    """Fail before building anything if a served vault path is not a directory.

    Checked up front because the alternative is a stack that comes up healthy and then
    refuses every ``open`` for one tenant — a misconfiguration that looks like a bug.
    """
    for settings in served_settings:
        if not settings.vault_path.is_dir():
            typer.echo(
                f"{command} failed: vault path for project {settings.project_id!r} is not a "
                f"directory: {settings.vault_path}",
                err=True,
            )
            raise typer.Exit(code=1)


def _startup_drift_gate(stack: ServedStack, *, allow_drift: bool, command: str) -> None:
    """Run the startup drift check and turn a refusal into a readable exit.

    The P2 carry-over: a long-running role must not silently serve an index that disagrees
    with its manifest (see :func:`~vaultrag.composition.check_startup_drift` for why that
    is the failure worth a hard stop). ``--allow-drift`` still *measures* and logs it —
    only the refusal is waived.
    """
    enabled = stack.base.startup_drift_check and not allow_drift
    try:
        check_startup_drift(stack, enabled=enabled)
    except DriftError as exc:
        typer.echo(f"{command} failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def serve(
    project: Annotated[
        list[str] | None,
        typer.Option(
            "--project",
            help="Project/tenant to serve; repeat to serve several. Omit to serve every "
            "registered project.",
        ),
    ] = None,
    vault: Annotated[
        Path | None, typer.Option("--vault", help="Path to the vault, for a single project.")
    ] = None,
    host: Annotated[
        str | None,
        typer.Option("--host", help="Bind address (default 127.0.0.1, VAULTRAG_API_HOST)."),
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Bind port (default 8000, VAULTRAG_API_PORT).")
    ] = None,
    role: Annotated[
        Role | None,
        typer.Option(
            "--role",
            help="'api' serves REST /v1 + /metrics; 'indexer' runs the reconcile + watcher "
            "with no HTTP surface. Defaults to VAULTRAG_ROLE (api).",
        ),
    ] = None,
    write: Annotated[
        bool,
        typer.Option(
            "--write/--no-write",
            help="Expose the write-back endpoints (and, with them, reindex). On by default; "
            "also gated by VAULTRAG_WRITEBACK_ENABLED.",
        ),
    ] = True,
    watch: Annotated[
        bool | None,
        typer.Option(
            "--watch/--no-watch",
            help="Live-watch the served vault(s). Default follows the role: on for indexer, "
            "off for api.",
        ),
    ] = None,
    allow_drift: Annotated[
        bool,
        typer.Option(
            "--allow-drift",
            help="Start even if the manifest and the vector store disagree. The drift is "
            "still measured and logged — only the refusal is waived.",
        ),
    ] = False,
) -> None:
    """Serve the REST API over HTTP (role=api), or run the indexer (role=indexer).

    One image, two roles (reference-docker-compose). The api role binds 127.0.0.1 by
    default and requires 'Authorization: Bearer <key>' on every /v1 route and on /metrics
    (reference-security-model) — argon2-hashed, per-project-scoped, rate-limited keys
    managed with `vaultrag key add|list|revoke`. An empty `api_keys` table gets one
    admin-everywhere key minted and printed at startup so there is always a way in. The
    indexer role runs the boot reconcile and the file watcher, and opens no port beyond
    the optional standalone metrics exporter.
    """
    served_settings = _resolve_served_settings(project, vault)
    base = served_settings[0]
    configure_logging(base.log_level)
    log = get_logger("vaultrag.cli")

    effective_role = role or base.role
    # The watcher default follows the role rather than being a flat False: an indexer whose
    # watcher is off is not an indexer, and an api container that quietly watches would
    # duplicate the indexer's work against the same manifest.
    watching = watch if watch is not None else (effective_role is Role.indexer)

    _require_vault_dirs(served_settings, command="serve")

    project_ids = [s.project_id for s in served_settings]
    try:
        with build_stack(
            served_settings, write=write, watch=watching, auth=(effective_role is Role.api)
        ) as stack:
            _startup_drift_gate(stack, allow_drift=allow_drift, command="serve")
            if effective_role is Role.indexer:
                _run_indexer_role(stack, log=log, watching=watching)
            else:
                _run_api_role(
                    stack,
                    log=log,
                    host=host if host is not None else base.api_host,
                    port=port if port is not None else base.api_port,
                    watching=watching,
                )
    except (ValueError, OSError, sqlite3.Error) as exc:
        log.error("serve.failed", projects=project_ids, error=str(exc))
        typer.echo(f"serve failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _run_api_role(
    stack: ServedStack, *, log: FilteringBoundLogger, host: str, port: int, watching: bool
) -> None:
    """Bind uvicorn over the app built around the already-constructed service.

    ``build_stack(auth=True)`` guarantees ``stack.store`` is open here — the ``api_keys``
    table lives on the same shared store as everything else P3 Stage 5 built on.
    """
    import uvicorn

    from vaultrag.api import create_app

    assert stack.store is not None  # auth=True in build_stack's call above guarantees this
    authenticator = KeyAuthenticator(stack.store)
    minted = authenticator.bootstrap_if_empty(stack.project_ids)
    if minted is not None:
        # Printed to stdout, once: `serve` owns its stdout (unlike `mcp`), and a key that
        # exists only in a log line the operator has to go find is a key they will work
        # around by disabling auth. `vaultrag key add` is how every key after this one is
        # made — there is no env var to set for next time, unlike Stage 4's bootstrap key.
        typer.echo(
            f"no API keys registered — minted one with admin access to every served "
            f"project: {minted}\nStore it now; this is the only time it is shown. Add "
            f"more with `vaultrag key add`, revoke this one with `vaultrag key revoke`."
        )
    app_ = create_app(stack.service, api_keys=authenticator)
    typer.echo(f"serving http://{host}:{port}  (docs: /docs, health: /healthz)")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        # Not a refusal — an operator behind Tailscale has a real reason — but the security
        # model's default is loopback, so a wider bind should never be silent.
        typer.echo(
            f"warning: bound to {host}, not loopback. The API is protected by one bearer "
            "key and no TLS; put it behind Tailscale/WireGuard, not the open internet.",
            err=True,
        )
    log.info(
        "serve.start",
        role="api",
        projects=[p.project_id for p in stack.service.projects],
        host=host,
        port=port,
        watching=watching,
    )
    try:
        uvicorn.run(app_, host=host, port=port, log_config=None)
    except KeyboardInterrupt:
        log.info("serve.stopped", role="api")


def _run_indexer_role(stack: ServedStack, *, log: FilteringBoundLogger, watching: bool) -> None:
    """Reconcile every served vault, then block while the watchers run."""
    if not watching:
        # A one-shot reconcile with no watcher is `vaultrag reindex`; saying so is more
        # useful than blocking forever on a process with nothing to do.
        typer.echo(
            "serve --role indexer --no-watch has nothing to run after startup; "
            "use `vaultrag reindex` for a one-shot reconcile.",
            err=True,
        )
        raise typer.Exit(code=1)
    log.info(
        "serve.start",
        role="indexer",
        projects=[p.project_id for p in stack.service.projects],
        watching=True,
    )
    typer.echo(f"indexing {len(stack.service.projects)} project(s) (Ctrl-C to stop)…")
    try:
        threading.Event().wait()  # block main thread; interruptible by Ctrl-C
    except KeyboardInterrupt:
        typer.echo("stopping…")
        log.info("serve.stopped", role="indexer")


# --- project registry subcommands (P3 Stage 2) ------------------------------
project_app = typer.Typer(
    name="project",
    help="Manage the project registry: one deployment, many vaults (P3 Stage 2).",
    no_args_is_help=True,
)
app.add_typer(project_app, name="project")


def _open_registry() -> tuple[SQLiteMetaStore, ProjectRegistry]:
    """Open the shared metadata store and a registry over it (caller closes the store)."""
    base = load_settings()
    store = SQLiteMetaStore.from_settings(base)
    store.init_schema()
    return store, ProjectRegistry(store, base)


def _parse_kv(pairs: list[str]) -> dict[str, object]:
    """Parse ``key=value`` overrides. Each value is JSON if parseable, else a bare string.

    So ``--set default_top_k=25`` stores the int 25 and ``--set embed_model=custom:1b``
    stores the string — the effective :class:`Settings` still coerces types on resolve, but
    storing them right keeps ``project show`` honest.
    """
    out: dict[str, object] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"--set expects key=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        try:
            out[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            out[key.strip()] = raw
    return out


@project_app.command("add")
def project_add(
    project_id: Annotated[str, typer.Argument(help="Unique project/tenant id.")],
    vault: Annotated[Path, typer.Option("--vault", help="Path to the project's vault.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Display name (defaults to the id).")
    ] = None,
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Per-project settings override, key=value (repeatable). E.g. "
            "--set default_top_k=25 --set embed_model=custom:1b.",
        ),
    ] = None,
) -> None:
    """Register (or update) a project. Idempotent — re-adding updates it in place."""
    store, reg = _open_registry()
    try:
        row = reg.add(project_id, vault, name=name, settings=_parse_kv(set_ or []))
        typer.echo(f"registered {row.id} → {row.vault_path}")
    except (NotADirectoryError, ValueError) as exc:
        typer.echo(f"project add failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@project_app.command("list")
def project_list() -> None:
    """List registered projects with their document counts."""
    store, reg = _open_registry()
    try:
        rows = reg.list()
        if not rows:
            typer.echo("no projects registered — add one with `vaultrag project add`")
            return
        for row in rows:
            typer.echo(f"{row.id}\t{store.document_count(row.id)} docs\t{row.vault_path}")
    finally:
        store.close()


@project_app.command("show")
def project_show(
    project_id: Annotated[str, typer.Argument(help="Project id to show.")],
) -> None:
    """Show one project's registry row (vault, status, doc count, overrides)."""
    store, reg = _open_registry()
    try:
        row = reg.get(project_id)
        if row is None:
            typer.echo(f"no such project: {project_id}", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"id:        {row.id}")
        typer.echo(f"name:      {row.name}")
        typer.echo(f"vault:     {row.vault_path}")
        typer.echo(f"status:    {row.status}")
        typer.echo(f"documents: {store.document_count(row.id)}")
        if row.settings:
            typer.echo(f"settings:  {json.dumps(row.settings)}")
    finally:
        store.close()


@project_app.command("rm")
def project_rm(
    project_id: Annotated[str, typer.Argument(help="Project id to remove.")],
    keep_vectors: Annotated[
        bool,
        typer.Option(
            "--keep-vectors",
            help="Leave this project's points in the vector store (they become orphaned). "
            "For recovering a mistaken removal; not a normal operation.",
        ),
    ] = False,
) -> None:
    """Remove a project and cascade its index rows + vectors. The vault on disk is untouched.

    **Both** derived caches are cleared, because they are lost independently and only the
    composition root holds them both. Clearing SQLite alone was the original behaviour and it
    is a bug found by dogfooding: the vectors outlive the documents, and re-registering the
    same id would make stale chunks searchable with no ``documents`` row behind them.

    The vector delete runs **first**: if it fails, the project is still registered and the
    command can simply be re-run. The other order would deregister a project whose vectors
    are still live and no longer reachable through the registry to clean up.
    """
    base = load_settings()
    store, reg = _open_registry()
    vector_store: QdrantVectorStore | None = None
    try:
        if reg.get(project_id) is None:
            typer.echo(f"no such project: {project_id}", err=True)
            raise typer.Exit(code=1)
        if not keep_vectors:
            try:
                vector_store = QdrantVectorStore.from_settings(base)
                vector_store.delete_by_project(project_id)
            except (VectorStoreError, OSError, ValueError) as exc:
                typer.echo(
                    f"removing {project_id} failed: could not clear its vectors ({exc}). "
                    f"The project is still registered — fix the vector store and retry, or "
                    f"use --keep-vectors to deregister it anyway.",
                    err=True,
                )
                raise typer.Exit(code=1) from exc
        reg.remove(project_id)
        kept = " (vectors kept, now orphaned)" if keep_vectors else ""
        typer.echo(
            f"removed {project_id} (index rows + vectors cascaded; vault on disk untouched){kept}"
        )
    finally:
        for closer in (vector_store, store):
            if closer is not None:
                closer.close()


# --- API keys (P3 Stage 5) ----------------------------------------------------
key_app = typer.Typer(
    name="key",
    help="Manage API keys for the HTTP transport: mint, list, revoke (P3 Stage 5).",
    no_args_is_help=True,
)
app.add_typer(key_app, name="key")


def _open_key_store() -> SQLiteMetaStore:
    """Open the shared metadata store the ``api_keys`` table lives on (caller closes it)."""
    store = SQLiteMetaStore.from_settings(load_settings())
    store.init_schema()
    return store


def _parse_scopes(pairs: list[str]) -> dict[str, str]:
    """Parse repeatable ``project:level`` pairs into a scopes mapping.

    Validated against :class:`~vaultrag.auth.Scope` here, at the CLI boundary, rather than
    left for the authenticator to discover at request time — a typo'd level should fail
    the command that made it, not silently mint a key nothing can ever use.
    """
    out: dict[str, str] = {}
    for pair in pairs:
        project_id, sep, level = pair.partition(":")
        if not sep or not project_id or not level:
            raise typer.BadParameter(f"--scope expects project:level, got {pair!r}")
        try:
            out[project_id] = Scope(level).value
        except ValueError as exc:
            raise typer.BadParameter(
                f"--scope level must be one of {[s.value for s in Scope]}, got {level!r}"
            ) from exc
    return out


@key_app.command("add")
def key_add(
    scope: Annotated[
        list[str] | None,
        typer.Option(
            "--scope",
            help="project:level, repeatable — level is read/write/admin, each implying "
            "the ones before it. E.g. --scope vaultrag-design:read "
            "--scope vaultrag-code:write.",
        ),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Label shown by `key list` (e.g. 'ci-bot').")
    ] = None,
    rate_limit: Annotated[
        int,
        typer.Option(
            "--rate-limit",
            help="Requests/minute this key's token bucket refills to. 0 disables the limit.",
        ),
    ] = DEFAULT_RATE_LIMIT,
) -> None:
    """Mint a new key and print it once. Nothing on this process holds it again.

    The store never sees the raw key, only its argon2 hash — a lost key has no recovery
    path, the same as any password: revoke it and mint a new one.
    """
    scopes = _parse_scopes(scope or [])
    if not scopes:
        typer.echo("key add failed: at least one --scope project:level is required", err=True)
        raise typer.Exit(code=1)
    store = _open_key_store()
    try:
        material = generate_key()
        store.create_api_key(
            key_id=material.id,
            name=name,
            key_hash=hash_secret(material.secret),
            scopes=scopes,
            rate_limit=rate_limit,
        )
        typer.echo(f"key: {material.raw}")
        typer.echo("store it now — this is the only time it is shown.")
        typer.echo(f"id: {material.id}  scopes: {scopes}  rate_limit: {rate_limit}/min")
    finally:
        store.close()


@key_app.command("list")
def key_list() -> None:
    """List keys by id and scope — never the key itself, never its hash."""
    store = _open_key_store()
    try:
        rows = store.list_api_keys()
        if not rows:
            typer.echo("no API keys registered — `serve` mints one on its next start")
            return
        for row in rows:
            scopes = ", ".join(f"{p}:{lvl}" for p, lvl in sorted(row.scopes.items()))
            last = "never" if row.last_used is None else str(row.last_used)
            label = f" ({row.name})" if row.name else ""
            typer.echo(f"{row.id}{label}\t{scopes}\trate={row.rate_limit}/min\tlast_used={last}")
    finally:
        store.close()


@key_app.command("revoke")
def key_revoke(
    key_id: Annotated[str, typer.Argument(help="Key id, as shown by `key list`.")],
) -> None:
    """Revoke a key. Takes effect on its very next request, not the next restart."""
    store = _open_key_store()
    try:
        if not store.delete_api_key(key_id):
            typer.echo(f"no such key: {key_id}", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"revoked {key_id}")
    finally:
        store.close()


# --- query log / mining (P3 Stage 3) ----------------------------------------
class QueryReport(StrEnum):
    """Which cut of the query log ``vaultrag queries`` prints."""

    summary = "summary"  # a little of each — the "what happened lately" default
    frequent = "frequent"  # most-asked
    slowest = "slowest"  # worst average latency
    recent = "recent"  # most-recently asked
    worst = "worst"  # lowest best-score — the queries retrieval answers badly
    zero_hit = "zero-hit"  # never returned a result at all (rare; see `queries`' docstring)


#: How many rows each section of the `summary` report shows. Small on purpose: summary is
#: a glance, and every cut is available in full through its own `--report`.
_SUMMARY_ROWS = 5


def _queries_project(project: str | None) -> str:
    """Which project a query-log command reports on.

    Same rule the MCP tools use, for the same reason: with one project the argument is
    optional, with several it is required and the error names them. A CLI that quietly
    picked the alphabetically-first project would report one tenant's numbers under
    another's name.
    """
    if project is not None:
        return project
    base = load_settings()
    with _registry(base) as registry:
        rows = registry.list() if registry is not None else []
    if len(rows) == 1:
        return rows[0].id
    if len(rows) > 1:
        raise typer.BadParameter(
            "--project is required when several projects are registered: "
            + ", ".join(sorted(r.id for r in rows))
        )
    return base.project_id


def _format_stat(stat: QueryStat) -> str:
    """One report line. Falls back to the hash when the text was not stored."""
    label = stat.query_text if stat.query_text is not None else f"<{stat.q_hash}>"
    score = f"{stat.avg_top_score:>5.2f}" if stat.avg_top_score is not None else "    -"
    return (
        f"{stat.count:>5}x  {stat.avg_latency_ms:>7.0f} ms avg  "
        f"{stat.avg_results:>4.1f} hits  top {score}  {label}"
    )


def _candidate_note(stat: QueryStat) -> str:
    """Why this query was mined — the thing that decides, weeks later, if it is worth a case."""
    if stat.max_results == 0:
        why = "zero-hit"
    elif stat.best_top_score is not None:
        why = f"best score {stat.best_top_score:.2f}"
    else:
        why = f"<={stat.max_results} hits"
    seen = time.strftime("%Y-%m-%d", time.gmtime(stat.last_ts))
    return f"{why}, asked {stat.count}x, last seen {seen}"


def _print_stats(title: str, stats: list[QueryStat]) -> None:
    typer.echo(f"\n{title}")
    if not stats:
        typer.echo("  (none)")
        return
    for stat in stats:
        typer.echo(f"  {_format_stat(stat)}")


@app.command()
def queries(
    project: Annotated[
        str | None, typer.Option("--project", help="Project whose query log to report on.")
    ] = None,
    report: Annotated[
        QueryReport,
        typer.Option("--report", help="Which cut of the log to print."),
    ] = QueryReport.summary,
    limit: Annotated[int, typer.Option("--limit", help="Rows per section.")] = 20,
    since_days: Annotated[
        int | None,
        typer.Option("--since-days", help="Only consider queries from the last N days."),
    ] = None,
    max_results: Annotated[
        int | None,
        typer.Option(
            "--max-results",
            help="Keep only queries whose best run returned at most N snippets. Weak on a "
            "hybrid retriever, which almost always returns something — prefer --max-score.",
        ),
    ] = None,
    max_score: Annotated[
        float | None,
        typer.Option(
            "--max-score",
            help="Keep only queries whose best snippet ever scored at or below this. The "
            "useful export selector. Per-vault: read a threshold off `--report worst` "
            "rather than assuming one, since the scale depends on which stages ran.",
        ),
    ] = None,
    export: Annotated[
        Path | None,
        typer.Option(
            "--export",
            help="Write the selected queries to a golden-set-shaped candidate file for "
            "review (expected_paths left blank — only you can say what should have matched).",
        ),
    ] = None,
    prune: Annotated[
        bool,
        typer.Option("--prune", help="Apply the retention caps now and report what was dropped."),
    ] = False,
) -> None:
    """Report on the query log and mine it for golden-set candidates (P3 Stage 3).

    The log is written by the core retrieval pipeline, so it covers every search the
    process served regardless of transport. Its point is the loop back into the eval gate:
    a query retrieval answers badly is a failure nobody wrote a test for, and ``--export``
    turns those into cases the harness can score once you fill in what *should* have
    answered them (``../Rag/projects/vaultrag/evaluation-framework.md``).

    **Start with ``--report worst``, not ``--report zero-hit``.** A hybrid retriever ranks
    *something* first for any query, so genuinely empty results are rare — measured on the
    design vault, a question about Stripe webhooks returns eight confident-looking hits from
    a vault that has never heard of Stripe. What separates those from real answers is the
    *score* of the best hit, not the count. Read a cutoff off ``worst`` and pass it to
    ``--export --max-score``; there is deliberately no default threshold, because the score
    scale depends on which retrieval stages ran (RRF, cross-encoder sigmoid, or constant).
    """
    settings = load_settings()
    project_id = _queries_project(project)
    since_ts = int(time.time()) - since_days * 86400 if since_days else None
    # Opened, never created: no db means no index, and a report on an index that was never
    # built is a blank page with extra steps.
    if not settings.db_path.is_file():
        typer.echo(f"no query log yet at {settings.db_path} — nothing has been indexed here")
        raise typer.Exit(code=1)
    store = SQLiteMetaStore.from_settings(settings)
    try:
        if prune:
            dropped = store.prune_query_log(
                max_age_days=settings.query_log_retention_days,
                max_rows=settings.query_log_max_rows,
            )
            typer.echo(
                f"pruned {dropped} rows (keep {settings.query_log_retention_days}d / "
                f"{settings.query_log_max_rows} rows)"
            )

        total = store.query_log_count(project_id)
        typer.echo(f"{project_id}: {total} logged queries")
        if total == 0 and not settings.query_log_enabled:
            typer.echo("query logging is off (VAULTRAG_QUERY_LOG_ENABLED=false)")

        def cut(
            order: QueryStatOrder,
            n: int,
            cap: int | None = None,
            score_cap: float | None = None,
        ) -> list[QueryStat]:
            return store.query_stats(
                project_id,
                order=order,
                limit=n,
                since_ts=since_ts,
                max_results=cap,
                max_score=score_cap,
            )

        if export is None:
            if report is QueryReport.summary:
                _print_stats("most frequent", cut(QueryStatOrder.frequent, _SUMMARY_ROWS))
                _print_stats("slowest", cut(QueryStatOrder.slowest, _SUMMARY_ROWS))
                _print_stats("worst-served", cut(QueryStatOrder.worst, _SUMMARY_ROWS))
            elif report is QueryReport.zero_hit:
                zero = cut(QueryStatOrder.frequent, limit, 0)
                _print_stats("zero-hit", zero)
                if not zero and total:
                    # Say why it is empty rather than letting it read as "retrieval is
                    # perfect" — this report is near-inert by construction on hybrid search.
                    typer.echo(
                        "\n  hybrid search nearly always returns something, so an empty "
                        "zero-hit report is normal.\n  Use --report worst to see the "
                        "queries answered badly."
                    )
            else:
                _print_stats(report.value, cut(QueryStatOrder(report.value), limit))
            return

        # --- export ---------------------------------------------------------
        # With no selector this is the zero-hit set, which is correct but usually empty;
        # --max-score is the one that actually mines a hybrid retriever.
        cap = max_results if max_results is not None else (None if max_score else 0)
        stats = cut(QueryStatOrder.worst, limit, cap, max_score)
        named = [s for s in stats if s.query_text]
        if stats and not named:
            typer.echo(
                f"{len(stats)} queries match, but none has stored text — this deployment "
                f"runs with VAULTRAG_QUERY_LOG_TEXT=false, and a hash cannot become a "
                f"golden-set case. Turn it on to mine future queries.",
                err=True,
            )
            raise typer.Exit(code=1)
        written = write_candidates(
            export,
            project_id=project_id,
            candidates=[(str(s.query_text), _candidate_note(s)) for s in named],
        )
        skipped = len(stats) - len(named)
        note = f" ({skipped} skipped: no stored text)" if skipped else ""
        if not written:
            typer.echo(
                "no queries matched — with no --max-score this exports only the zero-hit "
                "set, which hybrid search rarely produces. Try `--report worst` to pick a "
                "cutoff, then `--export --max-score <cutoff>`."
            )
        typer.echo(
            f"wrote {written} candidates to {export}{note} — fill in expected_paths, then "
            f"merge the ones worth keeping into a golden set"
        )
    finally:
        store.close()


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
