"""vaultrag.index — ``VectorStore`` interface + Qdrant impl (dense + BM25 sparse). Steps 7+9.

One Qdrant collection holds every project's chunk vectors, partitioned by a tenant-indexed
``project_id`` payload field. Dense (Qwen3, cosine) answers paraphrase queries; sparse
(BM25, IDF modifier) answers the identifier/error-code queries coding agents actually send
— one engine serves both (arch §9/§11). Write side (Step 7) upserts/deletes; ``query`` (Step
9) is the hybrid RRF read side vaultrag.retrieval builds cited snippets from.

See ``base.py`` (protocols + sparse shapes), ``payload.py`` (point payload),
``sparse.py`` (FastEmbed BM25), ``qdrant.py`` (the engine wrapper), and
``../Rag/projects/vaultrag/indexing-pipeline.md``.
"""

from __future__ import annotations

from vaultrag.index.base import (
    ScoredChunk,
    SearchFilters,
    SparseEmbedder,
    SparseVector,
    VectorStore,
    VectorStoreError,
)
from vaultrag.index.payload import EMBED_VER, chunk_payload, select_frontmatter
from vaultrag.index.qdrant import DENSE_VECTOR, SPARSE_VECTOR, QdrantVectorStore
from vaultrag.index.sparse import BM25_MODEL, Bm25Embedder

__all__ = [
    "BM25_MODEL",
    "DENSE_VECTOR",
    "EMBED_VER",
    "SPARSE_VECTOR",
    "Bm25Embedder",
    "QdrantVectorStore",
    "ScoredChunk",
    "SearchFilters",
    "SparseEmbedder",
    "SparseVector",
    "VectorStore",
    "VectorStoreError",
    "chunk_payload",
    "select_frontmatter",
]
