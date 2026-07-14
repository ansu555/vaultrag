"""``Embedder`` interface + vector helpers — the seam every embedding backend sits behind.

The dense embedder is shared by two stages: the indexer (batch-embed chunk bodies,
Step 8) and the query path (embed one query, Step 9). Both depend only on this
:class:`Embedder` protocol, so swapping Ollama for ONNX int8 or a paid API (Voyage) is a
config change, never a code change (CLAUDE.md §5; ``../Rag/decisions/dense-embeddings-qwen3.md``).

A :class:`Vector` is a plain ``list[float]``. Vectors are persisted in the SQLite
``embed_cache`` BLOB column (Step 2) as packed **float32** — half the size of Python's
float64, the precision Qdrant stores anyway (arch §7-§8), and enough for cosine ranking.
Serialization lives here so the store never has to know a vector's element type.
"""

from __future__ import annotations

import array
import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

Vector = list[float]


class EmbeddingError(RuntimeError):
    """A backend failed to produce a usable embedding.

    Raised instead of returning a zero/placeholder vector: a fabricated embedding would
    silently poison the index, so the caller must decide (retry, park the job, surface
    the error) — never fake success (CLAUDE.md §5, error handling).
    """


@runtime_checkable
class Embedder(Protocol):
    """Turns text into dense vectors. One embedding per input, order preserved.

    Implementations are synchronous: the model call is I/O to a local server (Ollama) or
    CPU-bound (ONNX). Async callers (the FastAPI query path) run it in a worker thread so
    it never blocks the event loop (CLAUDE.md §5, async-first). Every returned vector has
    exactly :attr:`dims` components; a backend that cannot honor that raises
    :class:`EmbeddingError` rather than returning a wrong-width vector.
    """

    @property
    def model(self) -> str:
        """Model identifier — part of the embed-cache key ``(model, dims, chunk_hash)``."""
        ...

    @property
    def dims(self) -> int:
        """Output dimensionality — the other half of the cache key and the Qdrant vector width."""
        ...

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Embed a batch of texts, returning one :class:`Vector` per input, in order."""
        ...


# ---------------------------------------------------------------------------
# Vector post-processing (Matryoshka truncation + normalization)
# ---------------------------------------------------------------------------
def l2_normalize(vec: Sequence[float]) -> Vector:
    """Scale ``vec`` to unit L2 norm. A zero vector is returned unchanged (no div-by-zero)."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]


def fit_dims(raw: Sequence[float], dims: int) -> Vector:
    """Coerce a raw model vector to exactly ``dims`` components.

    Qwen3-Embedding is Matryoshka: the native 1024-d vector can be truncated to its first
    ``dims`` components and re-normalized to trade a little quality for ~½ the index
    (``../Rag/projects/vaultrag/embedding-pipeline.md``). Truncation breaks unit norm, so
    the result is always re-normalized — which keeps every stored vector unit-length
    regardless of whether the backend pre-normalized, a clean invariant for Qdrant cosine.
    A backend returning *fewer* than ``dims`` components is a misconfiguration (wrong model
    or ``embed_dims``), not something to pad — raise.
    """
    native = len(raw)
    if native < dims:
        raise EmbeddingError(
            f"model returned {native}-d vector but embed_dims={dims}: "
            "check the model tag / VAULTRAG_EMBED_DIMS (cannot pad up)"
        )
    return l2_normalize(raw[:dims])


# ---------------------------------------------------------------------------
# Cache (BLOB) serialization — packed float32
# ---------------------------------------------------------------------------
def serialize_vector(vec: Sequence[float]) -> bytes:
    """Pack a vector into (native-endian) float32 bytes for the ``embed_cache`` BLOB.

    The cache is a local derived artifact always read back on the machine that wrote it
    (DR = reindex — CLAUDE.md §6), so native byte order is safe and avoids a swap step.
    """
    return array.array("f", vec).tobytes()


def deserialize_vector(data: bytes) -> Vector:
    """Unpack float32 bytes (from :func:`serialize_vector`) back into a :class:`Vector`."""
    a = array.array("f")
    a.frombytes(data)
    return a.tolist()
