"""Document -> indexed Chunk children (P0 Step 5).

Heading-tree chunking, not fixed-size sliding windows (arch §6,
``../Rag/decisions/chunking-heading-tree-not-fixed-size.md``): a child chunk is
the contiguous run of non-heading blocks under the lowest heading in effect.
Blocks are already parser-atomic units (Step 4 keeps code/table/list/blockquote
whole -- ``../Rag/projects/vaultrag/chunking-strategy.md``), so packing only ever
decides *between* blocks, never inside one.

Algorithm, in three passes over ``Document.blocks``:

1. **Section grouping** (:func:`_sections`) -- contiguous non-heading blocks that
   share the same ``heading_path`` form one section. Heading blocks themselves
   contribute no chunk body text (their text already surfaces via
   ``heading_path``/the contextual prefix); a heading with nothing under it before
   the next heading yields no chunk, only context for its descendants.
2. **Greedy packing** (:func:`_pack_section`) -- within a section, accumulate
   blocks toward the ~300-token target; cut *before* a block that would push the
   current piece over the 512-token hard max, unless the piece is still empty (an
   oversized atomic block becomes its own oversized chunk -- never split).
3. **Tiny-sibling merge** (:func:`_merge_tiny_siblings`) -- a single left-to-right
   pass that folds a piece under 120 tokens into its immediate predecessor when
   they share the same parent heading (``heading_path[:-1]``) and the merge still
   fits the hard max. This covers both a small leftover tail piece from packing
   (same ``heading_path``) and genuinely tiny sibling sections (same parent,
   different heading) -- e.g. a "Glossary" H2 full of one-line H3 terms.

``Chunk.heading_path`` can only hold one path (models.py), so a merged chunk
always inherits the *earlier* piece's path; the later piece's content is still
present in ``text``, just not separately labeled.

``parent_id`` (P1 Stage 2) is a **pointer**, not a stored record: ``<path>#<anchor>``
where ``anchor`` is the enclosing H1/H2 section's normalized heading
(:func:`~vaultrag.parsing.sections.parent_anchor`). No parent *text* is materialized —
children are the only indexed vectors; the retrievable parent is reconstructed on the
fly from the vault at swap time (retrieval stage 5), so the vault stays the source of
truth (CLAUDE.md §6 #1, ``../Rag/projects/vaultrag/chunking-strategy.md``). All children
of one section share the same pointer, which is what lets stage 5 dedupe them into a
single parent snippet. Root-level content (empty ``heading_path``) has no enclosing
section, so its ``parent_id`` stays ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from blake3 import blake3

from vaultrag.chunking.tokens import count_tokens
from vaultrag.models import Block, BlockType, Chunk, Document, chunk_point_id, document_id
from vaultrag.parsing.sections import parent_anchor

# Section 6 / chunking-strategy.md: target ~300 tok, hard max 512, merge tiny
# siblings until >=120 tok.
TARGET_TOKENS = 300
HARD_MAX_TOKENS = 512
MERGE_MIN_TOKENS = 120


@dataclass
class _Piece:
    """One candidate chunk before it's turned into a :class:`Chunk`."""

    heading_path: list[str]
    blocks: list[Block]
    tokens: int


def chunk_document(document: Document, *, project_id: str) -> list[Chunk]:
    """Turn a parsed :class:`Document` into its ordered list of indexed children."""
    pieces = _merge_tiny_siblings(_all_pieces(document))
    doc_id = document_id(project_id, document.path)
    point_ids = [chunk_point_id(project_id, document.path, ix) for ix in range(len(pieces))]

    chunks: list[Chunk] = []
    for ix, piece in enumerate(pieces):
        text = _piece_text(piece.blocks)
        chunks.append(
            Chunk(
                project_id=project_id,
                doc_id=doc_id,
                path=document.path,
                ix=ix,
                text=text,
                contextual_prefix=_contextual_prefix(project_id, document, piece.heading_path),
                heading_path=piece.heading_path,
                anchor=piece.heading_path[-1] if piece.heading_path else None,
                tags=document.metadata.tags,
                langs=_langs(piece.blocks),
                has_code=any(b.type == BlockType.code for b in piece.blocks),
                type=document.metadata.type,
                created_ts=document.metadata.created_ts,
                modified_ts=document.metadata.modified_ts,
                chunk_hash=blake3(text.encode("utf-8")).hexdigest(),
                tokens=piece.tokens,
                parent_id=_parent_pointer(document.path, piece.heading_path),
                prev_id=point_ids[ix - 1] if ix > 0 else None,
                next_id=point_ids[ix + 1] if ix < len(pieces) - 1 else None,
            )
        )
    return chunks


# ---------------------------------------------------------------------------
# Pass 1: group blocks into heading-path sections
# ---------------------------------------------------------------------------
def _sections(blocks: list[Block]) -> list[tuple[list[str], list[Block]]]:
    """Contiguous non-heading blocks sharing the same ``heading_path`` -> one section.

    Heading blocks are boundary markers only -- they never contribute chunk text
    themselves (they're already captured in descendants' ``heading_path``). A
    section's key changes exactly when the heading in effect changes, which is
    exactly when a heading block is encountered -- so this is a straight linear
    walk, no explicit tree needed.
    """
    sections: list[tuple[list[str], list[Block]]] = []
    current_path: list[str] | None = None
    current: list[Block] = []
    for block in blocks:
        if block.type == BlockType.heading:
            continue
        if block.heading_path != current_path:
            if current:
                sections.append((current_path or [], current))
            current_path = block.heading_path
            current = []
        current.append(block)
    if current:
        sections.append((current_path or [], current))
    return sections


# ---------------------------------------------------------------------------
# Pass 2: greedy target/hard-max packing within one section
# ---------------------------------------------------------------------------
def _pack_section(heading_path: list[str], blocks: list[Block]) -> list[_Piece]:
    pieces: list[_Piece] = []
    current: list[Block] = []
    current_tokens = 0
    for block in blocks:
        block_tokens = count_tokens(block.text)
        if current and current_tokens + block_tokens > HARD_MAX_TOKENS:
            pieces.append(_Piece(heading_path, current, current_tokens))
            current, current_tokens = [], 0
        current.append(block)
        current_tokens += block_tokens
        if current_tokens >= TARGET_TOKENS:
            pieces.append(_Piece(heading_path, current, current_tokens))
            current, current_tokens = [], 0
    if current:
        pieces.append(_Piece(heading_path, current, current_tokens))
    return pieces


def _all_pieces(document: Document) -> list[_Piece]:
    pieces: list[_Piece] = []
    for heading_path, blocks in _sections(document.blocks):
        pieces.extend(_pack_section(heading_path, blocks))
    return pieces


# ---------------------------------------------------------------------------
# Pass 3: merge tiny sibling pieces
# ---------------------------------------------------------------------------
def _merge_tiny_siblings(pieces: list[_Piece]) -> list[_Piece]:
    merged: list[_Piece] = []
    for piece in pieces:
        if merged and _same_parent(merged[-1].heading_path, piece.heading_path):
            prev = merged[-1]
            combined_tokens = prev.tokens + piece.tokens
            is_tiny = prev.tokens < MERGE_MIN_TOKENS or piece.tokens < MERGE_MIN_TOKENS
            if is_tiny and combined_tokens <= HARD_MAX_TOKENS:
                merged_blocks = [*prev.blocks, *piece.blocks]
                merged[-1] = _Piece(prev.heading_path, merged_blocks, combined_tokens)
                continue
        merged.append(piece)
    return merged


def _same_parent(a: list[str], b: list[str]) -> bool:
    """True if ``a`` and ``b`` sit at the same depth under the same ancestor path.

    Comparing only ``[:-1]`` would false-positive match root-level content
    (``heading_path == []``) against a top-level H1 section (``["H1"]``), since
    both have an empty ``[:-1]``. The length check guards that.
    """
    return len(a) == len(b) and a[:-1] == b[:-1]


# ---------------------------------------------------------------------------
# Chunk field derivation
# ---------------------------------------------------------------------------
def _parent_pointer(path: str, heading_path: list[str]) -> str | None:
    """``<path>#<anchor>`` pointer to the child's enclosing H1/H2 section, or ``None``.

    Not a chunk id (unlike ``prev_id``/``next_id``): retrieval never dereferences this
    string, it reconstructs the parent section from ``path`` + ``heading_path`` at swap
    time. The stored pointer is the durable record of the linkage (SQLite ``chunks.parent_id``)
    and the human-readable key two same-section children share.
    """
    anchor = parent_anchor(heading_path)
    return f"{path}#{anchor}" if anchor is not None else None


def _piece_text(blocks: list[Block]) -> str:
    return "\n\n".join(block.text for block in blocks)


def _langs(blocks: list[Block]) -> list[str]:
    return sorted({block.lang for block in blocks if block.lang})


def _contextual_prefix(project_id: str, document: Document, heading_path: list[str]) -> str:
    """«Project ▸ folder/note ▸ H1 ▸ H2» + tags line (chunking-strategy.md)."""
    note_ref = PurePosixPath(document.path).with_suffix("").as_posix()
    crumbs = " ▸ ".join([project_id, note_ref, *heading_path])
    header = f"«{crumbs}»"
    tags = document.metadata.tags
    if not tags:
        return header
    return f"{header}\nTags: {', '.join(tags)}"
