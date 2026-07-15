"""Vault scan + manifest diff — the boot-reconcile front of the indexer (P0 Step 3).

Walk a vault, BLAKE3-hash every candidate file, drop anything ``.ragignore`` or the
built-in :mod:`~vaultrag.indexer.secret_guard` excludes, and diff the surviving
``path -> hash`` set against the ``documents`` manifest to produce a
:class:`ScanDiff` of creates / updates / deletes / renames / unchanged. A rename is a
hash match at a new path (old path gone) — Step 8's job runner uses that to move the
document's metadata without re-embedding its vectors (arch §4;
``../Rag/projects/vaultrag/ingestion-pipeline.md``).

Scope boundary: this step *produces* the diff. Enqueuing jobs, parsing, and applying
renames as metadata-only updates belong to Step 8 (reindex pipeline), which owns the
worker pool and the parser. :func:`diff_manifest` is a pure function so the diff is
testable without touching a filesystem.

The index is a derived cache; the vault is truth (``CLAUDE.md`` §6). A scan of a bare
vault against an empty manifest must classify every indexable file as a create — that
is what makes ``vaultrag reindex`` a full rebuild.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from blake3 import blake3
from pathspec import PathSpec
from pathspec.patterns.gitignore.spec import GitIgnoreSpecPattern

from vaultrag.indexer.secret_guard import is_secret_filename, scan_for_secrets

log = structlog.get_logger(__name__)

# P0 indexes markdown only; other formats (PDF, Excalidraw, images) arrive in P2.
# Non-indexable extensions are skipped quietly — they are not "excluded", just not
# yet supported — so scan stats stay meaningful.
_INDEXABLE_SUFFIXES = frozenset({".md", ".markdown"})

# Directory names always pruned from the walk regardless of .ragignore, so we never
# descend huge machine-managed trees. .ragignore can add more but not remove these.
_ALWAYS_PRUNE_DIRS = frozenset({".git", ".obsidian", ".trash", ".smart-env", "node_modules"})

_HASH_CHUNK = 1 << 20  # 1 MiB streaming read; vault notes are small, PDFs (P2) are not.


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScannedFile:
    """One indexable file found on disk. ``path`` is vault-relative, POSIX form."""

    path: str
    hash: str  # BLAKE3 hex of the file bytes
    size: int
    mtime: int  # epoch seconds


@dataclass(frozen=True, slots=True)
class Rename:
    """A file that moved: same content hash, new path, old path gone."""

    old_path: str
    new_path: str
    hash: str


@dataclass(frozen=True, slots=True)
class ScanStats:
    """Observability counters for one walk (never carries file content)."""

    seen: int = 0  # files visited on disk (files under pruned dirs are never visited)
    indexable: int = 0  # markdown files that passed every gate
    ignored: int = 0  # individual files dropped by .ragignore (dir-level prunes not counted)
    secrets_blocked: int = 0  # dropped by the built-in secret guard (filename or content)
    unsupported: int = 0  # non-markdown (P0 scope)


@dataclass(frozen=True, slots=True)
class ScanDiff:
    """The reconcile plan: what changed between disk and the manifest.

    ``creates``/``updates`` carry the fresh :class:`ScannedFile` (new hash);
    ``deletes`` carry vault-relative paths that vanished; ``renames`` pair an old
    path to a new one at identical content; ``unchanged`` are paths whose hash
    already matches the manifest (nothing to do).
    """

    creates: list[ScannedFile] = field(default_factory=list)
    updates: list[ScannedFile] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    renames: list[Rename] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when disk and manifest already agree (no work to enqueue)."""
        return not (self.creates or self.updates or self.deletes or self.renames)


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------
def load_ignore_spec(vault_path: Path) -> PathSpec[GitIgnoreSpecPattern]:
    """Compile ``<vault>/.ragignore`` (gitignore syntax) into a :class:`PathSpec`.

    Missing file → an empty spec (nothing extra ignored). The built-in secret guard
    is enforced separately and is *not* sourced from this file, so a user editing
    ``.ragignore`` can never widen what is indexable past the guard (``CLAUDE.md`` §6).
    """
    ragignore = vault_path / ".ragignore"
    if not ragignore.is_file():
        return PathSpec.from_lines(GitIgnoreSpecPattern, [])
    lines = ragignore.read_text(encoding="utf-8", errors="replace").splitlines()
    return PathSpec.from_lines(GitIgnoreSpecPattern, lines)


def _hash_file(abs_path: Path) -> str:
    """Stream a file through BLAKE3 and return the hex digest."""
    hasher = blake3()
    with abs_path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def exclusion_reason(
    rel_path: str, *, ignore: PathSpec[GitIgnoreSpecPattern]
) -> str | None:
    """Why ``rel_path`` may not be indexed *or served*, or ``None`` if it is fair game.

    The single source of truth for the path-level gates, so every consumer refuses the
    same set: :func:`walk_vault` applies it while scanning, and the MCP ``open`` tool
    applies it before serving raw note text — if the two ever diverged, ``open`` would
    hand an agent exactly the files indexing withholds (CLAUDE.md §6). Content-level
    gating (:func:`~vaultrag.indexer.secret_guard.scan_for_secrets` on the file text)
    is the callers' second step — it needs the bytes, this needs only the path.

    Order matters and mirrors the walk: a file under an always-pruned directory is
    never even visited there; a secret filename is counted as secret-blocked even when
    ``.ragignore`` would also drop it.
    """
    parts = rel_path.split("/")
    if any(part in _ALWAYS_PRUNE_DIRS for part in parts[:-1]):
        return "pruned-dir"
    if is_secret_filename(rel_path):
        return "secret-filename"
    if ignore.match_file(rel_path):
        return "ragignore"
    if Path(rel_path).suffix.lower() not in _INDEXABLE_SUFFIXES:
        return "unsupported"
    return None


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------
def walk_vault(
    vault_path: Path, ignore: PathSpec[GitIgnoreSpecPattern] | None = None
) -> tuple[list[ScannedFile], ScanStats]:
    """Walk ``vault_path`` and return (indexable files, stats).

    A file is indexable when it is markdown, not ``.ragignore``-excluded, and passes
    the built-in secret guard (filename *and* content). Symlinks are not followed, so
    the walk cannot escape the vault or loop. Emitted paths are vault-relative POSIX.
    """
    vault_path = vault_path.resolve()
    spec = ignore if ignore is not None else load_ignore_spec(vault_path)
    files: list[ScannedFile] = []
    seen = indexable = ignored = secrets_blocked = unsupported = 0

    for dirpath, dirnames, filenames in os.walk(vault_path, followlinks=False):
        rel_dir = os.path.relpath(dirpath, vault_path)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        # Prune directories in place so os.walk never descends them. Match .ragignore
        # against the dir path with a trailing slash (gitignore dir semantics).
        kept: list[str] = []
        for d in dirnames:
            if d in _ALWAYS_PRUNE_DIRS:
                continue
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if spec.match_file(f"{rel}/"):
                continue
            kept.append(d)
        dirnames[:] = kept

        for fname in filenames:
            seen += 1
            rel = f"{rel_dir}/{fname}" if rel_dir else fname
            abs_path = Path(dirpath) / fname

            # One shared gate for scan and serve (see exclusion_reason): the walk only
            # translates each reason into its stats counter. "pruned-dir" cannot fire
            # here (those directories were pruned above) but stays handled defensively.
            reason = exclusion_reason(rel, ignore=spec)
            if reason == "secret-filename":
                secrets_blocked += 1
                log.warning("scan.secret_blocked", path=rel, rule="filename")
                continue
            if reason == "ragignore":
                ignored += 1
                continue
            if reason == "unsupported":
                unsupported += 1
                continue
            if reason is not None:
                continue

            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.warning("scan.read_failed", path=rel, error=str(exc))
                continue

            rule = scan_for_secrets(text)
            if rule is not None:
                # Conservative at this boundary: a note carrying live-looking key
                # material is withheld from indexing whole. (Step 4 parsing will do
                # line-level redaction; for P0 the file simply never enters the index.)
                secrets_blocked += 1
                log.warning("scan.secret_blocked", path=rel, rule=rule)
                continue

            try:
                stat = abs_path.stat()
                digest = _hash_file(abs_path)
            except OSError as exc:
                log.warning("scan.hash_failed", path=rel, error=str(exc))
                continue

            files.append(
                ScannedFile(path=rel, hash=digest, size=stat.st_size, mtime=int(stat.st_mtime))
            )
            indexable += 1

    stats = ScanStats(
        seen=seen,
        indexable=indexable,
        ignored=ignored,
        secrets_blocked=secrets_blocked,
        unsupported=unsupported,
    )
    return files, stats


# ---------------------------------------------------------------------------
# Diff (pure — no filesystem)
# ---------------------------------------------------------------------------
def diff_manifest(
    scanned: Sequence[ScannedFile],
    manifest: Mapping[str, str | None],
) -> ScanDiff:
    """Diff on-disk files against the stored ``path -> hash`` manifest.

    * present on disk, absent from manifest → **create**
    * present in both, hash differs → **update**
    * present in both, hash equal → **unchanged**
    * in manifest, absent from disk → **delete**

    Then renames are recovered: a create whose hash equals a delete's hash is folded
    into :attr:`ScanDiff.renames` (old_path → new_path), and both are removed from the
    create/delete lists so Step 8 moves the document instead of re-embedding it. Pure
    function — feed it a hand-built manifest to test the classifier without a vault.
    """
    by_path = {f.path: f for f in scanned}
    creates: list[ScannedFile] = []
    updates: list[ScannedFile] = []
    unchanged: list[str] = []

    for f in scanned:
        prior = manifest.get(f.path, _MISSING)
        if prior is _MISSING:
            creates.append(f)
        elif prior == f.hash:
            unchanged.append(f.path)
        else:
            updates.append(f)

    deletes = [path for path in manifest if path not in by_path]

    renames = _extract_renames(creates, deletes, manifest)
    return ScanDiff(
        creates=creates,
        updates=updates,
        deletes=deletes,
        renames=renames,
        unchanged=unchanged,
    )


# Sentinel: distinguishes "path absent from manifest" from "path present with NULL
# hash" (a half-recorded doc), which must be treated as a change, not a create-skip.
_MISSING = object()


def _extract_renames(
    creates: list[ScannedFile],
    deletes: list[str],
    manifest: Mapping[str, str | None],
) -> list[Rename]:
    """Fold create/delete pairs with matching content hash into renames, in place.

    Mutates ``creates`` and ``deletes``, removing any path folded into a rename.
    Pairs one-to-one and greedily so N identical files that all move are matched
    without cross-pairing.
    """
    deleted_by_hash: dict[str, list[str]] = {}
    for path in deletes:
        h = manifest.get(path)
        if h:  # skip NULL/empty manifest hashes — nothing to match against
            deleted_by_hash.setdefault(h, []).append(path)

    if not deleted_by_hash:
        return []

    renames: list[Rename] = []
    consumed_creates: set[str] = set()
    consumed_deletes: set[str] = set()
    for f in creates:
        candidates = deleted_by_hash.get(f.hash)
        if candidates:
            old_path = candidates.pop(0)
            renames.append(Rename(old_path=old_path, new_path=f.path, hash=f.hash))
            consumed_creates.add(f.path)
            consumed_deletes.add(old_path)

    if consumed_creates:
        creates[:] = [f for f in creates if f.path not in consumed_creates]
        deletes[:] = [p for p in deletes if p not in consumed_deletes]
    return renames


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------
def scan_vault(vault_path: Path, manifest: Mapping[str, str | None]) -> tuple[ScanDiff, ScanStats]:
    """Walk ``vault_path`` and diff it against ``manifest`` in one call.

    The convenience wrapper Step 8 will call: pass ``store.document_manifest(project)``
    as ``manifest``. Returns the reconcile plan plus walk stats for logging/metrics.
    """
    files, stats = walk_vault(vault_path)
    diff = diff_manifest(files, manifest)
    log.info(
        "scan.complete",
        vault=str(vault_path),
        indexable=stats.indexable,
        ignored=stats.ignored,
        secrets_blocked=stats.secrets_blocked,
        creates=len(diff.creates),
        updates=len(diff.updates),
        deletes=len(diff.deletes),
        renames=len(diff.renames),
        unchanged=len(diff.unchanged),
    )
    return diff, stats


def iter_indexable_suffixes() -> Iterable[str]:
    """The file suffixes scan currently treats as indexable (P0: markdown)."""
    return _INDEXABLE_SUFFIXES
