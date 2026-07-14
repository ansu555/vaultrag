"""vaultrag.parsing — Markdown to normalized Document parsing. Step 4.

Public entry point: :func:`parse_document`. See
``../Rag/projects/vaultrag/parsing-pipeline.md`` and ``p0-step-plan.md`` Step 4.
"""

from vaultrag.parsing.markdown import parse_document

__all__ = ["parse_document"]
