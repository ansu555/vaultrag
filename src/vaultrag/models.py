"""Core data models — the one normalized shape every stage shares.

Parsing (Step 4) produces a :class:`Document`; chunking (Step 5) turns it into
:class:`Chunk` children; embedding/indexing (Steps 6-8) persist those; retrieval
(Step 9) and MCP (Step 10) return :class:`SearchResult` / :class:`Snippet`. Keeping
this shape fixed is ordering constraint #1 (arch §5): every later stage depends on it.

Field provenance:
* Qdrant payload per point — ``../Rag/reference/reference-metadata-schema.md`` (arch §7)
* SQLite tables (documents, chunks, links, tags) — ``../Rag/reference/reference-database-schema.md``
* Block/Chunk semantics — ``../Rag/projects/vaultrag/{parsing,chunking}-*.md`` (arch §5-§6)
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Point-ID derivation (Qdrant point id == SQLite chunk id).
# uuid5(project, path, chunk_ix) makes upserts natural and delete-by-doc a filter
# (reference-metadata-schema.md). Use a unit-separator delimiter so paths that
# contain ':' can't collide.
# ---------------------------------------------------------------------------
_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "vaultrag://chunks")
_DOC_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "vaultrag://documents")
_SEP = "\x1f"


def chunk_point_id(project_id: str, path: str, chunk_ix: int) -> str:
    """Deterministic chunk/point id for ``(project, path, chunk_ix)``."""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{project_id}{_SEP}{path}{_SEP}{chunk_ix}"))


def document_id(project_id: str, path: str) -> str:
    """Deterministic document id (SQLite ``documents.id`` == payload ``doc_id``).

    Derived from ``(project, path)`` so the manifest row, every child chunk's
    ``doc_id``, and delete-by-doc filters all agree without a DB round-trip.
    """
    return str(uuid.uuid5(_DOC_NAMESPACE, f"{project_id}{_SEP}{path}"))


class _Base(BaseModel):
    """Shared config: reject unknown fields so typos surface immediately."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class DocType(StrEnum):
    """Document kind (Qdrant payload ``type``). P0 handles ``note`` only."""

    note = "note"
    pdf = "pdf"
    excalidraw = "excalidraw"
    image = "image"


class BlockType(StrEnum):
    """A parsed structural unit of a document."""

    heading = "heading"
    paragraph = "paragraph"
    code = "code"
    table = "table"
    callout = "callout"
    list = "list"
    quote = "quote"
    other = "other"


class LinkKind(StrEnum):
    """Edge kind in the wikilink graph (SQLite ``links.kind``)."""

    wikilink = "wikilink"  # [[Note]] / [[Note#Heading]] / [[Note#^block]]
    embed = "embed"  # ![[...]] transclusion — target indexed once, never duplicated


# ---------------------------------------------------------------------------
# Document-level models (output of parsing, Step 4)
# ---------------------------------------------------------------------------
class Block(_Base):
    """One structural block of a document, in source order.

    Atomic blocks (code/table/callout) are never split by the chunker (arch §6).
    """

    type: BlockType
    text: str
    level: int | None = None  # heading level 1..6 (when type == heading)
    lang: str | None = None  # code-fence language (when type == code)
    callout_type: str | None = None  # e.g. "note", "warning" (when type == callout)
    heading_path: list[str] = Field(default_factory=list)  # H1 ▸ H2 trail above this block
    line_start: int | None = None  # 1-based source line span, for anchors/citations
    line_end: int | None = None

    @property
    def is_atomic(self) -> bool:
        """True if the chunker must keep this block whole."""
        return self.type in {BlockType.code, BlockType.table, BlockType.callout}


class Link(_Base):
    """A wikilink or embed edge, as written (unresolved at parse time)."""

    kind: LinkKind
    target: str  # raw target as written — SQLite links.dst_raw
    anchor: str | None = None  # heading slug or ^block id (the part after '#')
    alias: str | None = None  # display text in [[Note|alias]]


class DocumentMetadata(_Base):
    """Normalized per-document metadata (SQLite ``documents`` + payload keys)."""

    path: str  # vault-relative path, POSIX form — the stable document key
    title: str
    type: DocType = DocType.note
    hash: str | None = None  # BLAKE3 content hash (set by scan, Step 3)
    tags: list[str] = Field(default_factory=list)  # nested a/b expanded to [a, a/b]
    aliases: list[str] = Field(default_factory=list)
    # Custom frontmatter + dataview keys preserved verbatim. Genuinely dynamic YAML
    # values → Any is the honest type at this data boundary (not a typing shortcut).
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    created_ts: int | None = None  # epoch seconds
    modified_ts: int | None = None


class Document(_Base):
    """A parsed document: normalized metadata + ordered blocks + link edges.

    Format-agnostic by design so chunking/embedding/indexing never branch on input
    type (arch §5). ``Document{blocks, metadata, links}`` — the P0-plan Step 1 shape.
    """

    metadata: DocumentMetadata
    blocks: list[Block] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)

    @property
    def path(self) -> str:
        return self.metadata.path


# ---------------------------------------------------------------------------
# Chunk (output of chunking, Step 5 — the indexed unit)
# ---------------------------------------------------------------------------
class Chunk(_Base):
    """An indexed child chunk: contiguous blocks under the lowest heading (arch §6).

    ``text`` is the raw chunk body; ``contextual_prefix`` is the
    «Project ▸ folder/note ▸ H1 ▸ H2» + tags header prepended *only* for embedding
    (free contextual retrieval). :attr:`embed_text` is what the embedder receives.
    """

    # identity / location
    project_id: str
    doc_id: str  # parent document key (path or SQLite documents.id)
    path: str
    ix: int  # chunk index within the document (chunk_ix)

    # content
    text: str
    contextual_prefix: str = ""
    heading_path: list[str] = Field(default_factory=list)
    anchor: str | None = None  # heading slug | ^block — for open()/citations

    # payload / filters (reference-metadata-schema.md)
    tags: list[str] = Field(default_factory=list)
    langs: list[str] = Field(default_factory=list)
    has_code: bool = False
    type: DocType = DocType.note
    page: int | None = None  # PDF page (P2)
    created_ts: int | None = None
    modified_ts: int | None = None

    # chunking bookkeeping (SQLite chunks table)
    chunk_hash: str  # BLAKE3 of text — embed-cache + dedupe key
    tokens: int
    parent_id: str | None = None  # enclosing H1/H2 section, for parent-swap (P1)
    prev_id: str | None = None
    next_id: str | None = None

    @property
    def embed_text(self) -> str:
        """Text handed to the embedder: contextual prefix + body."""
        prefix = self.contextual_prefix.strip()
        return f"{prefix}\n\n{self.text}" if prefix else self.text

    @property
    def point_id(self) -> str:
        """Deterministic Qdrant point id / SQLite chunk id for this chunk."""
        return chunk_point_id(self.project_id, self.path, self.ix)


# ---------------------------------------------------------------------------
# Return contract (retrieval Step 9 + MCP search Step 10)
# ---------------------------------------------------------------------------
class Snippet(_Base):
    """One cited result. The agent gets snippets, not answers (principle #4).

    ``path`` + ``anchor`` (or ``heading_path``) are enough to re-open the exact
    section via ``open(...)`` and, later, to build an ``obsidian://`` link (P1).
    """

    path: str
    title: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    anchor: str | None = None
    text: str
    score: float
    tokens: int
    doc_id: str | None = None
    chunk_id: str | None = None


class SearchResult(_Base):
    """The ``search`` response: ranked, budget-bounded cited snippets."""

    query: str
    project_id: str
    snippets: list[Snippet] = Field(default_factory=list)
    total_tokens: int = 0
    truncated: bool = False  # True if budget_tokens forced dropping lower-ranked hits
