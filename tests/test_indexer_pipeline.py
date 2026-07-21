"""Step 8 verify: reindex end-to-end -- idempotent, edits/deletes/renames handled right.

Runs against ``QdrantClient(":memory:")`` (no Docker, like Step 7's tests) and a fake
:class:`~vaultrag.embedding.base.Embedder` (no live Ollama, like Step 6's tests), plumbed
through a real :class:`~vaultrag.store.sqlite.SQLiteMetaStore` on a tmp-dir database and
real fixture markdown files on disk -- so the actual parse/chunk/embed-cache/upsert wiring
is exercised, only the two external services are faked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from vaultrag.embedding.base import EmbeddingError, Vector
from vaultrag.index import QdrantVectorStore, SparseVector, VectorStore, VectorStoreError
from vaultrag.indexer.pipeline import ReindexStats, reindex_vault
from vaultrag.models import Chunk, Document, chunk_point_id, document_id
from vaultrag.store.sqlite import SQLiteMetaStore

PROJECT = "test-project"
DIMS = 4


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeSparse:
    """Deterministic BM25 stand-in -- no FastEmbed/HuggingFace fetch (Step 7 pattern)."""

    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[1], values=[1.0])


class FakeEmbedder:
    """In-memory :class:`Embedder` that counts calls (Step 6 pattern). Fails on demand."""

    def __init__(self, *, dims: int = DIMS, fail_marker: str | None = None) -> None:
        self._dims = dims
        self.calls = 0
        self.fail_marker = fail_marker

    @property
    def model(self) -> str:
        return "fake-embed-model"

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        self.calls += 1
        if self.fail_marker and any(self.fail_marker in t for t in texts):
            raise EmbeddingError("simulated embedder failure")
        return [[float(len(t) + i) for i in range(self._dims)] for t in texts]

    def close(self) -> None:
        pass


class FlakyVectorStore:
    """Wraps a real :class:`QdrantVectorStore`, failing ``upsert_chunks`` N times first."""

    def __init__(self, inner: QdrantVectorStore, *, fail_calls: int = 1) -> None:
        self._inner = inner
        self._remaining_failures = fail_calls

    def ensure_collection(self) -> None:
        self._inner.ensure_collection()

    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        vectors: Mapping[str, Vector],
        *,
        embed_model: str,
        document: Document | None = None,
    ) -> int:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise VectorStoreError("simulated vector store failure")
        return self._inner.upsert_chunks(
            chunks, vectors, embed_model=embed_model, document=document
        )

    def delete_by_doc(self, project_id: str, doc_id: str) -> None:
        self._inner.delete_by_doc(project_id, doc_id)

    def count(self, project_id: str | None = None) -> int:
        return self._inner.count(project_id)

    def close(self) -> None:
        self._inner.close()


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------
def _paragraph(words: int, seed: str) -> str:
    """A body long enough (>120 heuristic tokens) not to trip the tiny-sibling merge."""
    return " ".join(f"{seed}{i}" for i in range(words))


def write_vault(tmp_path: Path, files: Mapping[str, str]) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    for rel_path, text in files.items():
        target = vault / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return vault


def make_vector_store() -> QdrantVectorStore:
    vs = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=FakeSparse()
    )
    vs.ensure_collection()
    return vs


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    yield s
    s.close()


@pytest.fixture
def vector_store() -> QdrantVectorStore:
    return make_vector_store()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


def run(
    vault: Path, store: SQLiteMetaStore, vector_store: VectorStore, embedder: FakeEmbedder
) -> ReindexStats:
    return reindex_vault(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=embedder,
    )


ONE_SECTION_NOTE = "# Note One\n\n## Section One\n\n" + _paragraph(150, "aaa") + "\n"
TWO_SECTION_NOTE = ONE_SECTION_NOTE + "\n## Section Two\n\n" + _paragraph(150, "bbb") + "\n"


# ---------------------------------------------------------------------------
# Full reindex + idempotency
# ---------------------------------------------------------------------------
def test_reindex_creates_documents_and_points(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    stats = run(vault, store, vector_store, embedder)

    assert stats.created == 2
    assert stats.failed == 0
    assert stats.chunks_written == 1 + 2  # one.md: 1 chunk, two.md: 2 chunks
    assert vector_store.count(PROJECT) == stats.chunks_written

    doc = store.get_document(PROJECT, "one.md")
    assert doc is not None
    assert doc.hash is not None  # committed, not left reserved


def test_rerun_with_no_changes_makes_zero_model_calls_and_zero_dupes(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    calls_after_first_run = embedder.calls
    count_after_first_run = vector_store.count(PROJECT)

    stats = run(vault, store, vector_store, embedder)
    assert stats.created == stats.updated == stats.renamed == stats.deleted == 0
    assert stats.unchanged == 1
    assert embedder.calls == calls_after_first_run  # 0 new model calls
    assert vector_store.count(PROJECT) == count_after_first_run  # no dupes


def test_edit_shrinks_chunks_and_old_tail_is_not_stranded(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"note.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    assert vector_store.count(PROJECT) == 2
    old_tail_point_id = chunk_point_id(PROJECT, "note.md", 1)
    assert store.get_chunk(old_tail_point_id) is not None

    (vault / "note.md").write_text(ONE_SECTION_NOTE, encoding="utf-8")
    stats = run(vault, store, vector_store, embedder)

    assert stats.updated == 1
    assert vector_store.count(PROJECT) == 1  # the shrunk note's stale tail point is gone
    assert store.get_chunk(old_tail_point_id) is None  # and its stale SQLite row too


def test_delete_removes_document_and_points(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    other_note = ONE_SECTION_NOTE.replace("aaa", "ccc")
    vault = write_vault(tmp_path, {"keep.md": ONE_SECTION_NOTE, "gone.md": other_note})
    run(vault, store, vector_store, embedder)
    total_before = vector_store.count(PROJECT)

    (vault / "gone.md").unlink()
    stats = run(vault, store, vector_store, embedder)

    assert stats.deleted == 1
    assert store.get_document(PROJECT, "gone.md") is None
    assert store.get_document(PROJECT, "keep.md") is not None
    assert vector_store.count(PROJECT) == total_before - 1


def test_rename_moves_points_without_reembedding(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"old_name.md": ONE_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    calls_after_first_run = embedder.calls
    total_before = vector_store.count(PROJECT)

    (vault / "old_name.md").rename(vault / "new_name.md")
    stats = run(vault, store, vector_store, embedder)

    assert stats.renamed == 1
    assert embedder.calls == calls_after_first_run  # embed cache served it, hash unchanged
    assert vector_store.count(PROJECT) == total_before  # moved, not duplicated

    old_doc_id = document_id(PROJECT, "old_name.md")
    assert store.get_document(PROJECT, "old_name.md") is None
    assert store.chunk_hashes(old_doc_id) == {}
    new_doc = store.get_document(PROJECT, "new_name.md")
    assert new_doc is not None
    assert new_doc.hash is not None


# ---------------------------------------------------------------------------
# Failure isolation + partial-failure safety
# ---------------------------------------------------------------------------
def test_one_bad_file_does_not_abort_the_rest_of_the_run(
    tmp_path: Path, store: SQLiteMetaStore
) -> None:
    bad_note = ONE_SECTION_NOTE.replace("aaa", "BOOM")
    vault = write_vault(tmp_path, {"good.md": ONE_SECTION_NOTE, "bad.md": bad_note})
    vs = make_vector_store()
    embedder = FakeEmbedder(fail_marker="BOOM")

    stats = run(vault, store, vs, embedder)

    assert stats.created == 1
    assert stats.failed == 1
    assert store.get_document(PROJECT, "good.md") is not None
    assert store.get_document(PROJECT, "bad.md") is None  # never even reached the reserve step


def test_partial_failure_reserves_hash_none_and_retries_next_run(
    tmp_path: Path, store: SQLiteMetaStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"note.md": ONE_SECTION_NOTE})
    inner = make_vector_store()
    flaky = FlakyVectorStore(inner, fail_calls=1)

    stats1 = run(vault, store, flaky, embedder)
    assert stats1.failed == 1
    assert stats1.created == 0
    doc = store.get_document(PROJECT, "note.md")
    assert doc is not None
    assert doc.hash is None  # reserved, never committed -- looks unindexed, not "done"

    stats2 = run(vault, store, flaky, embedder)
    assert stats2.failed == 0
    assert stats2.updated == 1  # present-with-hash=None -> update, not create (Step 3's sentinel)
    doc2 = store.get_document(PROJECT, "note.md")
    assert doc2 is not None
    assert doc2.hash is not None
    assert inner.count(PROJECT) == 1


# ---------------------------------------------------------------------------
# Link graph (P2 Stage 2): the pipeline persists edges, and a reindex rebuilds them
# ---------------------------------------------------------------------------
def _linked_note(title: str, *targets: str) -> str:
    body = " ".join(f"[[{t}]]" for t in targets)
    return f"# {title}\n\n{body}\n\n" + _paragraph(150, title.lower().replace(" ", ""))


def test_reindex_persists_the_link_graph(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(
        tmp_path,
        {"alpha.md": _linked_note("Alpha", "beta"), "beta.md": _linked_note("Beta", "alpha")},
    )
    run(vault, store, vector_store, embedder)

    alpha = document_id(PROJECT, "alpha.md")
    beta = document_id(PROJECT, "beta.md")
    assert [link.dst_doc for link in store.outgoing_links(PROJECT, alpha)] == [beta]
    assert [d.doc_id for d in store.backlinks(PROJECT, alpha)] == [beta]


def test_a_link_to_a_not_yet_written_note_resolves_when_it_lands(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"alpha.md": _linked_note("Alpha", "Future Note")})
    run(vault, store, vector_store, embedder)
    assert [link.dst_raw for link in store.unresolved_links(PROJECT)] == ["Future Note"]

    (vault / "future-note.md").write_text(_linked_note("Future Note"), encoding="utf-8")
    run(vault, store, vector_store, embedder)

    assert store.unresolved_links(PROJECT) == []
    alpha = document_id(PROJECT, "alpha.md")
    assert [link.dst_doc for link in store.outgoing_links(PROJECT, alpha)] == [
        document_id(PROJECT, "future-note.md")
    ]


def test_editing_a_note_updates_its_edges(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(
        tmp_path,
        {
            "alpha.md": _linked_note("Alpha", "beta"),
            "beta.md": _linked_note("Beta"),
            "gamma.md": _linked_note("Gamma"),
        },
    )
    run(vault, store, vector_store, embedder)
    (vault / "alpha.md").write_text(_linked_note("Alpha", "gamma"), encoding="utf-8")
    run(vault, store, vector_store, embedder)

    alpha = document_id(PROJECT, "alpha.md")
    assert [link.dst_doc for link in store.outgoing_links(PROJECT, alpha)] == [
        document_id(PROJECT, "gamma.md")
    ]
    assert store.backlinks(PROJECT, document_id(PROJECT, "beta.md")) == []


def test_deleting_a_note_dangles_the_links_pointing_at_it(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(
        tmp_path, {"alpha.md": _linked_note("Alpha", "beta"), "beta.md": _linked_note("Beta")}
    )
    run(vault, store, vector_store, embedder)
    (vault / "beta.md").unlink()
    run(vault, store, vector_store, embedder)

    (edge,) = store.outgoing_links(PROJECT, document_id(PROJECT, "alpha.md"))
    assert edge.dst_doc is None
    assert edge.dst_raw == "beta"


def test_a_bare_vault_reindex_rebuilds_the_whole_graph(
    tmp_path: Path, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    # The index is a derived cache (CLAUDE.md §6): throwing the database away and
    # reindexing must reproduce the graph exactly, not a subset.
    vault = write_vault(
        tmp_path,
        {"alpha.md": _linked_note("Alpha", "beta"), "beta.md": _linked_note("Beta", "alpha")},
    )
    first = SQLiteMetaStore(tmp_path / "state" / "one.db")
    run(vault, first, vector_store, embedder)
    alpha = document_id(PROJECT, "alpha.md")
    before = store_graph(first, alpha)
    first.close()

    second = SQLiteMetaStore(tmp_path / "state" / "two.db")
    run(vault, second, make_vector_store(), embedder)
    assert store_graph(second, alpha) == before
    second.close()


def store_graph(store: SQLiteMetaStore, doc_id: str) -> list[tuple[str, str | None]]:
    return [(link.dst_key, link.dst_doc) for link in store.outgoing_links(PROJECT, doc_id)]


def test_reindex_backfills_the_graph_for_docs_indexed_before_stage_2(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    # Simulate a pre-Stage-2 index: documents and chunks exist, but no edges and no
    # doc_keys. Their hashes still match disk, so the diff calls them "unchanged" and no
    # ingest would ever run — without the backfill the graph would silently stay empty.
    vault = write_vault(
        tmp_path,
        {"alpha.md": _linked_note("Alpha", "beta"), "beta.md": _linked_note("Beta", "alpha")},
    )
    run(vault, store, vector_store, embedder)
    alpha = document_id(PROJECT, "alpha.md")
    store._conn.execute("DELETE FROM links")
    store._conn.execute("DELETE FROM doc_keys")
    store._conn.commit()
    assert store.outgoing_links(PROJECT, alpha) == []

    stats = run(vault, store, vector_store, embedder)

    assert stats.unchanged == 2
    assert stats.graph_backfilled == 2
    assert stats.chunks_written == 0  # metadata + edges only: nothing was re-embedded
    assert [link.dst_doc for link in store.outgoing_links(PROJECT, alpha)] == [
        document_id(PROJECT, "beta.md")
    ]


def test_backfill_is_a_no_op_once_the_graph_exists(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"alpha.md": _linked_note("Alpha", "beta")})
    run(vault, store, vector_store, embedder)
    assert run(vault, store, vector_store, embedder).graph_backfilled == 0


def test_backfill_preserves_the_stored_hash(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    # The backfill re-parses without re-hashing; if it wrote a different hash the next
    # reindex would see a spurious update and re-embed the whole vault.
    vault = write_vault(tmp_path, {"alpha.md": _linked_note("Alpha", "beta")})
    run(vault, store, vector_store, embedder)
    doc = store.get_document(PROJECT, "alpha.md")
    assert doc is not None
    store._conn.execute("DELETE FROM doc_keys")
    store._conn.commit()

    run(vault, store, vector_store, embedder)

    after = store.get_document(PROJECT, "alpha.md")
    assert after is not None
    assert after.hash == doc.hash
    assert run(vault, store, vector_store, embedder).updated == 0
