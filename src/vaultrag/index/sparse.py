"""FastEmbed BM25 — the sparse half of hybrid search.

Dev notes are full of identifiers, error codes and config keys, and coding-agent traffic
skews hard toward exactly those queries, so keyword search is mandatory rather than a
nice-to-have (``../Rag/decisions/sparse-bm25-not-elasticsearch.md``, arch §11). FastEmbed
turns text into BM25 term-frequency vectors; Qdrant applies IDF at query time via the
collection's modifier. No second search service, no extra RAM.

BM25 here is not a neural model — it is tokenize + stopword-strip + stem + hash — but
FastEmbed still fetches a small stopword/stemmer artifact from HuggingFace on first use.
That download is deferred to the first encode (:attr:`Bm25Embedder._encoder`) so
constructing a store, or importing this module, never touches the network.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import structlog

from vaultrag.index.base import SparseVector, VectorStoreError

if TYPE_CHECKING:
    from fastembed import SparseTextEmbedding

log = structlog.get_logger(__name__)

BM25_MODEL = "Qdrant/bm25"


class Bm25Embedder:
    """BM25 sparse encoder backed by FastEmbed.

    Pass ``encoder`` to inject one (tests use a stub); otherwise it is constructed lazily
    on first use so the HuggingFace fetch happens when someone actually indexes.
    """

    def __init__(
        self,
        *,
        model: str = BM25_MODEL,
        encoder: SparseTextEmbedding | None = None,
    ) -> None:
        self._model = model
        self._encoder_instance = encoder

    @property
    def model(self) -> str:
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        """Encode chunk texts → term-frequency vectors, one per input, in order."""
        if not texts:
            return []
        raw = self._encoder.embed(list(texts))
        vectors = [_to_sparse_vector(r) for r in raw]
        if len(vectors) != len(texts):
            raise VectorStoreError(
                f"BM25 encoder returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return vectors

    def embed_query(self, text: str) -> SparseVector:
        """Encode one query → flat term weights; IDF is applied by Qdrant (Step 9)."""
        raw = list(self._encoder.query_embed(text))
        if not raw:
            # A query of pure stopwords legitimately has no terms; an empty sparse vector
            # simply matches nothing, leaving the dense branch to carry the query.
            return SparseVector(indices=[], values=[])
        return _to_sparse_vector(raw[0])

    # --- internals ------------------------------------------------------------
    @property
    def _encoder(self) -> SparseTextEmbedding:
        if self._encoder_instance is None:
            from fastembed import SparseTextEmbedding

            log.debug("bm25.load", model=self._model)
            self._encoder_instance = SparseTextEmbedding(model_name=self._model)
        return self._encoder_instance


def _to_sparse_vector(raw: Any) -> SparseVector:
    """Convert a FastEmbed ``SparseEmbedding`` (numpy arrays) to plain Python.

    The numpy scalar types serialize badly over Qdrant's JSON transport, and the indices
    are unsigned 32-bit term hashes — cast both to native types at this boundary so
    nothing downstream has to care that FastEmbed used numpy.
    """
    try:
        indices = [int(i) for i in raw.indices]
        values = [float(v) for v in raw.values]
    except AttributeError as exc:
        raise VectorStoreError(f"unexpected BM25 encoder output: {type(raw).__name__}") from exc
    return SparseVector(indices=indices, values=values)
