"""Live-watch: turn filesystem events into single-file (re)ingests (P1 Stage 4).

``watchdog`` ``Observer`` → **per-path debounce** (Obsidian saves via temp files, so one
Ctrl-S produces a create/modify/move storm; the debounce collapses it into one ingest) →
a small **worker pool** → the :func:`~vaultrag.indexer.pipeline.ingest_path` primitive.
A failed file **retries with backoff** and then parks its job ``dead`` — one bad note can
never kill the watcher (arch §4; ``../Rag/decisions/in-process-jobs-not-redis-celery.md``;
``../Rag/projects/vaultrag/ingestion-pipeline.md``).

**Threading model.** The whole P0/P1 pipeline is synchronous, and ``watchdog`` already
runs its ``Observer`` in its own thread, so this is thread-based (not asyncio): the
Observer thread only *marks paths dirty*; a **dispatcher** thread moves paths that have
gone quiet for ``debounce_seconds`` onto a :class:`~concurrent.futures.ThreadPoolExecutor`
(default 4 workers). A path already in-flight is not re-dispatched until it finishes, so
per-path work stays serial (last write wins) while different paths run in parallel — the
ordering the ingestion note specifies. The single shared SQLite connection serializes its
own writes (``store/sqlite.py`` opened ``check_same_thread=False`` for exactly this pool).

**Process model (settled — p1-step-plan Decisions).** ``serve``/the indexer own the
watcher; ``vaultrag watch`` runs it standalone; ``vaultrag mcp`` is watcher-free unless
``--watch`` is passed, and then runs one :class:`VaultWatcher` in a **daemon thread** that
dies with the stdio process — solo dogfooding, so no N-sessions → N-watchers race.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path

import structlog
from pathspec import PathSpec
from pathspec.patterns.gitignore.spec import GitIgnoreSpecPattern
from watchdog.events import (
    EVENT_TYPE_CLOSED,
    EVENT_TYPE_DELETED,
    EVENT_TYPE_MOVED,
    EVENT_TYPE_OPENED,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from vaultrag.indexer.pipeline import IngestContext, IngestOutcome, ingest_path
from vaultrag.indexer.scan import iter_indexable_suffixes, load_ignore_spec
from vaultrag.store.base import JobKind, JobState

log = structlog.get_logger(__name__)

DEFAULT_DEBOUNCE_SECONDS = 1.5  # Obsidian temp-file save storms settle well within this.
DEFAULT_WORKERS = 4
DEFAULT_MAX_ATTEMPTS = 3  # arch §4: retry 3 times with backoff, then park dead.
DEFAULT_RETRY_BACKOFF = 0.5  # seconds; grows as backoff * 2**attempt.
_DISPATCH_POLL_SECONDS = 0.2  # dispatcher's idle wake cadence (bounds debounce latency).

_INDEXABLE_SUFFIXES = frozenset(iter_indexable_suffixes())


class VaultWatcher:
    """Watch a vault and (re)ingest each changed file once it goes quiet.

    Start it (or use it as a context manager) to run the ``watchdog`` observer plus the
    debounce/dispatch loop; :meth:`stop` drains and shuts everything down. The heavy
    lifting is the shared :func:`~vaultrag.indexer.pipeline.ingest_path` primitive, so a
    live edit lands in the index identically to a ``reindex`` of that one file — including
    the secret/`.ragignore` gates and the evict-on-delete path.
    """

    def __init__(
        self,
        ctx: IngestContext,
        *,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        workers: int = DEFAULT_WORKERS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        ignore: PathSpec[GitIgnoreSpecPattern] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = _DISPATCH_POLL_SECONDS,
    ) -> None:
        # A resolved root so watchdog's absolute event paths map cleanly to vault-relative.
        self._ctx = IngestContext(
            project_id=ctx.project_id,
            vault_path=ctx.vault_path.resolve(),
            store=ctx.store,
            vector_store=ctx.vector_store,
            embedder=ctx.embedder,
        )
        self._debounce = debounce_seconds
        self._workers = workers
        self._max_attempts = max(1, max_attempts)
        self._retry_backoff = retry_backoff
        self._ignore = ignore if ignore is not None else load_ignore_spec(self._ctx.vault_path)
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval

        # Debounce state: path -> monotonic deadline it becomes eligible to dispatch.
        self._pending: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stopping = threading.Event()

        self._handler = _DirtyHandler(self)
        self._observer: BaseObserver | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._dispatcher: threading.Thread | None = None

    # --- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        """Start the observer, worker pool, and dispatcher thread. Non-blocking."""
        if self._observer is not None:
            raise RuntimeError("VaultWatcher already started")
        self._executor = ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="vaultrag-ingest"
        )
        observer = Observer()
        observer.daemon = True  # dies with the process (mcp --watch stdio) if stop() is skipped
        observer.schedule(self._handler, str(self._ctx.vault_path), recursive=True)
        observer.start()
        self._observer = observer
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="vaultrag-watch-dispatch", daemon=True
        )
        self._dispatcher.start()
        log.info(
            "watch.started",
            project=self._ctx.project_id,
            vault=str(self._ctx.vault_path),
            workers=self._workers,
            debounce_s=self._debounce,
        )

    def stop(self, *, drain: bool = True) -> None:
        """Stop the observer and dispatcher; optionally drain still-pending paths.

        ``drain`` processes any path whose debounce hadn't elapsed yet (so an edit made
        just before shutdown isn't lost), then the executor is joined so in-flight
        ingests finish. Safe to call more than once.
        """
        self._stopping.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        with self._cond:
            self._cond.notify_all()
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=5)
            self._dispatcher = None
        if drain:
            self.flush()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        log.info("watch.stopped", project=self._ctx.project_id)

    def __enter__(self) -> VaultWatcher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- event intake (called on the observer thread) -------------------------
    def _mark_dirty(self, rel_path: str) -> None:
        """Record a change to one vault-relative path; (re)arm its debounce window."""
        with self._cond:
            self._pending[rel_path] = self._clock() + self._debounce
            self._cond.notify_all()

    def _to_rel(self, abs_path: str) -> str | None:
        """Map an absolute watchdog path to a vault-relative POSIX path, or None if outside."""
        try:
            rel = Path(abs_path).resolve().relative_to(self._ctx.vault_path)
        except (ValueError, OSError):
            return None
        return rel.as_posix()

    def _handle_event(self, event: FileSystemEvent) -> None:
        """Translate one filesystem event into dirty-path marks (observer thread)."""
        if event.event_type in (EVENT_TYPE_OPENED, EVENT_TYPE_CLOSED):
            return  # Linux inotify open/close noise — never a content change on its own.
        if event.is_directory:
            self._handle_dir_event(event)
            return
        src = os.fsdecode(event.src_path)
        if event.event_type == EVENT_TYPE_MOVED:
            # Reconcile both ends: the old path (→ evicted) and the new one (→ indexed).
            for p in (src, os.fsdecode(event.dest_path)):
                rel = self._to_rel(p)
                if rel is not None:
                    self._mark_dirty(rel)
            return
        rel = self._to_rel(src)
        if rel is not None:
            self._mark_dirty(rel)

    def _handle_dir_event(self, event: FileSystemEvent) -> None:
        """A directory was deleted/moved: reconcile every indexed path under it.

        watchdog emits a single event for a directory rename/delete (not one per file),
        so fan it back out: evict the manifest docs under the old prefix, and walk any
        destination subtree for indexable files. ``ingest_path``'s own gates decide what
        actually indexes, so this over-marks safely.
        """
        if event.event_type == EVENT_TYPE_DELETED:
            self._mark_prefix(os.fsdecode(event.src_path))
        elif event.event_type == EVENT_TYPE_MOVED:
            self._mark_prefix(os.fsdecode(event.src_path))
            self._mark_subtree(os.fsdecode(event.dest_path))

    def _mark_prefix(self, abs_dir: str) -> None:
        """Mark every already-indexed document under ``abs_dir`` dirty (for eviction)."""
        rel = self._to_rel(abs_dir)
        if rel is None:
            return
        prefix = f"{rel}/"
        for path in self._ctx.store.document_manifest(self._ctx.project_id):
            if path == rel or path.startswith(prefix):
                self._mark_dirty(path)

    def _mark_subtree(self, abs_dir: str) -> None:
        """Mark every indexable file currently under ``abs_dir`` dirty (for a moved-in dir)."""
        base = Path(abs_dir)
        if not base.is_dir():
            return
        for child in base.rglob("*"):
            if child.is_file() and child.suffix.lower() in _INDEXABLE_SUFFIXES:
                rel = self._to_rel(str(child))
                if rel is not None:
                    self._mark_dirty(rel)

    # --- dispatch (dispatcher thread) -----------------------------------------
    def _dispatch_loop(self) -> None:
        while not self._stopping.is_set():
            due = self._take_due()
            for path in due:
                self._submit(path)
            if not due:
                with self._cond:
                    if self._stopping.is_set():
                        return
                    self._cond.wait(self._wait_timeout())

    def _take_due(self) -> list[str]:
        """Pop paths whose debounce elapsed and that aren't already in-flight; mark them so."""
        with self._lock:
            now = self._clock()
            ready = [
                p
                for p, deadline in self._pending.items()
                if deadline <= now and p not in self._inflight
            ]
            for p in ready:
                del self._pending[p]
                self._inflight.add(p)
            return ready

    def _wait_timeout(self) -> float:
        """How long the dispatcher may sleep: until the soonest deadline, capped by poll."""
        if not self._pending:
            return self._poll_interval
        soonest = min(self._pending.values())
        return max(0.0, min(self._poll_interval, soonest - self._clock()))

    def _submit(self, path: str) -> None:
        assert self._executor is not None  # set in start(); dispatcher runs only after
        future = self._executor.submit(self.process_path, path)
        future.add_done_callback(partial(self._on_done, path))

    def _on_done(self, path: str, _future: Future[IngestOutcome | None]) -> None:
        with self._cond:
            self._inflight.discard(path)
            self._cond.notify_all()

    # --- the worker -----------------------------------------------------------
    def process_path(self, path: str) -> IngestOutcome | None:
        """Reconcile one path with job tracking + retry/backoff. Returns the outcome.

        Enqueues a ``jobs`` row for visibility, runs :func:`ingest_path`, and on failure
        retries up to ``max_attempts`` with exponential backoff before parking the job
        ``dead`` (returning ``None``). Never raises — the dispatcher must survive any one
        file (CLAUDE.md §6: a bad note can't take down the watcher).
        """
        kind = JobKind.ingest if (self._ctx.vault_path / path).exists() else JobKind.delete
        job_id = self._ctx.store.enqueue_job(self._ctx.project_id, kind, path, None)
        self._ctx.store.set_job_state(job_id, JobState.running)

        last_error = ""
        for attempt in range(self._max_attempts):
            try:
                outcome = ingest_path(self._ctx, path, ignore=self._ignore)
            except Exception as exc:  # any failure dead-parks the job, never propagates
                last_error = str(exc)
                self._ctx.store.set_job_state(
                    job_id, JobState.running, error=last_error, increment_attempts=True
                )
                if attempt + 1 < self._max_attempts:
                    self._sleep(self._retry_backoff * (2**attempt))
                continue
            self._ctx.store.set_job_state(job_id, JobState.done)
            log.info(
                "watch.reconciled",
                path=path,
                status=str(outcome.status),
                chunks=outcome.chunks,
            )
            return outcome

        self._ctx.store.set_job_state(job_id, JobState.dead, error=last_error)
        log.error("watch.ingest_dead", path=path, attempts=self._max_attempts, error=last_error)
        return None

    # --- draining / test hooks ------------------------------------------------
    def flush(self) -> None:
        """Process every pending (not in-flight) path synchronously, ignoring debounce.

        Used by :meth:`stop` to avoid dropping a just-made edit, and by tests to force a
        deterministic reconcile without waiting out the debounce window.
        """
        with self._lock:
            pending = [p for p in self._pending if p not in self._inflight]
            for p in pending:
                del self._pending[p]
        for p in pending:
            self.process_path(p)

    def wait_idle(self, timeout: float) -> bool:
        """Block until nothing is pending or in-flight, or ``timeout`` elapses.

        Returns ``True`` if the watcher went idle in time. Test/e2e helper: write a file,
        then ``wait_idle`` before asserting it is searchable.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.1))
        return True


class _DirtyHandler(FileSystemEventHandler):
    """Bridges watchdog callbacks to :meth:`VaultWatcher._handle_event`."""

    def __init__(self, watcher: VaultWatcher) -> None:
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._watcher._handle_event(event)
