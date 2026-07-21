"""Write-back failure types (P1 Stage 5).

Two distinct failure modes the write tools raise, mapped to actionable MCP ``ToolError``
messages at the boundary (:mod:`vaultrag.mcp.server`):

* :class:`WriteRefused` — the write is not allowed at all: the path escapes the vault or
  the write allowlist, trips a secret / ``.ragignore`` gate, already exists, or blows the
  size / rate caps. A *different* request might succeed; this exact one won't. Carries a
  :class:`RefusalReason` so callers (and tests) branch on the cause, not on message text.
* :class:`WriteConflict` — an optimistic-lock (``base_hash``) mismatch: the note changed
  on disk since the agent last read it. Carries the *current* hash + content so the agent
  can re-read and retry (``../Rag/projects/vaultrag/write-back-design.md``: "409 + current
  content"). It is not a refusal — the same edit succeeds once rebased on the new hash.
"""

from __future__ import annotations

from enum import StrEnum


class RefusalReason(StrEnum):
    """Why a write was refused outright (attached to :class:`WriteRefused`)."""

    disabled = "disabled"  # write tools are turned off for this server
    empty_path = "empty-path"  # blank path argument
    empty = "empty"  # nothing to write (blank title/content/updates)
    escapes_vault = "escapes-vault"  # realpath resolves outside the vault root (traversal)
    outside_write_root = "outside-write-root"  # not under any writeback_allow_roots folder
    gated = "gated"  # exclusion_reason fired (secret filename / ragignore / unsupported / pruned)
    not_markdown = "not-markdown"  # target is an indexable *binary* source (PDF/image), not a note
    not_utf8 = "not-utf8"  # edit target isn't decodable UTF-8 (a rewrite would lose bytes)
    secret = "secret"  # scan_for_secrets found key material in the content
    exists = "exists"  # write_note target already exists (no silent overwrite)
    not_found = "not-found"  # append/patch target note does not exist
    heading_not_found = "heading-not-found"  # append_section heading not in the note
    too_large = "too-large"  # exceeds writeback_max_bytes
    rate_limited = "rate-limited"  # exceeds writeback_max_writes_per_minute


class WriteError(Exception):
    """Base for every write-back failure."""


class WriteRefused(WriteError):
    """The write is not permitted. ``reason`` is machine-checkable; ``str(self)`` is for humans."""

    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class WriteConflict(WriteError):
    """A ``base_hash`` mismatch — the note changed since the agent last read it.

    Carries the current on-disk ``hash`` and ``text`` so the boundary can hand the agent
    exactly what it needs to rebase and retry, without a second round-trip.
    """

    def __init__(self, path: str, current_hash: str, current_text: str) -> None:
        super().__init__(f"{path!r} changed on disk since base_hash was taken")
        self.path = path
        self.current_hash = current_hash
        self.current_text = current_text
