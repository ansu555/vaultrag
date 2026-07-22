"""Query logging — the offline half of the eval loop (P3 Stage 3).

Every non-blank search writes one row: what was asked (hashed, and optionally in
plain text), how it was served, and how it went. The point is not telemetry —
Prometheus already counts searches — it is **mining**: the queries that returned
nothing are the golden-set cases nobody thought to write, and
``../Rag/projects/vaultrag/evaluation-framework.md`` names ``query_log`` as where they
come from.

**Recorded in the core pipeline, not in a transport.** ``retrieval/pipeline.search`` is the
one place every caller passes through, so MCP today and the REST surface of Stage 4
tomorrow write the same row through the same code — a second, divergent copy in each
transport is the failure this placement exists to prevent.

**On storing the query text** (P3 Decision 2, ratified 2026-07-22 — see
``../Rag/agent-inbox/query-log-stores-text-opt-in.md``, which supersedes
``reference-database-schema.md``'s ``q_hash``-only row). ``reference-database-schema.md``
defines the row without the text and, eleven lines later, calls the table the source that
turns zero-hit queries into golden-set candidates. A hash cannot become a golden-set
query, so one of the two had to give. The text is stored, behind
``VAULTRAG_QUERY_LOG_TEXT`` (default **on**): the SQLite file sits beside the vault, under
the same trust boundary and far less sensitive than the notes it indexes. A shared
multi-tenant HTTP deployment sets it false and keeps the hash-only row.

What this does **not** relax is repo ``CLAUDE.md`` §5: raw query text never enters the
**log stream** or the metrics, at any level, under either setting. That rule is about what
leaves the box; this is a local table. ``tests/test_querylog.py`` pins the distinction,
because an exception that is only documented is an exception that leaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from blake3 import blake3

from vaultrag.config import Settings
from vaultrag.logging import get_logger

log = get_logger("vaultrag.querylog")

#: Hash prefix length. Long enough that collisions are not a practical concern at any
#: query volume a single box will see, short enough to read in a log line.
Q_HASH_CHARS = 12


def q_hash(query: str) -> str:
    """Stable short hash of a query — the grouping key, and the log-safe stand-in.

    Whitespace is normalized first so ``"docker  compose"`` and ``"docker compose\\n"``
    are one query in the mining reports. Case is **not** folded: this index serves a
    developer vault full of identifiers, and ``ERR_CONN_RESET`` is not ``err_conn_reset``
    to BM25, so it should not be one row here either.
    """
    return blake3(" ".join(query.split()).encode("utf-8")).hexdigest()[:Q_HASH_CHARS]


@runtime_checkable
class QueryLogSink(Protocol):
    """The one write method the pipeline needs — declared narrowly, like ``GraphStore``.

    Structurally satisfied by :class:`~vaultrag.store.sqlite.SQLiteMetaStore`; a test can
    hand the pipeline a list-backed fake without implementing the rest of ``MetaStore``.
    """

    def log_query(
        self,
        *,
        project_id: str,
        q_hash: str,
        query_text: str | None,
        mode: str,
        k: int,
        latency_ms: int,
        n_results: int,
        budget_used: int,
        top_score: float | None = None,
    ) -> None:
        """Append one query row (and prune the log to its retention cap)."""
        ...


@dataclass(frozen=True, slots=True)
class QueryLogParams:
    """Per-project query-log settings, in the shape the pipeline consumes.

    ``mode`` is the transport's own label for how the result was served (``snippets`` /
    ``compact`` for MCP, and whatever Stage 4's REST surface calls its modes). The pipeline
    does not have an opinion about it — it records what the caller says it is doing, so one
    report can tell a browse from a read.
    """

    mode: str = "snippets"
    store_text: bool = True

    def with_mode(self, mode: str) -> QueryLogParams:
        """Same policy, different mode label — how a transport stamps a per-call mode.

        The storage decision is per *project* and resolved once at startup; the mode is per
        *call*. Keeping them in one frozen object and narrowing it here means a transport
        cannot accidentally hand the pipeline a policy it did not resolve.
        """
        return QueryLogParams(mode=mode, store_text=self.store_text)


def query_log_params_for(settings: Settings, *, mode: str = "snippets") -> QueryLogParams | None:
    """The query-log behaviour a config implies — ``None`` when logging is off.

    Same placement rule as :func:`~vaultrag.retrieval.compress.compress_params_for` and its
    siblings: ``config`` is a dependency-free leaf, so the settings → behaviour mapping
    lives beside the behaviour. Returning ``None`` (rather than a disabled params object)
    keeps "don't log" and "log with these settings" distinguishable at the call site, which
    is what lets a transport skip building a sink at all.
    """
    if not settings.query_log_enabled:
        return None
    return QueryLogParams(mode=mode, store_text=settings.query_log_text)


def record_query(
    sink: QueryLogSink | None,
    params: QueryLogParams | None,
    *,
    query: str,
    project_id: str,
    k: int,
    latency_ms: float,
    n_results: int,
    budget_used: int,
    top_score: float | None = None,
) -> None:
    """Write one query row. Never raises — a failed log must not fail a search.

    A search that succeeded and then died writing its own telemetry would be the worst
    possible trade: the feature exists to *observe* retrieval, so it is strictly
    subordinate to it. Failures are logged once, at warning, with the hash only.

    **The sink alone decides whether a row is written.** ``params=None`` means "the shipped
    defaults", not "don't log" — a caller opting a tenant out withholds the *sink*, the same
    way :func:`~vaultrag.retrieval.graph.graph_expand` is disabled by withholding the graph
    store. Two arguments that could each mean "off" is how a project ends up logged because
    one of them was set and the other forgotten.
    """
    if sink is None:
        return
    effective = params if params is not None else QueryLogParams()
    digest = q_hash(query)
    try:
        sink.log_query(
            project_id=project_id,
            q_hash=digest,
            query_text=query if effective.store_text else None,
            mode=effective.mode,
            k=k,
            latency_ms=round(latency_ms),
            n_results=n_results,
            budget_used=budget_used,
            top_score=top_score,
        )
    except Exception as exc:  # deliberately total — see the docstring
        # q_hash, never the text: this is the log *stream* (CLAUDE.md §5), which the
        # storage knob does not govern.
        log.warning("querylog.write_failed", project=project_id, q_hash=digest, error=str(exc))
