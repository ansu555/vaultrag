"""Qdrant-backed :class:`VectorStore` — the P0 vector engine.

One collection (``chunks``) holds every project, with named vectors ``dense`` (cosine)
and sparse ``bm25`` (IDF modifier), and tenants separated by a tenant-indexed
``project_id`` payload field rather than a collection each — collection-per-project's
per-tenant overhead is what kills you at scale
(``../Rag/decisions/payload-partitioning-not-collection-per-project.md``, arch §9).

Collection config comes from ``../Rag/projects/vaultrag/indexing-pipeline.md``: int8
scalar quantization with quantized vectors in RAM and originals on disk (≈100 MB RAM at
100k chunks), HNSW ``m=16``/``ef_construct=128``.

Requires a running Qdrant::

    docker compose up -d qdrant
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

import structlog
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ApiException

from vaultrag.config import Settings
from vaultrag.embedding.base import Vector
from vaultrag.index.base import (
    ScoredChunk,
    SearchFilters,
    SparseEmbedder,
    SparseVector,
    VectorStoreError,
)
from vaultrag.index.payload import chunk_payload
from vaultrag.index.sparse import Bm25Embedder
from vaultrag.models import Chunk, Document

log = structlog.get_logger(__name__)

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"

_HNSW_M = 16
_HNSW_EF_CONSTRUCT = 128
_UPSERT_BATCH = 256

# Everything qdrant-client raises for a call that didn't work. ``ApiException`` is the base
# of both ``UnexpectedResponse`` (server said no) and ``ResponseHandlingException`` (never
# reached the server — connection refused wraps into this, *not* into OSError); local mode
# raises plain ValueError. Each becomes a VectorStoreError naming the collection.
_CLIENT_ERRORS = (ApiException, OSError, ValueError)

# Payload indexes (indexing-pipeline.md). ``project_id`` is the tenant key: the tenant
# index co-locates a project's points on disk, which is what keeps per-tenant queries fast
# at thousands of projects.
_KEYWORD_INDEXES = ("tags", "path", "type")


class QdrantVectorStore:
    """Dense + BM25 sparse index over one Qdrant collection.

    ``sparse`` defaults to :class:`~vaultrag.index.sparse.Bm25Embedder`; inject one (or a
    stub) to control the encoder. ``client`` is injectable the same way — tests pass
    ``QdrantClient(":memory:")``. A client this store constructed is closed by
    :meth:`close`; an injected one is left to its owner.
    """

    def __init__(
        self,
        *,
        dims: int,
        collection: str = "chunks",
        url: str = "http://localhost:6333",
        client: QdrantClient | None = None,
        sparse: SparseEmbedder | None = None,
    ) -> None:
        if dims <= 0:
            raise ValueError(f"dims must be positive, got {dims}")
        self._dims = dims
        self._collection = collection
        self._owns_client = client is None
        # check_compatibility=False: the default fires a blocking version-check GET from
        # the constructor, which turns "construct a store" into "reach the network" —
        # it would stall `vaultrag mcp` startup (and log a warning to stdout, the MCP
        # JSON-RPC channel) whenever Qdrant is down. Real calls surface that anyway.
        self._client = client or QdrantClient(url=url, check_compatibility=False)
        self._sparse: SparseEmbedder = sparse or Bm25Embedder()

    @classmethod
    def from_settings(cls, settings: Settings) -> QdrantVectorStore:
        """Build from env-driven :class:`~vaultrag.config.Settings`."""
        return cls(
            dims=settings.embed_dims,
            collection=settings.qdrant_collection,
            url=settings.qdrant_url,
        )

    @property
    def collection(self) -> str:
        return self._collection

    @property
    def dims(self) -> int:
        return self._dims

    # --- schema ---------------------------------------------------------------
    def ensure_collection(self) -> None:
        """Create the collection + payload indexes if absent; verify them if present."""
        try:
            exists = self._client.collection_exists(self._collection)
        except _CLIENT_ERRORS as exc:
            raise VectorStoreError(
                f"cannot reach Qdrant for collection {self._collection!r}: {exc} "
                "(is it running? `docker compose up -d qdrant`)"
            ) from exc

        if exists:
            self._check_dims()
        else:
            self._create_collection()
        self._ensure_payload_indexes()

    def _create_collection(self) -> None:
        log.info("qdrant.create_collection", collection=self._collection, dims=self._dims)
        try:
            self._client.create_collection(
                self._collection,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=self._dims,
                        distance=models.Distance.COSINE,
                        # Originals on disk, int8 copies in RAM: the quantized vectors do
                        # the searching, the originals only rescore.
                        on_disk=True,
                        hnsw_config=models.HnswConfigDiff(
                            m=_HNSW_M, ef_construct=_HNSW_EF_CONSTRUCT
                        ),
                        quantization_config=models.ScalarQuantization(
                            scalar=models.ScalarQuantizationConfig(
                                type=models.ScalarType.INT8, always_ram=True
                            )
                        ),
                    )
                },
                # IDF lives on the collection: FastEmbed emits term frequencies, Qdrant
                # supplies the corpus-wide IDF at query time.
                sparse_vectors_config={
                    SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
            )
        except _CLIENT_ERRORS as exc:
            raise VectorStoreError(
                f"failed to create collection {self._collection!r}: {exc}"
            ) from exc

    def _check_dims(self) -> None:
        """Fail loudly when an existing collection's width disagrees with ``embed_dims``.

        Qdrant would reject every upsert one by one; catching it here names the actual
        cause (a changed ``VAULTRAG_EMBED_DIMS`` or embedding model). The index is a
        derived cache, so the fix is to recreate it and reindex — never to reshape vectors.
        """
        try:
            info = self._client.get_collection(self._collection)
        except _CLIENT_ERRORS as exc:
            raise VectorStoreError(
                f"cannot read config of collection {self._collection!r}: {exc}"
            ) from exc
        config = info.config.params.vectors
        if not isinstance(config, dict) or DENSE_VECTOR not in config:
            raise VectorStoreError(
                f"collection {self._collection!r} has no {DENSE_VECTOR!r} named vector — "
                "it was not created by VaultRAG; use a different VAULTRAG_QDRANT_COLLECTION"
            )
        existing = config[DENSE_VECTOR].size
        if existing != self._dims:
            raise VectorStoreError(
                f"collection {self._collection!r} stores {existing}-d vectors but "
                f"embed_dims={self._dims}: recreate the collection and reindex, or restore "
                "VAULTRAG_EMBED_DIMS"
            )

    def _ensure_payload_indexes(self) -> None:
        try:
            self._client.create_payload_index(
                self._collection,
                "project_id",
                field_schema=models.KeywordIndexParams(
                    type=models.KeywordIndexType.KEYWORD, is_tenant=True
                ),
            )
            for field in _KEYWORD_INDEXES:
                self._client.create_payload_index(
                    self._collection, field, field_schema=models.PayloadSchemaType.KEYWORD
                )
            self._client.create_payload_index(
                self._collection, "modified_ts", field_schema=models.PayloadSchemaType.INTEGER
            )
        except _CLIENT_ERRORS as exc:
            raise VectorStoreError(
                f"failed to create payload indexes on {self._collection!r}: {exc}"
            ) from exc

    # --- writes ---------------------------------------------------------------
    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        vectors: Mapping[str, Vector],
        *,
        embed_model: str,
        document: Document | None = None,
    ) -> int:
        """Upsert one point per chunk (id = :attr:`Chunk.point_id`). Returns points written."""
        if not chunks:
            return 0
        t0 = time.perf_counter()
        sparse = self._sparse.embed_documents([c.embed_text for c in chunks])
        points = [
            self._point(chunk, vectors, sparse_vec, embed_model=embed_model, document=document)
            for chunk, sparse_vec in zip(chunks, sparse, strict=True)
        ]
        for start in range(0, len(points), _UPSERT_BATCH):
            batch = points[start : start + _UPSERT_BATCH]
            try:
                self._client.upsert(self._collection, points=batch, wait=True)
            except _CLIENT_ERRORS as exc:
                raise VectorStoreError(
                    f"upsert of {len(batch)} points into {self._collection!r} failed: {exc}"
                ) from exc
        log.debug(
            "qdrant.upsert",
            collection=self._collection,
            n=len(points),
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return len(points)

    def _point(
        self,
        chunk: Chunk,
        vectors: Mapping[str, Vector],
        sparse: SparseVector,
        *,
        embed_model: str,
        document: Document | None,
    ) -> models.PointStruct:
        dense = vectors.get(chunk.chunk_hash)
        if dense is None:
            # Indexing a chunk without its vector would make it permanently unfindable
            # while looking indexed — a caller bug, not something to paper over.
            raise VectorStoreError(
                f"no vector for chunk {chunk.path}#{chunk.ix} (hash {chunk.chunk_hash[:12]}): "
                "pass the mapping returned by embed_chunks()"
            )
        if len(dense) != self._dims:
            raise VectorStoreError(
                f"chunk {chunk.path}#{chunk.ix} has a {len(dense)}-d vector but the "
                f"collection stores {self._dims}-d"
            )
        return models.PointStruct(
            id=chunk.point_id,
            vector={
                DENSE_VECTOR: list(dense),
                SPARSE_VECTOR: models.SparseVector(indices=sparse.indices, values=sparse.values),
            },
            payload=chunk_payload(chunk, embed_model=embed_model, document=document),
        )

    def delete_by_doc(self, project_id: str, doc_id: str) -> None:
        """Delete every point of one document.

        ``doc_id`` is already project-scoped (``uuid5(project, path)``), but the filter
        carries ``project_id`` anyway: a tenant-scoped delete can never touch another
        project's points even if a caller passes a foreign id (CLAUDE.md §6, tenancy).
        """
        try:
            self._client.delete(
                self._collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            _match("project_id", project_id),
                            _match("doc_id", doc_id),
                        ]
                    )
                ),
                wait=True,
            )
        except _CLIENT_ERRORS as exc:
            raise VectorStoreError(
                f"delete of doc {doc_id} from {self._collection!r} failed: {exc}"
            ) from exc

    # --- reads ----------------------------------------------------------------
    def query(
        self,
        *,
        project_id: str,
        dense: Vector,
        sparse: SparseVector,
        limit: int,
        prefetch_limit: int = 100,
        filters: SearchFilters | None = None,
    ) -> list[ScoredChunk]:
        """Hybrid RRF search: one Query API call, two filtered prefetch branches.

        Both branches carry the same filter (``project_id`` + ``filters``) so neither can
        starve the other before fusion — a post-hoc filter on the fused list would let a
        narrow query lose candidates the ANN search never returned in the first place
        (``../Rag/projects/vaultrag/hybrid-search.md``).
        """
        if len(dense) != self._dims:
            raise VectorStoreError(
                f"query vector is {len(dense)}-d but the collection stores {self._dims}-d"
            )
        qfilter = _build_filter(project_id, filters)
        prefetch = [
            models.Prefetch(
                query=list(dense), using=DENSE_VECTOR, filter=qfilter, limit=prefetch_limit
            )
        ]
        if sparse.indices:
            prefetch.append(
                models.Prefetch(
                    query=models.SparseVector(indices=sparse.indices, values=sparse.values),
                    using=SPARSE_VECTOR,
                    filter=qfilter,
                    limit=prefetch_limit,
                )
            )
        try:
            result = self._client.query_points(
                self._collection,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
                with_payload=True,
            )
        except _CLIENT_ERRORS as exc:
            raise VectorStoreError(f"hybrid query on {self._collection!r} failed: {exc}") from exc
        return [
            ScoredChunk(point_id=str(point.id), score=point.score, payload=point.payload or {})
            for point in result.points
        ]

    def count(self, project_id: str | None = None) -> int:
        """Exact point count, optionally scoped to one project."""
        count_filter = (
            models.Filter(must=[_match("project_id", project_id)])
            if project_id is not None
            else None
        )
        try:
            return self._client.count(self._collection, count_filter=count_filter, exact=True).count
        except _CLIENT_ERRORS as exc:
            raise VectorStoreError(f"count on {self._collection!r} failed: {exc}") from exc

    def close(self) -> None:
        """Close the Qdrant client (only if this store created it)."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> QdrantVectorStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _match(key: str, value: str) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _build_filter(project_id: str, filters: SearchFilters | None) -> models.Filter:
    """``project_id`` is always a ``must`` clause; ``filters`` can only add more of them —
    there is no way to reach this function with a condition that widens past one project.
    """
    must: list[models.Condition] = [_match("project_id", project_id)]
    if filters is not None:
        if filters.tags:
            must.append(
                models.FieldCondition(key="tags", match=models.MatchAny(any=list(filters.tags)))
            )
        if filters.paths:
            must.append(
                models.FieldCondition(key="path", match=models.MatchAny(any=list(filters.paths)))
            )
        if filters.type is not None:
            must.append(_match("type", filters.type))
        if filters.modified_after is not None or filters.modified_before is not None:
            must.append(
                models.FieldCondition(
                    key="modified_ts",
                    range=models.Range(gte=filters.modified_after, lt=filters.modified_before),
                )
            )
    return models.Filter(must=must)
