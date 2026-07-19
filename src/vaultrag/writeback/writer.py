"""``NoteWriter`` — the agent write-back mechanism (P1 Stage 5), one class behind the
three MCP write tools.

Every accepted write runs the same gauntlet, in this order, so a write can never land
what the read side would refuse to serve (repo ``CLAUDE.md`` §6;
``../Rag/projects/vaultrag/write-back-design.md``):

1. **Containment** — the target realpath (``resolve()`` first, so symlinks can't tunnel
   out) must stay under the vault root: path traversal is refused.
2. **Write allowlist** — it must sit under one of ``allow_roots`` (default just
   ``agent-inbox/``); a per-vault config opens more (e.g. ``decisions/``). This is the
   configurable half of "default write root ``agent-inbox/``".
3. **Index gates** — the *same* :func:`~vaultrag.indexer.scan.exclusion_reason` (secret
   filename / ``.ragignore`` / unsupported suffix / pruned dir) the scanner applies, and
   the content-level :func:`~vaultrag.indexer.secret_guard.scan_for_secrets`. A note whose
   *content* is key material is refused — unwritable **and** (since ``ingest_path`` re-gates)
   never indexed. Writes are markdown-only, same as the P0 index scope.
4. **Size + rate caps** — a runaway note or a looping agent is refused, not indexed.
5. **Optimistic lock** — ``patch_frontmatter`` requires the ``base_hash`` the agent last
   saw; a mismatch is a :class:`~vaultrag.writeback.errors.WriteConflict` carrying the
   current hash + content, so the agent re-reads and retries (no silent overwrite).
   ``append_section`` is additive (it inserts at a section's end), so it takes no lock —
   two appends both survive — matching the ``reference-mcp-tools.md`` signatures.

Then it **writes**, **git-commits** (best-effort audit), and runs a **synchronous
:func:`~vaultrag.indexer.pipeline.ingest_path`** so the note is retrievable in the same
session — the memory loop the whole stage exists to close. The write is durable in the
vault (+ git) before indexing; if the index lags (Qdrant down), the note is still safely
written and a :class:`~vaultrag.models.WriteResult` says so (``indexed=False`` + warning).

Thread-safe: FastMCP runs tools in worker threads, so the accept→write→commit→ingest
critical section is serialized under one lock (also what makes the sliding-window rate
cap correct). Write-back is not a hot path, so serializing it is the simple, safe choice.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
import yaml
from blake3 import blake3

from vaultrag.embedding.base import EmbeddingError
from vaultrag.index.base import VectorStoreError
from vaultrag.indexer.pipeline import IngestContext, IngestStatus, ingest_path
from vaultrag.indexer.scan import exclusion_reason, load_ignore_spec
from vaultrag.indexer.secret_guard import scan_for_secrets
from vaultrag.models import BlockType, WriteResult
from vaultrag.parsing import heading_section, parse_document
from vaultrag.parsing.frontmatter import split_frontmatter
from vaultrag.retrieval.cite import obsidian_uri
from vaultrag.writeback.errors import RefusalReason, WriteConflict, WriteRefused

log = structlog.get_logger(__name__)

DEFAULT_AUTHOR = "claude-code"
DEFAULT_ALLOW_ROOTS: tuple[str, ...] = ("agent-inbox",)
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_WRITES_PER_MINUTE = 30
_RATE_WINDOW_SECONDS = 60.0
_HEADING_LIST_LIMIT = 30  # how many headings a "heading not found" error names back
_MAX_SLUG = 120  # bound a generated filename length

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_Action = Literal["created", "appended", "patched"]


def slugify(title: str) -> str:
    """Title → a filesystem-safe, lowercase, hyphenated note slug (bounded length)."""
    return _SLUG_RE.sub("-", title.strip().lower()).strip("-")[:_MAX_SLUG].strip("-")


class NoteWriter:
    """Create/edit vault notes through VaultRAG with the write guardrails enforced.

    Built once per served project at the composition root (the CLI) around an
    :class:`~vaultrag.indexer.pipeline.IngestContext`, whose stores/embedder it reuses for
    the synchronous post-write reindex. The three public methods back the MCP tools.
    """

    def __init__(
        self,
        ctx: IngestContext,
        *,
        author: str = DEFAULT_AUTHOR,
        allow_roots: Sequence[str] = DEFAULT_ALLOW_ROOTS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_writes_per_minute: int = DEFAULT_MAX_WRITES_PER_MINUTE,
        git_enabled: bool = True,
        vault_name: str | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        # Resolve the root so containment via is_relative_to matches watchdog/scan behavior.
        self._ctx = IngestContext(
            project_id=ctx.project_id,
            vault_path=ctx.vault_path.resolve(),
            store=ctx.store,
            vector_store=ctx.vector_store,
            embedder=ctx.embedder,
        )
        self._author = author
        self._allow_roots = tuple(cleaned for r in allow_roots if (cleaned := r.strip().strip("/")))
        self._max_bytes = max_bytes
        self._max_writes = max_writes_per_minute
        self._git_enabled = git_enabled
        self._vault_name = vault_name
        self._now = now
        self._lock = threading.Lock()
        self._writes: deque[float] = deque()  # accepted-write timestamps for the rate window

    # --- public tools ---------------------------------------------------------
    def write_note(
        self,
        *,
        title: str,
        content: str,
        folder: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> WriteResult:
        """Create a new note (default folder ``agent-inbox/``) with provenance frontmatter.

        Refuses to overwrite an existing note — pick another title or use
        ``append_section``/``patch_frontmatter``.
        """
        if not self._allow_roots:
            raise WriteRefused(
                RefusalReason.disabled, "write-back is disabled (no write roots configured)"
            )
        title = (title or "").strip()
        if not title:
            raise WriteRefused(RefusalReason.empty, "title must be a non-empty string")
        if not (content or "").strip():
            raise WriteRefused(RefusalReason.empty, "content must be a non-empty note body")
        slug = slugify(title)
        if not slug:
            raise WriteRefused(
                RefusalReason.empty, f"title {title!r} has no filename-safe characters"
            )
        folder = (folder or self._allow_roots[0]).strip("/")
        rel = self._contain(f"{folder}/{slug}.md" if folder else f"{slug}.md")
        if (self._ctx.vault_path / rel).exists():
            raise WriteRefused(
                RefusalReason.exists,
                f"{rel!r} already exists — no silent overwrite; use append_section/"
                "patch_frontmatter, or choose a different title",
            )
        return self._commit_write(
            rel, self._render_new_note(title, content, tags), action="created"
        )

    def append_section(self, *, path: str, heading: str, content: str) -> WriteResult:
        """Append ``content`` at the end of ``heading``'s section in an existing note.

        Additive and un-locked: the block is inserted just before the next same-or-higher
        heading (or at end of file), so concurrent appends don't clobber each other.
        """
        if not (heading or "").strip():
            raise WriteRefused(RefusalReason.empty, "heading must be non-empty")
        if not (content or "").strip():
            raise WriteRefused(RefusalReason.empty, "content must be a non-empty block to append")
        rel = self._contain(path)
        abs_path = self._ctx.vault_path / rel
        if not abs_path.is_file():
            raise WriteRefused(
                RefusalReason.not_found,
                f"note not found: {rel!r} — append_section edits an existing note",
            )
        current = abs_path.read_text(encoding="utf-8", errors="replace")
        document = parse_document(path=rel, raw_text=current)
        total = len(current.splitlines())
        span = heading_section(document, heading, total)
        if span is None:
            names = [b.text for b in document.blocks if b.type is BlockType.heading]
            available = ", ".join(names[:_HEADING_LIST_LIMIT]) or "(none)"
            raise WriteRefused(
                RefusalReason.heading_not_found,
                f"heading {heading!r} not found in {rel!r}; headings here: {available}",
            )
        _start, end, _block = span
        return self._commit_write(rel, _append_to_section(current, end, content), action="appended")

    def patch_frontmatter(
        self, *, path: str, updates: Mapping[str, Any], base_hash: str
    ) -> WriteResult:
        """Set/overwrite frontmatter keys on an existing note under an optimistic lock.

        ``base_hash`` must equal the note's current content hash (the one a prior read/
        write returned); a mismatch raises :class:`WriteConflict` with the live hash +
        content so the agent can rebase and retry. Body text is preserved verbatim.
        """
        if not updates:
            raise WriteRefused(
                RefusalReason.empty, "updates must be a non-empty object of frontmatter keys to set"
            )
        rel = self._contain(path)
        abs_path = self._ctx.vault_path / rel
        if not abs_path.is_file():
            raise WriteRefused(RefusalReason.not_found, f"note not found: {rel!r}")
        data = abs_path.read_bytes()
        current_hash = blake3(data).hexdigest()
        if base_hash != current_hash:
            raise WriteConflict(rel, current_hash, data.decode("utf-8", errors="replace"))
        existing_meta, body = split_frontmatter(rel, data.decode("utf-8", errors="replace"))
        merged: dict[str, Any] = {**existing_meta, **dict(updates)}
        sep = "" if body.startswith("\n") else "\n"
        text = f"{_render_frontmatter(merged)}{sep}{body}"
        if not text.endswith("\n"):
            text += "\n"
        return self._commit_write(rel, text, action="patched")

    # --- gates ----------------------------------------------------------------
    def _contain(self, path: str) -> str:
        """Resolve ``path`` to a vault-relative POSIX rel, enforcing every write gate."""
        raw = (path or "").strip()
        if not raw:
            raise WriteRefused(
                RefusalReason.empty_path, "path must be a non-empty vault-relative path"
            )
        candidate = (self._ctx.vault_path / raw).resolve()
        if not candidate.is_relative_to(self._ctx.vault_path):
            raise WriteRefused(
                RefusalReason.escapes_vault,
                f"path {raw!r} escapes the vault — writes must stay under the vault root",
            )
        rel = candidate.relative_to(self._ctx.vault_path).as_posix()
        if not self._under_allow_root(rel):
            allowed = ", ".join(self._allow_roots) or "(none)"
            raise WriteRefused(
                RefusalReason.outside_write_root,
                f"{rel!r} is outside the write allowlist ({allowed}); "
                "writes are restricted to those folders",
            )
        reason = exclusion_reason(rel, ignore=load_ignore_spec(self._ctx.vault_path))
        if reason is not None:
            if reason == "secret-filename":
                log.warning("writeback.blocked", path=rel, rule="filename")
            raise WriteRefused(
                RefusalReason.gated,
                f"{rel!r} is not writable ({reason}) — it is excluded from the index, "
                "so writing it is refused",
            )
        return rel

    def _under_allow_root(self, rel: str) -> bool:
        return any(rel == root or rel.startswith(f"{root}/") for root in self._allow_roots)

    # --- the shared write tail: caps → write → commit → reindex ----------------
    def _commit_write(self, rel: str, text: str, *, action: _Action) -> WriteResult:
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise WriteRefused(
                RefusalReason.too_large,
                f"note is {len(encoded)} bytes; the limit is {self._max_bytes} "
                "(raise VAULTRAG_WRITEBACK_MAX_BYTES to allow larger notes)",
            )
        rule = scan_for_secrets(text)
        if rule is not None:
            log.warning("writeback.secret_blocked", path=rel, rule=rule)
            raise WriteRefused(
                RefusalReason.secret,
                f"refusing to write {rel!r}: content matches the built-in secret guard "
                f"({rule}) — secrets never enter the vault or its index",
            )
        content_hash = blake3(encoded).hexdigest()
        with self._lock:
            self._enforce_rate()
            abs_path = self._ctx.vault_path / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(text, encoding="utf-8")
            self._writes.append(self._now())
            committed, sha = self._commit_git(rel, action)
            indexed, chunks, warning = self._reindex(rel)
        uri = obsidian_uri(self._vault_name, rel) if self._vault_name else None
        log.info(
            "writeback.wrote",
            path=rel,
            action=action,
            bytes=len(encoded),
            committed=committed,
            indexed=indexed,
            chunks=chunks,
        )
        return WriteResult(
            path=rel,
            action=action,
            hash=content_hash,
            bytes_written=len(encoded),
            committed=committed,
            commit=sha,
            indexed=indexed,
            chunks=chunks,
            uri=uri,
            warning=warning,
        )

    def _enforce_rate(self) -> None:
        """Refuse if too many writes landed in the trailing minute (call under the lock)."""
        cutoff = self._now() - _RATE_WINDOW_SECONDS
        while self._writes and self._writes[0] < cutoff:
            self._writes.popleft()
        if len(self._writes) >= self._max_writes:
            raise WriteRefused(
                RefusalReason.rate_limited,
                f"write rate limit reached ({self._max_writes}/min); wait a moment and retry",
            )

    def _commit_git(self, rel: str, action: _Action) -> tuple[bool, str | None]:
        if not self._git_enabled:
            return False, None
        # Imported lazily so a git-free deployment never pays the subprocess import cost and
        # writeback stays usable if the git module is trimmed.
        from vaultrag.writeback.git import commit_paths

        sha = commit_paths(
            self._ctx.vault_path,
            [rel],
            message=f"agent({self._author}): {action} {rel}",
            author=self._author,
        )
        return sha is not None, sha

    def _reindex(self, rel: str) -> tuple[bool, int, str | None]:
        """(Re)index the just-written note now; degrade gracefully if the index is down."""
        try:
            outcome = ingest_path(self._ctx, rel, ignore=load_ignore_spec(self._ctx.vault_path))
        except (VectorStoreError, EmbeddingError, OSError) as exc:
            log.warning("writeback.index_failed", path=rel, error=str(exc))
            return (
                False,
                0,
                f"written and committed, but indexing failed ({exc}); "
                "a later reindex/watch will pick it up",
            )
        searchable = outcome.status in (IngestStatus.indexed, IngestStatus.unchanged)
        warning = (
            None if searchable else f"written but not indexed (reconcile status: {outcome.status})"
        )
        return searchable, outcome.chunks, warning

    # --- rendering ------------------------------------------------------------
    def _render_new_note(self, title: str, content: str, tags: Sequence[str] | None) -> str:
        created = datetime.fromtimestamp(self._now(), tz=UTC).date().isoformat()
        meta: dict[str, Any] = {
            "title": title,
            "author": self._author,
            "via": "vaultrag",
            "created": created,
        }
        if tags:
            cleaned = [str(t).strip() for t in tags if str(t).strip()]
            if cleaned:
                meta["tags"] = cleaned
        body = content.strip("\n")
        return f"{_render_frontmatter(meta)}\n{body}\n"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _render_frontmatter(meta: Mapping[str, Any]) -> str:
    """Serialize a metadata mapping to a ``---``-fenced YAML frontmatter block."""
    body = yaml.safe_dump(dict(meta), sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{body}---\n"


def _append_to_section(current: str, section_end: int, content: str) -> str:
    """Insert ``content`` after line ``section_end`` (1-based inclusive) of ``current``.

    Keeps one blank line on each side so the appended block reads as its own paragraph and
    the following heading isn't glued to it. Always returns text ending in a single newline.
    """
    lines = current.splitlines()
    before = lines[:section_end]
    after = lines[section_end:]
    block = content.strip("\n").split("\n")

    inserted: list[str] = []
    if before and before[-1].strip():
        inserted.append("")  # separate from the section's existing content
    inserted.extend(block)
    if after and after[0].strip():
        inserted.append("")  # separate from the next heading / following content

    text = "\n".join(before + inserted + after)
    return text if text.endswith("\n") else f"{text}\n"
