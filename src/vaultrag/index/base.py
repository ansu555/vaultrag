"""``VectorStore`` interface + the sparse-vector shapes it carries.

The vector store owns the *derived* half of the index: the dense + sparse vectors and
the filterable payload for every chunk. Structure and ops state live in the
:class:`~vaultrag.store.base.MetaStore`; the vault itself is the source of truth and
``vaultrag reindex`` rebuilds both from bare markdown (CLAUDE.md §6).

:class:`VectorStore` is a :class:`typing.Protocol` so the engine stays swappable
(CLAUDE.md §5). Qdrant is the P0 implementation and the only one planned — see
``../Rag/decisions/vector-search-qdrant.md``. The protocol deliberately covers the
*write* side only (ensure/upsert/delete/count); the hybrid read path is Step 9.

Sparse (BM25) vectors are modeled here rather than in ``vaultrag.embedding`` because
they are a property of *this* layer: BM25 term weights only mean something to an engine
that can apply an IDF modifier over them, which is exactly why they live in Qdrant
instead of a second search service (``../Rag/decisions/sparse-bm25-not-elasticsearch.md``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from vaultrag.embedding.base import Vector
from vaultrag.models import Chunk, Document


class VectorStoreError(RuntimeError):
    """The vector store could not complete an operation.

    Raised rather than returning a partial/empty result: a silently dropped upsert
    leaves the index disagreeing with the vault, and the caller (the Step 8 job runner)
    is the one that can retry or park the job (CLAUDE.md §5, error handling).
    """


@dataclass(frozen=True, slots=True)
class SparseVector:
    """A BM25 sparse vector: term ids and their weights, index-aligned.

    ``indices`` are hashed term ids (not vocabulary offsets) and ``values`` are term
    frequencies. The IDF half of BM25 is applied by the engine at query time via the
    collection's IDF modifier, which is why a document vector is not directly comparable
    to a query vector on its own.
    """

    indices: list[int]
    values: list[float]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError(
                f"sparse vector is ragged: {len(self.indices)} indices vs {len(self.values)} values"
            )


@runtime_checkable
class SparseEmbedder(Protocol):
    """Turns text into BM25 :class:`SparseVector` s. Documents and queries differ.

    A document is encoded with term frequencies; a query is encoded with flat weights and
    gets its IDF from the engine. Implementations are synchronous and CPU-only (BM25 is
    tokenize + stem + hash — no neural model), so async callers run them in a worker thread
    like the dense :class:`~vaultrag.embedding.base.Embedder`.
    """

    @property
    def model(self) -> str:
        """Encoder identifier, e.g. ``Qdrant/bm25`` — recorded for provenance."""
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        """Encode a batch of chunk texts, one vector per input, in order."""
        ...

    def embed_query(self, text: str) -> SparseVector:
        """Encode one query. Used by the hybrid read path (Step 9)."""
        ...


@runtime_checkable
class VectorStore(Protocol):
    """Dense + sparse vectors and their payload, keyed by :attr:`Chunk.point_id`.

    Every write is idempotent: point ids are ``uuid5(project, path, chunk_ix)``, so
    re-upserting the same chunk overwrites its point instead of duplicating it, and a
    document's points are removable by a ``doc_id`` filter without tracking ids
    (``../Rag/reference/reference-metadata-schema.md``). Re-running any step must not
    duplicate or corrupt (CLAUDE.md §5, idempotency).
    """

    def ensure_collection(self) -> None:
        """Create the collection + payload indexes if absent. Safe to call repeatedly."""

    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        vectors: Mapping[str, Vector],
        *,
        embed_model: str,
        document: Document | None = None,
    ) -> int:
        """Upsert one point per chunk. Returns the number of points written.

        ``vectors`` maps ``chunk_hash -> dense vector`` — exactly what
        :func:`~vaultrag.embedding.cache.embed_chunks` returns, so identical chunk bodies
        share one vector. A chunk whose hash is missing raises rather than being indexed
        without a vector.

        ``document`` supplies the doc-level payload fields that :class:`Chunk` does not
        carry (``title``, ``links_out``, ``frontmatter``); it applies to the chunks whose
        ``path`` matches it, and those fields are simply absent for the rest.
        """

    def delete_by_doc(self, project_id: str, doc_id: str) -> None:
        """Delete every point of one document. Deleting nothing is not an error."""

    def count(self, project_id: str | None = None) -> int:
        """Exact point count, optionally scoped to one project."""

    def close(self) -> None:
        """Release the underlying client."""
