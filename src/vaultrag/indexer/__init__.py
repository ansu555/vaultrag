"""vaultrag.indexer — vault scan + manifest diff + secret guard; reindex orchestration.

Step 3 (done): :mod:`~vaultrag.indexer.scan` walks the vault, hashes files, applies
``.ragignore`` + the built-in :mod:`~vaultrag.indexer.secret_guard`, and diffs against
the ``documents`` manifest. Step 8 (done): :mod:`~vaultrag.indexer.pipeline` consumes the
:class:`~vaultrag.indexer.scan.ScanDiff` and drives parse → chunk → embed → upsert via
:func:`~vaultrag.indexer.pipeline.reindex_vault`.

``pipeline`` is deliberately **not** re-exported here (import it directly, e.g.
``from vaultrag.indexer.pipeline import reindex_vault``): it depends on ``parsing``,
``chunking``, ``embedding``, ``index`` and ``store``, while ``parsing.markdown`` depends
back on this package's own :mod:`~vaultrag.indexer.secret_guard`. Eagerly importing
``pipeline`` here would make that a real circular import (whichever of ``indexer``/
``parsing`` loads first would fail); submodule-direct imports sidestep it since Python
only needs this package's ``__init__`` to finish, not ``pipeline``, before
``secret_guard`` is reachable.

See ``../Rag/projects/vaultrag/p0-step-plan.md``.
"""

from __future__ import annotations

from vaultrag.indexer.scan import (
    Rename,
    ScanDiff,
    ScannedFile,
    ScanStats,
    diff_manifest,
    exclusion_reason,
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
    "exclusion_reason",
    "is_secret_filename",
    "load_ignore_spec",
    "scan_for_secrets",
    "scan_vault",
    "walk_vault",
]
