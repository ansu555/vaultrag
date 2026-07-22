"""Drift detection + ``reindex --force``: the repair path CLAUDE.md §6 promises.

The bug these pin down (found live on ``vaultrag-design``, 2026-07-21): reindex is
idempotent against the SQLite **manifest** and never consults the vector store, so when
Qdrant loses points SQLite still records, reindex calls those documents ``unchanged`` and
skips exactly what needs rebuilding. Retrieval keeps answering plausibly from a fraction
of the vault with no signal anywhere. See
``../Rag/agent-inbox/reindex-cannot-repair-a-drifted-vector-store.md``.

Same doubles as ``test_indexer_pipeline`` — ``QdrantClient(":memory:")`` and a fake
embedder, real SQLite and real files, so the drift and the repair are exercised through
the actual upsert wiring rather than mocked away.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from vaultrag.consistency import Drift, check_drift
from vaultrag.embedding.base import Vector
from vaultrag.index import QdrantVectorStore, SparseVector, VectorStore
from vaultrag.indexer.pipeline import ReindexStats, reindex_vault
from vaultrag.indexer.scan import ScannedFile, diff_manifest
from vaultrag.models import document_id
from vaultrag.store.sqlite import SQLiteMetaStore

PROJECT = "test-project"
DIMS = 4


class FakeSparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[1], values=[1.0])


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-embed-model"

    @property
    def dims(self) -> int:
        return DIMS

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        self.calls += 1
        return [[float(len(t) + i) for i in range(DIMS)] for t in texts]

    def close(self) -> None:
        pass


def _paragraph(words: int, seed: str) -> str:
    return " ".join(f"{seed}{i}" for i in range(words))


ONE_SECTION_NOTE = "# Note One\n\n## Section One\n\n" + _paragraph(150, "aaa") + "\n"
TWO_SECTION_NOTE = ONE_SECTION_NOTE + "\n## Section Two\n\n" + _paragraph(150, "bbb") + "\n"


def write_vault(tmp_path: Path, files: Mapping[str, str]) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    for rel_path, text in files.items():
        target = vault / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return vault


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
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
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


def run(
    vault: Path,
    store: SQLiteMetaStore,
    vector_store: VectorStore,
    embedder: FakeEmbedder,
    *,
    force: bool = False,
) -> ReindexStats:
    return reindex_vault(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=embedder,
        force=force,
    )


# ---------------------------------------------------------------------------
# chunk_count — the manifest half of the check
# ---------------------------------------------------------------------------
def test_chunk_count_matches_what_reindex_wrote(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    stats = run(vault, store, vector_store, embedder)

    assert store.chunk_count(PROJECT) == stats.chunks_written == 3


def test_chunk_count_is_project_scoped(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """A count that leaked across tenants would make every drift check meaningless."""
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE})
    run(vault, store, vector_store, embedder)

    assert store.chunk_count(PROJECT) == 1
    assert store.chunk_count("some-other-project") == 0


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------
def test_no_drift_on_a_healthy_index(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)

    assert check_drift(PROJECT, store=store, vector_store=vector_store) is None


def test_drift_detected_when_the_vector_store_loses_points(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)

    # Qdrant loses one document's points while SQLite keeps believing they are indexed —
    # what a wiped volume or a recreated collection looks like from the manifest's side.
    vector_store.delete_by_doc(PROJECT, document_id(PROJECT, "two.md"))

    drift = check_drift(PROJECT, store=store, vector_store=vector_store)
    assert drift == Drift(project_id=PROJECT, manifest_chunks=3, vector_points=1)
    assert drift.missing == 2
    assert "3 chunks" in drift.describe() and "1" in drift.describe()


def test_drift_reports_orphaned_points_too(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """The rarer direction — more points than the manifest accounts for — still counts."""
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    store.delete_document(PROJECT, "one.md")

    drift = check_drift(PROJECT, store=store, vector_store=vector_store)
    assert drift is not None
    assert drift.missing == -1


def test_empty_project_is_not_drift(
    store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    assert check_drift("never-indexed", store=store, vector_store=vector_store) is None


# ---------------------------------------------------------------------------
# force in the scan classifier
# ---------------------------------------------------------------------------
def test_force_reclassifies_unchanged_as_updates() -> None:
    scanned = [ScannedFile(path="a.md", hash="h1", size=1, mtime=0)]
    manifest = {"a.md": "h1"}

    assert diff_manifest(scanned, manifest).unchanged == ["a.md"]
    assert diff_manifest(scanned, manifest).updates == []

    forced = diff_manifest(scanned, manifest, force=True)
    assert forced.unchanged == []
    assert [f.path for f in forced.updates] == ["a.md"]


def test_force_still_derives_deletes_and_renames() -> None:
    """Force must stay a *reconcile* — a blind re-upsert would strand deleted docs."""
    scanned = [
        ScannedFile(path="kept.md", hash="h1", size=1, mtime=0),
        ScannedFile(path="new-name.md", hash="h3", size=1, mtime=0),
    ]
    manifest = {"kept.md": "h1", "gone.md": "h2", "old-name.md": "h3"}

    forced = diff_manifest(scanned, manifest, force=True)
    assert forced.deletes == ["gone.md"]
    assert [(r.old_path, r.new_path) for r in forced.renames] == [("old-name.md", "new-name.md")]
    assert [f.path for f in forced.updates] == ["kept.md"]


# ---------------------------------------------------------------------------
# The end-to-end bug: plain reindex cannot repair, --force can
# ---------------------------------------------------------------------------
def test_plain_reindex_cannot_repair_a_drifted_vector_store(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """The bug, pinned. Reindex reports success and converges on nothing."""
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    vector_store.delete_by_doc(PROJECT, document_id(PROJECT, "two.md"))

    stats = run(vault, store, vector_store, embedder)

    assert stats.unchanged == 2  # both notes declared fine
    assert stats.failed == 0  # and nothing reported wrong
    assert vector_store.count(PROJECT) == 1  # while a third of the index is still missing
    assert check_drift(PROJECT, store=store, vector_store=vector_store) is not None


def test_force_repairs_a_drifted_vector_store(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    vector_store.delete_by_doc(PROJECT, document_id(PROJECT, "two.md"))

    stats = run(vault, store, vector_store, embedder, force=True)

    assert stats.updated == 2
    assert stats.unchanged == 0
    assert stats.failed == 0
    assert vector_store.count(PROJECT) == 3
    assert check_drift(PROJECT, store=store, vector_store=vector_store) is None


def test_force_survives_total_vector_store_loss(
    tmp_path: Path, store: SQLiteMetaStore, embedder: FakeEmbedder
) -> None:
    """The realistic trigger: the Qdrant volume is gone but state/vaultrag.db persists."""
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    first = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=FakeSparse()
    )
    first.ensure_collection()
    run(vault, store, first, embedder)
    assert store.chunk_count(PROJECT) == 3

    # A brand-new empty Qdrant, same state DB.
    replacement = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=FakeSparse()
    )
    replacement.ensure_collection()
    assert check_drift(PROJECT, store=store, vector_store=replacement) is not None

    run(vault, store, replacement, embedder, force=True)

    assert replacement.count(PROJECT) == 3
    assert check_drift(PROJECT, store=store, vector_store=replacement) is None


def test_force_does_not_re_embed_unchanged_text(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """Force re-upserts, but the ``(model, dims, chunk_hash)`` cache still absorbs it.

    This is what makes the repair affordable on a big vault: the expensive stage is
    skipped even though every chunk is rewritten.
    """
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    calls_after_first = embedder.calls

    run(vault, store, vector_store, embedder, force=True)

    assert embedder.calls == calls_after_first
    assert vector_store.count(PROJECT) == 3


def test_force_is_idempotent(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)

    run(vault, store, vector_store, embedder, force=True)
    run(vault, store, vector_store, embedder, force=True)

    assert vector_store.count(PROJECT) == 3
    assert store.chunk_count(PROJECT) == 3


# ---------------------------------------------------------------------------
# The startup gate (P3 Stage 4) — the P2 carry-over closed
# ---------------------------------------------------------------------------
def _stack(
    tmp_path: Path,
    store: SQLiteMetaStore | None,
    vector_store: object,
    *,
    project: str = PROJECT,
) -> object:
    """A ServedStack with just the fields the drift check reads."""
    from vaultrag.composition import ServedStack
    from vaultrag.config import Settings

    settings = Settings(project_id=project, vault_path=tmp_path, state_dir=tmp_path / "state")
    return ServedStack(
        service=None,  # type: ignore[arg-type]  # the check never touches it
        settings=[settings],
        vector_store=vector_store,  # type: ignore[arg-type]
        store=store,
    )


def test_a_drifted_index_refuses_to_start(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """The whole point of the gate: a long-running role must not serve stale results.

    The drift is created the way the live incident created it — index, then wipe the
    vector store while SQLite keeps its manifest.
    """
    from vaultrag.composition import check_startup_drift
    from vaultrag.consistency import DriftError

    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE, "two.md": TWO_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    vector_store.delete_by_project(PROJECT)

    with pytest.raises(DriftError) as excinfo:
        check_startup_drift(_stack(tmp_path, store, vector_store))  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "reindex --force" in message  # names the repair, not just the problem
    assert "--allow-drift" in message  # and the escape hatch


def test_allow_drift_still_measures_it(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """Refusing to *fail* on drift is an operator's call; refusing to *measure* it is not."""
    from vaultrag.composition import check_startup_drift
    from vaultrag.metrics import registry

    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE})
    run(vault, store, vector_store, embedder)
    vector_store.delete_by_project(PROJECT)

    drifted = check_startup_drift(_stack(tmp_path, store, vector_store), enabled=False)  # type: ignore[arg-type]
    assert [d.project_id for d in drifted] == [PROJECT]
    gauge = registry().get_sample_value("vaultrag_index_drift_chunks", {"project": PROJECT})
    assert gauge is not None and gauge > 0


def test_a_healthy_index_starts_and_publishes_zero(
    tmp_path: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, embedder: FakeEmbedder
) -> None:
    """Zero is a real measurement, and must be published — an absent series and a drift of
    zero mean opposite things to whoever is reading the dashboard."""
    from vaultrag.composition import check_startup_drift
    from vaultrag.metrics import registry

    vault = write_vault(tmp_path, {"one.md": ONE_SECTION_NOTE})
    run(vault, store, vector_store, embedder)

    assert check_startup_drift(_stack(tmp_path, store, vector_store)) == []  # type: ignore[arg-type]
    assert registry().get_sample_value("vaultrag_index_drift_chunks", {"project": PROJECT}) == 0.0


def test_an_unreachable_vector_store_is_not_drift_and_does_not_block_startup(
    tmp_path: Path, store: SQLiteMetaStore
) -> None:
    """A down Qdrant must not turn into a refusal to launch.

    Before this gate existed, a read-only ``vaultrag mcp`` came up instantly whether or not
    Qdrant was running, and a down backend surfaced as an actionable per-call error. The
    gate guards against the *opposite* problem — drift is dangerous because it is silent —
    so trading a loud, recoverable failure for a dead agent session would be a bad swap.
    """
    from vaultrag.composition import check_startup_drift
    from vaultrag.index.base import VectorStoreError

    class _Down:
        def count(self, project_id: str | None = None) -> int:
            raise VectorStoreError("count on 'chunks' failed: [Errno 61] Connection refused")

    assert check_startup_drift(_stack(tmp_path, store, _Down())) == []  # type: ignore[arg-type]


def test_no_metadata_store_means_nothing_to_check(tmp_path: Path) -> None:
    """A read-only role over an index this process cannot see has no manifest to compare."""
    from vaultrag.composition import check_startup_drift

    class _Unused:
        def count(self, project_id: str | None = None) -> int:
            raise AssertionError("must not be consulted without a manifest")

    assert check_startup_drift(_stack(tmp_path, None, _Unused())) == []  # type: ignore[arg-type]
