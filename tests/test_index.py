"""Step 7 verify: upsert; count; delete-by-doc removes them; re-upsert idempotent.

These run against ``QdrantClient(":memory:")`` — qdrant-client's local implementation —
so the real client/query surface is exercised with no Docker and no live server (the
server is an external dependency this repo doesn't control, like Ollama at Step 6). Local
mode stores the HNSW/quantization config without applying it, so the *engine* behaviors it
cannot show (real tenant-index placement, int8 recall, IDF scoring) are covered by the
live verify against `docker compose up -d qdrant`, not here.

BM25 is faked by default: FastEmbed fetches its stopword artifact from HuggingFace on
first use, which no unit test should depend on. The real encoder gets its own test that
skips when that artifact can't be fetched.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from qdrant_client import QdrantClient

from vaultrag.index import (
    DENSE_VECTOR,
    EMBED_VER,
    SPARSE_VECTOR,
    Bm25Embedder,
    QdrantVectorStore,
    ScoredChunk,
    SearchFilters,
    SparseEmbedder,
    SparseVector,
    VectorStore,
    VectorStoreError,
    chunk_payload,
    select_frontmatter,
)
from vaultrag.models import (
    Chunk,
    Document,
    DocumentMetadata,
    Link,
    LinkKind,
    chunk_point_id,
    document_id,
)

PROJECT = "vaultrag-design"
OTHER_PROJECT = "someone-elses-vault"
PATH = "projects/vaultrag/indexing-pipeline.md"
DIMS = 4


# ---------------------------------------------------------------------------
# Test doubles / fixtures
# ---------------------------------------------------------------------------
class FakeSparse:
    """Deterministic BM25 stand-in that records what it was asked to encode."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        self.seen.extend(texts)
        return [SparseVector(indices=[1, 2], values=[float(len(t)), 0.5]) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        self.seen.append(text)
        return SparseVector(indices=[1], values=[1.0])


class WordHashSparse:
    """A minimal but *distinguishing* BM25 stand-in for hybrid-query tests.

    Unlike ``FakeSparse`` (a constant vector, fine for upsert-path tests that only check
    plumbing), this hashes each whitespace token to a stable index so different texts
    produce different sparse vectors — enough to prove the *engine's* RRF fusion, not
    FastEmbed specifically, combines dense + sparse signal correctly.
    """

    @property
    def model(self) -> str:
        return "fake-wordhash-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [self._encode(t) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return self._encode(text)

    def _encode(self, text: str) -> SparseVector:
        terms: dict[int, float] = {}
        for word in text.lower().split():
            idx = hash(word) % (2**31)
            terms[idx] = terms.get(idx, 0.0) + 1.0
        if not terms:
            return SparseVector(indices=[], values=[])
        return SparseVector(indices=list(terms.keys()), values=list(terms.values()))


def make_chunk(
    *,
    ix: int = 0,
    path: str = PATH,
    project_id: str = PROJECT,
    text: str = "Qdrant holds dense + sparse vectors.",
    tags: list[str] | None = None,
) -> Chunk:
    return Chunk(
        project_id=project_id,
        doc_id=document_id(project_id, path),
        path=path,
        ix=ix,
        text=text,
        contextual_prefix=f"{project_id} ▸ {path} ▸ Design",
        heading_path=["Design"],
        anchor="design",
        tags=tags or ["component/qdrant"],
        langs=["python"],
        has_code=True,
        chunk_hash=f"hash-{path}-{ix}",
        tokens=12,
    )


def make_document(path: str = PATH) -> Document:
    return Document(
        metadata=DocumentMetadata(
            path=path,
            title="Indexing Pipeline",
            tags=["component/qdrant"],
            frontmatter={"status": "active", "author": "anik"},
            modified_ts=1_752_537_600,
        ),
        links=[Link(kind=LinkKind.wikilink, target="vector-search-qdrant")],
    )


def vectors_for(chunks: Sequence[Chunk]) -> dict[str, list[float]]:
    return {c.chunk_hash: [0.5, 0.5, 0.5, 0.5] for c in chunks}


@pytest.fixture
def store() -> QdrantVectorStore:
    s = QdrantVectorStore(
        dims=DIMS,
        collection="chunks",
        client=QdrantClient(":memory:"),
        sparse=FakeSparse(),
    )
    s.ensure_collection()
    return s


@pytest.fixture
def query_store() -> QdrantVectorStore:
    """Same as ``store`` but with a *distinguishing* sparse encoder — the ``store``
    fixture's ``FakeSparse`` returns an identical vector for every text, which is fine for
    upsert-path plumbing tests but useless for proving hybrid-query fusion behaves.
    """
    s = QdrantVectorStore(
        dims=DIMS,
        collection="chunks",
        client=QdrantClient(":memory:"),
        sparse=WordHashSparse(),
    )
    s.ensure_collection()
    return s


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
def test_qdrant_store_satisfies_vector_store_protocol(store: QdrantVectorStore) -> None:
    assert isinstance(store, VectorStore)


def test_bm25_satisfies_sparse_embedder_protocol() -> None:
    assert isinstance(Bm25Embedder(), SparseEmbedder)
    assert isinstance(FakeSparse(), SparseEmbedder)


def test_ragged_sparse_vector_is_rejected() -> None:
    with pytest.raises(ValueError, match="ragged"):
        SparseVector(indices=[1, 2], values=[1.0])


# ---------------------------------------------------------------------------
# Collection lifecycle
# ---------------------------------------------------------------------------
def test_ensure_collection_creates_named_dense_and_sparse_vectors(
    store: QdrantVectorStore,
) -> None:
    info = store._client.get_collection("chunks")
    dense = info.config.params.vectors
    assert isinstance(dense, dict) and dense[DENSE_VECTOR].size == DIMS
    assert SPARSE_VECTOR in (info.config.params.sparse_vectors or {})


def test_ensure_collection_is_idempotent(store: QdrantVectorStore) -> None:
    store.ensure_collection()
    store.ensure_collection()
    assert store._client.collection_exists("chunks")


def test_ensure_collection_rejects_dims_mismatch(store: QdrantVectorStore) -> None:
    """A changed VAULTRAG_EMBED_DIMS must name itself, not fail per-upsert later."""
    widened = QdrantVectorStore(
        dims=DIMS * 2, collection="chunks", client=store._client, sparse=FakeSparse()
    )
    with pytest.raises(VectorStoreError, match="recreate the collection"):
        widened.ensure_collection()


def test_unreachable_qdrant_says_so_instead_of_leaking_a_client_error() -> None:
    """Connection-refused surfaces as ResponseHandlingException — not an OSError — so a
    naive handler misses the single most likely failure and buries the fix."""
    store = QdrantVectorStore(dims=DIMS, url="http://localhost:6399", sparse=FakeSparse())
    with pytest.raises(VectorStoreError, match="is it running"):
        store.ensure_collection()
    store.close()


def test_ensure_collection_rejects_a_foreign_collection() -> None:
    client = QdrantClient(":memory:")
    client.create_collection("chunks", vectors_config={"other": {"size": 4, "distance": "Cosine"}})
    store = QdrantVectorStore(dims=DIMS, collection="chunks", client=client, sparse=FakeSparse())
    with pytest.raises(VectorStoreError, match="not created by VaultRAG"):
        store.ensure_collection()


# ---------------------------------------------------------------------------
# Upsert / count / delete — the Step 7 verify criteria
# ---------------------------------------------------------------------------
def test_upsert_writes_one_point_per_chunk(store: QdrantVectorStore) -> None:
    chunks = [make_chunk(ix=i) for i in range(3)]
    written = store.upsert_chunks(chunks, vectors_for(chunks), embed_model="fake-model")
    assert written == 3
    assert store.count() == 3
    assert store.count(PROJECT) == 3


def test_upsert_of_nothing_is_a_no_op(store: QdrantVectorStore) -> None:
    assert store.upsert_chunks([], {}, embed_model="fake-model") == 0
    assert store.count() == 0


def test_reupsert_is_idempotent_same_ids_no_dupes(store: QdrantVectorStore) -> None:
    """Same (project, path, ix) → same uuid5 → overwrite, never a second point."""
    chunks = [make_chunk(ix=i) for i in range(3)]
    store.upsert_chunks(chunks, vectors_for(chunks), embed_model="fake-model")
    store.upsert_chunks(chunks, vectors_for(chunks), embed_model="fake-model")
    assert store.count() == 3

    edited = [make_chunk(ix=0, text="Rewritten body."), *chunks[1:]]
    store.upsert_chunks(edited, vectors_for(edited), embed_model="fake-model")
    assert store.count() == 3
    point = store._client.retrieve("chunks", ids=[edited[0].point_id], with_payload=True)[0]
    assert point.payload is not None
    assert point.payload["text"] == "Rewritten body."


def test_point_id_is_the_deterministic_chunk_point_id(store: QdrantVectorStore) -> None:
    chunk = make_chunk(ix=2)
    store.upsert_chunks([chunk], vectors_for([chunk]), embed_model="fake-model")
    expected = chunk_point_id(PROJECT, PATH, 2)
    assert store._client.retrieve("chunks", ids=[expected])


def test_delete_by_doc_removes_that_doc_and_leaves_others(store: QdrantVectorStore) -> None:
    mine = [make_chunk(ix=i) for i in range(3)]
    other = [make_chunk(ix=0, path="decisions/vector-search-qdrant.md")]
    store.upsert_chunks(mine, vectors_for(mine), embed_model="fake-model")
    store.upsert_chunks(other, vectors_for(other), embed_model="fake-model")
    assert store.count() == 4

    store.delete_by_doc(PROJECT, document_id(PROJECT, PATH))
    assert store.count() == 1
    assert store._client.retrieve("chunks", ids=[other[0].point_id])


def test_delete_by_doc_of_an_unknown_doc_is_not_an_error(store: QdrantVectorStore) -> None:
    chunks = [make_chunk()]
    store.upsert_chunks(chunks, vectors_for(chunks), embed_model="fake-model")
    store.delete_by_doc(PROJECT, document_id(PROJECT, "does/not/exist.md"))
    assert store.count() == 1


def test_delete_by_doc_cannot_reach_across_projects(store: QdrantVectorStore) -> None:
    """The tenant filter is in the delete, so a foreign project_id deletes nothing."""
    chunks = [make_chunk()]
    store.upsert_chunks(chunks, vectors_for(chunks), embed_model="fake-model")
    store.delete_by_doc(OTHER_PROJECT, document_id(PROJECT, PATH))
    assert store.count() == 1


def test_count_is_scoped_by_project(store: QdrantVectorStore) -> None:
    mine = [make_chunk(ix=0)]
    theirs = [make_chunk(ix=0, project_id=OTHER_PROJECT)]
    store.upsert_chunks(mine, vectors_for(mine), embed_model="fake-model")
    store.upsert_chunks(theirs, vectors_for(theirs), embed_model="fake-model")
    assert store.count() == 2
    assert store.count(PROJECT) == 1
    assert store.count(OTHER_PROJECT) == 1


# ---------------------------------------------------------------------------
# Vector wiring
# ---------------------------------------------------------------------------
def test_chunk_without_a_vector_raises_rather_than_indexing_it(store: QdrantVectorStore) -> None:
    """A vector-less point would look indexed but be permanently unfindable."""
    chunks = [make_chunk()]
    with pytest.raises(VectorStoreError, match="no vector for chunk"):
        store.upsert_chunks(chunks, {}, embed_model="fake-model")
    assert store.count() == 0


def test_wrong_width_vector_raises(store: QdrantVectorStore) -> None:
    chunks = [make_chunk()]
    with pytest.raises(VectorStoreError, match="1-d vector"):
        store.upsert_chunks(chunks, {chunks[0].chunk_hash: [1.0]}, embed_model="fake-model")


def test_chunks_sharing_a_hash_share_a_vector(store: QdrantVectorStore) -> None:
    """embed_chunks() de-dupes by chunk_hash — identical bodies map to one vector."""
    a = make_chunk(ix=0, text="same body")
    b = make_chunk(ix=1, text="same body")
    shared = {"h": [0.1, 0.2, 0.3, 0.4]}
    a = a.model_copy(update={"chunk_hash": "h"})
    b = b.model_copy(update={"chunk_hash": "h"})
    assert store.upsert_chunks([a, b], shared, embed_model="fake-model") == 2
    assert store.count() == 2


def test_both_named_vectors_are_written(store: QdrantVectorStore) -> None:
    chunks = [make_chunk()]
    store.upsert_chunks(chunks, vectors_for(chunks), embed_model="fake-model")
    point = store._client.retrieve("chunks", ids=[chunks[0].point_id], with_vectors=True)[0]
    assert isinstance(point.vector, dict)
    assert len(point.vector[DENSE_VECTOR]) == DIMS
    assert point.vector[SPARSE_VECTOR].indices == [1, 2]


def test_sparse_is_encoded_from_embed_text_not_the_bare_body(store: QdrantVectorStore) -> None:
    """Path/heading tokens in the prefix are what make known-item keyword lookups land."""
    sparse = store._sparse
    assert isinstance(sparse, FakeSparse)
    chunk = make_chunk()
    store.upsert_chunks([chunk], vectors_for([chunk]), embed_model="fake-model")
    assert sparse.seen == [chunk.embed_text]
    assert PATH in sparse.seen[0]


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
def test_payload_carries_the_metadata_schema_fields(store: QdrantVectorStore) -> None:
    chunk = make_chunk()
    doc = make_document()
    store.upsert_chunks([chunk], vectors_for([chunk]), embed_model="qwen3", document=doc)
    point = store._client.retrieve("chunks", ids=[chunk.point_id], with_payload=True)[0]
    payload = point.payload
    assert payload is not None
    assert payload["project_id"] == PROJECT
    assert payload["doc_id"] == document_id(PROJECT, PATH)
    assert payload["path"] == PATH
    assert payload["chunk_ix"] == 0
    assert payload["text"] == chunk.text
    assert payload["heading_path"] == ["Design"]
    assert payload["anchor"] == "design"
    assert payload["tags"] == ["component/qdrant"]
    assert payload["type"] == "note"
    assert payload["langs"] == ["python"]
    assert payload["has_code"] is True
    assert payload["chunk_hash"] == chunk.chunk_hash
    assert payload["tokens"] == 12
    assert payload["embed_model"] == "qwen3"
    assert payload["embed_ver"] == EMBED_VER
    # doc-level fields Chunk doesn't carry, supplied by ``document``
    assert payload["title"] == "Indexing Pipeline"
    assert payload["links_out"] == ["vector-search-qdrant"]
    assert payload["frontmatter"] == {"status": "active", "author": "anik"}


def test_payload_omits_doc_fields_when_no_document_is_given() -> None:
    payload = chunk_payload(make_chunk(), embed_model="qwen3")
    assert "title" not in payload
    assert "links_out" not in payload
    assert "frontmatter" not in payload


def test_document_never_leaks_onto_another_documents_chunk() -> None:
    """Upserting a batch spanning docs must not stamp one doc's title on another's chunk."""
    stranger = make_chunk(path="decisions/vector-search-qdrant.md")
    payload = chunk_payload(stranger, embed_model="qwen3", document=make_document(PATH))
    assert "title" not in payload


def test_page_is_absent_for_notes_and_present_for_pdfs() -> None:
    assert "page" not in chunk_payload(make_chunk(), embed_model="qwen3")
    paged = make_chunk().model_copy(update={"page": 7})
    assert chunk_payload(paged, embed_model="qwen3")["page"] == 7


def test_select_frontmatter_keeps_filterable_values_only() -> None:
    kept = select_frontmatter(
        {
            "status": "active",
            "priority": 3,
            "done": False,
            "tags": ["a", "b"],
            "nested": {"deep": "value"},  # no filter can use it
            "essay": "x" * 1000,  # bloats every point
        }
    )
    assert kept == {"status": "active", "priority": 3, "done": False, "tags": ["a", "b"]}


def test_select_frontmatter_drops_non_flat_lists() -> None:
    """ "Flat list of scalars" means flat: a nested list is structure no filter can match,
    exactly like the dict case, and carrying it just bloats every point in the note."""
    kept = select_frontmatter(
        {
            "matrix": [[1, 2], [3]],
            "objects": [{"a": 1}],
            "mixed": ["ok", ["nope"]],
            "essays": ["x" * 1000],  # per-element length rules apply inside lists too
            "tags": ["a", "b"],
        }
    )
    assert kept == {"tags": ["a", "b"]}


# ---------------------------------------------------------------------------
# Hybrid query — the Step 9 read path (retrieval/ builds Snippets on top of this)
# ---------------------------------------------------------------------------
def _seed_query_fixture(store: QdrantVectorStore) -> dict[str, Chunk]:
    """Three chunks with distinguishable dense *and* sparse signal.

    - ``dense_match``: dense vector identical to the query's, body has no error code.
    - ``bm25_match``: dense vector orthogonal to the query's, body *is* the exact
      identifier the query asks for — proves fusion, not dense alone, surfaces it.
    - ``irrelevant``: neither dense- nor sparse-similar; used to prove filters narrow.
    """
    dense_match = make_chunk(
        ix=0, text="How connection pooling reuses sockets.", tags=["component/pool"]
    )
    bm25_match = make_chunk(
        ix=1, text="Investigate ERR_CONN_RESET before retrying.", tags=["component/errors"]
    )
    irrelevant = make_chunk(ix=2, text="Unrelated changelog entry.", tags=["component/docs"])
    vectors = {
        dense_match.chunk_hash: [1.0, 0.0, 0.0, 0.0],
        bm25_match.chunk_hash: [0.0, 1.0, 0.0, 0.0],
        irrelevant.chunk_hash: [0.0, 0.0, 1.0, 0.0],
    }
    store.upsert_chunks([dense_match, bm25_match, irrelevant], vectors, embed_model="fake-model")
    return {"dense_match": dense_match, "bm25_match": bm25_match, "irrelevant": irrelevant}


def test_query_returns_relevant_hits_scoped_to_project(
    query_store: QdrantVectorStore,
) -> None:
    seeded = _seed_query_fixture(query_store)
    other = make_chunk(ix=0, project_id=OTHER_PROJECT, text="Someone else's ERR_CONN_RESET note")
    query_store.upsert_chunks(
        [other], {other.chunk_hash: [0.0, 1.0, 0.0, 0.0]}, embed_model="fake-model"
    )

    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=WordHashSparse().embed_query("connection pooling sockets"),
        limit=10,
    )

    point_ids = {hit.point_id for hit in hits}
    assert seeded["dense_match"].point_id in point_ids
    assert other.point_id not in point_ids  # tenancy: never another project's points


def test_query_fusion_surfaces_exact_identifier_via_bm25(
    query_store: QdrantVectorStore,
) -> None:
    """Dense alone would favor `dense_match`; RRF fusion must still rank the exact-token
    hit highly on the strength of its sparse match (arch §11, plan Step 9 verify)."""
    seeded = _seed_query_fixture(query_store)
    sparse = WordHashSparse()

    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],  # closest to dense_match, orthogonal to bm25_match
        sparse=sparse.embed_query("ERR_CONN_RESET"),
        limit=10,
    )

    ranked_ids = [hit.point_id for hit in hits]
    assert seeded["bm25_match"].point_id in ranked_ids
    # the exact-identifier hit must not be buried behind the purely-irrelevant chunk
    assert ranked_ids.index(seeded["bm25_match"].point_id) < ranked_ids.index(
        seeded["irrelevant"].point_id
    )


def test_query_respects_limit(query_store: QdrantVectorStore) -> None:
    _seed_query_fixture(query_store)
    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=SparseVector(indices=[], values=[]),
        limit=2,
    )
    assert len(hits) == 2


def test_query_with_no_sparse_terms_still_returns_dense_hits(
    query_store: QdrantVectorStore,
) -> None:
    """A pure-stopword query encodes to an empty sparse vector; the dense branch alone
    must still carry the query rather than the call erroring out."""
    _seed_query_fixture(query_store)
    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=SparseVector(indices=[], values=[]),
        limit=10,
    )
    assert len(hits) == 3


def test_query_filters_by_tags_narrow_results(query_store: QdrantVectorStore) -> None:
    seeded = _seed_query_fixture(query_store)
    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=SparseVector(indices=[], values=[]),
        limit=10,
        filters=SearchFilters(tags=("component/errors",)),
    )
    assert {hit.point_id for hit in hits} == {seeded["bm25_match"].point_id}


def test_query_filters_by_paths_narrow_results(query_store: QdrantVectorStore) -> None:
    seeded = _seed_query_fixture(query_store)
    other_path_chunk = make_chunk(
        ix=0, path="decisions/vector-search-qdrant.md", text="A different note."
    )
    query_store.upsert_chunks(
        [other_path_chunk],
        {other_path_chunk.chunk_hash: [0.0, 0.0, 0.0, 1.0]},
        embed_model="fake-model",
    )
    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=SparseVector(indices=[], values=[]),
        limit=10,
        filters=SearchFilters(paths=(PATH,)),
    )
    point_ids = {hit.point_id for hit in hits}
    assert seeded["dense_match"].point_id in point_ids
    assert other_path_chunk.point_id not in point_ids


def test_query_filters_by_type_narrow_results(query_store: QdrantVectorStore) -> None:
    _seed_query_fixture(query_store)
    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=SparseVector(indices=[], values=[]),
        limit=10,
        filters=SearchFilters(type="pdf"),  # none of the fixture chunks are PDFs
    )
    assert hits == []


def test_query_filters_by_modified_range_narrow_results(
    query_store: QdrantVectorStore,
) -> None:
    old = make_chunk(ix=0, text="Old note.").model_copy(update={"modified_ts": 1_000})
    new = make_chunk(ix=1, text="New note.").model_copy(update={"modified_ts": 2_000_000})
    query_store.upsert_chunks(
        [old, new],
        {old.chunk_hash: [1.0, 0.0, 0.0, 0.0], new.chunk_hash: [1.0, 0.0, 0.0, 0.0]},
        embed_model="fake-model",
    )
    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=SparseVector(indices=[], values=[]),
        limit=10,
        filters=SearchFilters(modified_after=1_500_000),
    )
    assert {hit.point_id for hit in hits} == {new.point_id}


def test_query_dims_mismatch_raises(query_store: QdrantVectorStore) -> None:
    with pytest.raises(VectorStoreError, match=r"is 1-d"):
        query_store.query(
            project_id=PROJECT,
            dense=[1.0],
            sparse=SparseVector(indices=[], values=[]),
            limit=10,
        )


def test_query_unreachable_qdrant_raises() -> None:
    store = QdrantVectorStore(dims=DIMS, url="http://localhost:6399", sparse=WordHashSparse())
    with pytest.raises(VectorStoreError, match="hybrid query"):
        store.query(
            project_id=PROJECT,
            dense=[1.0, 0.0, 0.0, 0.0],
            sparse=SparseVector(indices=[], values=[]),
            limit=10,
        )
    store.close()


def test_query_result_payload_matches_scored_chunk_shape(
    query_store: QdrantVectorStore,
) -> None:
    seeded = _seed_query_fixture(query_store)
    hits = query_store.query(
        project_id=PROJECT,
        dense=[1.0, 0.0, 0.0, 0.0],
        sparse=SparseVector(indices=[], values=[]),
        limit=1,
    )
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, ScoredChunk)
    assert hit.point_id == seeded["dense_match"].point_id
    assert hit.payload["text"] == "How connection pooling reuses sockets."


# ---------------------------------------------------------------------------
# Real BM25 encoder (needs the FastEmbed artifact; skips offline)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def bm25() -> Bm25Embedder:
    encoder = Bm25Embedder()
    try:
        encoder.embed_documents(["warm up the artifact"])
    except Exception as exc:  # any fetch/runtime failure means "can't test this here"
        pytest.skip(f"FastEmbed BM25 artifact unavailable (offline?): {exc}")
    return encoder


def test_bm25_encodes_documents_to_plain_python_sparse_vectors(bm25: Bm25Embedder) -> None:
    vectors = bm25.embed_documents(["the socket closes", "ERR_CONN_RESET on the socket"])
    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec.indices) == len(vec.values)
        assert all(isinstance(i, int) for i in vec.indices)
        assert all(isinstance(v, float) for v in vec.values)


def test_bm25_keeps_an_exact_identifier_as_a_term(bm25: Bm25Embedder) -> None:
    """The whole point of sparse: `ERR_CONN_RESET` must be findable as itself (arch §11)."""
    doc = bm25.embed_documents(["ERR_CONN_RESET on the socket"])[0]
    query = bm25.embed_query("ERR_CONN_RESET")
    assert set(query.indices) & set(doc.indices)


def test_bm25_query_and_document_weighting_differ(bm25: Bm25Embedder) -> None:
    """Queries carry flat weights; IDF is the collection modifier's job, not ours."""
    assert bm25.embed_query("socket socket socket").values == [1.0]


def test_bm25_embeds_nothing_without_calling_the_encoder() -> None:
    """No lazy load, no HF fetch, for an empty batch."""
    assert Bm25Embedder().embed_documents([]) == []
