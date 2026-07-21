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

2. **No metric carries a path, query, title, or project label.** Cardinality first — one
   series per vault file would dwarf the vault — but privacy too: ``/metrics`` is
   unauthenticated, and CLAUDE.md §6 keeps raw query text out of logs, so it certainly
   does not belong in a label. (A ``project`` label is deliberately absent as well: one
   process serves one project today, so it would be a constant. Multi-project is P3, and
   that is when it earns its place.)

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
SEARCH_STAGES = ("embed", "fetch", "rerank", "parents", "graph", "cite", "pack", "total")

#: The two routes by which a change becomes searchable — see :func:`record_ingest_lag`.
INGEST_SOURCES = ("watch", "writeback")

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
        ["source"],
        buckets=_LAG_BUCKETS,
        registry=registry,
    )
    ingests = Counter(
        "vaultrag_ingests_total",
        "Single-path reconciles completed by the watcher, by outcome.",
        ["status"],
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
        registry=registry,
    )
    searches_empty = Counter(
        "vaultrag_searches_empty_total",
        "Searches that returned no snippets.",
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
        ["action"],
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
    for stage in SEARCH_STAGES:
        search_latency.labels(stage=stage)
    for result in ("hit", "miss"):
        embed_cache_lookups.labels(result=result)
    for decision in ("ran", "skipped"):
        rerank_decisions.labels(decision=decision)
    for action in WRITE_ACTIONS:
        write_ops.labels(action=action)
    for reason in WRITE_REFUSAL_REASONS:
        write_refused.labels(reason=reason)
    for status in ("indexed", "unchanged", "removed", "skipped"):
        ingests.labels(status=status)
    for source in INGEST_SOURCES:
        ingest_lag.labels(source=source)

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


def record_ingest_lag(seconds: float, *, source: str) -> None:
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
    _M.ingest_lag.labels(source=source).observe(seconds)


def record_ingest(status: str) -> None:
    """Count one completed single-path reconcile by its :class:`IngestStatus`."""
    _M.ingests.labels(status=status).inc()


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


def record_search(*, empty: bool) -> None:
    """Count one served search, and whether it came back with nothing."""
    _M.searches.inc()
    if empty:
        _M.searches_empty.inc()


def record_rerank_decision(*, ran: bool) -> None:
    """Count one per-query rerank decision (``auto``'s fast path shows up as skipped)."""
    _M.rerank_decisions.labels(decision="ran" if ran else "skipped").inc()


def record_write(action: str) -> None:
    """Count one write that reached the vault."""
    _M.write_ops.labels(action=action).inc()


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
