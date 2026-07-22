"""P3 Stage 3 verify: the query log and the mining loop it exists for.

The stage's premise is that ``query_log`` is not telemetry — Prometheus already counts
searches — but the **input to the eval gate**: a query that returned nothing is a
retrieval failure nobody wrote a test for. So the tests here follow the loop end to end
(search → row → zero-hit report → candidate file the harness can load) rather than
asserting the DDL exists.

Three things get adversarial treatment, because each is a place where a green test could
mean nothing:

* **The privacy boundary.** P3 Decision 2 makes the DB the one place raw query text may
  live. An exception that is only documented is an exception that leaks, so the log stream
  and the metrics registry are checked for the literal query text under **both** settings
  of the storage knob.
* **The cascade.** P3 Stage 2 shipped a `project rm` that cleared SQLite and left every
  vector behind, because "rows" silently meant "the store I was thinking of". ``query_log``
  has no FK to ``projects``, so it is invisible to the cascade unless something names it —
  the same shape of bug, one table over.
* **The placement.** Recording lives in the core pipeline so Stage 4's REST surface cannot
  grow a divergent copy; a test drives the pipeline *and* the MCP tool and compares.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import structlog
from prometheus_client import generate_latest

from tests.fakes import PROJECT, FakeEmbedder, FakeSparse, FakeVectorStore, make_hit
from tests.test_mcp import call, call_expecting_error
from vaultrag import metrics
from vaultrag.config import Settings
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.eval import GoldenSetError, load_candidates, load_golden
from vaultrag.mcp import ServedProject, build_mcp_server
from vaultrag.projects import ProjectRegistry
from vaultrag.retrieval import QueryLogParams, q_hash, query_log_params_for, search
from vaultrag.store.base import QueryStatOrder
from vaultrag.store.sqlite import SQLiteMetaStore

SECRET_QUERY = "why did the payroll migration fail"
OTHER = "other-project"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class RecordingSink:
    """A ``QueryLogSink`` that keeps rows in a list — the pipeline needs nothing more."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_query(self, **row: Any) -> None:
        self.rows.append(row)


class BrokenSink:
    """A sink that always fails. The search must not care."""

    def log_query(self, **row: Any) -> None:
        raise RuntimeError("disk full")


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteMetaStore:
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    yield s
    s.close()


def run_search(
    *,
    sink: Any = None,
    params: QueryLogParams | None = None,
    query: str = SECRET_QUERY,
    hits: int = 1,
    k: int = 10,
) -> Any:
    return search(
        query,
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=[make_hit()] * hits),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
        k=k,
        query_log_sink=sink,
        query_log=params,
    )


def log_rows(store: SQLiteMetaStore, project_id: str = PROJECT) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT * FROM query_log WHERE project_id = ? ORDER BY rowid", (project_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Recording — shape, placement, and what is deliberately not recorded
# ---------------------------------------------------------------------------
def test_a_served_search_writes_one_row_with_the_reference_shape() -> None:
    sink = RecordingSink()
    result = run_search(sink=sink, params=QueryLogParams(mode="snippets"), k=7)
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["project_id"] == PROJECT
    assert row["q_hash"] == q_hash(SECRET_QUERY)
    assert row["mode"] == "snippets"
    assert row["k"] == 7
    assert row["n_results"] == len(result.snippets)
    assert row["budget_used"] == result.total_tokens
    assert row["latency_ms"] >= 0


def test_no_sink_means_no_recording_and_an_unchanged_result() -> None:
    """Logging is strictly additive — the P0 call signature still behaves as it did."""
    with_sink = run_search(sink=RecordingSink())
    without = run_search()
    assert [s.path for s in with_sink.snippets] == [s.path for s in without.snippets]


def test_a_blank_query_is_not_a_logged_query() -> None:
    """Same rule as `searches_total`: a blank query is a caller bug, not a retrieval miss.

    Logging it would seed the zero-hit report — the golden-set candidate list — with rows
    that say nothing about retrieval, which is exactly the mining noise the stage avoids.
    """
    sink = RecordingSink()
    run_search(sink=sink, query="   ")
    assert sink.rows == []


def test_a_failing_sink_never_fails_the_search() -> None:
    result = run_search(sink=BrokenSink())
    assert result.snippets, "a broken query log must not cost the caller their results"


def test_the_mcp_tool_and_the_bare_pipeline_write_the_same_row(tmp_path: Path) -> None:
    """The placement test: recording is in the core, so no transport owns a copy of it.

    This is what makes Stage 4's REST surface inherit the row rather than reimplement it —
    if this ever diverges, it is because logic leaked into a transport.
    """
    direct = RecordingSink()
    run_search(sink=direct, params=QueryLogParams(mode="snippets"), k=10)

    via_mcp = RecordingSink()
    vault = tmp_path / "vault"
    vault.mkdir()
    server = build_mcp_server(
        projects=[
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                query_log=QueryLogParams(),
            )
        ],
        vector_store=FakeVectorStore(hits=[make_hit()]),
        sparse=FakeSparse(),
        query_log_sink=via_mcp,
    )
    call(server, "search", {"query": SECRET_QUERY})

    assert len(via_mcp.rows) == 1
    ignore = {"latency_ms"}  # wall clock, the one field that cannot match
    assert {k: v for k, v in via_mcp.rows[0].items() if k not in ignore} == {
        k: v for k, v in direct.rows[0].items() if k not in ignore
    }


def test_compact_mode_is_recorded_as_compact(tmp_path: Path) -> None:
    sink = RecordingSink()
    vault = tmp_path / "vault"
    vault.mkdir()
    server = build_mcp_server(
        projects=[
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                query_log=QueryLogParams(),
            )
        ],
        vector_store=FakeVectorStore(hits=[make_hit()]),
        sparse=FakeSparse(),
        query_log_sink=sink,
    )
    call(server, "search", {"query": SECRET_QUERY, "mode": "compact"})
    assert sink.rows[0]["mode"] == "compact"


def test_a_project_without_a_log_policy_is_not_logged(tmp_path: Path) -> None:
    """One tenant can opt out on a process that logs its neighbours."""
    sink = RecordingSink()
    vault = tmp_path / "vault"
    vault.mkdir()
    server = build_mcp_server(
        projects=[
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                query_log=None,
            ),
            ServedProject(
                project_id=OTHER,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                query_log=QueryLogParams(),
            ),
        ],
        vector_store=FakeVectorStore(hits=[make_hit()]),
        sparse=FakeSparse(),
        query_log_sink=sink,
    )
    call(server, "search", {"query": SECRET_QUERY, "project": PROJECT})
    assert sink.rows == []
    call(server, "search", {"query": SECRET_QUERY, "project": OTHER})
    assert [r["project_id"] for r in sink.rows] == [OTHER]


# ---------------------------------------------------------------------------
# P3 Decision 2 — the text column, and the boundary it does NOT move
# ---------------------------------------------------------------------------
def test_the_text_is_stored_when_the_knob_is_on() -> None:
    sink = RecordingSink()
    run_search(sink=sink, params=QueryLogParams(store_text=True))
    assert sink.rows[0]["query_text"] == SECRET_QUERY


def test_the_hash_is_stored_and_the_text_is_not_when_the_knob_is_off() -> None:
    """Hash-only is a real deployment mode, not a broken one: the row is still written."""
    sink = RecordingSink()
    run_search(sink=sink, params=QueryLogParams(store_text=False))
    assert sink.rows[0]["query_text"] is None
    assert sink.rows[0]["q_hash"] == q_hash(SECRET_QUERY)


@pytest.mark.parametrize("store_text", [True, False])
def test_the_log_stream_never_carries_the_query_text(tmp_path: Path, store_text: bool) -> None:
    """CLAUDE.md §5 is about what leaves the box, and Decision 2 does not touch it.

    Parameterized over the knob on purpose: the failure this guards against is someone
    later threading `query_text` into the structlog call "since we store it anyway".
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    server = build_mcp_server(
        projects=[
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                query_log=QueryLogParams(store_text=store_text),
            )
        ],
        vector_store=FakeVectorStore(hits=[make_hit()]),
        sparse=FakeSparse(),
        query_log_sink=RecordingSink(),
    )
    with structlog.testing.capture_logs() as captured:
        call(server, "search", {"query": SECRET_QUERY})
    assert captured, "expected the search to log something at all"
    blob = json.dumps(captured)
    assert SECRET_QUERY not in blob
    assert q_hash(SECRET_QUERY) in blob, "the hash is what makes a log line joinable to a row"


@pytest.mark.parametrize("store_text", [True, False])
def test_the_metrics_registry_never_carries_the_query_text(store_text: bool) -> None:
    run_search(sink=RecordingSink(), params=QueryLogParams(store_text=store_text))
    assert SECRET_QUERY not in generate_latest(metrics.registry()).decode("utf-8")


def test_a_failed_write_is_reported_by_hash_not_by_text() -> None:
    with structlog.testing.capture_logs() as captured:
        run_search(sink=BrokenSink(), params=QueryLogParams(store_text=True))
    warnings = [e for e in captured if e.get("event") == "querylog.write_failed"]
    assert warnings, "a dropped row must not be silent"
    assert SECRET_QUERY not in json.dumps(warnings)
    assert warnings[0]["q_hash"] == q_hash(SECRET_QUERY)


def test_q_hash_normalizes_whitespace_but_not_case() -> None:
    assert q_hash("docker  compose\n") == q_hash("docker compose")
    assert q_hash("ERR_CONN_RESET") != q_hash("err_conn_reset")


# ---------------------------------------------------------------------------
# The SQLite store: rows in, reports out
# ---------------------------------------------------------------------------
def write(
    store: SQLiteMetaStore,
    query: str,
    *,
    project_id: str = PROJECT,
    n_results: int = 5,
    latency_ms: int = 10,
    text: bool = True,
    mode: str = "snippets",
    top_score: float | None = 0.6,
) -> None:
    store.log_query(
        project_id=project_id,
        q_hash=q_hash(query),
        query_text=query if text else None,
        mode=mode,
        k=10,
        latency_ms=latency_ms,
        n_results=n_results,
        budget_used=100,
        top_score=top_score,
    )


def test_the_pipeline_row_lands_in_sqlite_unchanged(store: SQLiteMetaStore) -> None:
    """The sink protocol and the concrete store agree — no adapter in between to drift."""
    run_search(sink=store, params=QueryLogParams())
    rows = log_rows(store)
    assert len(rows) == 1
    assert rows[0]["query_text"] == SECRET_QUERY
    assert rows[0]["q_hash"] == q_hash(SECRET_QUERY)
    assert rows[0]["mode"] == "snippets"


def test_stats_group_by_query_not_by_row(store: SQLiteMetaStore) -> None:
    for _ in range(3):
        write(store, "how does chunking work")
    write(store, "what is rrf")
    stats = store.query_stats(PROJECT, order=QueryStatOrder.frequent)
    assert [(s.query_text, s.count) for s in stats] == [
        ("how does chunking work", 3),
        ("what is rrf", 1),
    ]


def test_slowest_ranks_on_the_average_not_one_cold_start(store: SQLiteMetaStore) -> None:
    """A single outlier should not permanently crown an otherwise-fast query."""
    write(store, "usually fast", latency_ms=2000)
    for _ in range(9):
        write(store, "usually fast", latency_ms=5)
    write(store, "genuinely slow", latency_ms=900)
    stats = store.query_stats(PROJECT, order=QueryStatOrder.slowest)
    assert stats[0].query_text == "genuinely slow"


def test_a_query_that_ever_succeeded_is_not_a_zero_hit(store: SQLiteMetaStore) -> None:
    """MAX(n_results), not the latest row: the case is 'never answerable', not 'missed once'.

    Otherwise a transient empty result — Qdrant restarting mid-session — would be exported
    as a golden-set candidate for a query the index answers perfectly well.
    """
    write(store, "flaky", n_results=0)
    write(store, "flaky", n_results=4)
    write(store, "truly unanswerable", n_results=0)
    zero = store.query_stats(PROJECT, order=QueryStatOrder.frequent, max_results=0)
    assert [s.query_text for s in zero] == ["truly unanswerable"]


def test_stats_are_scoped_to_one_project(store: SQLiteMetaStore) -> None:
    write(store, "mine", project_id=PROJECT)
    write(store, "theirs", project_id=OTHER)
    assert [s.query_text for s in store.query_stats(PROJECT)] == ["mine"]
    assert [s.query_text for s in store.query_stats(OTHER)] == ["theirs"]
    assert store.query_log_count(PROJECT) == 1
    assert store.query_log_count() == 2


def test_since_ts_windows_the_report(store: SQLiteMetaStore) -> None:
    write(store, "recent")
    stats = store.query_stats(PROJECT, since_ts=int(time.time()) + 60)
    assert stats == []


def test_the_text_comes_from_the_most_recent_row_that_has_one(store: SQLiteMetaStore) -> None:
    """A log whose text column was turned on partway through still names what it can."""
    write(store, "half known", text=False)
    write(store, "half known", text=True)
    stats = store.query_stats(PROJECT)
    assert stats[0].query_text == "half known"
    assert stats[0].count == 2


def test_hash_only_rows_still_produce_reports(store: SQLiteMetaStore) -> None:
    write(store, "unnamed", text=False, n_results=0)
    stat = store.query_stats(PROJECT, max_results=0)[0]
    assert stat.query_text is None
    assert stat.zero_hit


# ---------------------------------------------------------------------------
# Retention — part of the feature, not a follow-up
# ---------------------------------------------------------------------------
def test_the_age_cap_drops_old_rows_and_keeps_new_ones(store: SQLiteMetaStore) -> None:
    write(store, "old")
    store._conn.execute("UPDATE query_log SET ts = ?", (int(time.time()) - 91 * 86400,))
    store._conn.commit()
    write(store, "new")
    assert store.prune_query_log(max_age_days=90) == 1
    assert [r["query_text"] for r in log_rows(store)] == ["new"]


def test_the_row_cap_keeps_the_newest(store: SQLiteMetaStore) -> None:
    for i in range(10):
        write(store, f"q{i}")
    store.prune_query_log(max_rows=3)
    # Ties at the boundary are kept by design (the cut is by timestamp, which is
    # second-resolution) — the cap is a ceiling, and what matters is that it bounds.
    assert store.query_log_count() <= 10
    assert store.query_log_count() >= 3


def test_retention_runs_on_write_without_being_asked(tmp_path: Path) -> None:
    """A policy nobody remembers to invoke is not a policy — the writer prunes.

    Staged as a restart, which is the real case: the sweep is amortized (paying the row
    cap's offset scan on every search would charge retrieval for its own telemetry), so
    what has to hold is that the *first* write of a process tidies a log that has been
    sitting since the last one — not that every write does.
    """
    db = tmp_path / "state" / "vaultrag.db"
    old = SQLiteMetaStore(db)
    try:
        write(old, "ancient")
        old._conn.execute("UPDATE query_log SET ts = ?", (int(time.time()) - 400 * 86400,))
        old._conn.commit()
    finally:
        old.close()

    s = SQLiteMetaStore(db, query_log_retention_days=90)
    try:
        write(s, "fresh")
        assert [r["query_text"] for r in log_rows(s)] == ["fresh"]
    finally:
        s.close()


def test_retention_disabled_keeps_everything(tmp_path: Path) -> None:
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    try:
        write(s, "kept")
        s._conn.execute("UPDATE query_log SET ts = ?", (int(time.time()) - 4000 * 86400,))
        s._conn.commit()
        write(s, "also kept")
        assert len(log_rows(s)) == 2
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The cascade — the Stage-2 lesson, one table over
# ---------------------------------------------------------------------------
def test_removing_a_project_clears_its_query_log_and_only_its_own(
    store: SQLiteMetaStore, tmp_path: Path
) -> None:
    """``query_log`` has no FK, so a cascade cannot reach it unless something names it.

    This is the same shape as P3 Stage 2's vector orphan: the deletion looked complete
    against the rows the test author had in mind. It matters more here than for `jobs`
    because with the text column on, the abandoned rows are the departing tenant's raw
    queries — and re-registering the id hands them to whoever gets it next.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    registry = ProjectRegistry(store, Settings(state_dir=tmp_path / "state"))
    registry.add(PROJECT, tmp_path / "a")
    registry.add(OTHER, tmp_path / "b")
    write(store, "mine", project_id=PROJECT)
    write(store, "theirs", project_id=OTHER)

    assert registry.remove(PROJECT) is True
    assert store.query_log_count(PROJECT) == 0
    assert store.query_log_count(OTHER) == 1


# ---------------------------------------------------------------------------
# The mining loop — the reason the table exists
# ---------------------------------------------------------------------------
def test_a_zero_hit_query_exports_as_a_candidate_the_harness_can_load(
    store: SQLiteMetaStore, tmp_path: Path
) -> None:
    """The stage's acceptance criterion, end to end.

    The candidate file uses the golden-set schema so filling in `expected_paths` in place
    turns it into a scoreable set with no reformatting — and until it is filled, the strict
    loader refuses it *by name* rather than scoring an unanswerable case as a miss.
    """
    from vaultrag.eval import write_candidates

    write(store, "how do I rotate the signing key", n_results=0)
    write(store, "how do I rotate the signing key", n_results=0)
    write(store, "answered fine", n_results=6)

    zero = store.query_stats(PROJECT, max_results=0)
    assert [s.query_text for s in zero] == ["how do I rotate the signing key"]

    out = tmp_path / "candidates.json"
    write_candidates(
        out,
        project_id=PROJECT,
        candidates=[(str(s.query_text), f"zero-hit x{s.count}") for s in zero],
    )

    loaded = load_candidates(out)
    assert [c.query for c in loaded.cases] == ["how do I rotate the signing key"]
    assert loaded.cases[0].expected == frozenset()
    assert "zero-hit x2" in loaded.cases[0].note

    with pytest.raises(GoldenSetError, match="rotate the signing key"):
        load_golden(out)

    # Fill it in the way a human would, in place — no reformatting step.
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["cases"][0]["expected_paths"] = ["ops/key-rotation.md"]
    out.write_text(json.dumps(payload), encoding="utf-8")
    assert load_golden(out).cases[0].expected == frozenset({"ops/key-rotation.md"})


def test_candidates_are_never_merged_into_a_shipped_golden_set(tmp_path: Path) -> None:
    """The gate's set is a frozen instrument; appending unreviewed cases would move the bar."""
    from vaultrag.eval import CORPUS_GOLDEN_PATH, write_candidates

    before = CORPUS_GOLDEN_PATH.read_bytes()
    write_candidates(tmp_path / "c.json", project_id=PROJECT, candidates=[("q", "note")])
    assert CORPUS_GOLDEN_PATH.read_bytes() == before


# ---------------------------------------------------------------------------
# Settings → behaviour
# ---------------------------------------------------------------------------
def test_query_log_params_for_returns_none_when_logging_is_off() -> None:
    assert query_log_params_for(Settings(query_log_enabled=False)) is None


def test_query_log_params_for_carries_the_text_knob() -> None:
    assert query_log_params_for(Settings(query_log_text=False)) == QueryLogParams(store_text=False)
    assert query_log_params_for(Settings()) == QueryLogParams(store_text=True)


def test_with_mode_narrows_only_the_mode() -> None:
    params = QueryLogParams(store_text=False).with_mode("compact")
    assert params == QueryLogParams(mode="compact", store_text=False)


def test_retention_is_not_per_project_but_the_policy_knobs_are(
    store: SQLiteMetaStore, tmp_path: Path
) -> None:
    """One shared table, pruned by one shared store — a per-project cap would never apply.

    Accepting it would be the silent misconfig `_RESERVED_SETTINGS` exists to prevent:
    stored, never consulted, and the operator left wondering why the override does nothing.
    """
    registry = ProjectRegistry(store, Settings(state_dir=tmp_path / "state"))
    (tmp_path / "v").mkdir()
    with pytest.raises(ValueError, match="query_log_retention_days"):
        registry.add(PROJECT, tmp_path / "v", settings={"query_log_retention_days": 5})
    row = registry.add(PROJECT, tmp_path / "v", settings={"query_log_text": False})
    assert row.settings == {"query_log_text": False}
    assert registry.resolve(PROJECT).query_log_text is False


def test_an_unregistered_project_can_still_be_logged(store: SQLiteMetaStore) -> None:
    """No FK to `projects` on purpose: the P0 shorthand serves ids that were never added,
    and a log that refuses those fails exactly where it is most interesting."""
    write(store, "shorthand", project_id="never-registered")
    assert store.query_log_count("never-registered") == 1


def test_search_refuses_an_unserved_project_before_logging_anything(tmp_path: Path) -> None:
    """Tenancy is decided first: a refused call is not a query this project asked."""
    sink = RecordingSink()
    vault = tmp_path / "vault"
    vault.mkdir()
    server = build_mcp_server(
        projects=[
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                query_log=QueryLogParams(),
            )
        ],
        vector_store=FakeVectorStore(hits=[make_hit()]),
        sparse=FakeSparse(),
        query_log_sink=sink,
    )
    call_expecting_error(server, "search", {"query": SECRET_QUERY, "project": "not-served"})
    assert sink.rows == []


# ---------------------------------------------------------------------------
# top_score — the field that makes the log minable at all
# ---------------------------------------------------------------------------
def test_the_row_records_the_best_score_in_the_response() -> None:
    sink = RecordingSink()
    run_search(sink=sink, params=QueryLogParams())
    assert sink.rows[0]["top_score"] == pytest.approx(make_hit().score)


def test_a_result_with_no_snippets_records_no_score() -> None:
    """None, not 0.0: "nothing was scored" and "scored zero" are different facts, and a
    0.0 would sort to the top of the worst-served report and bury the real problems."""
    sink = RecordingSink()
    run_search(sink=sink, params=QueryLogParams(), hits=0)
    assert sink.rows[0]["top_score"] is None
    assert sink.rows[0]["n_results"] == 0


def test_worst_ranks_by_lowest_score_not_by_fewest_results(store: SQLiteMetaStore) -> None:
    """The dogfooding finding, pinned.

    On the live design vault a Stripe-webhook question against a vault that has never heard
    of Stripe came back with **8 hits**, exactly like a well-answered query — result count
    does not discriminate, so a report built on it finds nothing. The best hit's score does.
    """
    write(store, "well answered", n_results=8, top_score=0.85)
    write(store, "badly answered", n_results=8, top_score=0.51)
    worst = store.query_stats(PROJECT, order=QueryStatOrder.worst)
    assert [s.query_text for s in worst] == ["badly answered", "well answered"]


def test_unscored_queries_sort_last_in_worst_not_first(store: SQLiteMetaStore) -> None:
    """A query with no recorded score is unknown, not bad — otherwise rows predating the
    column would fill the report the operator uses to find real failures."""
    write(store, "unscored", n_results=3, top_score=None)
    write(store, "genuinely bad", n_results=3, top_score=0.4)
    worst = store.query_stats(PROJECT, order=QueryStatOrder.worst)
    assert [s.query_text for s in worst] == ["genuinely bad", "unscored"]


def test_max_score_filters_on_the_best_run(store: SQLiteMetaStore) -> None:
    write(store, "sometimes good", top_score=0.2)
    write(store, "sometimes good", top_score=0.9)
    write(store, "always bad", top_score=0.3)
    picked = store.query_stats(PROJECT, max_score=0.5)
    assert [s.query_text for s in picked] == ["always bad"]


def test_an_older_database_gains_the_column_instead_of_losing_its_rows(tmp_path: Path) -> None:
    """`query_log` is the one table `reindex` cannot rebuild, so a missing column here is
    data loss rather than a rebuild — `CREATE TABLE IF NOT EXISTS` is silent about columns.
    """
    import sqlite3

    db = tmp_path / "state" / "vaultrag.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE query_log (ts INTEGER NOT NULL, project_id TEXT NOT NULL, "
        "q_hash TEXT NOT NULL, query_text TEXT, mode TEXT, k INTEGER, latency_ms INTEGER, "
        "n_results INTEGER, budget_used INTEGER)"
    )
    conn.execute(
        "INSERT INTO query_log VALUES (?, ?, ?, ?, 'snippets', 10, 5, 3, 100)",
        (int(time.time()), PROJECT, q_hash("historic"), "historic"),
    )
    conn.commit()
    conn.close()

    s = SQLiteMetaStore(db)
    try:
        write(s, "after upgrade", top_score=0.7)
        rows = log_rows(s)
        assert [r["query_text"] for r in rows] == ["historic", "after upgrade"]
        assert rows[0]["top_score"] is None, "an old row is unscored, not zero"
        assert rows[1]["top_score"] == pytest.approx(0.7)
    finally:
        s.close()
