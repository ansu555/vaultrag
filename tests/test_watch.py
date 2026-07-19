"""P1 Stage 4 verify: the live-watch loop (debounce, worker retry/dead, real observer).

Three layers, fastest first:

* **debounce/dispatch** logic driven with an injected clock — no threads, no filesystem;
* the **worker** (:meth:`VaultWatcher.process_path`) against a real store + in-memory
  Qdrant + fake embedder, including the retry→``dead`` path (injected no-op sleep);
* one **end-to-end** test with the real ``watchdog`` observer + real file writes, polling
  the store so it tolerates inotify delivery latency without flaking.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from vaultrag.embedding.base import EmbeddingError, Vector
from vaultrag.index import QdrantVectorStore, SparseVector
from vaultrag.indexer.pipeline import IngestContext, IngestStatus
from vaultrag.indexer.watch import VaultWatcher
from vaultrag.store.base import JobKind, JobState
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


class Clock:
    """A hand-cranked monotonic clock so debounce windows are deterministic."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _paragraph(words: int, seed: str) -> str:
    return " ".join(f"{seed}{i}" for i in range(words))


NOTE = "# Note\n\n## Section\n\n" + _paragraph(150, "aaa") + "\n"


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


# ---------------------------------------------------------------------------
# Debounce / dispatch (no threads, injected clock)
# ---------------------------------------------------------------------------
def test_debounce_holds_a_path_until_it_goes_quiet(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    clock = Clock()
    watcher = VaultWatcher(
        make_ctx(vault, store, vector_store, FakeEmbedder()),
        debounce_seconds=1.5,
        clock=clock,
    )

    watcher._mark_dirty("note.md")  # deadline = 0 + 1.5
    clock.t = 1.0
    assert watcher._take_due() == []  # still within the quiet window

    clock.t = 2.0
    assert watcher._take_due() == ["note.md"]  # now due


def test_a_save_storm_collapses_to_one_dispatch(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    clock = Clock()
    watcher = VaultWatcher(
        make_ctx(vault, store, vector_store, FakeEmbedder()),
        debounce_seconds=1.5,
        clock=clock,
    )

    # Obsidian's temp-file save storm: many events, each re-arming the window.
    for _ in range(5):
        watcher._mark_dirty("note.md")
        clock.t += 0.2  # each event within 1.5 s of the last

    # At t=1.0 the last event's deadline (0.8 + 1.5) hasn't passed → nothing due.
    clock.t = 1.0
    assert watcher._take_due() == []
    # Once quiet, exactly one dispatch for the whole storm.
    clock.t = 5.0
    assert watcher._take_due() == ["note.md"]
    assert watcher._take_due() == []  # and it's now in-flight, not re-dispatched


# ---------------------------------------------------------------------------
# Worker: job tracking + retry → dead
# ---------------------------------------------------------------------------
def test_process_path_indexes_and_marks_job_done(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    (vault / "note.md").write_text(NOTE, encoding="utf-8")
    watcher = VaultWatcher(make_ctx(vault, store, vector_store, FakeEmbedder()))

    outcome = watcher.process_path("note.md")

    assert outcome is not None and outcome.status is IngestStatus.indexed
    assert vector_store.count(PROJECT) == 1
    job_id = store.enqueue_job(PROJECT, JobKind.ingest, "note.md", None)  # same deterministic id
    job = store.get_job(job_id)
    assert job is not None and job.state == JobState.done


def test_process_path_retries_then_parks_dead(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    (vault / "note.md").write_text(NOTE, encoding="utf-8")
    sleeps: list[float] = []
    watcher = VaultWatcher(
        make_ctx(vault, store, vector_store, FakeEmbedder(fail_marker="aaa")),
        max_attempts=3,
        retry_backoff=0.5,
        sleep=sleeps.append,
    )

    outcome = watcher.process_path("note.md")

    assert outcome is None  # dead-parked, not raised — the dispatcher survives
    assert sleeps == [0.5, 1.0]  # backoff * 2**attempt for attempts 0 and 1, none after last
    job_id = store.enqueue_job(PROJECT, JobKind.ingest, "note.md", None)
    job = store.get_job(job_id)
    assert job is not None
    assert job.state == JobState.dead
    assert job.attempts == 3
    assert "simulated embedder failure" in (job.error or "")


def test_flush_processes_pending_synchronously(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    (vault / "note.md").write_text(NOTE, encoding="utf-8")
    watcher = VaultWatcher(make_ctx(vault, store, vector_store, FakeEmbedder()))

    watcher._mark_dirty("note.md")
    watcher.flush()

    assert store.get_document(PROJECT, "note.md") is not None
    assert watcher._take_due() == []  # nothing left pending


# ---------------------------------------------------------------------------
# End-to-end: the real watchdog observer + real file writes
# ---------------------------------------------------------------------------
def _poll(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:  # type: ignore[no-untyped-def]
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_live_watch_indexes_then_evicts_a_file(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    # Small debounce + a single worker: snappy and keeps the in-memory Qdrant single-threaded.
    watcher = VaultWatcher(
        make_ctx(vault, store, vector_store, FakeEmbedder()),
        debounce_seconds=0.05,
        workers=1,
        poll_interval=0.02,
    )
    with watcher:
        (vault / "note.md").write_text(NOTE, encoding="utf-8")
        assert _poll(lambda: store.get_document(PROJECT, "note.md") is not None), (
            "edited note was not indexed by the live watcher within the timeout"
        )
        assert vector_store.count(PROJECT) == 1

        (vault / "note.md").unlink()
        assert _poll(lambda: store.get_document(PROJECT, "note.md") is None), (
            "deleted note was not evicted by the live watcher within the timeout"
        )
