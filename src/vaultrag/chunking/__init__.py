"""vaultrag.chunking — Heading-tree chunker: parent/child chunks + contextual prefix. Step 5.

See ``chunker.py`` for the algorithm and ``../Rag/projects/vaultrag/p0-step-plan.md``.
"""

from __future__ import annotations

from vaultrag.chunking.chunker import (
    HARD_MAX_TOKENS,
    MERGE_MIN_TOKENS,
    TARGET_TOKENS,
    chunk_document,
)
from vaultrag.chunking.tokens import count_tokens

__all__ = [
    "HARD_MAX_TOKENS",
    "MERGE_MIN_TOKENS",
    "TARGET_TOKENS",
    "chunk_document",
    "count_tokens",
]
