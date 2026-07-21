"""Core data models — the one normalized shape every stage shares.

Parsing (Step 4) produces a :class:`Document`; chunking (Step 5) turns it into
:class:`Chunk` children; embedding/indexing (Steps 6-8) persist those; retrieval
(Step 9) and MCP (Step 10) return :class:`SearchResult` / :class:`Snippet` /
:class:`OpenResult`. Keeping this shape fixed is ordering constraint #1 (arch §5):
every later stage depends on it.

Field provenance:
* Qdrant payload per point — ``../Rag/reference/reference-metadata-schema.md`` (arch §7)
* SQLite tables (documents, chunks, links, tags) — ``../Rag/reference/reference-database-schema.md``
* Block/Chunk semantics — ``../Rag/projects/vaultrag/{parsing,chunking}-*.md`` (arch §5-§6)
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Literal

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
    # A node label drawn in a diagram (Mermaid fence / Excalidraw scene), P2 Stage 5.
    # Anchored on the *containing* note, not node-to-node: `links.src_doc` is the
    # document whose edges `replace_links` owns, so an edge sourced anywhere else
    # could never be revoked when that note changes (parsing/mermaid.py).
    diagram = "diagram"


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
    page: int | None = None  # 1-based source page (PDFs, P2) — propagates to Chunk.page

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
    line_start: int | None = None  # 1-based source line span (min/max over the chunk's blocks)
    line_end: int | None = None  # → citation line range (retrieval stage 8)

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

    Carries everything retrieval stage 8 cites (arch §10-stage8): ``path`` + ``anchor``
    (or ``heading_path``) to re-open the exact section via ``open(...)``; ``line_start`` /
    ``line_end`` (the source line range); and ``uri``, a one-click ``obsidian://open``
    link into the vault (stamped only when a vault display name is configured — P1 Stage 3).
    """

    path: str
    title: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    anchor: str | None = None
    line_start: int | None = None  # 1-based source line range of the cited text
    line_end: int | None = None
    # 1-based page for PDF hits, absent for notes (P2 Stage 4). `open` cannot serve a
    # PDF's text, so this is the *only* way an agent can locate a PDF hit in the source
    # document — without it the page would reach Qdrant and stop there.
    page: int | None = None
    text: str
    score: float
    tokens: int
    uri: str | None = None  # obsidian://open?vault=…&file=… — None until a vault_name is set
    doc_id: str | None = None
    chunk_id: str | None = None


class RelatedNote(_Base):
    """One note reachable from another through the vault's own graph — **no text**.

    The unit of retrieval stage 6 and of the ``related`` tool: a pointer an agent can
    follow with ``open`` / ``search``, deliberately carrying titles + paths only. Text
    would cost ``budget_tokens`` and duplicate what a real search would return better;
    a pointer costs nothing and is what "explore outward from a hit" actually needs
    (``../Rag/projects/vaultrag/knowledge-graph-integration.md``, arch §14).

    ``relation`` says *why* this note is here, which is the part an agent reasons with:

    * ``links_to``    — the source note links **out** to this one.
    * ``backlink``    — this note links **in** to the source (who cites it).
    * ``neighbor``    — reachable in ``hops`` link-hops, either direction (2-hop only;
      1-hop always arrives as the more specific ``links_to``/``backlink``).
    * ``tag_sibling`` — no link at all: it shares tags. ``hops`` is ``None`` because a
      tag is not a hop, and ``shared_tags`` carries the (prefix-expanded) overlap count.
    """

    path: str
    title: str | None = None
    relation: Literal["links_to", "backlink", "neighbor", "tag_sibling"]
    hops: int | None = None  # link distance; None for tag siblings (not a link relation)
    shared_tags: int | None = None  # tag_sibling only — how many tags overlap
    uri: str | None = None  # obsidian://open?vault=…&file=… — None until a vault_name is set


class SearchResult(_Base):
    """The ``search`` response: ranked, budget-bounded cited snippets.

    ``related`` is retrieval stage 6's neighbor append: notes adjacent in the link graph
    to the top hits, as pointers only. It is deliberately **not** part of ``snippets`` —
    those are ranked retrieval results with text, these are unranked exploration hints —
    and it costs no ``budget_tokens``, so it never displaces a snippet.
    """

    query: str
    project_id: str
    snippets: list[Snippet] = Field(default_factory=list)
    related: list[RelatedNote] = Field(default_factory=list)
    total_tokens: int = 0
    truncated: bool = False  # True if budget_tokens forced dropping lower-ranked hits


class RelatedResult(_Base):
    """The ``related`` response: one note's graph neighborhood, as pointers.

    ``notes`` is deduped by path (a mutually-linked note appears once, under the most
    specific relation) and ordered ``links_to`` → ``backlink`` → ``neighbor`` →
    ``tag_sibling``. ``unresolved`` lists the wikilink targets this note writes that name
    no indexed document — dangling links, the vault-hygiene signal (arch §14) — so an
    agent can tell "nothing links here" apart from "the links here are broken".
    """

    path: str
    title: str | None = None
    doc_id: str
    hops: int
    notes: list[RelatedNote] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    truncated: bool = False  # True if `limit` cut the neighborhood short


class OpenResult(_Base):
    """The ``open`` response: one exact slice of a note, read straight from the vault.

    The vault is the source of truth, so this is always at least as fresh as the index
    the citation came from. ``line_start``/``line_end`` are 1-based inclusive (an empty
    slice is the canonical ``1..0``); ``total_lines`` lets an agent page a long note
    with ``lines=[start, end]`` follow-ups.
    """

    path: str
    title: str | None = None
    heading: str | None = None  # resolved heading text when a heading was requested
    line_start: int
    line_end: int
    total_lines: int
    text: str
    tokens: int
    truncated: bool = False  # hard safety cap hit — narrow with heading= or lines=


# ---------------------------------------------------------------------------
# Write contract (write-back Stage 5 — the memory loop close)
# ---------------------------------------------------------------------------
class WriteResult(_Base):
    """The response for an accepted write tool call (``write_note`` / ``append_section`` /
    ``patch_frontmatter``).

    Reports what actually landed: the note ``path`` and its new content ``hash`` (pass
    this as the next ``base_hash`` to edit safely), whether a git audit commit was made,
    and whether the synchronous reindex made it retrievable in this same session
    (``../Rag/projects/vaultrag/write-back-design.md``). ``indexed=False`` with a
    ``warning`` means the note is safely on disk + committed but the index lagged (e.g.
    Qdrant was down) — the vault is the source of truth, so a later reindex/watch recovers
    it. ``uri`` is the one-click ``obsidian://open`` link to the written note.
    """

    path: str
    action: Literal["created", "appended", "patched"]
    hash: str  # BLAKE3 of the new file content — the base_hash for a subsequent edit
    bytes_written: int
    committed: bool = False  # a git commit was made (False = non-git vault / commit skipped)
    commit: str | None = None  # short commit SHA when committed
    indexed: bool = False  # the synchronous reindex succeeded → searchable now
    chunks: int = 0  # chunks (re)written by that reindex
    uri: str | None = None  # obsidian://open?vault=…&file=… (when a vault_name is configured)
    warning: str | None = None  # non-fatal note (index lagged / git skipped), else None
