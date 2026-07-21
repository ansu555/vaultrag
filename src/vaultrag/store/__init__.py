"""vaultrag.store — ``MetaStore`` interface + SQLite (WAL) impl (Step 2).

Structure, graph, and ops state (documents, chunks, tags, links, jobs, embed cache) live here;
vectors live in Qdrant. This is a derived cache — ``vaultrag reindex`` rebuilds it
from the vault (CLAUDE.md §6). See ../Rag/reference/reference-database-schema.md and
../Rag/decisions/metadata-store-sqlite-to-postgres.md.
"""

from vaultrag.store.base import (
    ChunkRow,
    DocumentRow,
    JobKind,
    JobRow,
    JobState,
    LinkedDoc,
    LinkRow,
    MetaStore,
    NeighborDoc,
    ProjectRow,
    SiblingDoc,
)
from vaultrag.store.links import DocKey, DocKeyKind, LinkEdge, doc_keys_for, normalize_key
from vaultrag.store.sqlite import SQLiteMetaStore

__all__ = [
    "ChunkRow",
    "DocKey",
    "DocKeyKind",
    "DocumentRow",
    "JobKind",
    "JobRow",
    "JobState",
    "LinkEdge",
    "LinkRow",
    "LinkedDoc",
    "MetaStore",
    "NeighborDoc",
    "ProjectRow",
    "SQLiteMetaStore",
    "SiblingDoc",
    "doc_keys_for",
    "normalize_key",
]
