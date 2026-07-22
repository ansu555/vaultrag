"""The composition root: :class:`~vaultrag.config.Settings` → a live, closable stack.

Every long-running role — ``vaultrag mcp`` (stdio) and ``vaultrag serve`` (HTTP) — needs
the same thing built the same way: a Qdrant client, a reranker, one shared SQLite store,
and then per project an embedder, a vault root, maybe a writer, maybe an ingest context.
Before P3 Stage 4 that lived inside the ``mcp`` command, which was fine while there was one
role. With two, leaving it there would have meant ``serve`` growing a second copy — and a
second copy is not a stylistic problem: it is where one transport ends up with the graph
store attached and the other without it, or where one opens a metadata store the other
declines to, and the two front doors quietly start serving different systems.

So the rule this module enforces is the same one P3 Stage 3 applied to the query log and
Stage 4 applied to the operations: **build it once, in the core, and let the transports
inherit it.** ``serve`` and ``mcp`` differ in exactly one line each — which transport they
hand the finished :class:`~vaultrag.service.VaultRAGService` to.

What is built once vs per project mirrors :class:`~vaultrag.service.ServedProject`'s own
split: the vector store, sparse embedder, reranker and metadata store are already
``project_id``-scoped on every call, while a vault root, a writer and an embedder belong to
exactly one tenant.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from vaultrag.config import Settings
from vaultrag.consistency import Drift, DriftError, check_drift
from vaultrag.embedding import OllamaEmbedder
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index import QdrantVectorStore
from vaultrag.index.base import VectorStoreError
from vaultrag.index.sparse import Bm25Embedder
from vaultrag.indexer.pipeline import IngestContext
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.logging import get_logger
from vaultrag.metrics import init_project_labels, set_index_drift
from vaultrag.parsing.image import load_image_parser
from vaultrag.parsing.pdf import load_pdf_parser
from vaultrag.retrieval import (
    compress_params_for,
    graph_params_for,
    query_log_params_for,
    rerank_params_for,
)
from vaultrag.retrieval.rerank import load_reranker
from vaultrag.service import ServedProject, VaultRAGService
from vaultrag.store.sqlite import SQLiteMetaStore
from vaultrag.writeback import NoteWriter

log = get_logger(__name__)

__all__ = ["ServedStack", "build_stack", "check_startup_drift"]


@dataclass(slots=True)
class ServedStack:
    """A built service plus the resources that must be closed when the role exits.

    The service is the only thing a transport touches; everything else on this object
    exists so the command that opened these handles can close them in the right order.
    """

    service: VaultRAGService
    settings: list[Settings]
    vector_store: QdrantVectorStore
    store: SQLiteMetaStore | None = None
    embedders: list[OllamaEmbedder] = field(default_factory=list)
    watchers: list[VaultWatcher] = field(default_factory=list)

    @property
    def base(self) -> Settings:
        """Settings for the shared infrastructure.

        Shared infrastructure is reserved per-project (``projects._RESERVED_SETTINGS``), so
        the first project's view of it is every project's view of it.
        """
        return self.settings[0]

    @property
    def project_ids(self) -> list[str]:
        return [s.project_id for s in self.settings]

    def close(self) -> None:
        """Stop watchers, then close every handle. Safe to call twice."""
        for watcher in self.watchers:
            watcher.stop()
        self.watchers.clear()
        for closer in (*self.embedders, self.vector_store, self.store):
            if closer is not None:
                closer.close()
        self.embedders.clear()


@contextmanager
def build_stack(
    served_settings: Sequence[Settings],
    *,
    write: bool = True,
    watch: bool = False,
    auth: bool = False,
) -> Iterator[ServedStack]:
    """Build the whole stack for one role, and tear it down on exit.

    ``write`` exposes the write operations (and, with them, the per-project ingest context
    that ``POST /v1/{project}/reindex`` needs — the same capability under two names: both
    mean "this process may modify this project's index"). ``watch`` additionally starts a
    live watcher per vault. ``auth`` is P3 Stage 5's addition: the ``api`` role always needs
    the ``api_keys`` table to authenticate a request, even when it serves no writes, no
    watch, and no graph — a read-only deployment is still an HTTP deployment, and an HTTP
    deployment always needs somewhere to check a key against.

    By default search reads only Qdrant and ``open`` reads only the vault, and neither
    client connects at construction time, so a read-only stack comes up instantly (a down
    Qdrant/Ollama surfaces as an actionable per-call error). Four features need more: the
    write operations and ``watch`` both drive the single-file pipeline, graph expand +
    ``related`` read the link graph, and now auth reads the key table — all of them want
    the SQLite store, so it is opened once and shared.

    Raises ``ValueError`` (misconfiguration), ``OSError`` (a client that resolves/binds at
    construction) or ``sqlite3.Error`` (an unopenable store) — the caller owns how to
    report them, because a stdio role and an HTTP role fail differently.
    """
    if not served_settings:
        raise ValueError("build_stack needs at least one project's settings")
    settings_list = list(served_settings)
    base = settings_list[0]

    vector_store = QdrantVectorStore.from_settings(base)
    stack = ServedStack(
        service=None,  # type: ignore[arg-type]  # assigned below, before anything can read it
        settings=settings_list,
        vector_store=vector_store,
    )
    try:
        # Built at startup so the (possibly first-run) model download blocks here,
        # visibly, not mid-query. load_reranker never raises: a fetch/load failure
        # degrades to a no-op reranker so the server still serves (rerank-off).
        reranker = load_reranker(
            mode=base.rerank_mode,
            repo_id=base.rerank_model,
            model_file=base.rerank_model_file,
        )

        # One store for every tenant — it is one db with `project_id` on every row
        # (payload-partitioned tenancy, [[multi-tenant-architecture]]). SQLite serializes
        # its own writes and is opened check_same_thread=False for exactly this
        # multi-thread reuse. The write/watch/auth paths *create* it — write/watch own the
        # manifest and jobs tables, and auth owns (or bootstraps) the key table. The
        # graph/overview readers only attach to an existing DB and never conjure one: a
        # read-only server pointed at a state dir with no index would otherwise leave an
        # empty database behind, and an index that was never built has no graph to serve
        # anyway. ``auth`` is unconditional (not gated on the db already existing) because
        # an api role with no prior state is exactly the first-boot case that needs the
        # table created so a key can be bootstrapped into it.
        any_write = any(write and s.writeback_enabled for s in settings_list)
        any_read_store = base.db_path.is_file() and any(
            s.graph_expand_enabled for s in settings_list
        )
        # The query log (P3 Stage 3) attaches under the same rule as the graph reader,
        # and for a sharper reason: a state dir with no database has no index either, so
        # every search against it is a zero-hit by construction. Conjuring a db to record
        # that would fill the mining report with rows that say nothing about retrieval.
        any_query_log = base.db_path.is_file() and any(s.query_log_enabled for s in settings_list)
        if any_write or watch or any_read_store or any_query_log or auth:
            stack.store = SQLiteMetaStore.from_settings(base)
        store = stack.store

        # Only the paths that *ingest* need it (write-back, --watch); a read-only
        # server never converts anything, so don't pay for the parser it won't use.
        ingests = any_write or watch
        pdf_parser = load_pdf_parser() if ingests else None

        served: list[ServedProject] = []
        for settings in settings_list:
            vault_root = settings.vault_path.resolve()
            write_enabled = write and settings.writeback_enabled
            # Obsidian display name for citation URIs (stage 8) + write-back note links:
            # configured value, else the vault directory name.
            vault_name = settings.vault_name or vault_root.name
            # Per project: the embedding model is a per-project setting
            # ([[multi-tenant-architecture]]), so each tenant queries through its own.
            embedder = OllamaEmbedder.from_settings(settings)
            stack.embedders.append(embedder)
            image_parser = load_image_parser(enabled=settings.ocr_enabled) if ingests else None
            # Built only when something will drive it: a read-only server has no store
            # to put in one, and constructing a half-valid context to discard is how a
            # None slips into the write path later.
            ctx: IngestContext | None = None
            if store is not None and (write_enabled or watch):
                ctx = IngestContext(
                    project_id=settings.project_id,
                    vault_path=vault_root,
                    store=store,
                    vector_store=vector_store,
                    embedder=embedder,
                    pdf_parser=pdf_parser,
                    image_parser=image_parser,
                )
            writer: NoteWriter | None = None
            if write_enabled:
                assert ctx is not None  # any_write opened the store above
                writer = NoteWriter(
                    ctx,
                    author=settings.writeback_author,
                    allow_roots=settings.writeback_allow_roots,
                    max_bytes=settings.writeback_max_bytes,
                    max_writes_per_minute=settings.writeback_max_writes_per_minute,
                    git_enabled=settings.writeback_git_enabled,
                    vault_name=vault_name,
                )
            served.append(
                ServedProject(
                    project_id=settings.project_id,
                    vault_root=vault_root,
                    embedder=QueryEmbedder(embedder),
                    vault_name=vault_name,
                    writer=writer,
                    default_k=settings.default_top_k,
                    default_budget_tokens=settings.budget_tokens,
                    rerank_params=rerank_params_for(settings),
                    parent_swap_enabled=settings.parent_swap_enabled,
                    parent_max_tokens=settings.parent_max_tokens,
                    graph_params=graph_params_for(settings),
                    # Stage 6 + the `related` tool: one flag drives both, so there is no
                    # half-on state where `related` answers from a graph that search
                    # ignores. `store` can be non-None purely for write-back/--watch,
                    # hence the second term.
                    graph_enabled=settings.graph_expand_enabled,
                    compress_params=compress_params_for(settings),
                    # Per-project policy; `store is None` means there is nowhere to
                    # write, so the policy is moot rather than silently ignored.
                    query_log=(query_log_params_for(settings) if store is not None else None),
                    # Same object the writer drives, so a reindex over HTTP and a
                    # write-back ingest cannot disagree about parsers or stores.
                    ingest=ctx,
                )
            )
            init_project_labels(settings.project_id)
            if watch:
                # The observer + dispatcher are daemon threads that die with this
                # process; stop() in close() drains them on a clean exit. One watcher per
                # served vault — they share the store and the vector store, and each owns
                # its own debounce/worker pool.
                assert ctx is not None  # `watch` opened the store above
                watcher = VaultWatcher(
                    ctx,
                    debounce_seconds=settings.watch_debounce_seconds,
                    workers=settings.watch_workers,
                )
                stack.watchers.append(watcher)
                watcher.start()

        stack.service = VaultRAGService(
            projects=served,
            vector_store=vector_store,
            sparse=Bm25Embedder(),
            reranker=reranker,
            graph_store=store if any(p.graph_enabled for p in served) else None,
            meta_store=store,
            # Shared, like every other project_id-scoped store; the per-project
            # decision to record at all rides on ServedProject.query_log.
            query_log_sink=store if any(p.query_log is not None for p in served) else None,
        )
        yield stack
    finally:
        stack.close()


def check_startup_drift(stack: ServedStack, *, enabled: bool = True) -> list[Drift]:
    """Compare every served project's manifest against the vector store, at startup.

    The P2 carry-over this stage closes. Drift is the failure mode with no symptom: a
    vector store that lost points SQLite still records answers every query *plausibly*,
    from a fraction of the vault, and ``reindex`` reports the missing documents as
    ``unchanged`` and skips exactly what needs rebuilding (``consistency.py``). A
    long-running role is the worst place for it, because the state persists for as long as
    the process does — which is precisely why the check belongs at startup rather than in
    a flag someone sets after they already suspect a problem.

    Always publishes the gauge, even when ``enabled`` is false: refusing to *fail* on drift
    is a reasonable operator choice, refusing to *measure* it is not. Returns the drifted
    projects; raises :class:`~vaultrag.consistency.DriftError` when ``enabled``, so the
    role stops instead of serving stale results.

    **An unreachable vector store is not drift, and must not fail startup.** This check is
    the first thing in the process that would touch Qdrant, and before it existed a
    read-only ``vaultrag mcp`` came up instantly whether or not Qdrant was running — a down
    backend surfaced as an actionable per-call error. Turning that into a refusal to launch
    would trade a loud, recoverable failure for a dead agent session, and it would do so to
    guard against the *opposite* problem: drift is dangerous precisely because it is
    **silent**, and a Qdrant that answers nothing is the least silent failure there is. So a
    store error skips the project, logs, and leaves the gauge unset — "unknown" is a third
    state, and publishing 0 for it would be a lie.
    """
    if stack.store is None:
        # No metadata store means no manifest to compare against — a read-only role over
        # an index this process cannot see. Nothing to check, and nothing wrong.
        return []
    drifted: list[Drift] = []
    for settings in stack.settings:
        try:
            drift = check_drift(
                settings.project_id, store=stack.store, vector_store=stack.vector_store
            )
        except (VectorStoreError, OSError) as exc:
            log.warning(
                "startup.drift_check_unavailable",
                project=settings.project_id,
                error=str(exc),
            )
            continue
        set_index_drift(0 if drift is None else drift.missing, project=settings.project_id)
        if drift is not None:
            drifted.append(drift)
    if not drifted:
        return []
    for drift in drifted:
        log.warning("startup.drift", project=drift.project_id, missing=drift.missing)
    if not enabled:
        return drifted
    details = "; ".join(d.describe() for d in drifted)
    raise DriftError(
        f"{details}. Serving this index would answer queries from part of the vault with "
        f"no signal that anything is missing. Repair it with `vaultrag reindex --force "
        f"--project {drifted[0].project_id}`, or start with --allow-drift "
        f"(VAULTRAG_STARTUP_DRIFT_CHECK=false) if you know why."
    )
