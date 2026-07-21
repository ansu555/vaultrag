"""``MetaStore`` interface + the row shapes it returns.

The metadata store holds structure, graph, and ops state (documents, chunks, tags,
links, jobs, the embed cache) — everything *except* the vectors, which live in
Qdrant. It is a
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

from vaultrag.models import Chunk, DocumentMetadata, Link


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


@dataclass(frozen=True, slots=True)
class LinkRow:
    """One ``links`` row — an edge exactly as stored, resolved or not.

    ``dst_doc is None`` means the target does not (yet) name an indexed document:
    a link to an unwritten note, a typo, or an ambiguous title. ``dst_raw`` is what
    the human wrote; ``dst_key`` is its normalized form
    (:func:`vaultrag.store.links.normalize_key`), which is what re-resolution joins on
    when a document later appears or disappears.
    """

    id: str
    project_id: str
    src_doc: str
    dst_doc: str | None
    dst_raw: str
    dst_key: str
    kind: str
    anchor: str | None = None

    @property
    def resolved(self) -> bool:
        return self.dst_doc is not None


@dataclass(frozen=True, slots=True)
class LinkedDoc:
    """A document at the far end of one resolved edge (backlinks / outgoing)."""

    doc_id: str
    path: str
    title: str
    kind: str  # the edge kind that connected it (wikilink | embed | …)
    anchor: str | None = None


@dataclass(frozen=True, slots=True)
class NeighborDoc:
    """A document reachable within N hops of another, direction-agnostic.

    ``hops`` is the *shortest* distance found — a doc both linked and back-linked
    appears once, at 1.
    """

    doc_id: str
    path: str
    title: str
    hops: int


@dataclass(frozen=True, slots=True)
class SiblingDoc:
    """A document sharing tags with another, ranked by how many it shares."""

    doc_id: str
    path: str
    title: str
    shared_tags: int


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

    # --- links / graph (P2 Stage 2) -------------------------------------------
    # The wikilink graph. Write side is edge *replacement* (a document's edges are
    # whatever its current text says); read side is backlinks / neighbors / siblings,
    # which P2 Stage 3 turns into the `related` tool and retrieval stage 6.
    def replace_links(self, project_id: str, src_doc: str, links: Iterable[Link]) -> int:
        """Replace a document's outgoing edges from its parsed links. Returns the count.

        Resolves each target against the ``doc_keys`` index; unresolvable targets are
        stored with ``dst_doc=NULL`` rather than dropped, so they can be re-resolved
        later and reported as vault hygiene in the meantime.
        """

    def reresolve_links(self, project_id: str, doc_id: str) -> int:
        """Re-resolve every edge whose target could mean ``doc_id``. Returns rows changed.

        Called after a document is (re)ingested: a newly-appeared note both *fixes*
        dangling links that were waiting for it and can *re-point* links that had
        resolved to a lower-precedence match. Bounded — only targets naming this
        document are touched, never a full-graph sweep.
        """

    def outgoing_links(self, project_id: str, doc_id: str) -> list[LinkRow]:
        """Every edge out of ``doc_id``, resolved or not, in stable order."""

    def linked_docs(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        """Documents ``doc_id`` links *to* (resolved edges only, self-links excluded)."""

    def backlinks(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        """Documents that link *to* ``doc_id`` — the reverse lookup, indexed."""

    def neighbors(
        self,
        project_id: str,
        doc_id: str,
        hops: int = 1,
        limit: int = 50,
    ) -> list[NeighborDoc]:
        """Documents within ``hops`` link-hops of ``doc_id``, either direction."""

    def tag_siblings(self, project_id: str, doc_id: str, limit: int = 20) -> list[SiblingDoc]:
        """Documents sharing tags with ``doc_id``, most-shared first."""

    def unresolved_links(self, project_id: str, limit: int = 100) -> list[LinkRow]:
        """Dangling edges — the "unresolved links" hygiene signal (arch §14)."""

    def doc_key_count(self, doc_id: str) -> int:
        """How many ``doc_keys`` rows a document has. Zero ⇒ its graph was never built."""

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
