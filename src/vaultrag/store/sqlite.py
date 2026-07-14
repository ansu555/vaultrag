"""SQLite (WAL) implementation of :class:`~vaultrag.store.base.MetaStore`.

Zero-ops, single-file, single-writer — the right default for a single-user box
(``../Rag/decisions/metadata-store-sqlite-to-postgres.md``). The DDL is written to
carry forward to Postgres 16 unchanged (``reference-database-schema.md``). P0 tables
only: ``projects, documents, chunks, jobs, embed_cache, tags`` — ``links, api_keys,
query_log`` are deferred (p0-step-plan.md Step 2).

Identity is content-addressed (``vaultrag.models``): a document is
``document_id(project, path)`` and a chunk is ``chunk_point_id(project, path, ix)``,
so every write is an idempotent upsert — re-running the pipeline never duplicates rows.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

from vaultrag.config import Settings
from vaultrag.models import Chunk, DocumentMetadata, document_id
from vaultrag.store.base import (
    ChunkRow,
    DocumentRow,
    JobRow,
    JobState,
    ProjectRow,
)

# jobs are idempotent by (project, path, hash); a deterministic id makes re-enqueue
# a PK no-op rather than a duplicate row.
_JOB_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "vaultrag://jobs")
_SEP = "\x1f"

# Same DDL runs on SQLite now and Postgres 16 later (reference-database-schema.md).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    vault_path  TEXT NOT NULL,
    settings    TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'active',
    created_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    hash        TEXT,
    title       TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'note',
    mtime       INTEGER,
    status      TEXT NOT NULL DEFAULT 'indexed',
    UNIQUE(project_id, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id           TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ix           INTEGER NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '[]',
    hash         TEXT NOT NULL,
    tokens       INTEGER NOT NULL DEFAULT 0,
    parent_id    TEXT,
    prev_id      TEXT,
    next_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS tags (
    project_id  TEXT NOT NULL,
    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_project_tag ON tags(project_id, tag);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    path        TEXT,
    hash        TEXT,
    state       TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(project_id, state);

CREATE TABLE IF NOT EXISTS embed_cache (
    model      TEXT NOT NULL,
    dims       INTEGER NOT NULL,
    chunk_hash TEXT NOT NULL,
    vector     BLOB NOT NULL,
    PRIMARY KEY (model, dims, chunk_hash)
);
"""


def _now() -> int:
    return int(time.time())


def _job_id(project_id: str, path: str | None, hash: str | None) -> str:
    return str(uuid.uuid5(_JOB_NAMESPACE, f"{project_id}{_SEP}{path or ''}{_SEP}{hash or ''}"))


class SQLiteMetaStore:
    """A :class:`~vaultrag.store.base.MetaStore` backed by one WAL SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Deferred-transaction isolation (the default) so ``with self._conn:`` frames
        # our multi-statement writes atomically; check_same_thread off for the future
        # worker pool (writes stay serialized — the single-writer decision holds).
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    @classmethod
    def from_settings(cls, settings: Settings) -> SQLiteMetaStore:
        """Open the store at ``settings.db_path`` (inside the derived state dir)."""
        return cls(settings.db_path)

    # --- schema / lifecycle ---------------------------------------------------
    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SQLiteMetaStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- projects -------------------------------------------------------------
    def upsert_project(
        self,
        project_id: str,
        name: str,
        vault_path: str,
        settings: Mapping[str, Any] | None = None,
        status: str = "active",
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO projects (id, name, vault_path, settings, status, created_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    vault_path=excluded.vault_path,
                    settings=excluded.settings,
                    status=excluded.status
                """,
                (
                    project_id,
                    name,
                    vault_path,
                    json.dumps(dict(settings or {})),
                    status,
                    _now(),
                ),
            )

    def get_project(self, project_id: str) -> ProjectRow | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        return ProjectRow(
            id=row["id"],
            name=row["name"],
            vault_path=row["vault_path"],
            settings=json.loads(row["settings"]),
            status=row["status"],
            created_ts=int(row["created_ts"]),
        )

    # --- documents ------------------------------------------------------------
    def upsert_document(
        self,
        project_id: str,
        meta: DocumentMetadata,
        status: str = "indexed",
    ) -> str:
        doc_id = document_id(project_id, meta.path)
        with self._conn:
            # ON CONFLICT DO UPDATE (never INSERT OR REPLACE): a REPLACE would delete
            # the row and cascade-drop its chunks/tags out from under us.
            self._conn.execute(
                """
                INSERT INTO documents (id, project_id, path, hash, title, type, mtime, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    hash=excluded.hash,
                    title=excluded.title,
                    type=excluded.type,
                    mtime=excluded.mtime,
                    status=excluded.status
                """,
                (
                    doc_id,
                    project_id,
                    meta.path,
                    meta.hash,
                    meta.title,
                    str(meta.type),
                    meta.modified_ts,
                    status,
                ),
            )
            self._replace_tags(project_id, doc_id, meta.tags)
        return doc_id

    def get_document(self, project_id: str, path: str) -> DocumentRow | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id = ?",
            (document_id(project_id, path),),
        ).fetchone()
        if row is None:
            return None
        return DocumentRow(
            id=row["id"],
            project_id=row["project_id"],
            path=row["path"],
            hash=row["hash"],
            title=row["title"],
            type=row["type"],
            mtime=None if row["mtime"] is None else int(row["mtime"]),
            status=row["status"],
        )

    def document_manifest(self, project_id: str) -> dict[str, str | None]:
        rows = self._conn.execute(
            "SELECT path, hash FROM documents WHERE project_id = ?", (project_id,)
        ).fetchall()
        return {row["path"]: row["hash"] for row in rows}

    def delete_document(self, project_id: str, path: str) -> None:
        with self._conn:
            # chunks + tags cascade via ON DELETE CASCADE.
            self._conn.execute(
                "DELETE FROM documents WHERE id = ?",
                (document_id(project_id, path),),
            )

    # --- chunks ---------------------------------------------------------------
    def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        rows = [
            (
                c.point_id,
                document_id(c.project_id, c.path),
                c.ix,
                json.dumps(c.heading_path),
                c.chunk_hash,
                c.tokens,
                c.parent_id,
                c.prev_id,
                c.next_id,
            )
            for c in chunks
        ]
        if not rows:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO chunks
                    (id, doc_id, ix, heading_path, hash, tokens, parent_id, prev_id, next_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    doc_id=excluded.doc_id,
                    ix=excluded.ix,
                    heading_path=excluded.heading_path,
                    hash=excluded.hash,
                    tokens=excluded.tokens,
                    parent_id=excluded.parent_id,
                    prev_id=excluded.prev_id,
                    next_id=excluded.next_id
                """,
                rows,
            )

    def chunk_hashes(self, doc_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT id, hash FROM chunks WHERE doc_id = ?", (doc_id,)
        ).fetchall()
        return {row["id"]: row["hash"] for row in rows}

    def get_chunk(self, chunk_id: str) -> ChunkRow | None:
        row = self._conn.execute(
            "SELECT * FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            return None
        return ChunkRow(
            id=row["id"],
            doc_id=row["doc_id"],
            ix=int(row["ix"]),
            heading_path=json.loads(row["heading_path"]),
            hash=row["hash"],
            tokens=int(row["tokens"]),
            parent_id=row["parent_id"],
            prev_id=row["prev_id"],
            next_id=row["next_id"],
        )

    def delete_chunks_for_doc(self, doc_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    # --- tags -----------------------------------------------------------------
    def set_tags(self, project_id: str, doc_id: str, tags: Iterable[str]) -> None:
        with self._conn:
            self._replace_tags(project_id, doc_id, tags)

    def _replace_tags(self, project_id: str, doc_id: str, tags: Iterable[str]) -> None:
        """Replace a document's tag set. Caller owns the transaction frame."""
        self._conn.execute("DELETE FROM tags WHERE doc_id = ?", (doc_id,))
        unique = sorted(set(tags))
        if unique:
            self._conn.executemany(
                "INSERT INTO tags (project_id, doc_id, tag) VALUES (?, ?, ?)",
                [(project_id, doc_id, tag) for tag in unique],
            )

    def tags_for_doc(self, doc_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT tag FROM tags WHERE doc_id = ? ORDER BY tag", (doc_id,)
        ).fetchall()
        return [row["tag"] for row in rows]

    # --- jobs -----------------------------------------------------------------
    def enqueue_job(
        self,
        project_id: str,
        kind: str,
        path: str | None = None,
        hash: str | None = None,
    ) -> str:
        job_id = _job_id(project_id, path, hash)
        with self._conn:
            # INSERT OR IGNORE: a re-enqueue of the same (project, path, hash) keeps the
            # existing row (and its state) — the idempotency the ingestion note requires.
            self._conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (id, project_id, kind, path, hash, state, attempts, error, ts)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (job_id, project_id, kind, path, hash, JobState.pending, _now()),
            )
        return job_id

    def set_job_state(
        self,
        job_id: str,
        state: str,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                UPDATE jobs
                   SET state = ?,
                       error = ?,
                       attempts = attempts + ?,
                       ts = ?
                 WHERE id = ?
                """,
                (state, error, 1 if increment_attempts else 0, _now(), job_id),
            )

    def get_job(self, job_id: str) -> JobRow | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return JobRow(
            id=row["id"],
            project_id=row["project_id"],
            kind=row["kind"],
            path=row["path"],
            hash=row["hash"],
            state=row["state"],
            attempts=int(row["attempts"]),
            error=row["error"],
            ts=int(row["ts"]),
        )

    # --- embed cache ----------------------------------------------------------
    def get_cached_vectors(
        self,
        model: str,
        dims: int,
        chunk_hashes: Iterable[str],
    ) -> dict[str, bytes]:
        hashes = list(dict.fromkeys(chunk_hashes))  # de-dupe, preserve order
        if not hashes:
            return {}
        out: dict[str, bytes] = {}
        # Chunk the IN clause to stay under SQLite's variable limit (999) at scale.
        for start in range(0, len(hashes), 900):
            batch = hashes[start : start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                "SELECT chunk_hash, vector FROM embed_cache "
                f"WHERE model = ? AND dims = ? AND chunk_hash IN ({placeholders})",
                (model, dims, *batch),
            ).fetchall()
            for row in rows:
                out[row["chunk_hash"]] = bytes(row["vector"])
        return out

    def put_cached_vector(self, model: str, dims: int, chunk_hash: str, vector: bytes) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO embed_cache (model, dims, chunk_hash, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model, dims, chunk_hash) DO UPDATE SET vector=excluded.vector
                """,
                (model, dims, chunk_hash, vector),
            )
