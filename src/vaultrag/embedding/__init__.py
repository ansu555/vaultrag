"""vaultrag.embedding — ``Embedder`` interface + Ollama impl + (model, dims, hash) cache. Step 6.

The dense embedder is shared by the indexer (batch, Step 8) and the query path (Step 9),
both depending only on the :class:`Embedder` protocol so the backend is a config swap
(CLAUDE.md §5). Ollama/Qwen3 is the P0 default; ONNX and paid APIs are future impls.
See ``base.py`` (protocol + codec), ``ollama.py`` (backend), ``cache.py`` (cache-through +
query LRU), and ``../Rag/projects/vaultrag/embedding-pipeline.md``.
"""

from __future__ import annotations

from vaultrag.embedding.base import (
    Embedder,
    EmbeddingError,
    Vector,
    deserialize_vector,
    fit_dims,
    l2_normalize,
    serialize_vector,
)
from vaultrag.embedding.cache import QueryEmbedder, embed_chunks
from vaultrag.embedding.ollama import OllamaEmbedder

__all__ = [
    "Embedder",
    "EmbeddingError",
    "OllamaEmbedder",
    "QueryEmbedder",
    "Vector",
    "deserialize_vector",
    "embed_chunks",
    "fit_dims",
    "l2_normalize",
    "serialize_vector",
]
