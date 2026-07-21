"""Hybrid retrieval read path — Step 9.

Turns a query into cited :class:`~vaultrag.models.Snippet` s in one Qdrant round-trip:
embed the query dense (Step 6's :class:`~vaultrag.embedding.cache.QueryEmbedder`) + BM25
sparse (Step 7's :class:`~vaultrag.index.sparse.Bm25Embedder`), hand both to
:meth:`~vaultrag.index.base.VectorStore.query` (dense k=100 + sparse k=100 prefetch, RRF
fusion, ``project_id`` + ``filters`` applied inside both branches — Step 7's
``QdrantVectorStore.query``), then greedily pack the already-ranked hits into
``budget_tokens``.

This module is the stage sequencer. Stage 0 (scope + filters) and 2-3 (hybrid prefetch +
fusion, done server-side by the vector store) live here; stages 4 (rerank), 5 (parent
swap), 6 (graph expand) and 8 (cite) are each their own module and each opt in through a
parameter being non-``None``, so a caller composes exactly the pipeline it wants. Stage 7
is still the P0-simple version — greedy budget truncation, no MMR/near-dupe drop (arch
§13). Stage 1 (query expansion) is default-off by design and unbuilt.

Ref: ``../Rag/projects/vaultrag/hybrid-search.md``,
``../Rag/projects/vaultrag/retrieval-pipeline.md``, arch §9-§11.
"""

from __future__ import annotations

from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index.base import ScoredChunk, SearchFilters, SparseEmbedder, VectorStore
from vaultrag.models import SearchResult, Snippet
from vaultrag.retrieval.cite import cite
from vaultrag.retrieval.graph import GraphExpandParams, GraphStore, graph_expand
from vaultrag.retrieval.parents import DEFAULT_PARENT_MAX_TOKENS, ParentResolver, parent_swap
from vaultrag.retrieval.rerank import Reranker, RerankParams, rerank_candidates

DEFAULT_K = 10
DEFAULT_PREFETCH_LIMIT = 100
DEFAULT_BUDGET_TOKENS = 2000


def search(
    query: str,
    *,
    project_id: str,
    vector_store: VectorStore,
    embedder: QueryEmbedder,
    sparse: SparseEmbedder,
    k: int = DEFAULT_K,
    filters: SearchFilters | None = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    prefetch_limit: int = DEFAULT_PREFETCH_LIMIT,
    reranker: Reranker | None = None,
    rerank: RerankParams | None = None,
    parent_resolver: ParentResolver | None = None,
    parent_max_tokens: int = DEFAULT_PARENT_MAX_TOKENS,
    graph_store: GraphStore | None = None,
    graph: GraphExpandParams | None = None,
    vault_name: str | None = None,
) -> SearchResult:
    """Run the hybrid read path for one query, scoped to ``project_id``.

    ``project_id`` is the caller's job to inject server-side from the API-key scope
    (CLAUDE.md §6); ``filters`` is whatever a client additionally asked to narrow by and
    can never widen past it — the same rule :meth:`VectorStore.query` enforces one layer
    down. An empty/whitespace-only query returns an empty result rather than embedding
    nothing meaningful.

    When ``reranker`` is given, stage 4 runs: the fused prefetch is widened from ``k`` to
    ``rerank.top_n`` candidates, cross-encoder-reranked, thresholded, and cut back to
    ``k`` before budget packing (``../Rag/projects/vaultrag/reranking.md``). Without a
    reranker the path is P0's fusion-order-then-pack.

    When ``parent_resolver`` is given, stage 5 runs after rerank: a surviving child is
    swapped for its enclosing H1/H2 section and same-section children are deduped into one
    parent snippet, capped at ``min(parent_max_tokens, budget_tokens)`` (a parent that
    won't fit falls back to the child — ``retrieval/parents.py``). Without a resolver the
    hits become child snippets unchanged.

    When ``graph_store`` is given, stage 6 runs after parent-swap: the kept snippets are
    reordered by how many *other* result documents link to each (a query-conditional
    authority signal from the vault's own wikilinks), and the 1-hop neighbours of the top
    documents come back in ``SearchResult.related`` as titles+paths — outside ``snippets``
    and outside the token budget, so a pointer can never displace a result
    (``retrieval/graph.py``). Without a store the stage is skipped entirely.

    When ``vault_name`` is given, stage 8 (cite) stamps each snippet with a working
    ``obsidian://open`` URI into that vault (``retrieval/cite.py``); without it, ``uri``
    stays ``None`` (a caller not serving an Obsidian client, e.g. the eval harness).
    """
    stripped = query.strip()
    if not stripped:
        return SearchResult(query=query, project_id=project_id)

    rerank_params = rerank if rerank is not None else RerankParams()
    # Widen the fetch only when we'll actually rerank — no point pulling 50 to keep 10.
    # `max` because `top_n` is a *cost* knob (how deep the cross-encoder looks), never a
    # recall cap: at top_n < k a bare `top_n` here would fetch fewer candidates than the
    # caller asked to keep, so rerank-on would silently return a shorter list than
    # rerank-off and a top_n sweep would measure truncation instead of rerank depth.
    fetch_limit = max(rerank_params.top_n, k) if reranker is not None else k

    dense_vector = embedder.embed_query(stripped)
    sparse_vector = sparse.embed_query(stripped)

    hits = vector_store.query(
        project_id=project_id,
        dense=dense_vector,
        sparse=sparse_vector,
        limit=fetch_limit,
        prefetch_limit=prefetch_limit,
        filters=filters,
    )
    hits = rerank_candidates(stripped, hits, reranker, keep=k, params=rerank_params)

    if parent_resolver is not None:
        # Cap the swap at the smaller of the ~2k parent cap and the response budget: a
        # section that can't fit the budget should fall back to the child, not evict it.
        cap = min(parent_max_tokens, budget_tokens)
        snippets = parent_swap(hits, parent_resolver, _to_snippet, cap_tokens=cap)
    else:
        snippets = [_to_snippet(hit) for hit in hits]

    # Stage 6: link-graph authority reorder + neighbour pointers. Runs on the post-swap
    # snippets (so it ranks whole sections when stage 5 produced them) and *before* packing,
    # because reordering decides which prefix the greedy packer keeps. `related` is returned
    # alongside — never merged into `snippets` — so pointers cost no budget and can't be
    # mistaken for retrieved results (that mistake would silently inflate any recall metric).
    snippets, related = graph_expand(
        snippets,
        project_id=project_id,
        store=graph_store,
        params=graph if graph is not None else GraphExpandParams(),
        vault_name=vault_name,
    )
    # Stage 8: attach the obsidian:// citation URI (no-op when vault_name is unset). Done
    # before packing so the kept snippets carry it; the URI isn't text, so it costs no budget.
    snippets = cite(snippets, vault_name=vault_name)
    packed, total_tokens, truncated = pack_budget(snippets, budget_tokens)
    return SearchResult(
        query=query,
        project_id=project_id,
        snippets=packed,
        related=related,
        total_tokens=total_tokens,
        truncated=truncated,
    )


def _to_snippet(hit: ScoredChunk) -> Snippet:
    """Build a cited :class:`Snippet` straight from the payload — no SQLite round-trip."""
    payload = hit.payload
    heading_path = payload.get("heading_path")
    tokens = payload.get("tokens")
    return Snippet(
        path=str(payload.get("path", "")),
        title=_as_str(payload.get("title")),
        heading_path=[str(h) for h in heading_path] if isinstance(heading_path, list) else [],
        anchor=_as_str(payload.get("anchor")),
        line_start=_as_int(payload.get("line_start")),
        line_end=_as_int(payload.get("line_end")),
        # Absent on notes — the payload omits `page` entirely rather than storing null.
        page=_as_int(payload.get("page")),
        text=str(payload.get("text", "")),
        score=hit.score,
        tokens=int(tokens) if isinstance(tokens, (int, float)) else 0,
        doc_id=_as_str(payload.get("doc_id")),
        chunk_id=hit.point_id,
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    # bool is an int subclass; a stray True/False in the payload isn't a line number.
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def pack_budget(snippets: list[Snippet], budget_tokens: int) -> tuple[list[Snippet], int, bool]:
    """Greedy best-first pack: hits already arrive RRF-ranked, so keep taking snippets in
    that order until the next one would exceed ``budget_tokens``. Returns
    ``(kept, total_tokens, truncated)``.

    The top hit is always kept even if it alone exceeds the budget — an oversized best
    match beats an empty response, and a search that returns nothing because its single
    best hit didn't fit would look like "no relevant results" when the opposite is true.
    ``truncated`` is True whenever a lower-ranked hit existed but didn't make the cut.
    Full compression (near-dupe drop, MMR, per-doc cap) is P1 (arch §13); P0 is this
    "simple truncation" per the plan.

    Public because it is *the* budget-packing rule: the MCP layer re-packs compact-mode
    previews with it so ``budget_tokens``/``truncated`` mean the same thing in every
    search mode.
    """
    packed: list[Snippet] = []
    total = 0
    for snippet in snippets:
        if packed and total + snippet.tokens > budget_tokens:
            break
        packed.append(snippet)
        total += snippet.tokens
    return packed, total, len(packed) < len(snippets)
