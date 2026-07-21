"""vaultrag.parsing — Markdown to normalized Document parsing. Step 4 (+ sections, Step 10).

Public entry points: :func:`parse_document` (file text → ``Document``) and
:func:`heading_section`/:func:`normalize_heading` (heading → line span over a parsed
``Document`` — used by MCP ``open`` and, later, P1 write-back). See
``../Rag/projects/vaultrag/parsing-pipeline.md`` and ``p0-step-plan.md``.
"""

from vaultrag.parsing.markdown import parse_document
from vaultrag.parsing.sections import heading_section, normalize_heading, parent_anchor

# What the shipped parser would extract from an unchanged file. Stored per project
# (``index_state.parser_version``) so ``reindex`` can tell an index built by an older
# parser and say so — a hash-equal file is "unchanged" to the manifest even when this
# code would now read *more* out of it, so the ordinary reconcile does nothing and the
# new data silently never appears. That exact trap swallowed P2 Stage 2's link graph
# (caught late, by hand) and would have swallowed Stage 5's diagram edges.
#
# **Bump this whenever the parser starts extracting something new**, then let
# ``vaultrag reindex --force`` do the repair — it re-upserts without re-embedding, so
# the cost is parse + upsert, not the embedding bill (``CLAUDE.md`` §6: the index is
# derived; the vault is truth).
#
#   1 — P0/P1: markdown, frontmatter, wikilinks/embeds, callouts, dataview
#   2 — P2 Stage 4: PDFs via Docling (``Block.page``)
#   3 — P2 Stage 5: Mermaid ``diagram`` edges + Excalidraw scenes
#   4 — P2 Stage 6: image OCR (RapidOCR/Tesseract)
PARSER_VERSION = 4

__all__ = [
    "PARSER_VERSION",
    "heading_section",
    "normalize_heading",
    "parent_anchor",
    "parse_document",
]
