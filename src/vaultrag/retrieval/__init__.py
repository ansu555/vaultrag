"""vaultrag.retrieval — Hybrid search read path: dense + BM25 to RRF fuse to cited snippets. Step 9.

:func:`search` is the one entry point: embed query (dense + BM25 sparse) → hybrid RRF
query against the Step 7 :class:`~vaultrag.index.base.VectorStore` → budget-packed
:class:`~vaultrag.models.SearchResult`. See ``pipeline.py`` and
``../Rag/projects/vaultrag/{hybrid-search,retrieval-pipeline}.md``.
"""

from __future__ import annotations

from vaultrag.index.base import SearchFilters
from vaultrag.retrieval.cite import cite, obsidian_uri
from vaultrag.retrieval.compress import (
    CompressParams,
    compress,
    compress_params_for,
)
from vaultrag.retrieval.graph import (
    GraphExpandParams,
    GraphStore,
    authority_boost,
    graph_expand,
    graph_params_for,
    neighbor_notes,
    related_notes,
)
from vaultrag.retrieval.parents import (
    DEFAULT_PARENT_MAX_TOKENS,
    ParentResolver,
    ParentSection,
    VaultParentResolver,
    parent_swap,
)
from vaultrag.retrieval.pipeline import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_K,
    DEFAULT_PREFETCH_LIMIT,
    search,
)
from vaultrag.retrieval.rerank import (
    BgeReranker,
    NoOpReranker,
    Reranker,
    RerankError,
    RerankParams,
    load_reranker,
    rerank_candidates,
    rerank_params_for,
    should_rerank,
)

__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "DEFAULT_K",
    "DEFAULT_PARENT_MAX_TOKENS",
    "DEFAULT_PREFETCH_LIMIT",
    "BgeReranker",
    "CompressParams",
    "GraphExpandParams",
    "GraphStore",
    "NoOpReranker",
    "ParentResolver",
    "ParentSection",
    "RerankError",
    "RerankParams",
    "Reranker",
    "SearchFilters",
    "VaultParentResolver",
    "authority_boost",
    "cite",
    "compress",
    "compress_params_for",
    "graph_expand",
    "graph_params_for",
    "load_reranker",
    "neighbor_notes",
    "obsidian_uri",
    "parent_swap",
    "related_notes",
    "rerank_candidates",
    "rerank_params_for",
    "search",
    "should_rerank",
]
