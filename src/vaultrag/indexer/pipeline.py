"""Reindex orchestration + the single-file ingest primitive (P0 Step 8, P1 Stage 4).

Two things live here:

* :func:`reindex_vault` — the "manual"/"boot reconcile" trigger from
  ``../Rag/projects/vaultrag/ingestion-pipeline.md`` (arch §4). Called by
  ``vaultrag reindex``: scan the vault against the stored manifest, then process
  creates/updates/renames/deletes serially and in-process, parking a failed file's job
  as ``dead`` so one bad note can't abort the rest of the vault.

* :func:`ingest_path` / :func:`remove_path` — the reusable **single-file** primitive
  (P1 Stage 4). Reconcile *one* vault-relative path against disk. This is the unit both
  live-watch (:mod:`vaultrag.indexer.watch`) and write-back (P1 Stage 5) drive; the boot
  reconcile above shares its inner :func:`_ingest_file` / :func:`_delete_file` work.

Both wrap their per-file work in an :class:`IngestContext` — the dependency bundle
(project, vault, stores, embedder) that a single-file ingest needs.

**Two traps every (re)ingest must not fall into** (left by Step 7's point-id shape):

1. **A shrunk document must not strand its tail.** Chunk/point ids are
   ``uuid5(project, path, ix)`` — upserting only overwrites the ids it writes, so if a
   note goes from 5 chunks to 3, ids 3-4 survive untouched in both SQLite and Qdrant
   unless something explicitly deletes them first. Every (re)ingest therefore deletes a
   document's existing chunks/points *before* writing the new ones.
2. **A rename must move points, not just metadata**, because the point id embeds the
   path — a hash match at a new path cannot be a metadata-only update. It is handled as
   delete-at-old-path + ingest-at-new-path; the embed cache (keyed by content hash, not
   path) is what makes that still re-embed nothing.

**Single-file gating (P1 Stage 4).** :func:`ingest_path` re-applies the *same* gates the
boot scan applies in :func:`~vaultrag.indexer.scan.walk_vault` — the path-level
:func:`~vaultrag.indexer.scan.exclusion_reason` (secret filename / ``.ragignore`` /
unsupported suffix / pruned dir) **and** the content-level
:func:`~vaultrag.indexer.secret_guard.scan_for_secrets`. A live-watch or write-back event
therefore can never sneak a file into the index that a full ``reindex`` would withhold
(repo ``CLAUDE.md`` §6: secrets never enter the index). If a path that *was* indexed later
trips a gate (becomes a secret, gets ``.ragignore``d) or vanishes, :func:`ingest_path`
evicts it — reconcile means "make the index match disk for this one path", in both
directions.

**Partial-failure safety:** a document's ``hash`` column is written *last*, after its
chunks and vectors are fully upserted. Every step before that reserves the row with
``hash=None`` — a "half-recorded doc", exactly the case
:func:`~vaultrag.indexer.scan.diff_manifest` treats as an update rather than a
create/unchanged. So a crash mid-ingest leaves the file eligible for reprocessing on the
next reconcile, instead of looking done while it isn't (CLAUDE.md §5/§6).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path

import structlog
from blake3 import blake3
from pathspec import PathSpec
from pathspec.patterns.gitignore.spec import GitIgnoreSpecPattern

from vaultrag.chunking.chunker import chunk_document
from vaultrag.embedding.base import Embedder
from vaultrag.embedding.cache import embed_chunks
from vaultrag.index.base import VectorStore
from vaultrag.indexer.scan import (
    Rename,
    ScanDiff,
    ScannedFile,
    exclusion_reason,
    is_binary_source,
    is_image_source,
    load_ignore_spec,
    scan_vault,
)
from vaultrag.indexer.secret_guard import scan_for_secrets
from vaultrag.models import Document, document_id
from vaultrag.parsing import PARSER_VERSION
from vaultrag.parsing.excalidraw import is_excalidraw_source, parse_excalidraw
from vaultrag.parsing.image import ImageParser, NoTextFound
from vaultrag.parsing.markdown import parse_document
from vaultrag.parsing.pdf import PdfError, PdfParser
from vaultrag.store.base import JobKind, JobState, MetaStore

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# The dependency bundle a single-file ingest needs.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IngestContext:
    """Everything the per-file pipeline touches, bundled so callers pass one object.

    Shared by the boot reconcile (:func:`reindex_vault`), live-watch
    (:mod:`vaultrag.indexer.watch`), and — from P1 Stage 5 — write-back. Frozen: the
    context is fixed for the life of a project's indexer; the *work* mutates the stores.
    """

    project_id: str
    vault_path: Path
    store: MetaStore
    vector_store: VectorStore
    embedder: Embedder
    # None = no PDF support installed (the base install). PDFs are then skipped
    # cleanly rather than failing, so a vault of notes+PDFs still indexes its notes.
    pdf_parser: PdfParser | None = None
    # None = no OCR engine installed, or `ocr_enabled` is off. Images are then skipped
    # the same way (P2 Stage 6). Kept separate from `pdf_parser` because the two are
    # independently available: RapidOCR can be present without Docling and vice versa.
    image_parser: ImageParser | None = None


# ---------------------------------------------------------------------------
# Single-file primitive result
# ---------------------------------------------------------------------------
class IngestStatus(StrEnum):
    """Outcome of reconciling one path (:func:`ingest_path` / :func:`remove_path`)."""

    indexed = "indexed"  # (re)parsed → chunked → embedded → upserted
    unchanged = "unchanged"  # on-disk hash already matches the manifest; no work done
    removed = "removed"  # a previously-indexed doc was evicted (gone / now-gated)
    skipped = "skipped"  # gated or absent, and nothing was indexed to remove


class SkipReason(StrEnum):
    """Why :func:`ingest_path` did not index a path (attached to removed/skipped)."""

    excluded = "excluded"  # exclusion_reason fired (secret filename / ragignore / unsupported)
    secret = "secret"  # scan_for_secrets found key material in the content
    gone = "gone"  # the file is missing or unreadable on disk
    no_parser = "no_parser"  # format is indexable but its converter isn't installed
    no_text = "no_text"  # converted cleanly and holds nothing to index (an image with no writing)


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What :func:`ingest_path` / :func:`remove_path` did to one path."""

    path: str
    status: IngestStatus
    chunks: int = 0
    reason: SkipReason | None = None

    @property
    def indexed(self) -> bool:
        return self.status is IngestStatus.indexed


# ---------------------------------------------------------------------------
# Boot reconcile (the whole-vault trigger)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReindexStats:
    """Counters for one ``reindex_vault`` run (log/CLI reporting, never file content)."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    renamed: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped: int = 0  # deliberately not indexed (no converter installed / secret content)
    chunks_written: int = 0
    graph_backfilled: int = 0  # unchanged docs whose link edges were built after the fact
    elapsed_ms: float = 0.0
    # True when this index was built by an older parser and unchanged files were left
    # alone that today's parser would read more out of. Reported, not acted on — see
    # `vaultrag.parsing.PARSER_VERSION`; the repair is `reindex --force`.
    stale_parser: bool = False


def reindex_vault(
    *,
    project_id: str,
    vault_path: Path,
    store: MetaStore,
    vector_store: VectorStore,
    embedder: Embedder,
    force: bool = False,
    pdf_parser: PdfParser | None = None,
    image_parser: ImageParser | None = None,
) -> ReindexStats:
    """Boot-reconcile the index against a vault. Safe to re-run at any time.

    Registers the project, ensures the Qdrant collection exists, scans the vault against
    the stored manifest, then processes creates/updates/renames/deletes. Nothing changed
    means zero embedder/Qdrant calls (Step 3's diff is empty; Step 6's cache is untouched).

    ``force`` re-ingests every file regardless of its hash — the documented DR path
    ("the index is derived; the vault is truth", CLAUDE.md §6), which the default run
    cannot provide. Ordinary reindex is idempotent against the **manifest** and never
    consults the vector store, so when Qdrant loses data that SQLite still records
    (volume wiped, collection recreated, server-vs-``:memory:`` mix-up) it reports the
    missing documents as ``unchanged`` and skips exactly what needs rebuilding. Forcing
    is not free — every chunk is re-upserted — but it is cheaper than it looks, because
    the embed cache is keyed ``(model, dims, chunk_hash)`` and survives, so unchanged
    text is re-upserted without being re-embedded.
    """
    t0 = time.perf_counter()
    vault_path = vault_path.resolve()
    store.upsert_project(project_id, name=project_id, vault_path=str(vault_path))
    vector_store.ensure_collection()
    ctx = IngestContext(
        project_id=project_id,
        vault_path=vault_path,
        store=store,
        vector_store=vector_store,
        embedder=embedder,
        pdf_parser=pdf_parser,
        image_parser=image_parser,
    )

    # Sampled before any work: both answers are about the index as it was *found*.
    stored_parser = store.get_parser_version(project_id)
    was_empty = store.chunk_count(project_id) == 0

    diff, _scan_stats = scan_vault(vault_path, store.document_manifest(project_id), force=force)
    diff, skipped = _drop_unparseable(ctx, diff)

    created = updated = renamed = deleted = failed = 0
    chunks_written = 0

    for f in diff.creates:
        n = _process_scanned(ctx, f)
        if n is None:
            failed += 1
        elif isinstance(n, _Withheld):
            skipped += 1
        else:
            created += 1
            chunks_written += n

    for f in diff.updates:
        n = _process_scanned(ctx, f)
        if n is None:
            failed += 1
        elif isinstance(n, _Withheld):
            skipped += 1
        else:
            updated += 1
            chunks_written += n

    for r in diff.renames:
        n = _process_rename(ctx, r)
        if n is None:
            failed += 1
        elif isinstance(n, _Withheld):
            skipped += 1
        else:
            renamed += 1
            chunks_written += n

    for path in diff.deletes:
        if _process_delete(ctx, path):
            deleted += 1
        else:
            failed += 1

    graph_backfilled = _backfill_graph(ctx, diff.unchanged)

    # Only a run that actually re-read every file may claim this index is current: a
    # plain reindex leaves hash-equal files untouched, so it cannot have applied a
    # newer parser to them. Recording the version after a partial run would mark the
    # index clean while the stale documents are still stale — the precise silent-green
    # shape this marker exists to end (`vaultrag.parsing.PARSER_VERSION`).
    stale_parser = _reconcile_parser_version(
        ctx, stored=stored_parser, was_empty=was_empty, force=force, failed=failed
    )

    stats = ReindexStats(
        created=created,
        updated=updated,
        deleted=deleted,
        renamed=renamed,
        unchanged=len(diff.unchanged),
        failed=failed,
        skipped=skipped,
        chunks_written=chunks_written,
        graph_backfilled=graph_backfilled,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        stale_parser=stale_parser,
    )
    log.info("reindex.complete", project=project_id, vault=str(vault_path), **asdict(stats))
    return stats


# ---------------------------------------------------------------------------
# The single-file primitive (live-watch + write-back drive this)
# ---------------------------------------------------------------------------
def ingest_path(
    ctx: IngestContext,
    path: str,
    *,
    ignore: PathSpec[GitIgnoreSpecPattern] | None = None,
) -> IngestOutcome:
    """Reconcile ONE vault-relative path against disk. The shared per-file work unit.

    Applies the boot scan's gates so a single-file event can never index what a full
    reindex would withhold, then makes the index match disk for this one path:

    * gated (secret filename / ``.ragignore`` / unsupported / pruned) → evict any prior
      entry; ``removed`` if one existed, else ``skipped`` (``reason=excluded``);
    * missing/unreadable on disk → evict; ``removed`` / ``skipped`` (``reason=gone``);
    * content trips the secret guard → evict; ``removed`` / ``skipped``
      (``reason=secret``);
    * on-disk BLAKE3 already equals the manifest hash → ``unchanged`` (no re-embed);
    * otherwise parse → chunk → embed → upsert → ``indexed``.

    ``ignore`` is the compiled ``.ragignore`` spec; pass it to avoid re-reading the file
    on every call (the watcher caches one). ``None`` loads it fresh (correctness first).

    Raises :class:`~vaultrag.index.base.VectorStoreError` /
    :class:`~vaultrag.embedding.base.EmbeddingError` / :class:`OSError` on a genuine
    ingest failure — the caller (watcher worker / write-back) owns job tracking + retry.
    Expected non-index cases (gated / gone / secret / unchanged) return an outcome; they
    are not errors.
    """
    spec = ignore if ignore is not None else load_ignore_spec(ctx.vault_path)

    if exclusion_reason(path, ignore=spec) is not None:
        return _evict(ctx, path, SkipReason.excluded)

    abs_path = ctx.vault_path / path
    try:
        data = abs_path.read_bytes()
        mtime = int(abs_path.stat().st_mtime)
    except OSError:
        return _evict(ctx, path, SkipReason.gone)

    # Hash the raw bytes (matches scan's _hash_file), decode for parse/secret-scan. Using
    # the same bytes for hash+parse closes the TOCTOU window a re-read would open.
    content_hash = blake3(data).hexdigest()
    binary = is_binary_source(path)

    # A text file's content gate runs here, before anything expensive. A binary source
    # has no text yet, so its gate runs inside _parse_source, after conversion.
    if not binary and scan_for_secrets(data.decode("utf-8", errors="replace")) is not None:
        log.warning("ingest.secret_blocked", path=path)
        return _evict(ctx, path, SkipReason.secret)

    # Checked before conversion, not after: converting a PDF costs ~0.6 s per page, and
    # an unchanged file must stay free (the property live-watch depends on).
    prior = ctx.store.get_document(ctx.project_id, path)
    if prior is not None and prior.hash == content_hash:
        return IngestOutcome(path, IngestStatus.unchanged)

    if binary and _parser_for(ctx, path) is None:
        return _evict(ctx, path, SkipReason.no_parser)

    try:
        n = _ingest_file(ctx, path=path, data=data, content_hash=content_hash, mtime=mtime)
    except SecretBlocked:
        return _evict(ctx, path, SkipReason.secret)
    except NoTextFound:
        # OCR found nothing. Evicting rather than skipping matters: an image that used
        # to carry text and was edited to remove it must leave the index too.
        return _evict(ctx, path, SkipReason.no_text)
    return IngestOutcome(path, IngestStatus.indexed, chunks=n)


def remove_path(ctx: IngestContext, path: str) -> IngestOutcome:
    """Evict ONE path from the index (a delete event, or a path that became gated).

    Idempotent: removing a document that was never indexed is a no-op that reports
    ``skipped`` rather than ``removed``.
    """
    return _evict(ctx, path, SkipReason.gone)


def _evict(ctx: IngestContext, path: str, reason: SkipReason) -> IngestOutcome:
    """Delete a path's index entry if present; report removed vs skipped honestly."""
    existed = ctx.store.get_document(ctx.project_id, path) is not None
    if existed:
        _delete_file(ctx, path)
        return IngestOutcome(path, IngestStatus.removed, reason=reason)
    return IngestOutcome(path, IngestStatus.skipped, reason=reason)


# ---------------------------------------------------------------------------
# Per-file work (the actual pipeline) — shared by reconcile and the primitive
# ---------------------------------------------------------------------------
class SecretBlocked(Exception):
    """A converted document's text tripped the secret guard — withhold, don't fail.

    Only binary sources can raise this: a markdown file's content is scanned earlier
    (at the walk, and again in :func:`ingest_path`), but a PDF has no text until it is
    converted, so its content gate necessarily runs later. Distinct from an ingest
    *error* because the file is fine — the index must simply not hold it (CLAUDE.md §6).
    """


def _parse_source(
    ctx: IngestContext,
    *,
    path: str,
    data: bytes,
    content_hash: str | None,
    mtime: int | None,
) -> Document:
    """Bytes → :class:`Document`, routed by format. The one place formats branch.

    Everything downstream (chunk → embed → upsert) is format-agnostic by design
    (arch §5), so a new source type is a new branch here and nothing else. Today:
    markdown, Excalidraw scenes (P2 Stage 5), PDFs via Docling (P2 Stage 4), and images
    via OCR (P2 Stage 6).

    Raises :class:`~vaultrag.parsing.pdf.PdfError` /
    :class:`~vaultrag.parsing.image.OcrError` if conversion fails,
    :class:`~vaultrag.parsing.image.NoTextFound` if an image holds no text, and
    :class:`SecretBlocked` if the converted text carries key material — the content
    gate the walk could not apply to bytes.
    """
    if not is_binary_source(path):
        text = data.decode("utf-8", errors="replace")
        if is_excalidraw_source(path, text):
            return parse_excalidraw(
                path=path, raw_text=text, content_hash=content_hash, mtime=mtime
            )
        return parse_document(
            path=path,
            raw_text=text,
            content_hash=content_hash,
            mtime=mtime,
        )

    parser = _parser_for(ctx, path)
    if parser is None:  # guarded by callers; defensive, never the normal path
        raise PdfError(f"no parser available for {path!r} (install vaultrag[pdf])")

    document = parser.parse(path=path, data=data, content_hash=content_hash, mtime=mtime)
    # The secret gate every binary source shares: the walk scans text files' content,
    # but a PDF's or an image's bytes are not text, so the gate necessarily runs here on
    # the *converted* output. Without it Stage 4/6 would open a hole in CLAUDE.md §6 —
    # a screenshot of a `.env` is exactly as leaky as the file.
    rule = scan_for_secrets("\n\n".join(block.text for block in document.blocks))
    if rule is not None:
        log.warning("ingest.secret_blocked", path=path, rule=rule)
        raise SecretBlocked(path)
    return document


def _parser_for(ctx: IngestContext, path: str) -> PdfParser | ImageParser | None:
    """The converter a binary source needs, or ``None`` when it is not installed.

    Format-aware on purpose: PDF and OCR support are installed independently, so
    "is a parser available" has to mean "for *this* format" — a vault with Docling but
    no OCR engine must index its PDFs and skip its images, not the reverse.
    """
    if is_image_source(path):
        return ctx.image_parser
    return ctx.pdf_parser


def _ingest_file(
    ctx: IngestContext,
    *,
    path: str,
    data: bytes,
    content_hash: str,
    mtime: int,
) -> int:
    """Parse → chunk → embed → upsert one already-read file. Returns chunks written.

    Deletes the document's existing chunks/points before writing the fresh ones (trap
    #1) and only commits the document's real ``hash`` after every write succeeds (the
    partial-failure safety described in the module docstring).
    """
    document = _parse_source(ctx, path=path, data=data, content_hash=content_hash, mtime=mtime)
    chunks = chunk_document(document, project_id=ctx.project_id)
    vectors = embed_chunks(ctx.embedder, ctx.store, chunks)

    doc_id = document_id(ctx.project_id, path)
    ctx.store.delete_chunks_for_doc(doc_id)
    ctx.vector_store.delete_by_doc(ctx.project_id, doc_id)

    # Reserve the document row (hash=None) so the chunk insert below satisfies the FK
    # even on a brand-new document, without yet claiming this file is fully indexed.
    ctx.store.upsert_document(ctx.project_id, document.metadata.model_copy(update={"hash": None}))

    ctx.vector_store.upsert_chunks(
        chunks, vectors, embed_model=ctx.embedder.model, document=document
    )
    ctx.store.upsert_chunks(chunks)

    # Commit last: only now does the manifest agree with disk, so a crash anywhere above
    # leaves this path looking unindexed (hash=None) rather than falsely "done".
    ctx.store.upsert_document(ctx.project_id, document.metadata)
    _write_graph(ctx, doc_id, document)
    return len(chunks)


def _write_graph(ctx: IngestContext, doc_id: str, document: Document) -> None:
    """Persist this document's link edges, then let its arrival fix everyone else's.

    Two halves, and both are needed for the graph to be a pure function of the vault
    (P2 Stage 2; ``../Rag/projects/vaultrag/knowledge-graph-integration.md``):

    * **Outgoing** — replace the doc's own edges from the links just parsed. Targets
      that name no indexed document are stored dangling (``dst_doc=NULL``), not
      dropped.
    * **Incoming** — re-resolve every edge anywhere in the project that *this*
      document's names could satisfy. A note appearing is what turns a dangling
      ``[[Future Note]]`` into a real edge, and it can also re-point an edge that had
      settled for a weaker match (basename beats title). Bounded to targets naming
      this doc, so it stays O(edges-that-mention-me), not O(graph).

    Runs after ``upsert_document`` commits, because both halves read ``doc_keys`` —
    which that call is what writes.
    """
    ctx.store.replace_links(ctx.project_id, doc_id, document.links)
    ctx.store.reresolve_links(ctx.project_id, doc_id)


def _backfill_graph(ctx: IngestContext, unchanged: Sequence[str]) -> int:
    """Build link edges for already-indexed documents that never got any.

    Without this, P2 Stage 2 would land as a **silently empty graph** on every vault
    that was indexed before it: those files' hashes still match, so the diff calls them
    ``unchanged`` and no ingest ever runs to write their edges. The index would look
    healthy and ``related``/graph-expand would simply find nothing — the class of
    silent-green failure the eval-gate session was burned by.

    Cheap by construction: it re-parses only documents with **zero** ``doc_keys`` rows
    (the marker of pre-Stage-2 data — every ingested doc has at least a ``path`` key),
    and it writes only metadata + edges. No chunking, no embedding, no Qdrant call, so
    the vectors are untouched. Once a vault is backfilled this scans a counter and does
    nothing.

    A file that fails to parse is logged and skipped: a bad note must not abort a
    reconcile that had nothing else to do.
    """
    backfilled = 0
    for path in unchanged:
        doc_id = document_id(ctx.project_id, path)
        if ctx.store.doc_key_count(doc_id) > 0:
            continue
        try:
            abs_path = ctx.vault_path / path
            # Through `_parse_source`, not `parse_document`: an Excalidraw scene or a
            # PDF read as plain markdown yields the *wrong* edges (or none), and the
            # backfill is the one path that would quietly write them.
            document = _parse_source(
                ctx,
                path=path,
                data=abs_path.read_bytes(),
                content_hash=None,
                mtime=int(abs_path.stat().st_mtime),
            )
            # Preserve the stored hash: this path is genuinely unchanged, and rewriting
            # the manifest with a fresh one would be a lie if parsing ever normalizes.
            prior = ctx.store.get_document(ctx.project_id, path)
            meta = document.metadata.model_copy(
                update={"hash": prior.hash if prior else document.metadata.hash}
            )
            ctx.store.upsert_document(ctx.project_id, meta)
            _write_graph(ctx, doc_id, document)
        except Exception as exc:  # one bad note can't abort the backfill
            log.warning("reindex.graph_backfill_failed", path=path, error=str(exc))
            continue
        backfilled += 1
    if backfilled:
        log.info("reindex.graph_backfilled", project=ctx.project_id, documents=backfilled)
    return backfilled


def _reconcile_parser_version(
    ctx: IngestContext, *, stored: int | None, was_empty: bool, force: bool, failed: int
) -> bool:
    """Update/read the stored parser generation. Returns whether the index is stale.

    A brand-new project is never "stale" — there are no pre-existing documents to have
    been parsed by anything older — so the marker is simply recorded. An existing
    project at an older version is reported until a clean ``--force`` run re-reads
    every file, which is the only run that can honestly claim otherwise.

    ``was_empty`` is sampled *before* the run: by the time this is called the project
    has just been populated, so asking "is it empty?" here would answer "no" for every
    first-ever reindex and report it stale.
    """
    if (stored is None and was_empty) or (force and failed == 0):
        ctx.store.set_parser_version(ctx.project_id, PARSER_VERSION)
        return False

    if stored is not None and stored >= PARSER_VERSION:
        return False

    log.warning(
        "reindex.stale_parser",
        project=ctx.project_id,
        indexed_with=stored,
        current=PARSER_VERSION,
    )
    return True


def _read_and_ingest(ctx: IngestContext, path: str, content_hash: str) -> int:
    """Read a vetted file from disk and ingest it (the reconcile path, hash known).

    Used by :func:`reindex_vault`, whose files already passed :func:`walk_vault`'s gates
    and carry the scan hash — so it skips :func:`ingest_path`'s re-gating.
    """
    abs_path = ctx.vault_path / path
    data = abs_path.read_bytes()
    mtime = int(abs_path.stat().st_mtime)
    return _ingest_file(ctx, path=path, data=data, content_hash=content_hash, mtime=mtime)


def _delete_file(ctx: IngestContext, path: str) -> None:
    """Remove one document's points + row. Deleting an already-gone document is a no-op."""
    doc_id = document_id(ctx.project_id, path)
    ctx.vector_store.delete_by_doc(ctx.project_id, doc_id)
    ctx.store.delete_document(ctx.project_id, path)


# ---------------------------------------------------------------------------
# Job-tracked wrappers for the boot reconcile: one row in `jobs` per changed path,
# dead-parked on failure so a single bad file can't abort the rest of the run.
# ---------------------------------------------------------------------------
def _drop_unparseable(ctx: IngestContext, diff: ScanDiff) -> tuple[ScanDiff, int]:
    """Remove files whose format has no converter installed, counting them as skipped.

    Done once, up front, rather than per file: without a PDF parser every PDF in the
    vault would otherwise become a dead job, and a base install indexing a vault that
    happens to contain PDFs would report a wall of failures for working as designed.
    A skip is honest; a failure is not (CLAUDE.md §4 — the base install stays light).

    The test is **per format**, not "any parser is loaded": Docling and an OCR engine
    install independently, so an install with PDFs working and OCR missing must still
    drop its images here. Testing them together would let every image become a dead job
    the moment the `pdf` extra happened to be present.
    """

    def has_parser(path: str) -> bool:
        return not is_binary_source(path) or _parser_for(ctx, path) is not None

    creates = [f for f in diff.creates if has_parser(f.path)]
    updates = [f for f in diff.updates if has_parser(f.path)]
    renames = [r for r in diff.renames if has_parser(r.new_path)]
    skipped = (
        len(diff.creates)
        - len(creates)
        + len(diff.updates)
        - len(updates)
        + len(diff.renames)
        - len(renames)
    )
    if skipped:
        missing = sorted(
            {"pdf" if not is_image_source(p) else "ocr" for p in _dropped_paths(diff, has_parser)}
        )
        log.info(
            "reindex.format_skipped",
            project=ctx.project_id,
            count=skipped,
            extra=",".join(missing),
        )
    return (
        replace(diff, creates=creates, updates=updates, renames=renames),
        skipped,
    )


def _dropped_paths(diff: ScanDiff, has_parser: Callable[[str], bool]) -> list[str]:
    """The paths :func:`_drop_unparseable` removed — used only to name the missing extra."""
    paths = [f.path for f in diff.creates] + [f.path for f in diff.updates]
    paths += [r.new_path for r in diff.renames]
    return [p for p in paths if not has_parser(p)]


class _Withheld:
    """Sentinel: the job ran and deliberately indexed nothing (not a failure)."""

    __slots__ = ()


_WITHHELD = _Withheld()

_JobResult = int | None | _Withheld


def _process_scanned(ctx: IngestContext, f: ScannedFile) -> _JobResult:
    """Run one create/update ingest job.

    Returns chunks written, ``None`` on failure, or :data:`_WITHHELD` when the file
    converted fine but must not be indexed — its text tripped the secret guard, or it is
    an image OCR found no text in. Both are a *success* for job purposes: retrying would
    reach the same conclusion, so the job is marked done, not dead. Treating either as a
    failure would make an ordinary vault (a blank screenshot, a photo) report a wall of
    errors for behaving as designed — the rule Stage 4 set for a missing extra.
    """
    job_id = ctx.store.enqueue_job(ctx.project_id, JobKind.ingest, f.path, f.hash)
    ctx.store.set_job_state(job_id, JobState.running)
    try:
        n = _read_and_ingest(ctx, f.path, f.hash)
    except (SecretBlocked, NoTextFound):
        _delete_file(ctx, f.path)
        ctx.store.set_job_state(job_id, JobState.done)
        return _WITHHELD
    except Exception as exc:
        ctx.store.set_job_state(job_id, JobState.dead, error=str(exc), increment_attempts=True)
        log.error("reindex.ingest_failed", path=f.path, error=str(exc))
        return None
    ctx.store.set_job_state(job_id, JobState.done)
    return n


def _process_rename(ctx: IngestContext, r: Rename) -> _JobResult:
    """Move a renamed file: delete at ``old_path``, ingest at ``new_path`` (trap #2)."""
    job_id = ctx.store.enqueue_job(ctx.project_id, JobKind.ingest, r.new_path, r.hash)
    ctx.store.set_job_state(job_id, JobState.running)
    try:
        _delete_file(ctx, r.old_path)
        n = _read_and_ingest(ctx, r.new_path, r.hash)
    except (SecretBlocked, NoTextFound):
        _delete_file(ctx, r.new_path)
        ctx.store.set_job_state(job_id, JobState.done)
        return _WITHHELD
    except Exception as exc:
        ctx.store.set_job_state(job_id, JobState.dead, error=str(exc), increment_attempts=True)
        log.error("reindex.rename_failed", old_path=r.old_path, new_path=r.new_path, error=str(exc))
        return None
    ctx.store.set_job_state(job_id, JobState.done)
    return n


def _process_delete(ctx: IngestContext, path: str) -> bool:
    """Run one delete job. Returns whether it succeeded."""
    job_id = ctx.store.enqueue_job(ctx.project_id, JobKind.delete, path, None)
    ctx.store.set_job_state(job_id, JobState.running)
    try:
        _delete_file(ctx, path)
    except Exception as exc:
        ctx.store.set_job_state(job_id, JobState.dead, error=str(exc), increment_attempts=True)
        log.error("reindex.delete_failed", path=path, error=str(exc))
        return False
    ctx.store.set_job_state(job_id, JobState.done)
    return True
