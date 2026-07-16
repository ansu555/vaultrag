"""Heading → section-span resolution over a parsed :class:`~vaultrag.models.Document`.

"Which lines belong to this heading" is a document-structure question, so it lives with
the parser rather than in any one consumer: the MCP ``open(path, heading=...)`` tool
(Step 10) resolves citations with it, and P1's write-back (``append_section``) needs the
identical definition of a section's boundaries. Resolution runs over parsed blocks, not
raw lines, so a ``## fake heading`` inside a code fence can never match (the tokenizer
already knows it isn't a heading).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from vaultrag.models import Block, BlockType, Document

# Parent-swap (retrieval stage 5) rolls a child up to its enclosing H1/H2 section —
# the heading at heading-tree depth <= 2. A child nested deeper (H3/H4...) still rolls
# up to that H2, so all children of one section share one parent pointer and can be
# deduped into a single parent snippet (chunking-strategy.md, arch §6/§10-stage5).
PARENT_MAX_DEPTH = 2


def normalize_heading(text: str) -> str:
    """Forgiving heading key: casefold + collapse non-word runs *and* ``_`` to hyphens.

    Makes 'Quick Start', 'quick start', 'quick-start', and 'Quick_Start' all meet at
    'quick-start' — agents pass anchors in every one of those forms. ``_`` is folded
    explicitly because it is a ``\\w`` word character the ``\\W`` class would keep.
    """
    return re.sub(r"[\W_]+", "-", text.casefold()).strip("-")


def heading_section(
    document: Document, heading: str, total_lines: int
) -> tuple[int, int, Block] | None:
    """Resolve ``heading`` to ``(line_start, line_end, matched heading block)``.

    The span is 1-based inclusive and runs from the heading's own line to the line
    before the next heading of the same or higher level (else ``total_lines``) — the
    whole subsection, nested children included. Returns ``None`` when no heading
    normalizes to the same key (a heading without a line span is skipped rather than
    matched, so a later same-named heading can still resolve).
    """
    headings = [b for b in document.blocks if b.type is BlockType.heading]
    wanted = normalize_heading(heading)
    for i, block in enumerate(headings):
        if normalize_heading(block.text) != wanted or block.line_start is None:
            continue
        end = total_lines
        for later in headings[i + 1 :]:
            if later.line_start is not None and (later.level or 99) <= (block.level or 99):
                end = later.line_start - 1
                break
        return block.line_start, end, block
    return None


def parent_anchor(heading_path: Sequence[str]) -> str | None:
    """Normalized anchor of the enclosing H1/H2 section a child at ``heading_path`` swaps to.

    The parent for parent-swap is the section at heading-tree depth :data:`PARENT_MAX_DEPTH`
    (the H2) when the child is nested that deep, else its top (H1) heading. Root-level
    content (empty ``heading_path``) has no enclosing section, so it returns ``None`` and
    never swaps. The key is normalized here so the pointer the chunker stores and the anchor
    retrieval hands :func:`heading_section` are the same forgiving form.
    """
    if not heading_path:
        return None
    depth = min(len(heading_path), PARENT_MAX_DEPTH)
    return normalize_heading(heading_path[depth - 1])
