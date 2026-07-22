"""P2 Stage 7 verify: Prometheus instrumentation + the standalone ``/metrics`` exporter.

Four layers:

* the **contract** — every series the design note names exists, ratio components ship as
  pairs, labels are pre-initialised, and no metric can carry a path/query label;
* the **call sites** — a real ``search``/``rerank``/``embed_chunks``/``NoteWriter``/
  ``VaultWatcher`` moves the counter it is supposed to and *doesn't* move the ones it
  isn't (the "unchanged reconcile records no lag" case is the one that matters);
* **bucket fitness** — the histogram boundaries actually bracket this system's measured
  latencies, since bad buckets yield a metric that exists but answers nothing;
* the **exporter** — it serves, and a port clash degrades instead of raising.

Every test calls :func:`reset_metrics` first, so each starts on a fresh registry:
counters have no public reset, by design.
"""

from __future__ import annotations

import socket
import typing
import urllib.request
from collections.abc import Sequence
from pathlib import Path

import pytest
from blake3 import blake3
from qdrant_client import QdrantClient

from tests.fakes import (
    PROJECT,
    FakeEmbedder,
    FakeReranker,
    FakeSparse,
    FakeVectorStore,
    make_hit,
)
from vaultrag import metrics
from vaultrag.embedding.base import Vector
from vaultrag.embedding.cache import QueryEmbedder, embed_chunks
from vaultrag.index import QdrantVectorStore, SparseVector
from vaultrag.indexer.pipeline import IngestContext, IngestStatus
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.metrics import (
    INGEST_SOURCES,
    INGEST_STATUSES,
    SEARCH_STAGES,
    WRITE_ACTIONS,
    WRITE_REFUSAL_REASONS,
    init_project_labels,
    registry,
    reset_metrics,
)
from vaultrag.models import Chunk
from vaultrag.retrieval import search
from vaultrag.retrieval.rerank import RerankParams, rerank_candidates
from vaultrag.store.sqlite import SQLiteMetaStore
from vaultrag.writeback import NoteWriter
from vaultrag.writeback.errors import RefusalReason, WriteRefused
from vaultrag.writeback.writer import _Action

WATCH_PROJECT = "test-project"
DIMS = 4


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    """Every test starts from zero — a Counter has no public reset, so rebuild instead."""
    reset_metrics()


def value(name: str, **labels: str) -> float:
    """Read one sample, treating "series absent" as 0.0 for readable assertions."""
    got = registry().get_sample_value(name, labels or None)
    return 0.0 if got is None else got


def series_names() -> set[str]:
    """Sample names, not family names — a Counter's family drops the ``_total`` suffix,
    so comparing families would silently accept a metric a scrape never shows."""
    return {s.name for m in registry().collect() for s in m.samples}


# ---------------------------------------------------------------------------
# The contract: what /metrics promises to expose
# ---------------------------------------------------------------------------
def test_every_series_the_design_note_names_is_exposed() -> None:
    """monitoring-observability.md's list, translated through the ratio rule.

    ``embed_cache_hit_ratio``, ``empty_result_ratio`` and ``rerank_skipped_total`` ship as
    component counters instead (metrics.py rule 1) — this pins that the *information* is
    all present, so the deviation is a naming choice and never a gap.

    The ``project``-labelled families materialise only once a project is announced (P3
    Stage 2 — their label values are whatever the process was told to serve), so this
    announces one first, exactly as every composition root does at startup.
    """
    init_project_labels(PROJECT)
    names = series_names()
    for expected in (
        "vaultrag_ingest_lag_seconds",
        "vaultrag_jobs_queue_depth",
        "vaultrag_jobs_dead_total",
        "vaultrag_embed_cache_lookups_total",  # was: embed_cache_hit_ratio
        "vaultrag_search_latency_seconds",
        "vaultrag_rerank_decisions_total",  # was: rerank_skipped_total
        "vaultrag_searches_total",  # denominator for...
        "vaultrag_searches_empty_total",  # ...the empty-result ratio
        "vaultrag_write_ops_total",
    ):
        # Prefix match: a Histogram never emits its bare name, only `_bucket`/`_sum`/
        # `_count` samples, so an exact match would reject the two histograms outright.
        assert any(n.startswith(expected) for n in names), f"{expected} missing from the registry"


def test_ratios_ship_as_components_not_as_a_ratio_gauge() -> None:
    """No ``*_ratio`` series exists — a lifetime average can't be windowed by PromQL."""
    assert not [n for n in series_names() if n.endswith("_ratio")]


def test_labels_are_preinitialised_so_a_scrape_shows_zero_not_nothing() -> None:
    """A stage that hasn't run yet must read 0, not be absent: ``rate()`` on a missing
    series is undefined exactly when you first need it."""
    for stage in SEARCH_STAGES:
        assert value("vaultrag_search_latency_seconds_count", stage=stage) == 0.0
    for decision in ("ran", "skipped"):
        assert value("vaultrag_rerank_decisions_total", decision=decision) == 0.0
    for result in ("hit", "miss"):
        assert value("vaultrag_embed_cache_lookups_total", result=result) == 0.0


def test_no_metric_carries_a_path_query_or_title_label() -> None:
    """metrics.py rule 2, enforced rather than documented.

    Cardinality (one series per vault file) and privacy both forbid these. The privacy half
    is not softened by P3 Stage 4 putting ``/metrics`` behind a bearer key: the **standalone
    exporter** (P2 Decision 5) still serves the same registry on a keyless loopback port, so
    a label added here is exposed by whichever front door is open. CLAUDE.md §6 keeps query
    text out of logs for the same reason. An allowlist rather than a denylist, so a *new*
    high-cardinality label fails this test by default.

    ``project`` joined the allowlist in P3 Stage 2, when one process began serving several
    projects and the label stopped being a constant. It is bounded by the registry — an
    operator-managed list — which is what distinguishes it from the labels this test exists
    to keep out: a path or a query is bounded only by user behaviour.
    """
    init_project_labels(PROJECT)  # the label must be present to be judged
    allowed = {
        "stage",
        "result",
        "decision",
        "action",
        "reason",
        "status",
        "version",
        "source",
        "project",
        "le",  # Prometheus's own histogram bucket boundary, not user data.
    }
    for metric in registry().collect():
        for sample in metric.samples:
            assert set(sample.labels) <= allowed, f"{sample.name} carries {set(sample.labels)}"


def test_search_latency_stays_unlabelled_by_project() -> None:
    """The one place cardinality was deliberately *not* spent (metrics.py rule 2).

    8 stages by 18 buckets multiplied by tenants is the largest series count in the module
    and answers the least — stage latency is dominated by the shared embedder, Qdrant and
    reranker. Pinned as a test because "we chose not to" is invisible in the code otherwise,
    and the obvious next edit is to add it everywhere for consistency.
    """
    init_project_labels(PROJECT)
    for metric in registry().collect():
        if metric.name == "vaultrag_search_latency_seconds":
            for sample in metric.samples:
                assert "project" not in sample.labels


def test_a_project_is_preinitialised_at_zero_across_every_labelled_series() -> None:
    """``init_project_labels`` is the deferred half of ``_build``'s pre-initialisation.

    Without it a tenant that has served no traffic is *absent* rather than zero, and
    "this project is idle" reads identically to "this project is not being scraped" —
    the exact confusion the eager loop exists to prevent.
    """
    init_project_labels("fresh-tenant")
    assert value("vaultrag_searches_total", project="fresh-tenant") == 0.0
    assert value("vaultrag_searches_empty_total", project="fresh-tenant") == 0.0
    for action in WRITE_ACTIONS:
        assert value("vaultrag_write_ops_total", action=action, project="fresh-tenant") == 0.0
    for status in INGEST_STATUSES:
        assert value("vaultrag_ingests_total", status=status, project="fresh-tenant") == 0.0
    for source in INGEST_SOURCES:
        assert (
            value("vaultrag_ingest_lag_seconds_count", source=source, project="fresh-tenant") == 0.0
        )


def test_ingest_statuses_stay_in_sync_with_the_pipeline_enum() -> None:
    """metrics.py duplicates these to stay a leaf module; this keeps the copy honest."""
    assert set(INGEST_STATUSES) == {s.value for s in IngestStatus}


def test_two_projects_count_independently() -> None:
    """The whole point of the label: one tenant's traffic never lands on another's series."""
    metrics.record_search(empty=False, project="alpha")
    metrics.record_search(empty=True, project="beta")
    assert value("vaultrag_searches_total", project="alpha") == 1.0
    assert value("vaultrag_searches_empty_total", project="alpha") == 0.0
    assert value("vaultrag_searches_total", project="beta") == 1.0
    assert value("vaultrag_searches_empty_total", project="beta") == 1.0


def test_write_actions_stay_in_sync_with_the_writer() -> None:
    """metrics.py duplicates these to stay a leaf module; this is what keeps the copy honest."""
    assert set(WRITE_ACTIONS) == set(typing.get_args(_Action))


def test_write_refusal_reasons_stay_in_sync_with_the_enum() -> None:
    assert set(WRITE_REFUSAL_REASONS) == {r.value for r in RefusalReason}


def test_latency_buckets_bracket_this_system_s_measured_stage_range() -> None:
    """Guards the reason default buckets were rejected (metrics.py ``_LATENCY_BUCKETS``).

    Measured on this box (MEMORY.md 2026-07-21): graph expand p50 **0.39 ms**, a reranked
    query **~25 s**. prometheus_client's defaults span 5 ms to 10 s, which puts every graph
    observation in the first bucket and every reranked one in ``+Inf`` — a histogram that
    exists but yields no p50 or p95 at either end, i.e. neither number the note asks for.
    """
    buckets = metrics._LATENCY_BUCKETS
    assert buckets[0] < 0.00039, "graph expand (0.39 ms) would land in the first bucket"
    assert buckets[-1] > 25.0, "a reranked query (~25 s) would land in +Inf"
    assert list(buckets) == sorted(buckets)


def test_ingest_lag_buckets_reach_the_alert_threshold() -> None:
    """The design note alerts on ingest lag > 5 min, so 300 s must be an edge to compare on."""
    assert 300.0 in metrics._LAG_BUCKETS


# ---------------------------------------------------------------------------
# Search: per-stage latency + the empty-result ratio's components
# ---------------------------------------------------------------------------
def _search(**kwargs: object) -> object:
    return search(
        "hybrid search",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=[make_hit()]),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_search_times_every_stage_and_the_total() -> None:
    _search()
    for stage in SEARCH_STAGES:
        assert value("vaultrag_search_latency_seconds_count", stage=stage) == 1.0, stage


def test_a_search_counts_toward_the_empty_result_denominator() -> None:
    _search()
    assert value("vaultrag_searches_total", project=PROJECT) == 1.0
    assert value("vaultrag_searches_empty_total", project=PROJECT) == 0.0


def test_a_search_that_returns_nothing_counts_as_empty() -> None:
    search(
        "nothing matches this",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=[]),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
    )
    assert value("vaultrag_searches_total", project=PROJECT) == 1.0
    assert value("vaultrag_searches_empty_total", project=PROJECT) == 1.0


def test_a_blank_query_is_not_counted_as_a_search_at_all() -> None:
    """It never reaches the index, so scoring it empty would blame retrieval for a caller
    bug and drift the very ratio the metric exists to watch."""
    search(
        "   ",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=[make_hit()]),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
    )
    assert value("vaultrag_searches_total", project=PROJECT) == 0.0
    assert value("vaultrag_searches_empty_total", project=PROJECT) == 0.0


# ---------------------------------------------------------------------------
# Rerank: the decision counter that replaces `rerank_skipped_total`
# ---------------------------------------------------------------------------
def _candidates(top1: float, top2: float) -> list:  # type: ignore[type-arg]
    return [
        make_hit(point_id="a", score=top1, text="alpha"),
        make_hit(point_id="b", score=top2, text="beta"),
    ]


def test_a_reranked_query_counts_as_ran() -> None:
    rerank_candidates(
        "q",
        _candidates(1.0, 0.99),  # margin too small to skip
        FakeReranker(),
        keep=2,
        params=RerankParams(dominant_ratio=1.5),
    )
    assert value("vaultrag_rerank_decisions_total", decision="ran") == 1.0
    assert value("vaultrag_rerank_decisions_total", decision="skipped") == 0.0


def test_auto_mode_s_fusion_dominance_fast_path_counts_as_skipped() -> None:
    reranker = FakeReranker()
    rerank_candidates(
        "q",
        _candidates(1.0, 0.1),  # top-1 dominates → skip the model
        reranker,
        keep=2,
        params=RerankParams(dominant_ratio=1.5),
    )
    assert reranker.calls == [], "precondition: the model was not called"
    assert value("vaultrag_rerank_decisions_total", decision="skipped") == 1.0
    assert value("vaultrag_rerank_decisions_total", decision="ran") == 0.0


def test_the_stage_being_off_is_not_a_skip_decision() -> None:
    """With ``reranker=None`` there is no decision to make; counting it would report
    ``auto`` as skipping ~100% of queries on a rerank-never deployment."""
    rerank_candidates("q", _candidates(1.0, 0.9), None, keep=2, params=RerankParams())
    assert value("vaultrag_rerank_decisions_total", decision="skipped") == 0.0
    assert value("vaultrag_rerank_decisions_total", decision="ran") == 0.0


# ---------------------------------------------------------------------------
# Embed cache: the hit-ratio components
# ---------------------------------------------------------------------------
def _chunk(doc_id: str, text: str) -> Chunk:
    # chunk_hash is the real embed-cache key, so it must be content-derived here too —
    # a per-doc hash would make the "identical bodies count once" test vacuous.
    return Chunk(
        doc_id=doc_id,
        project_id=PROJECT,
        path=f"{doc_id}.md",
        ix=0,
        text=text,
        chunk_hash=blake3(text.encode()).hexdigest(),
        tokens=len(text.split()),
    )


def test_a_cold_embed_is_all_misses_and_a_warm_one_all_hits(tmp_path: Path) -> None:
    store = SQLiteMetaStore(tmp_path / "vaultrag.db")
    embedder = FakeEmbedder()
    chunks = [_chunk("a", "alpha text"), _chunk("b", "beta text")]

    embed_chunks(embedder, store, chunks)
    assert value("vaultrag_embed_cache_lookups_total", result="miss") == 2.0
    assert value("vaultrag_embed_cache_lookups_total", result="hit") == 0.0

    embed_chunks(embedder, store, chunks)  # same bodies → the (model,dims,hash) cache
    assert value("vaultrag_embed_cache_lookups_total", result="miss") == 2.0
    assert value("vaultrag_embed_cache_lookups_total", result="hit") == 2.0
    store.close()


def test_identical_bodies_count_once_not_twice(tmp_path: Path) -> None:
    """Counted in deduplicated hashes, matching what the model is actually asked for —
    counting raw chunks would credit the cache for ``embed_chunks``' own by-hash dedupe."""
    store = SQLiteMetaStore(tmp_path / "vaultrag.db")
    same = [_chunk("a", "identical body"), _chunk("b", "identical body")]
    embed_chunks(FakeEmbedder(), store, same)
    assert value("vaultrag_embed_cache_lookups_total", result="miss") == 1.0
    store.close()


# ---------------------------------------------------------------------------
# Write-back: accepted vs refused
# ---------------------------------------------------------------------------
class _WatchEmbedder:
    """Dense stand-in for the ingest path (the shared fake is read-path shaped)."""

    @property
    def model(self) -> str:
        return "fake-embed-model"

    @property
    def dims(self) -> int:
        return DIMS

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [[float(len(t) + i) for i in range(DIMS)] for t in texts]

    def close(self) -> None:
        pass


class _WatchSparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[1], values=[1.0])


@pytest.fixture
def ingest_ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    vault.mkdir()
    store = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    store.upsert_project(WATCH_PROJECT, name=WATCH_PROJECT, vault_path=str(vault))
    vector_store = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=_WatchSparse()
    )
    vector_store.ensure_collection()
    yield IngestContext(
        project_id=WATCH_PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=_WatchEmbedder(),
    )
    store.close()


def test_an_accepted_write_counts_as_a_write_op(ingest_ctx: IngestContext) -> None:
    writer = NoteWriter(ingest_ctx, git_enabled=False)
    writer.write_note(title="Metrics Note", content="body text here")
    assert value("vaultrag_write_ops_total", action="created", project=WATCH_PROJECT) == 1.0
    assert value("vaultrag_write_refused_total", reason="secret") == 0.0


def test_a_refused_write_counts_its_reason_and_no_write_op(ingest_ctx: IngestContext) -> None:
    writer = NoteWriter(ingest_ctx, git_enabled=False)
    with pytest.raises(WriteRefused):
        # Outside the default agent-inbox/ write root → the allowlist gate.
        writer.write_note(title="Escapee", content="body", folder="decisions")
    assert value("vaultrag_write_refused_total", reason="outside-write-root") == 1.0
    assert value("vaultrag_write_ops_total", action="created", project=WATCH_PROJECT) == 0.0


def test_a_secret_body_is_counted_as_a_secret_refusal(ingest_ctx: IngestContext) -> None:
    """The guardrail from CLAUDE.md §6 is now observable, not just enforced."""
    writer = NoteWriter(ingest_ctx, git_enabled=False)
    with pytest.raises(WriteRefused):
        writer.write_note(
            title="Creds",
            content="AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI0K7MDENGbPxRfiCYEXAMPLEKEY",
        )
    assert value("vaultrag_write_refused_total", reason="secret") == 1.0


def test_a_write_back_write_records_its_own_ingest_lag(ingest_ctx: IngestContext) -> None:
    """Regression: found by the live run, not by fixtures.

    Write-back ingests **synchronously**, so by the time the watcher sees the fs event the
    file already matches the manifest and its reconcile is ``unchanged`` — which records
    no lag, correctly. Instrumenting only the watcher therefore left the whole memory
    loop (agent writes a note, agent retrieves it) with a permanently-zero lag histogram,
    and the plan's own acceptance criterion — "writing a note moves ingest_lag_seconds" —
    silently unmet while every intermediate layer looked right.
    """
    writer = NoteWriter(ingest_ctx, git_enabled=False)
    result = writer.write_note(title="Loop Note", content="body text here")
    assert result.indexed, "precondition: the synchronous reindex made it searchable"
    assert (
        value("vaultrag_ingest_lag_seconds_count", source="writeback", project=WATCH_PROJECT) == 1.0
    )
    assert value("vaultrag_ingest_lag_seconds_count", source="watch", project=WATCH_PROJECT) == 0.0


def test_the_two_ingest_routes_are_separate_distributions(ingest_ctx: IngestContext) -> None:
    """The label is what keeps a p95 meaningful: the watch route is floored near the 1.5 s
    debounce, write-back is sub-second, and one shared histogram would describe neither."""
    assert set(metrics.INGEST_SOURCES) == {"watch", "writeback"}
    writer = NoteWriter(ingest_ctx, git_enabled=False)
    writer.write_note(title="Split Note", content="body text here")
    watcher = VaultWatcher(ingest_ctx, sleep=lambda _: None)
    (ingest_ctx.vault_path / "external.md").write_text("# Ext\n\nbody\n", encoding="utf-8")
    watcher._mark_dirty("external.md")
    watcher.process_path("external.md")
    assert (
        value("vaultrag_ingest_lag_seconds_count", source="writeback", project=WATCH_PROJECT) == 1.0
    )
    assert value("vaultrag_ingest_lag_seconds_count", source="watch", project=WATCH_PROJECT) == 1.0


def test_an_append_counts_as_appended(ingest_ctx: IngestContext) -> None:
    writer = NoteWriter(ingest_ctx, git_enabled=False)
    writer.write_note(title="Target", content="## Section\n\nfirst line\n")
    writer.append_section(path="agent-inbox/target.md", heading="Section", content="second line")
    assert value("vaultrag_write_ops_total", action="created", project=WATCH_PROJECT) == 1.0
    assert value("vaultrag_write_ops_total", action="appended", project=WATCH_PROJECT) == 1.0


# ---------------------------------------------------------------------------
# Watcher: ingest lag, queue depth, dead jobs
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_ingest_lag_spans_the_first_event_not_the_last(ingest_ctx: IngestContext) -> None:
    """A save storm re-arms the debounce; timing from the last event would silently
    exclude the storm from the one metric that measures how long the user waited."""
    clock = _Clock()
    watcher = VaultWatcher(ingest_ctx, clock=clock, sleep=lambda _: None)
    (ingest_ctx.vault_path / "note.md").write_text("# Note\n\nbody\n", encoding="utf-8")

    watcher._mark_dirty("note.md")  # first event of the storm, t=0
    clock.t = 1.0
    watcher._mark_dirty("note.md")  # re-arm, t=1
    clock.t = 5.0
    watcher.process_path("note.md")

    assert value("vaultrag_ingest_lag_seconds_count", source="watch", project=WATCH_PROJECT) == 1.0
    # 5.0 from the *first* event; timing from the last would have recorded 4.0.
    assert value(
        "vaultrag_ingest_lag_seconds_sum", source="watch", project=WATCH_PROJECT
    ) == pytest.approx(5.0)


def test_an_unchanged_reconcile_records_no_lag(ingest_ctx: IngestContext) -> None:
    """Nothing became newly searchable, so its near-zero span would flatter the p95 with
    a case nobody is waiting on (a touch, or a save that edited nothing)."""
    clock = _Clock()
    watcher = VaultWatcher(ingest_ctx, clock=clock, sleep=lambda _: None)
    (ingest_ctx.vault_path / "note.md").write_text("# Note\n\nbody\n", encoding="utf-8")

    watcher._mark_dirty("note.md")
    watcher.process_path("note.md")  # indexed
    assert value("vaultrag_ingest_lag_seconds_count", source="watch", project=WATCH_PROJECT) == 1.0

    watcher._mark_dirty("note.md")
    watcher.process_path("note.md")  # hash-equal → unchanged
    assert value("vaultrag_ingests_total", status="unchanged", project=WATCH_PROJECT) == 1.0
    assert (
        value("vaultrag_ingest_lag_seconds_count", source="watch", project=WATCH_PROJECT) == 1.0
    ), "unchanged must not observe lag"


def test_process_path_without_a_recorded_event_reports_no_fabricated_lag(
    ingest_ctx: IngestContext,
) -> None:
    """Called directly (a test, or flush after the span was consumed) there is no start
    time; timing from now would record a fabricated ~0."""
    watcher = VaultWatcher(ingest_ctx, sleep=lambda _: None)
    (ingest_ctx.vault_path / "note.md").write_text("# Note\n\nbody\n", encoding="utf-8")
    watcher.process_path("note.md")
    assert value("vaultrag_ingests_total", status="indexed", project=WATCH_PROJECT) == 1.0
    assert value("vaultrag_ingest_lag_seconds_count", source="watch", project=WATCH_PROJECT) == 0.0


def test_queue_depth_tracks_pending_and_inflight(ingest_ctx: IngestContext) -> None:
    clock = _Clock()
    watcher = VaultWatcher(ingest_ctx, debounce_seconds=1.5, clock=clock, sleep=lambda _: None)

    watcher._mark_dirty("a.md")
    watcher._mark_dirty("b.md")
    assert value("vaultrag_jobs_queue_depth") == 2.0

    clock.t = 2.0
    watcher._take_due()  # pending -> inflight; the path is still queued work
    assert value("vaultrag_jobs_queue_depth") == 2.0

    watcher._on_done("a.md", None)  # type: ignore[arg-type]
    assert value("vaultrag_jobs_queue_depth") == 1.0


def test_a_dead_job_is_counted(ingest_ctx: IngestContext) -> None:
    """The note's headline alert is ``dead jobs > 0``, so this counter is the alert."""

    class Boom:
        @property
        def model(self) -> str:
            return "boom"

        @property
        def dims(self) -> int:
            return DIMS

        def embed(self, texts: Sequence[str]) -> list[Vector]:
            raise RuntimeError("simulated embedder failure")

        def close(self) -> None:
            pass

    ctx = IngestContext(
        project_id=ingest_ctx.project_id,
        vault_path=ingest_ctx.vault_path,
        store=ingest_ctx.store,
        vector_store=ingest_ctx.vector_store,
        embedder=Boom(),
    )
    watcher = VaultWatcher(ctx, max_attempts=2, sleep=lambda _: None)
    (ctx.vault_path / "bad.md").write_text("# Bad\n\nbody\n", encoding="utf-8")

    watcher._mark_dirty("bad.md")
    assert watcher.process_path("bad.md") is None
    assert value("vaultrag_jobs_dead_total") == 1.0
    assert (
        value("vaultrag_ingest_lag_seconds_count", source="watch", project=WATCH_PROJECT) == 0.0
    ), "a dead job has no lag to report"


# ---------------------------------------------------------------------------
# The exporter
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_the_exporter_serves_the_registry() -> None:
    port = _free_port()
    assert metrics.start_metrics_server(port=port) is True
    metrics.set_build_info("1.2.3")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        body = resp.read().decode()
    assert "vaultrag_search_latency_seconds" in body
    assert "vaultrag_ingest_lag_seconds" in body
    assert 'vaultrag_build_info{version="1.2.3"} 1.0' in body


def test_a_port_clash_degrades_instead_of_killing_the_process() -> None:
    """Two VaultRAG processes on one box is normal (``watch`` plus ``mcp --metrics``).
    Losing metrics is an inconvenience; losing the indexer would be an outage."""
    port = _free_port()
    with socket.socket() as taken:
        taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        taken.bind(("127.0.0.1", port))
        taken.listen(1)
        assert metrics.start_metrics_server(port=port) is False  # warns, does not raise
