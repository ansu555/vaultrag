"""vaultrag.indexer — vault scan + manifest diff + secret guard; reindex orchestration.

Step 3 (done): :mod:`~vaultrag.indexer.scan` walks the vault, hashes files, applies
``.ragignore`` + the built-in :mod:`~vaultrag.indexer.secret_guard`, and diffs against
the ``documents`` manifest. Step 8 will add the reindex pipeline (parse → chunk → embed
→ upsert) that consumes the :class:`~vaultrag.indexer.scan.ScanDiff`.
See ``../Rag/projects/vaultrag/p0-step-plan.md``.
"""

from __future__ import annotations

from vaultrag.indexer.scan import (
    Rename,
    ScanDiff,
    ScannedFile,
    ScanStats,
    diff_manifest,
    load_ignore_spec,
    scan_vault,
    walk_vault,
)
from vaultrag.indexer.secret_guard import is_secret_filename, scan_for_secrets

__all__ = [
    "Rename",
    "ScanDiff",
    "ScanStats",
    "ScannedFile",
    "diff_manifest",
    "is_secret_filename",
    "load_ignore_spec",
    "scan_for_secrets",
    "scan_vault",
    "walk_vault",
]
