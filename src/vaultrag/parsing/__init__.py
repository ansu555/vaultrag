"""vaultrag.parsing — Markdown to normalized Document parsing. Step 4 (+ sections, Step 10).

Public entry points: :func:`parse_document` (file text → ``Document``) and
:func:`heading_section`/:func:`normalize_heading` (heading → line span over a parsed
``Document`` — used by MCP ``open`` and, later, P1 write-back). See
``../Rag/projects/vaultrag/parsing-pipeline.md`` and ``p0-step-plan.md``.
"""

from vaultrag.parsing.markdown import parse_document
from vaultrag.parsing.sections import heading_section, normalize_heading

__all__ = ["heading_section", "normalize_heading", "parse_document"]
