"""vaultrag.writeback — agent write-back: create/edit vault notes, commit, reindex (P1 Stage 5).

One class, :class:`NoteWriter`, backs the three MCP write tools
(``write_note``/``append_section``/``patch_frontmatter``). It enforces the write
guardrails (containment, allowlist, secret/`.ragignore` gates, size/rate caps, optimistic
lock), auto-commits each accepted write to the vault's git repo (best-effort audit), and
runs a synchronous :func:`~vaultrag.indexer.pipeline.ingest_path` so a write is retrievable
the same session — closing the memory loop (arch §17;
``../Rag/projects/vaultrag/write-back-design.md``; repo ``CLAUDE.md`` §6).
"""

from vaultrag.writeback.errors import RefusalReason, WriteConflict, WriteError, WriteRefused
from vaultrag.writeback.writer import NoteWriter, slugify

__all__ = [
    "NoteWriter",
    "RefusalReason",
    "WriteConflict",
    "WriteError",
    "WriteRefused",
    "slugify",
]
