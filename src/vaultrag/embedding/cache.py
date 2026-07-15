"""Cache-through embedding: only ever embed a chunk body once per ``(model, dims, hash)``.

Two caches, both from ``../Rag/projects/vaultrag/embedding-pipeline.md`` (arch §8):

* **Chunk cache** — persistent, in the SQLite ``embed_cache`` table keyed
  ``(model, dims, chunk_hash)`` (Step 2). Because ``chunk_hash`` is content-addressed,
  an edited note re-embeds only its changed chunks and a pure rename re-embeds nothing.
  :func:`embed_chunks` is the seam the indexer (Step 8) calls.
* **Query cache** — in-process LRU on the read path (Step 9); repeat agent queries are
  common, and re-embedding the same string is pure waste. :class:`QueryEmbedder`.

Vectors cross the store boundary as packed float32 bytes
(:func:`~vaultrag.embedding.base.serialize_vector`); the store never sees a float list.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence

from vaultrag.embedding.base import (
    Embedder,
    Vector,
    deserialize_vector,
    serialize_vector,
)
from vaultrag.models import Chunk
from vaultrag.store.base import MetaStore


def embed_chunks(
    embedder: Embedder,
    store: MetaStore,
    chunks: Sequence[Chunk],
) -> dict[str, Vector]:
    """Embed ``chunks``, returning ``chunk_hash -> vector``, hitting the cache first.

    De-duplicates by ``chunk_hash`` so identical bodies embed once; looks up the store's
    ``embed_cache`` for ``(embedder.model, embedder.dims, hash)``; calls the model **only**
    for misses and writes those back. Re-running with an unchanged vault therefore makes
    zero model calls — the Step 6 "re-embed hits cache" property, and what keeps reindex
    cheap. Callers map each chunk to its vector via ``chunk.chunk_hash`` (a chunk that
    shares a body with another shares its point vector, which is the intended dedupe).
    """
    # First embed_text wins per hash; a shared hash means an identical body → identical embed.
    by_hash: dict[str, str] = {}
    for chunk in chunks:
        by_hash.setdefault(chunk.chunk_hash, chunk.embed_text)
    if not by_hash:
        return {}

    model, dims = embedder.model, embedder.dims
    cached = store.get_cached_vectors(model, dims, by_hash.keys())
    result: dict[str, Vector] = {h: deserialize_vector(b) for h, b in cached.items()}

    missing = [h for h in by_hash if h not in result]
    if missing:
        fresh = embedder.embed([by_hash[h] for h in missing])
        for chunk_hash, vector in zip(missing, fresh, strict=True):
            store.put_cached_vector(model, dims, chunk_hash, serialize_vector(vector))
            result[chunk_hash] = vector
    return result


class QueryEmbedder:
    """Wraps an :class:`Embedder` with an LRU cache over single-query embeds (read path).

    The query path embeds one string at a time and repeats popular queries; an LRU keyed
    on the query text avoids re-hitting the model. The cache stores an immutable tuple and
    every call returns a fresh list, so a caller mutating the result can't corrupt the
    cache.

    Safe to share across threads (the MCP server calls it from FastMCP's worker-thread
    pool): ``lru_cache`` locks its own bookkeeping, and the only race left is two threads
    missing on the same query at once, which costs one duplicate model call and stores an
    identical vector. Nothing to guard — a lock here would serialize every query embed to
    prevent a harmless repeat.
    """

    def __init__(self, embedder: Embedder, *, maxsize: int = 256) -> None:
        self._embedder = embedder
        self._cached = functools.lru_cache(maxsize=maxsize)(self._embed_uncached)

    @property
    def model(self) -> str:
        return self._embedder.model

    @property
    def dims(self) -> int:
        return self._embedder.dims

    def embed_query(self, text: str) -> Vector:
        """Embed one query string, memoized. Returns a fresh (safe-to-mutate) list."""
        return list(self._cached(text))

    def cache_clear(self) -> None:
        """Drop all memoized query vectors (e.g. after a model swap)."""
        self._cached.cache_clear()

    def _embed_uncached(self, text: str) -> tuple[float, ...]:
        return tuple(self._embedder.embed([text])[0])
