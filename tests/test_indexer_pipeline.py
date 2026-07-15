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
