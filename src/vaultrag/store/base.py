"""``MetaStore`` interface + the row shapes it returns.

The metadata store holds structure and ops state (documents, chunks, tags, jobs,
the embed cache) — everything *except* the vectors, which live in Qdrant. It is a
derived cache: ``vaultrag reindex`` from a bare vault rebuilds it (repo CLAUDE.md §6).

``MetaStore`` is a :class:`typing.Protocol` so vendors stay swappable (CLAUDE.md §5):
the SQLite impl is one implementation; Postgres 16 is a future one behind the same
surface. The DDL is designed to move to Postgres unchanged — see
``../Rag/decisions/metadata-store-sqlite-to-postgres.md`` and
``../Rag/reference/reference-database-schema.md``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from vaultrag.models import Chunk, DocumentMetadata


class JobState(StrEnum):
    """Lifecycle of a per-file indexing job (``jobs.state``)."""

    pending = "pending"
    running = "running"
    done = "done"
    dead = "dead"  # exhausted retries — parked with the error (arch §4)


class JobKind(StrEnum):
    """What a job asks the indexer to do (``jobs.kind``)."""

    # ``ingest`` not ``index``: an ``index`` member would shadow ``str.index`` (StrEnum
    # is a ``str`` subclass). Value is free-form — nothing consumes it until Step 8.
    ingest = "ingest"  # (re)parse → chunk → embed → upsert
    delete = "delete"  # remove a document + its points


# ---------------------------------------------------------------------------
# Row shapes returned by queries (read side). Frozen: rows are snapshots.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProjectRow:
    id: str
    name: str
    vault_path: str
    settings: dict[str, Any]
    status: str
    created_ts: int


@dataclass(frozen=True, slots=True)
class DocumentRow:
    id: str
    project_id: str
    path: str
    hash: str | None
    title: str
    type: str
    mtime: int | None
    status: str


@dataclass(frozen=True, slots=True)
class ChunkRow:
    id: str
    doc_id: str
    ix: int
    heading_path: list[str] = field(default_factory=list)
    hash: str = ""
    tokens: int = 0
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobRow:
    id: str
    project_id: str
    kind: str
    path: str | None
    hash: str | None
    state: str
    attempts: int
    error: str | None
    ts: int


@runtime_checkable
class MetaStore(Protocol):
    """Structure + ops-state store. Every mutation is deterministic and idempotent.

    Re-running any write with the same inputs must not duplicate or corrupt rows
    (CLAUDE.md §5 idempotency). Document/chunk identity is content-addressed via
    ``vaultrag.models.document_id`` / ``chunk_point_id``.
    """

    # --- schema / lifecycle ---------------------------------------------------
    def init_schema(self) -> None:
        """Create the P0 tables if absent. Safe to call repeatedly."""

    def close(self) -> None:
        """Release the underlying connection."""

    # --- projects -------------------------------------------------------------
    def upsert_project(
        self,
        project_id: str,
        name: str,
        vault_path: str,
        settings: Mapping[str, Any] | None = None,
        status: str = "active",
    ) -> None: ...

    def get_project(self, project_id: str) -> ProjectRow | None: ...

    # --- documents (the scan manifest) ---------------------------------------
    def upsert_document(
        self,
        project_id: str,
        meta: DocumentMetadata,
        status: str = "indexed",
    ) -> str:
        """Upsert one document + replace its tag set. Returns the ``doc_id``."""

    def get_document(self, project_id: str, path: str) -> DocumentRow | None: ...

    def document_manifest(self, project_id: str) -> dict[str, str | None]:
        """``path -> hash`` for every document — the diff source for scan (Step 3)."""

    def delete_document(self, project_id: str, path: str) -> None:
        """Delete a document and cascade its chunks + tags."""

    # --- chunks ---------------------------------------------------------------
    def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        """Idempotent upsert of chunk rows (keyed by :attr:`Chunk.point_id`)."""

    def chunk_hashes(self, doc_id: str) -> dict[str, str]:
        """``chunk_id -> chunk_hash`` — lets reindex re-embed only changed chunks."""

    def get_chunk(self, chunk_id: str) -> ChunkRow | None: ...

    def delete_chunks_for_doc(self, doc_id: str) -> None: ...

    # --- tags -----------------------------------------------------------------
    def set_tags(self, project_id: str, doc_id: str, tags: Iterable[str]) -> None:
        """Replace the tag set for a document (idempotent)."""

    def tags_for_doc(self, doc_id: str) -> list[str]: ...

    # --- jobs -----------------------------------------------------------------
    def enqueue_job(
        self,
        project_id: str,
        kind: str,
        path: str | None = None,
        hash: str | None = None,
    ) -> str:
        """Enqueue a job, idempotent by ``(project, path, hash)``. Returns its id."""

    def set_job_state(
        self,
        job_id: str,
        state: str,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> None: ...

    def get_job(self, job_id: str) -> JobRow | None: ...

    # --- embed cache (keyed (model, dims, chunk_hash)) ------------------------
    def get_cached_vectors(
        self,
        model: str,
        dims: int,
        chunk_hashes: Iterable[str],
    ) -> dict[str, bytes]:
        """Return ``chunk_hash -> vector bytes`` for the hashes already cached."""

    def put_cached_vector(self, model: str, dims: int, chunk_hash: str, vector: bytes) -> None: ...
