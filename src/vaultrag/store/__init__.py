"""vaultrag.store — ``MetaStore`` interface + SQLite (WAL) impl (Step 2).

Structure and ops state (documents, chunks, tags, jobs, embed cache) live here;
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
    MetaStore,
    ProjectRow,
)
from vaultrag.store.sqlite import SQLiteMetaStore

__all__ = [
    "ChunkRow",
    "DocumentRow",
    "JobKind",
    "JobRow",
    "JobState",
    "MetaStore",
    "ProjectRow",
    "SQLiteMetaStore",
]
