"""Step 9 verify: the hybrid read-path orchestration in ``retrieval/pipeline.py``.

RRF fusion and filter application are Qdrant's own job — Step 7's
``QdrantVectorStore.query()``, exercised against a real ``QdrantClient(":memory:")`` in
``tests/test_index.py`` (relevant hits, exact-identifier-via-BM25, narrowing filters).
Here the vector store is a fake that just records what it was asked and returns canned
hits, so these tests isolate pure orchestration: does ``search()`` embed the query
correctly, forward ``project_id``/``filters``/``k``/``prefetch_limit`` untouched, build
``Snippet`` s from whatever payload the store returns, and pack them into a token budget.
"""

from __future__ import annotations

from tests.fakes import PROJECT, FakeEmbedder, FakeSparse, FakeVectorStore, make_hit
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index.base import ScoredChunk, SearchFilters
from vaultrag.retrieval import search
from vaultrag.retrieval.pipeline import DEFAULT_K, DEFAULT_PREFETCH_LIMIT


# ---------------------------------------------------------------------------
# Snippet building
# ---------------------------------------------------------------------------
def test_search_builds_snippets_from_payload() -> None:
    hit = make_hit()
    store = FakeVectorStore(hits=[hit])
    result = search(
        "hybrid search",
        project_id=PROJECT,
        vector_store=store,
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
    )
    assert len(result.snippets) == 1
    snippet = result.snippets[0]
    assert snippet.path == "projects/vaultrag/hybrid-search.md"
    assert snippet.title == "Hybrid Search Details"
    assert snippet.heading_path == ["Design"]
    assert snippet.anchor == "design"
    assert snippet.text == "Dense catches paraphrase; BM25 catches exact tokens."
    assert snippet.score == 1.0
    assert snippet.tokens == 10
    assert snippet.doc_id == "doc-1"
    assert snippet.chunk_id == "point-1"


def test_search_result_carries_query_and_project() -> None:
    result = search(
        "how does hybrid search work",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=[make_hit()]),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
    )
    assert result.query == "how does hybrid search work"
    assert result.project_id == PROJECT


def test_snippet_survives_a_payload_missing_optional_fields() -> None:
    """A hit whose payload lacks title/anchor/doc_id must not crash Snippet building."""
    hit = ScoredChunk(
        point_id="point-2",
        score=0.4,
        payload={"path": "notes/x.md", "text": "body", "heading_path": []},
    )
    result = search(
        "q",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=[hit]),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
    )
    snippet = result.snippets[0]
    assert snippet.title is None
    assert snippet.anchor is None
    assert snippet.doc_id is None
    assert snippet.tokens == 0


# ---------------------------------------------------------------------------
# Query embedding + argument forwarding
# ---------------------------------------------------------------------------
def test_search_embeds_the_stripped_query_dense_and_sparse() -> None:
    embedder = FakeEmbedder()
    sparse = FakeSparse()
    search(
        "  ERR_CONN_RESET  ",
        project_id=PROJECT,
        vector_store=FakeVectorStore(),
        embedder=QueryEmbedder(embedder),
        sparse=sparse,
    )
    assert embedder.calls == 1
    assert sparse.queries == ["ERR_CONN_RESET"]


def test_search_forwards_project_id_filters_k_and_prefetch_limit() -> None:
    store = FakeVectorStore()
    filters = SearchFilters(tags=("component/qdrant",))
    search(
        "query",
        project_id=PROJECT,
        vector_store=store,
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
        k=5,
        filters=filters,
        prefetch_limit=50,
    )
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["project_id"] == PROJECT
    assert call["limit"] == 5
    assert call["prefetch_limit"] == 50
    assert call["filters"] is filters


def test_search_uses_default_k_and_prefetch_limit_when_not_given() -> None:
    store = FakeVectorStore()
    search(
        "query",
        project_id=PROJECT,
        vector_store=store,
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
    )
    call = store.calls[0]
    assert call["limit"] == DEFAULT_K
    assert call["prefetch_limit"] == DEFAULT_PREFETCH_LIMIT
    assert call["filters"] is None


def test_empty_query_returns_empty_result_without_embedding() -> None:
    embedder = FakeEmbedder()
    sparse = FakeSparse()
    store = FakeVectorStore(hits=[make_hit()])
    result = search(
        "   ",
        project_id=PROJECT,
        vector_store=store,
        embedder=QueryEmbedder(embedder),
        sparse=sparse,
    )
    assert result.snippets == []
    assert embedder.calls == 0
    assert sparse.queries == []
    assert store.calls == []


# ---------------------------------------------------------------------------
# Budget packing — P0's "simple truncation" (arch §13, plan Step 9)
# ---------------------------------------------------------------------------
def test_no_truncation_when_everything_fits_the_budget() -> None:
    hits = [
        make_hit(point_id="a", score=0.9, tokens=100),
        make_hit(point_id="b", score=0.5, tokens=100),
    ]
    result = search(
        "q",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=hits),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
        budget_tokens=2000,
    )
    assert [s.chunk_id for s in result.snippets] == ["a", "b"]
    assert result.total_tokens == 200
    assert result.truncated is False


def test_budget_packing_drops_lower_ranked_hits_and_flags_truncated() -> None:
    hits = [
        make_hit(point_id="a", score=0.9, tokens=800),
        make_hit(point_id="b", score=0.7, tokens=800),
        make_hit(point_id="c", score=0.5, tokens=800),
    ]
    result = search(
        "q",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=hits),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
        budget_tokens=2000,
    )
    assert [s.chunk_id for s in result.snippets] == ["a", "b"]
    assert result.total_tokens == 1600
    assert result.truncated is True


def test_budget_packing_always_keeps_the_top_hit_even_if_oversized() -> None:
    """An oversized best match beats an empty response — never return zero snippets just
    because the single top-ranked hit alone exceeds budget_tokens."""
    hits = [
        make_hit(point_id="a", score=0.9, tokens=5000),
        make_hit(point_id="b", score=0.1, tokens=10),
    ]
    result = search(
        "q",
        project_id=PROJECT,
        vector_store=FakeVectorStore(hits=hits),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
        budget_tokens=2000,
    )
    assert [s.chunk_id for s in result.snippets] == ["a"]
    assert result.total_tokens == 5000
    assert result.truncated is True
