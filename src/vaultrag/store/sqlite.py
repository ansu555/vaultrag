"""SQLite (WAL) implementation of :class:`~vaultrag.store.base.MetaStore`.

Zero-ops, single-file, single-writer — the right default for a single-user box
(``../Rag/decisions/metadata-store-sqlite-to-postgres.md``). The DDL is written to
carry forward to Postgres 16 unchanged (``reference-database-schema.md``). Tables:
``projects, documents, chunks, tags, jobs, embed_cache`` (P0) + ``links, doc_keys``
(P2 Stage 2 — the wikilink graph); ``api_keys, query_log`` remain deferred to P3.

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
from vaultrag.models import Chunk, DocumentMetadata, Link, document_id
from vaultrag.store.base import (
    ChunkRow,
    DocumentRow,
    JobRow,
    JobState,
    LinkedDoc,
    LinkRow,
    NeighborDoc,
    ProjectRow,
    SiblingDoc,
)
from vaultrag.store.links import DocKeyKind, doc_keys_for, link_edges, resolve_key

# jobs are idempotent by (project, path, hash); a deterministic id makes re-enqueue
# a PK no-op rather than a duplicate row.
_JOB_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "vaultrag://jobs")
# Edges are identified by what they *are* — (source, kind, normalized target, anchor) —
# so replacing a document's links rewrites the same ids rather than churning the table.
_LINK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "vaultrag://links")
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

-- The wikilink graph (P2 Stage 2). `dst_doc` is NULL while a target names no indexed
-- document; `dst_key` is the normalized target (store/links.normalize_key) and is what
-- re-resolution joins on when a note later appears or disappears.
--   src_doc CASCADE   — deleting a note removes the links it wrote.
--   dst_doc SET NULL  — deleting a note does NOT remove links pointing at it; they
--                       dangle, exactly as they do in Obsidian, and re-resolve if the
--                       note comes back (or if its removal breaks a title tie).
CREATE TABLE IF NOT EXISTS links (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    src_doc     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    dst_doc     TEXT REFERENCES documents(id) ON DELETE SET NULL,
    dst_raw     TEXT NOT NULL,
    dst_key     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    anchor      TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_doc);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_doc);
CREATE INDEX IF NOT EXISTS idx_links_key ON links(project_id, dst_key);

-- Reverse index of "what can this document be linked by" — path / basename / title /
-- alias, normalized. Derived entirely from `documents` + frontmatter, so it is rebuilt
-- on every upsert; it exists so resolving a target is one indexed lookup, not four.
CREATE TABLE IF NOT EXISTS doc_keys (
    project_id  TEXT NOT NULL,
    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    PRIMARY KEY (doc_id, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_doc_keys_lookup ON doc_keys(project_id, key);

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


def _link_id(src_doc: str, kind: str, dst_key: str, anchor: str | None) -> str:
    return str(
        uuid.uuid5(_LINK_NAMESPACE, f"{src_doc}{_SEP}{kind}{_SEP}{dst_key}{_SEP}{anchor or ''}")
    )


def _link_row(row: sqlite3.Row) -> LinkRow:
    return LinkRow(
        id=row["id"],
        project_id=row["project_id"],
        src_doc=row["src_doc"],
        dst_doc=row["dst_doc"],
        dst_raw=row["dst_raw"],
        dst_key=row["dst_key"],
        kind=row["kind"],
        anchor=row["anchor"],
    )


def _linked_doc(row: sqlite3.Row) -> LinkedDoc:
    return LinkedDoc(
        doc_id=row["id"],
        path=row["path"],
        title=row["title"],
        kind=row["kind"],
        anchor=row["anchor"],
    )


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
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
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
            self._replace_doc_keys(project_id, doc_id, meta)
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
        doc_id = document_id(project_id, path)
        # Read the doc's names BEFORE the delete cascades `doc_keys` away: losing a
        # document can *un*-break a tie (two notes titled "Foo" blocked every [[Foo]];
        # delete one and the survivor now wins), so those targets need re-resolving.
        keys = self._keys_for_doc(doc_id)
        with self._conn:
            # chunks + tags + doc_keys cascade, and links pointing here go dst_doc=NULL,
            # via the FK actions declared in _SCHEMA.
            self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        if keys:
            self._reresolve_keys(project_id, keys)

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
        row = self._conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
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

    # --- doc_keys (the resolver's reverse index) ------------------------------
    def _replace_doc_keys(self, project_id: str, doc_id: str, meta: DocumentMetadata) -> None:
        """Rebuild a document's addressable names. Caller owns the transaction frame."""
        self._conn.execute("DELETE FROM doc_keys WHERE doc_id = ?", (doc_id,))
        keys = doc_keys_for(meta)
        if keys:
            self._conn.executemany(
                "INSERT INTO doc_keys (project_id, doc_id, kind, key) VALUES (?, ?, ?, ?)",
                [(project_id, doc_id, str(k.kind), k.key) for k in keys],
            )

    def _keys_for_doc(self, doc_id: str) -> set[str]:
        rows = self._conn.execute("SELECT key FROM doc_keys WHERE doc_id = ?", (doc_id,)).fetchall()
        return {row["key"] for row in rows}

    def doc_key_count(self, doc_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM doc_keys WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return int(row["n"])

    # --- links / graph --------------------------------------------------------
    def _resolve_keys(self, project_id: str, keys: Iterable[str]) -> dict[str, str | None]:
        """Resolve normalized targets → doc id (or ``None``), in one indexed sweep.

        Batched deliberately: a document with 40 links must not become 40 queries.
        """
        wanted = list(dict.fromkeys(k for k in keys if k))
        if not wanted:
            return {}
        # key -> tier -> [doc_id]; resolve_key() applies the precedence + no-guess rule.
        buckets: dict[str, dict[DocKeyKind, list[str]]] = {k: {} for k in wanted}
        for start in range(0, len(wanted), 900):  # SQLite's 999-variable ceiling
            batch = wanted[start : start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self._conn.execute(
                "SELECT key, kind, doc_id FROM doc_keys "
                f"WHERE project_id = ? AND key IN ({placeholders}) "
                "ORDER BY doc_id",  # stable: an ambiguous tier must look ambiguous every run
                (project_id, *batch),
            ).fetchall()
            for row in rows:
                buckets[row["key"]].setdefault(DocKeyKind(row["kind"]), []).append(row["doc_id"])
        return {key: resolve_key(key, tiers) for key, tiers in buckets.items()}

    def replace_links(self, project_id: str, src_doc: str, links: Iterable[Link]) -> int:
        edges = link_edges(links)
        resolved = self._resolve_keys(project_id, (e.dst_key for e in edges))
        rows = [
            (
                _link_id(src_doc, e.kind, e.dst_key, e.anchor),
                project_id,
                src_doc,
                resolved.get(e.dst_key),
                e.dst_raw,
                e.dst_key,
                e.kind,
                e.anchor,
            )
            for e in edges
        ]
        with self._conn:
            # Replace, don't merge: a document's edges are whatever its text says now,
            # so a deleted wikilink must leave the graph on the next ingest.
            self._conn.execute("DELETE FROM links WHERE src_doc = ?", (src_doc,))
            if rows:
                self._conn.executemany(
                    "INSERT INTO links "
                    "(id, project_id, src_doc, dst_doc, dst_raw, dst_key, kind, anchor) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        return len(rows)

    def reresolve_links(self, project_id: str, doc_id: str) -> int:
        keys = self._keys_for_doc(doc_id)
        return self._reresolve_keys(project_id, keys) if keys else 0

    def _reresolve_keys(self, project_id: str, keys: Iterable[str]) -> int:
        """Re-point every edge whose target is one of ``keys``. Returns rows changed.

        Runs over *all* matching edges, not just the dangling ones. An edge that
        already resolved can still be wrong after a change: a new note whose basename
        matches now outranks the title match the edge was pointing at, and a deleted
        note can leave a previously-ambiguous target with a single winner. Re-deriving
        both directions from the current index is what keeps the graph a pure function
        of the vault (CLAUDE.md §6) instead of a pile of history.
        """
        resolved = self._resolve_keys(project_id, keys)
        if not resolved:
            return 0
        changed = 0
        with self._conn:
            for key, dst in resolved.items():
                cur = self._conn.execute(
                    "UPDATE links SET dst_doc = ? "
                    "WHERE project_id = ? AND dst_key = ? AND dst_doc IS NOT ?",
                    (dst, project_id, key, dst),
                )
                changed += cur.rowcount
        return changed

    def outgoing_links(self, project_id: str, doc_id: str) -> list[LinkRow]:
        rows = self._conn.execute(
            "SELECT * FROM links WHERE project_id = ? AND src_doc = ? ORDER BY dst_key, kind",
            (project_id, doc_id),
        ).fetchall()
        return [_link_row(row) for row in rows]

    def linked_docs(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        rows = self._conn.execute(
            """
            SELECT d.id, d.path, d.title, l.kind, l.anchor
              FROM links l
              JOIN documents d ON d.id = l.dst_doc
             WHERE l.project_id = ? AND l.src_doc = ? AND l.dst_doc IS NOT ?
             ORDER BY d.path, l.kind
            """,
            (project_id, doc_id, doc_id),  # IS NOT doc_id: drop [[self]] links
        ).fetchall()
        return [_linked_doc(row) for row in rows]

    def backlinks(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        rows = self._conn.execute(
            """
            SELECT d.id, d.path, d.title, l.kind, l.anchor
              FROM links l
              JOIN documents d ON d.id = l.src_doc
             WHERE l.project_id = ? AND l.dst_doc = ? AND l.src_doc IS NOT ?
             ORDER BY d.path, l.kind
            """,
            (project_id, doc_id, doc_id),
        ).fetchall()
        return [_linked_doc(row) for row in rows]

    def neighbors(
        self,
        project_id: str,
        doc_id: str,
        hops: int = 1,
        limit: int = 50,
    ) -> list[NeighborDoc]:
        if hops < 1:
            return []
        rows = self._conn.execute(
            # UNION (not UNION ALL) terminates on cycles — a vault's graph is dense and
            # very much not a DAG. It dedupes (doc, hops) pairs, so the same document can
            # still arrive at two depths; MIN(hops) below collapses it to the shortest.
            """
            WITH RECURSIVE reach(doc_id, hops) AS (
                SELECT ?, 0
                UNION
                SELECT CASE WHEN l.src_doc = reach.doc_id THEN l.dst_doc ELSE l.src_doc END,
                       reach.hops + 1
                  FROM links l, reach
                 WHERE reach.hops < ?
                   AND l.project_id = ?
                   AND l.dst_doc IS NOT NULL
                   AND (l.src_doc = reach.doc_id OR l.dst_doc = reach.doc_id)
            )
            SELECT d.id, d.path, d.title, MIN(reach.hops) AS hops
              FROM reach
              JOIN documents d ON d.id = reach.doc_id
             WHERE reach.doc_id IS NOT ? AND d.project_id = ?
             GROUP BY d.id
             ORDER BY hops, d.path
             LIMIT ?
            """,
            (doc_id, hops, project_id, doc_id, project_id, limit),
        ).fetchall()
        return [
            NeighborDoc(
                doc_id=row["id"], path=row["path"], title=row["title"], hops=int(row["hops"])
            )
            for row in rows
        ]

    def tag_siblings(self, project_id: str, doc_id: str, limit: int = 20) -> list[SiblingDoc]:
        rows = self._conn.execute(
            # Note: `tags` holds prefix-expanded tags (a/b/c → a, a/b, a/b/c), so a shared
            # top-level namespace counts. That is intentional — a shared `project/x` root
            # is a real (if weak) sibling signal — but it means the count is a ranking
            # weight, not a claim about how many literal tags two notes share.
            """
            SELECT d.id, d.path, d.title, COUNT(*) AS shared
              FROM tags mine
              JOIN tags theirs ON theirs.tag = mine.tag AND theirs.doc_id IS NOT mine.doc_id
              JOIN documents d ON d.id = theirs.doc_id
             WHERE mine.doc_id = ? AND theirs.project_id = ?
             GROUP BY d.id
             ORDER BY shared DESC, d.path
             LIMIT ?
            """,
            (doc_id, project_id, limit),
        ).fetchall()
        return [
            SiblingDoc(
                doc_id=row["id"],
                path=row["path"],
                title=row["title"],
                shared_tags=int(row["shared"]),
            )
            for row in rows
        ]

    def unresolved_links(self, project_id: str, limit: int = 100) -> list[LinkRow]:
        rows = self._conn.execute(
            "SELECT * FROM links WHERE project_id = ? AND dst_doc IS NULL "
            "ORDER BY dst_key, src_doc LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [_link_row(row) for row in rows]

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
