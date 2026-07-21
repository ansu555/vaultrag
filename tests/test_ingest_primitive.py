"""P1 Stage 4 verify: the single-file ``ingest_path`` / ``remove_path`` primitive.

The unit both live-watch and (P1 Stage 5) write-back drive: reconcile ONE vault-relative
path against disk. These exercise the real parse/chunk/embed-cache/upsert wiring through a
real :class:`~vaultrag.store.sqlite.SQLiteMetaStore` + in-memory Qdrant + a fake embedder
(the ``test_indexer_pipeline`` pattern), and pin the single-file gates that keep a live
edit from indexing anything a full ``reindex`` would withhold (CLAUDE.md §6).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from vaultrag.embedding.base import EmbeddingError, Vector
from vaultrag.index import QdrantVectorStore, SparseVector
from vaultrag.indexer.pipeline import (
    IngestContext,
    IngestStatus,
    SkipReason,
    ingest_path,
    remove_path,
)
from vaultrag.store.sqlite import SQLiteMetaStore

PROJECT = "test-project"
DIMS = 4

# An on-sight AWS access-key-id (matches secret_guard._TOKEN_RULES); never a real key.
PLANTED_SECRET = "AKIAIOSFODNN7EXAMPLE"


class FakeSparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[1], values=[1.0])


class FakeEmbedder:
    """Counts calls; can be told to fail when a marker appears in the batch."""

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


def _paragraph(words: int, seed: str) -> str:
    return " ".join(f"{seed}{i}" for i in range(words))


NOTE = "# Note\n\n## Section\n\n" + _paragraph(150, "aaa") + "\n"
OTHER_NOTE = "# Note\n\n## Section\n\n" + _paragraph(150, "bbb") + "\n"


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    s.upsert_project(PROJECT, name=PROJECT, vault_path=str(tmp_path / "vault"))
    yield s
    s.close()


@pytest.fixture
def vector_store() -> QdrantVectorStore:
    vs = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=FakeSparse()
    )
    vs.ensure_collection()
    return vs


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


def make_ctx(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> IngestContext:
    return IngestContext(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=embedder,
    )


def write(vault: Path, rel: str, text: str) -> None:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Index / unchanged / evict
# ---------------------------------------------------------------------------
def test_ingest_path_indexes_a_new_file(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    write(vault, "note.md", NOTE)
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)

    outcome = ingest_path(ctx, "note.md")

    assert outcome.status is IngestStatus.indexed
    assert outcome.indexed and outcome.chunks == 1
    doc = store.get_document(PROJECT, "note.md")
    assert doc is not None and doc.hash is not None  # committed, not left reserved
    assert vector_store.count(PROJECT) == 1


def test_ingest_path_unchanged_content_skips_reembed(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    write(vault, "note.md", NOTE)
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)

    ingest_path(ctx, "note.md")
    calls_after_first = embedder.calls

    outcome = ingest_path(ctx, "note.md")
    assert outcome.status is IngestStatus.unchanged
    assert outcome.chunks == 0
    assert embedder.calls == calls_after_first  # manifest hash matched → no model call
    assert vector_store.count(PROJECT) == 1  # no dupes


def test_ingest_path_reindexes_on_edit(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    write(vault, "note.md", NOTE)
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)
    ingest_path(ctx, "note.md")

    write(vault, "note.md", OTHER_NOTE)  # same shape, different words → different hash
    outcome = ingest_path(ctx, "note.md")
    assert outcome.status is IngestStatus.indexed
    assert vector_store.count(PROJECT) == 1


def test_ingest_path_gone_file_is_evicted(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    write(vault, "note.md", NOTE)
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)
    ingest_path(ctx, "note.md")

    (vault / "note.md").unlink()
    outcome = ingest_path(ctx, "note.md")

    assert outcome.status is IngestStatus.removed
    assert outcome.reason is SkipReason.gone
    assert store.get_document(PROJECT, "note.md") is None
    assert vector_store.count(PROJECT) == 0


def test_remove_path_is_idempotent(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)

    # never indexed → skipped, not removed
    assert remove_path(ctx, "ghost.md").status is IngestStatus.skipped

    write(vault, "note.md", NOTE)
    ingest_path(ctx, "note.md")
    assert remove_path(ctx, "note.md").status is IngestStatus.removed
    assert store.get_document(PROJECT, "note.md") is None
    # second remove is a no-op
    assert remove_path(ctx, "note.md").status is IngestStatus.skipped


# ---------------------------------------------------------------------------
# Gates: a single-file event must never index what the boot scan withholds
# ---------------------------------------------------------------------------
def test_planted_secret_content_is_never_indexed(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """The write-side twin of P0's scan guardrail: a note pasting a live key is refused."""
    write(vault, "leak.md", f"# Leak\n\nhere is a key: {PLANTED_SECRET}\n")
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)

    outcome = ingest_path(ctx, "leak.md")

    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.secret
    assert store.get_document(PROJECT, "leak.md") is None
    assert vector_store.count(PROJECT) == 0
    assert embedder.calls == 0  # never even reached the embedder


def test_note_that_becomes_a_secret_is_evicted(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    write(vault, "note.md", NOTE)
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)
    ingest_path(ctx, "note.md")
    assert vector_store.count(PROJECT) == 1

    write(vault, "note.md", f"# Note\n\noops pasted {PLANTED_SECRET}\n")
    outcome = ingest_path(ctx, "note.md")
    assert outcome.status is IngestStatus.removed
    assert outcome.reason is SkipReason.secret
    assert vector_store.count(PROJECT) == 0  # the previously-indexed content is gone


def test_secret_filename_is_excluded(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    write(vault, "my-credentials.md", NOTE)
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)

    outcome = ingest_path(ctx, "my-credentials.md")
    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.excluded
    assert store.get_document(PROJECT, "my-credentials.md") is None


def test_ragignore_excludes_a_path(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    (vault / ".ragignore").write_text("private/\n", encoding="utf-8")
    write(vault, "private/note.md", NOTE)
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)

    outcome = ingest_path(ctx, "private/note.md")  # ignore=None → .ragignore loaded fresh
    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.excluded
    assert store.get_document(PROJECT, "private/note.md") is None


def test_symlink_out_of_the_vault_is_refused_and_evicted(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, tmp_path: Path
) -> None:
    """The single-file route enforces the walk's containment gate, so a watcher event or a
    write-back reindex can't index a symlink target ``reindex`` now refuses."""
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "private-diary.md").write_text(NOTE, encoding="utf-8")
    (vault / "notes").mkdir(parents=True, exist_ok=True)
    (vault / "notes" / "leak.md").symlink_to(outside / "private-diary.md")
    ctx = make_ctx(vault, store, vector_store, FakeEmbedder())

    outcome = ingest_path(ctx, "notes/leak.md")
    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.escapes_vault
    assert store.get_document(PROJECT, "notes/leak.md") is None
    assert vector_store.count(PROJECT) == 0


def test_unsupported_suffix_is_excluded(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    write(vault, "notes.txt", "plain text, not markdown\n")
    embedder = FakeEmbedder()
    ctx = make_ctx(vault, store, vector_store, embedder)

    outcome = ingest_path(ctx, "notes.txt")
    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.excluded
