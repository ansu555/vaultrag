"""Hybrid retrieval read path — Step 9.

Turns a query into cited :class:`~vaultrag.models.Snippet` s in one Qdrant round-trip:
embed the query dense (Step 6's :class:`~vaultrag.embedding.cache.QueryEmbedder`) + BM25
sparse (Step 7's :class:`~vaultrag.index.sparse.Bm25Embedder`), hand both to
:meth:`~vaultrag.index.base.VectorStore.query` (dense k=100 + sparse k=100 prefetch, RRF
fusion, ``project_id`` + ``filters`` applied inside both branches — Step 7's
``QdrantVectorStore.query``), then greedily pack the already-ranked hits into
``budget_tokens``.

Rerank / parent-swap / graph-expand / MMR-compression are stages 4-7 of
``../Rag/projects/vaultrag/retrieval-pipeline.md`` and are explicitly **P1+, not P0**
(``../Rag/projects/vaultrag/p0-step-plan.md`` Step 9) — this module is stage 0 (scope +
filters), 2-3 (hybrid prefetch + fusion, done server-side by the vector store), a
P0-simple version of 7 (budget truncation only), and 8 (cite + respond).

Ref: ``../Rag/projects/vaultrag/hybrid-search.md``,
``../Rag/projects/vaultrag/retrieval-pipeline.md``, arch §9-§11.
"""

from __future__ import annotations

from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index.base import ScoredChunk, SearchFilters, SparseEmbedder, VectorStore
from vaultrag.models import SearchResult, Snippet

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
) -> SearchResult:
    """Run the hybrid read path for one query, scoped to ``project_id``.

    ``project_id`` is the caller's job to inject server-side from the API-key scope
    (CLAUDE.md §6); ``filters`` is whatever a client additionally asked to narrow by and
    can never widen past it — the same rule :meth:`VectorStore.query` enforces one layer
    down. An empty/whitespace-only query returns an empty result rather than embedding
    nothing meaningful.
    """
    stripped = query.strip()
    if not stripped:
        return SearchResult(query=query, project_id=project_id)

    dense_vector = embedder.embed_query(stripped)
    sparse_vector = sparse.embed_query(stripped)

    hits = vector_store.query(
        project_id=project_id,
        dense=dense_vector,
        sparse=sparse_vector,
        limit=k,
        prefetch_limit=prefetch_limit,
        filters=filters,
    )

    snippets = [_to_snippet(hit) for hit in hits]
    packed, total_tokens, truncated = pack_budget(snippets, budget_tokens)
    return SearchResult(
        query=query,
        project_id=project_id,
        snippets=packed,
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
        text=str(payload.get("text", "")),
        score=hit.score,
        tokens=int(tokens) if isinstance(tokens, (int, float)) else 0,
        doc_id=_as_str(payload.get("doc_id")),
        chunk_id=hit.point_id,
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def pack_budget(
    snippets: list[Snippet], budget_tokens: int
) -> tuple[list[Snippet], int, bool]:
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
