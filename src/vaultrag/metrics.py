"""Prometheus metrics + the standalone ``/metrics`` exporter (P2 Stage 7).

Implements the series named in ``../Rag/projects/vaultrag/monitoring-observability.md``
(arch §22): ingest lag, job queue depth/deaths, embed-cache effectiveness, per-stage search
latency, rerank skip rate, empty-result rate, and write-back volume. structlog already
emits per-stage timings for *one* request; this adds the aggregate scrape surface — the
thing that answers "is ingest lagging *right now*" without grepping a log.

**Transport (p2-step-plan Decision 5).** There is no FastAPI ``serve`` role yet, so the
exporter is standalone: :func:`start_metrics_server` runs ``prometheus_client``'s own tiny
WSGI server on its own port. When ``serve`` lands (P3) it mounts :data:`registry` at
``/metrics`` and this function becomes one of two front-ends onto the same registry — no
rework, and nothing here is coupled to the HTTP transport.

**Four rules this module holds to.**

1. **Ratios are exported as their components, never as a ratio gauge.** The design note
   names ``embed_cache_hit_ratio`` and ``empty_result_ratio``; both ship here as
   *counter pairs* (``vaultrag_embed_cache_lookups_total{result}``,
   ``vaultrag_searches_total`` / ``vaultrag_searches_empty_total``), and
   ``rerank_skipped_total`` ships as ``vaultrag_rerank_decisions_total{decision}`` for the
   same reason. A ratio computed in-process is a **lifetime average**: it cannot be
   windowed, so after a week of uptime a real regression is invisible inside it and
   "did the cache hit rate drop this hour?" has no answer. Exported as components, PromQL
   divides over any window (``rate(...{result="hit"}[1h]) / rate(...[1h])``) and the
   lifetime average is still recoverable. This is Prometheus's own guidance and the one
   place the metric *names* deliberately diverge from the vault note.

2. **No metric carries a path, query, or title label.** Cardinality first — one series per
   vault file would dwarf the vault — but privacy too: ``/metrics`` is unauthenticated, and
   CLAUDE.md §6 keeps raw query text out of logs, so it certainly does not belong in a
   label. ``project`` **is** a label as of P3 Stage 2, and only as of then: it was withheld
   while one process served one project (where it is a constant) and earns its place now
   that one process serves N, because "is *this* tenant's ingest lagging / returning
   nothing?" is unanswerable without it. It is bounded by the registry, which is an
   operator-managed list, not by user input.

   It is on the **counters** and on ``ingest_lag`` — the per-tenant questions — and
   deliberately **not** on ``search_latency``. That histogram is already 8 stages by 18
   buckets; multiplying it by tenants buys the least (stage latency is dominated by shared
   components — the embedder, Qdrant, the reranker — not by which vault was queried) and
   costs the most. Cardinality is spent where it answers something.

3. **The exporter never takes down the process it observes.** A failed bind (the usual
   cause: ``vaultrag watch`` and ``vaultrag mcp --metrics`` both up) logs a warning and
   returns ``False``. Losing metrics is an inconvenience; losing the indexer is an outage.

4. **Recording is always on; the config flag gates only the HTTP server.** A counter
   increment is sub-microsecond, so there is nothing to save by branching at every call
   site — and a metrics-disabled build whose call sites were compiled out would silently
   report zeros the moment someone enabled it mid-process.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import start_http_server as _start_http_server

log = structlog.get_logger(__name__)

DEFAULT_METRICS_PORT = 9464
DEFAULT_METRICS_ADDR = "127.0.0.1"

#: Stages of :func:`~vaultrag.retrieval.pipeline.search`, in pipeline order, plus the
#: end-to-end ``total``. Pre-initialised as label values so every stage appears in a
#: scrape at zero rather than materialising on first use — a series that only exists
#: after the stage first runs makes ``rate()`` undefined exactly when you need it.
SEARCH_STAGES = ("embed", "fetch", "rerank", "parents", "graph", "cite", "compress", "total")

#: The two routes by which a change becomes searchable — see :func:`record_ingest_lag`.
INGEST_SOURCES = ("watch", "writeback")

#: Mirrors :class:`~vaultrag.indexer.pipeline.IngestStatus`, same leaf argument as
#: :data:`WRITE_ACTIONS` below. ``test_metrics`` pins the two in sync.
INGEST_STATUSES = ("indexed", "unchanged", "removed", "skipped")

#: Mirrors :class:`~vaultrag.writeback.writer._Action`. Duplicated rather than imported
#: because this module is a leaf (like ``config``/``logging``) that write-back imports —
#: importing back would invert the dependency. ``test_metrics`` pins the two in sync.
WRITE_ACTIONS = ("created", "appended", "patched")

#: Mirrors :class:`~vaultrag.writeback.errors.RefusalReason`, same leaf argument as above.
WRITE_REFUSAL_REASONS = (
    "disabled",
    "empty-path",
    "empty",
    "escapes-vault",
    "outside-write-root",
    "gated",
    "not-markdown",
    "not-utf8",
    "secret",
    "exists",
    "not-found",
    "heading-not-found",
    "too-large",
    "rate-limited",
)

# Search stages on this system span five orders of magnitude — graph expand measured
# p50 0.39 ms, a reranked query ~25 s at rerank_top_n=50 (both MEMORY.md, 2026-07-21).
# prometheus_client's default buckets (5 ms … 10 s) would put every graph observation in
# the first bucket and every reranked one in +Inf, i.e. no usable p50 *or* p95 at either
# end — the exact two numbers the design note asks for. Hence an explicit decade ladder.
_LATENCY_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)

# Ingest lag is bounded below by the 1.5 s debounce (a save storm must go quiet before
# work starts), so sub-second buckets would all be empty; the interesting range runs from
# "normal" (~2 s) to the note's alert threshold (5 min) and beyond.
_LAG_BUCKETS = (1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)


@dataclass(frozen=True, slots=True)
class _Metrics:
    """The metric objects, bound to one registry.

    Held in a dataclass rather than as module globals so :func:`reset_metrics` can rebuild
    the whole set for a test. Call sites go through the ``record_*`` helpers below and
    never hold a reference to a metric object, so a rebuild can't strand one.
    """

    registry: CollectorRegistry
    build_info: Gauge
    ingest_lag: Histogram
    ingests: Counter
    jobs_queue_depth: Gauge
    jobs_dead: Counter
    embed_cache_lookups: Counter
    search_latency: Histogram
    searches: Counter
    searches_empty: Counter
    rerank_decisions: Counter
    write_ops: Counter
    write_refused: Counter
    index_drift: Gauge


def _build() -> _Metrics:
    """Construct a fresh registry and every metric on it, with labels pre-initialised.

    A private registry (not ``prometheus_client.REGISTRY``) keeps VaultRAG's series out of
    any embedding process's global default and — the practical reason — makes rebuilding
    idempotent: re-registering the same name on the global default raises, so a module
    reimport or a second test would crash on a name collision.
    """
    registry = CollectorRegistry()

    build_info = Gauge(
        "vaultrag_build_info",
        "Build metadata for the running process; the value is always 1.",
        ["version"],
        registry=registry,
    )
    ingest_lag = Histogram(
        "vaultrag_ingest_lag_seconds",
        "Seconds from a change being made to that path being searchable, by ingest route.",
        ["source", "project"],
        buckets=_LAG_BUCKETS,
        registry=registry,
    )
    ingests = Counter(
        "vaultrag_ingests_total",
        "Single-path reconciles completed by the watcher, by outcome.",
        ["status", "project"],
        registry=registry,
    )
    jobs_queue_depth = Gauge(
        "vaultrag_jobs_queue_depth",
        "Paths awaiting or undergoing ingest (debounce-pending plus in-flight).",
        registry=registry,
    )
    jobs_dead = Counter(
        "vaultrag_jobs_dead_total",
        "Ingest jobs parked dead after exhausting their retries.",
        registry=registry,
    )
    embed_cache_lookups = Counter(
        "vaultrag_embed_cache_lookups_total",
        "Chunk-embedding cache lookups by result; hit ratio is the PromQL of the two.",
        ["result"],
        registry=registry,
    )
    search_latency = Histogram(
        "vaultrag_search_latency_seconds",
        "Wall time of each search stage, plus the end-to-end total.",
        ["stage"],
        buckets=_LATENCY_BUCKETS,
        registry=registry,
    )
    searches = Counter(
        "vaultrag_searches_total",
        "Searches served (the denominator for the empty-result ratio).",
        ["project"],
        registry=registry,
    )
    searches_empty = Counter(
        "vaultrag_searches_empty_total",
        "Searches that returned no snippets.",
        ["project"],
        registry=registry,
    )
    rerank_decisions = Counter(
        "vaultrag_rerank_decisions_total",
        "Per-query rerank decisions; 'skipped' is auto-mode's fusion-dominance fast path.",
        ["decision"],
        registry=registry,
    )
    write_ops = Counter(
        "vaultrag_write_ops_total",
        "Write-back operations that reached the vault, by action.",
        ["action", "project"],
        registry=registry,
    )
    write_refused = Counter(
        "vaultrag_write_refused_total",
        "Write-back operations refused by a guardrail, by reason.",
        ["reason"],
        registry=registry,
    )

    # Pre-initialise every known label value to 0 so a scrape shows the full shape of the
    # system from the first second. Without this a stage/reason that has not fired yet is
    # simply absent, and "no series" is indistinguishable from "not scraped".
    #
    # The `project`-labelled metrics are absent from this loop by necessity — their label
    # set is not known until a composition root decides which projects it serves. They are
    # pre-initialised the same way, just later, by `init_project_labels`.
    for stage in SEARCH_STAGES:
        search_latency.labels(stage=stage)
    for result in ("hit", "miss"):
        embed_cache_lookups.labels(result=result)
    for decision in ("ran", "skipped"):
        rerank_decisions.labels(decision=decision)
    for reason in WRITE_REFUSAL_REASONS:
        write_refused.labels(reason=reason)

    index_drift = Gauge(
        "vaultrag_index_drift_chunks",
        "Manifest chunk count minus vector-store point count, per project. Zero is "
        "healthy; positive means the vector store is missing chunks SQLite records "
        "(reindex will skip them — see consistency.py); negative means orphaned points.",
        ["project"],
        registry=registry,
    )

    return _Metrics(
        registry=registry,
        build_info=build_info,
        ingest_lag=ingest_lag,
        ingests=ingests,
        jobs_queue_depth=jobs_queue_depth,
        jobs_dead=jobs_dead,
        embed_cache_lookups=embed_cache_lookups,
        search_latency=search_latency,
        searches=searches,
        searches_empty=searches_empty,
        rerank_decisions=rerank_decisions,
        write_ops=write_ops,
        write_refused=write_refused,
        index_drift=index_drift,
    )


_M = _build()


def registry() -> CollectorRegistry:
    """The registry every VaultRAG metric is on — what a future ``serve`` will mount."""
    return _M.registry


def reset_metrics() -> None:
    """Rebuild every metric from scratch (tests only).

    ``prometheus_client`` has no public "set this counter back to zero", by design — a
    counter that can go down is not a counter. Tests that assert on absolute values
    therefore need a fresh registry rather than a reset one, which is exactly what
    rebuilding gives them.
    """
    global _M
    _M = _build()


# --- recording helpers ---------------------------------------------------------
# Call sites import these, never the metric objects or `prometheus_client` itself, so the
# vendor stays behind one module (CLAUDE.md §5, interfaces over vendors) and a rebuilt
# registry can never leave an instrumented module holding a stale metric.


def set_build_info(version: str) -> None:
    """Publish the running version as ``vaultrag_build_info{version="…"} 1``."""
    _M.build_info.labels(version=version).set(1)


def set_index_drift(chunks: int, *, project: str) -> None:
    """Publish one project's manifest-vs-vector disagreement (P3 Stage 4).

    A gauge rather than a counter because drift is a *state*, not an event: it is repaired
    by ``reindex --force`` and the series must fall back to zero when it is. Set at startup
    by the drift check and after each reindex, which is as often as the number can change
    without something else already having logged.
    """
    _M.index_drift.labels(project=project).set(chunks)


def init_project_labels(project_id: str) -> None:
    """Pre-initialise every ``project``-labelled series for one tenant, at zero.

    The deferred half of ``_build``'s pre-initialisation: those metrics cannot be
    materialised up front because the label's values are whatever projects this process was
    told to serve. Composition roots (``vaultrag mcp``/``watch``/``reindex``) call this once
    per served project at startup, which restores the property the eager loop exists for —
    a scrape shows the full shape of the deployment from the first second, so "this tenant
    has served no searches" and "this tenant is not being scraped" stay distinguishable.

    Idempotent: ``labels()`` returns the existing child on a repeat call.
    """
    for status in INGEST_STATUSES:
        _M.ingests.labels(status=status, project=project_id)
    for source in INGEST_SOURCES:
        _M.ingest_lag.labels(source=source, project=project_id)
    for action in WRITE_ACTIONS:
        _M.write_ops.labels(action=action, project=project_id)
    _M.searches.labels(project=project_id)
    _M.searches_empty.labels(project=project_id)
    # Zero is the healthy value *and* a real measurement, so it must be present from the
    # first scrape: an absent drift series and a drift of 0 mean opposite things, and the
    # startup check publishes the true number moments later anyway.
    _M.index_drift.labels(project=project_id).set(0)


def record_ingest_lag(seconds: float, *, source: str, project: str) -> None:
    """Observe one path's change → searchable latency (the SLO that matters).

    ``source`` separates the two routes a note becomes searchable by, because they are
    different distributions and averaging them together would make both percentiles lie:

    * ``watch``     — an external edit (Obsidian, an editor). Includes the 1.5 s debounce
      and any queue wait, so it is floored near 2 s. This is the "is the index behind the
      vault?" SLO the design note is about.
    * ``writeback`` — an agent's own ``write_note``/``append_section``/``patch_frontmatter``,
      which ingests **synchronously** before returning. Sub-second, and it is the latency
      of the memory loop: how long after an agent writes can it retrieve what it wrote.

    Without the label the watch route's debounce floor would drag every write-back
    observation into the same buckets and a p95 would describe neither one.
    """
    _M.ingest_lag.labels(source=source, project=project).observe(seconds)


def record_ingest(status: str, *, project: str) -> None:
    """Count one completed single-path reconcile by its :class:`IngestStatus`."""
    _M.ingests.labels(status=status, project=project).inc()


def set_queue_depth(depth: int) -> None:
    """Publish the watcher's current pending + in-flight path count."""
    _M.jobs_queue_depth.set(depth)


def record_job_dead() -> None:
    """Count one ingest job parked dead after exhausting retries."""
    _M.jobs_dead.inc()


def record_embed_cache(*, hits: int, misses: int) -> None:
    """Count one batch's chunk-embedding cache outcome."""
    if hits:
        _M.embed_cache_lookups.labels(result="hit").inc(hits)
    if misses:
        _M.embed_cache_lookups.labels(result="miss").inc(misses)


def record_search(*, empty: bool, project: str) -> None:
    """Count one served search, and whether it came back with nothing."""
    _M.searches.labels(project=project).inc()
    if empty:
        _M.searches_empty.labels(project=project).inc()


def record_rerank_decision(*, ran: bool) -> None:
    """Count one per-query rerank decision (``auto``'s fast path shows up as skipped)."""
    _M.rerank_decisions.labels(decision="ran" if ran else "skipped").inc()


def record_write(action: str, *, project: str) -> None:
    """Count one write that reached the vault."""
    _M.write_ops.labels(action=action, project=project).inc()


def record_write_refused(reason: str) -> None:
    """Count one write stopped by a guardrail (CLAUDE.md §6's write-safety rules)."""
    _M.write_refused.labels(reason=reason).inc()


@contextmanager
def time_stage(stage: str) -> Iterator[None]:
    """Time one search stage into ``vaultrag_search_latency_seconds{stage=…}``.

    Observes on the way out even if the stage raised: a stage that blew up still consumed
    wall time, and dropping that observation would make a failing stage look *fast*.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        _M.search_latency.labels(stage=stage).observe(time.perf_counter() - start)


def start_metrics_server(
    port: int = DEFAULT_METRICS_PORT,
    addr: str = DEFAULT_METRICS_ADDR,
) -> bool:
    """Serve ``/metrics`` on ``addr:port``. Returns whether it came up.

    Never raises. The common failure is a port already bound — two VaultRAG processes on
    one box (``watch`` plus ``mcp --metrics``) is a normal thing to do — and the right
    response to that is a warning naming the port, not killing an indexer that is
    otherwise working. See rule 3 in the module docstring.
    """
    try:
        _start_http_server(port, addr=addr, registry=_M.registry)
    except OSError as exc:
        log.warning(
            "metrics.bind_failed",
            port=port,
            addr=addr,
            error=str(exc),
            hint="another process is likely already exporting; set VAULTRAG_METRICS_PORT",
        )
        return False
    log.info("metrics.serving", port=port, addr=addr, url=f"http://{addr}:{port}/metrics")
    return True
