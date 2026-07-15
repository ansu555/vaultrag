"""Reindex orchestration: scan -> parse -> chunk -> embed -> upsert (P0 Step 8).

:func:`reindex_vault` is what ``vaultrag reindex`` calls. It wires together every stage
built so far -- Step 3's :func:`~vaultrag.indexer.scan.scan_vault`, Step 4's
:func:`~vaultrag.parsing.markdown.parse_document`, Step 5's
:func:`~vaultrag.chunking.chunker.chunk_document`, Step 6's
:func:`~vaultrag.embedding.cache.embed_chunks`, and Step 7's
:class:`~vaultrag.index.base.VectorStore` -- into the "manual"/"boot reconcile" trigger
from ``../Rag/projects/vaultrag/ingestion-pipeline.md`` (arch §4). Retry/backoff and
worker-pool parallelism belong to the *live-watch* trigger (watchdog, P1), which doesn't
exist yet; this entrypoint runs the diff serially and in-process, parking a failed file's
job as ``dead`` so one bad note can't abort the rest of the vault.

**Two traps this stage must not fall into** (left by Step 7's point-id shape):

1. **A shrunk document must not strand its tail.** Chunk/point ids are
   ``uuid5(project, path, ix)`` -- upserting only overwrites the ids it writes, so if a
   note goes from 5 chunks to 3, ids 3-4 survive untouched in both SQLite and Qdrant
   unless something explicitly deletes them first. Every (re)ingest therefore deletes a
   document's existing chunks/points *before* writing the new ones.
2. **A rename must move points, not just metadata**, because the point id embeds the
   path -- a hash match at a new path cannot be a metadata-only update here. It is
   handled as delete-at-old-path + ingest-at-new-path; the embed cache (keyed by content
   hash, not path) is what makes that still re-embed nothing.

**Partial-failure safety:** a document's ``hash`` column is only written *last*, after its
chunks and vectors are fully upserted. Every step before that reserves the row with
``hash=None`` -- a "half-recorded doc", exactly the case
:func:`~vaultrag.indexer.scan.diff_manifest` already treats as an update rather than a
create/unchanged. So a crash mid-ingest leaves the file eligible for reprocessing on the
next `reindex`, instead of looking done while it isn't (CLAUDE.md §5/§6: never fake
success, re-running must not corrupt).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

import structlog

from vaultrag.chunking.chunker import chunk_document
from vaultrag.embedding.base import Embedder
from vaultrag.embedding.cache import embed_chunks
from vaultrag.index.base import VectorStore
from vaultrag.indexer.scan import Rename, ScannedFile, scan_vault
from vaultrag.models import document_id
from vaultrag.parsing.markdown import parse_document
from vaultrag.store.base import JobKind, JobState, MetaStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReindexStats:
    """Counters for one ``reindex_vault`` run (log/CLI reporting, never file content)."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    renamed: int = 0
    unchanged: int = 0
    failed: int = 0
    chunks_written: int = 0
    elapsed_ms: float = 0.0


def reindex_vault(
    *,
    project_id: str,
    vault_path: Path,
    store: MetaStore,
    vector_store: VectorStore,
    embedder: Embedder,
) -> ReindexStats:
    """Boot-reconcile the index against a vault. Safe to re-run at any time.

    Registers the project, ensures the Qdrant collection exists, scans the vault against
    the stored manifest, then processes creates/updates/renames/deletes. Nothing changed
    means zero embedder/Qdrant calls (Step 3's diff is empty; Step 6's cache is untouched).
    """
    t0 = time.perf_counter()
    vault_path = vault_path.resolve()
    store.upsert_project(project_id, name=project_id, vault_path=str(vault_path))
    vector_store.ensure_collection()

    diff, _scan_stats = scan_vault(vault_path, store.document_manifest(project_id))

    created = updated = renamed = deleted = failed = 0
    chunks_written = 0

    for f in diff.creates:
        n = _process_scanned(
            f,
            project_id=project_id,
            vault_path=vault_path,
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
        if n is None:
            failed += 1
        else:
            created += 1
            chunks_written += n

    for f in diff.updates:
        n = _process_scanned(
            f,
            project_id=project_id,
            vault_path=vault_path,
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
        if n is None:
            failed += 1
        else:
            updated += 1
            chunks_written += n

    for r in diff.renames:
        n = _process_rename(
            r,
            project_id=project_id,
            vault_path=vault_path,
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
        if n is None:
            failed += 1
        else:
            renamed += 1
            chunks_written += n

    for path in diff.deletes:
        if _process_delete(path, project_id=project_id, store=store, vector_store=vector_store):
            deleted += 1
        else:
            failed += 1

    stats = ReindexStats(
        created=created,
        updated=updated,
        deleted=deleted,
        renamed=renamed,
        unchanged=len(diff.unchanged),
        failed=failed,
        chunks_written=chunks_written,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
    )
    log.info("reindex.complete", project=project_id, vault=str(vault_path), **asdict(stats))
    return stats


# ---------------------------------------------------------------------------
# Per-file ingest / delete (the actual pipeline)
# ---------------------------------------------------------------------------
def _ingest_path(
    *,
    project_id: str,
    vault_path: Path,
    path: str,
    content_hash: str,
    store: MetaStore,
    vector_store: VectorStore,
    embedder: Embedder,
) -> int:
    """Parse -> chunk -> embed -> upsert one file. Returns the number of chunks written.

    Deletes the document's existing chunks/points before writing the fresh ones (trap
    #1) and only commits the document's real ``hash`` after every write succeeds (the
    partial-failure safety described in the module docstring).
    """
    abs_path = vault_path / path
    raw_text = abs_path.read_text(encoding="utf-8", errors="replace")
    mtime = int(abs_path.stat().st_mtime)

    document = parse_document(path=path, raw_text=raw_text, content_hash=content_hash, mtime=mtime)
    chunks = chunk_document(document, project_id=project_id)
    vectors = embed_chunks(embedder, store, chunks)

    doc_id = document_id(project_id, path)
    store.delete_chunks_for_doc(doc_id)
    vector_store.delete_by_doc(project_id, doc_id)

    # Reserve the document row (hash=None) so the chunk insert below satisfies the FK
    # even on a brand-new document, without yet claiming this file is fully indexed.
    store.upsert_document(project_id, document.metadata.model_copy(update={"hash": None}))

    vector_store.upsert_chunks(chunks, vectors, embed_model=embedder.model, document=document)
    store.upsert_chunks(chunks)

    # Commit last: only now does the manifest agree with disk, so a crash anywhere above
    # leaves this path looking unindexed (hash=None) rather than falsely "done".
    store.upsert_document(project_id, document.metadata)
    return len(chunks)


def _delete_path(
    *, project_id: str, path: str, store: MetaStore, vector_store: VectorStore
) -> None:
    """Remove one document's points + row. Deleting an already-gone document is a no-op."""
    doc_id = document_id(project_id, path)
    vector_store.delete_by_doc(project_id, doc_id)
    store.delete_document(project_id, path)


# ---------------------------------------------------------------------------
# Job-tracked wrappers: one row in `jobs` per changed path, dead-parked on failure so a
# single bad file can't abort the rest of the run.
# ---------------------------------------------------------------------------
def _process_scanned(
    f: ScannedFile,
    *,
    project_id: str,
    vault_path: Path,
    store: MetaStore,
    vector_store: VectorStore,
    embedder: Embedder,
) -> int | None:
    """Run one create/update ingest job. Returns chunks written, or ``None`` on failure."""
    job_id = store.enqueue_job(project_id, JobKind.ingest, f.path, f.hash)
    store.set_job_state(job_id, JobState.running)
    try:
        n = _ingest_path(
            project_id=project_id,
            vault_path=vault_path,
            path=f.path,
            content_hash=f.hash,
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
    except Exception as exc:
        store.set_job_state(job_id, JobState.dead, error=str(exc), increment_attempts=True)
        log.error("reindex.ingest_failed", path=f.path, error=str(exc))
        return None
    store.set_job_state(job_id, JobState.done)
    return n


def _process_rename(
    r: Rename,
    *,
    project_id: str,
    vault_path: Path,
    store: MetaStore,
    vector_store: VectorStore,
    embedder: Embedder,
) -> int | None:
    """Move a renamed file: delete at ``old_path``, ingest at ``new_path`` (trap #2)."""
    job_id = store.enqueue_job(project_id, JobKind.ingest, r.new_path, r.hash)
    store.set_job_state(job_id, JobState.running)
    try:
        _delete_path(project_id=project_id, path=r.old_path, store=store, vector_store=vector_store)
        n = _ingest_path(
            project_id=project_id,
            vault_path=vault_path,
            path=r.new_path,
            content_hash=r.hash,
            store=store,
            vector_store=vector_store,
            embedder=embedder,
        )
    except Exception as exc:
        store.set_job_state(job_id, JobState.dead, error=str(exc), increment_attempts=True)
        log.error("reindex.rename_failed", old_path=r.old_path, new_path=r.new_path, error=str(exc))
        return None
    store.set_job_state(job_id, JobState.done)
    return n


def _process_delete(
    path: str, *, project_id: str, store: MetaStore, vector_store: VectorStore
) -> bool:
    """Run one delete job. Returns whether it succeeded."""
    job_id = store.enqueue_job(project_id, JobKind.delete, path, None)
    store.set_job_state(job_id, JobState.running)
    try:
        _delete_path(project_id=project_id, path=path, store=store, vector_store=vector_store)
    except Exception as exc:
        store.set_job_state(job_id, JobState.dead, error=str(exc), increment_attempts=True)
        log.error("reindex.delete_failed", path=path, error=str(exc))
        return False
    store.set_job_state(job_id, JobState.done)
    return True
