"""vaultrag.retrieval — Hybrid search read path: dense + BM25 to RRF fuse to cited snippets. Step 9.

:func:`search` is the one entry point: embed query (dense + BM25 sparse) → hybrid RRF
query against the Step 7 :class:`~vaultrag.index.base.VectorStore` → budget-packed
:class:`~vaultrag.models.SearchResult`. See ``pipeline.py`` and
``../Rag/projects/vaultrag/{hybrid-search,retrieval-pipeline}.md``.
"""

from __future__ import annotations

from vaultrag.index.base import SearchFilters
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
)

__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "DEFAULT_K",
    "DEFAULT_PREFETCH_LIMIT",
    "BgeReranker",
    "NoOpReranker",
    "RerankError",
    "RerankParams",
    "Reranker",
    "SearchFilters",
    "load_reranker",
    "rerank_candidates",
    "search",
]
