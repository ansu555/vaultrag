"""PDF → :class:`~vaultrag.models.Document` via Docling (P2 Stage 4).

Docling converts a PDF to markdown; that markdown then goes through the *existing*
markdown parser, so chunking / embedding / indexing never learn that PDFs exist
(``parsing-pipeline.md``: "Docling → markdown with page anchors; tables → markdown
tables; `page` stored per chunk for citation"; arch §5). Docling is the pin because it
is MIT — PyMuPDF4LLM is AGPL and marker is GPL, both license-excluded
(``reference-technology-stack.md``).

**Page numbers survive by construction.** Docling can export markdown one page at a
time, but parsing each page *independently* would break the heading tree: an H1 on page
1 would not scope the blocks on page 2, and the chunker's whole model is heading-path
sections. So the pages are exported separately, concatenated into one markdown document
(heading structure continuous), and the line range each page occupies is recorded on the
way. Blocks already carry ``line_start``, so stamping ``Block.page`` afterwards is a
lookup rather than a second parse.

**Docling is an optional extra** (``pip install vaultrag[pdf]``, Decision 3 of the P2
plan): it pulls ~100 packages including torch, which the base install must not require
(local-first, principle #2). :func:`load_pdf_parser` returns ``None`` when it is absent,
and the scanner then skips PDFs cleanly — the ``NoOpReranker`` degradation pattern.
"""

from __future__ import annotations

import io
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from vaultrag.logging import get_logger
from vaultrag.models import Block, DocType, Document
from vaultrag.parsing.markdown import parse_document

log = get_logger(__name__)

__all__ = ["DoclingPdfParser", "PdfError", "PdfParser", "docling_available", "load_pdf_parser"]


class PdfError(Exception):
    """A PDF could not be converted (corrupt file, Docling failure)."""


@runtime_checkable
class PdfParser(Protocol):
    """Convert PDF bytes into the same ``Document`` shape markdown produces."""

    def parse(
        self,
        *,
        path: str,
        data: bytes,
        content_hash: str | None = None,
        mtime: int | None = None,
    ) -> Document:
        """Parse one PDF. Raises :class:`PdfError` if it cannot be converted."""


@dataclass(frozen=True, slots=True)
class _PageSpan:
    """Which 1-based line range of the assembled markdown came from which page."""

    first_line: int
    page: int


class DoclingPdfParser:
    """:class:`PdfParser` backed by Docling's ``DocumentConverter``.

    The converter is built once and reused: constructing it is the expensive part
    (model loading), so a per-file converter would pay that cost on every PDF in a vault.
    """

    def __init__(self, converter: Any | None = None) -> None:
        self._converter = converter if converter is not None else _build_converter()

    def parse(
        self,
        *,
        path: str,
        data: bytes,
        content_hash: str | None = None,
        mtime: int | None = None,
    ) -> Document:
        markdown, spans = self._to_markdown(path=path, data=data)
        document = parse_document(
            path=path, raw_text=markdown, content_hash=content_hash, mtime=mtime
        )
        blocks = [_with_page(block, spans) for block in document.blocks]
        # A PDF has no frontmatter, so `type` cannot come from the source the way a
        # note's can — it is a property of the parser that produced the document.
        metadata = document.metadata.model_copy(update={"type": DocType.pdf})
        return document.model_copy(update={"blocks": blocks, "metadata": metadata})

    def _to_markdown(self, *, path: str, data: bytes) -> tuple[str, list[_PageSpan]]:
        """Convert to markdown, page by page, recording where each page starts.

        Concatenating per-page exports (rather than parsing each page as its own
        document) is what keeps the heading tree continuous across page breaks — see
        the module docstring.
        """
        doc = self._convert(path=path, data=data)
        pages = _page_count(doc)

        parts: list[str] = []
        spans: list[_PageSpan] = []
        line = 1
        for page in range(1, pages + 1):
            try:
                text = doc.export_to_markdown(page_no=page)
            except Exception as exc:  # a single unrenderable page must not lose the rest
                log.warning("pdf.page_export_failed", path=path, page=page, error=str(exc))
                text = ""
            spans.append(_PageSpan(first_line=line, page=page))
            parts.append(text)
            # +1 for the blank line joining pages, so the recorded starts stay aligned
            # with the assembled document's real line numbering.
            line += text.count("\n") + 2

        return "\n\n".join(parts), spans

    def _convert(self, *, path: str, data: bytes) -> Any:
        from docling.datamodel.base_models import DocumentStream

        try:
            stream = DocumentStream(name=path, stream=io.BytesIO(data))
            return self._converter.convert(stream).document
        except Exception as exc:
            raise PdfError(f"docling could not convert {path!r}: {exc}") from exc


def _with_page(block: Block, spans: list[_PageSpan]) -> Block:
    """Stamp the page a block starts on, from its line number."""
    if block.line_start is None or not spans:
        return block
    starts = [span.first_line for span in spans]
    ix = bisect_right(starts, block.line_start) - 1
    if ix < 0:
        return block
    return block.model_copy(update={"page": spans[ix].page})


def _page_count(doc: Any) -> int:
    """Docling's page count, defensively — a converted doc always has at least one."""
    try:
        pages = doc.num_pages()
    except Exception:
        pages = len(getattr(doc, "pages", ()) or ())
    return max(int(pages or 0), 1)


def _build_converter() -> Any:
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def docling_available() -> bool:
    """Whether the ``pdf`` extra is installed. Import-only — loads no models."""
    from importlib.util import find_spec

    try:
        return find_spec("docling") is not None
    except (ImportError, ValueError):  # a broken/partial install is "not available"
        return False


def load_pdf_parser() -> PdfParser | None:
    """Composition-root helper: build the PDF parser, or ``None`` if unavailable.

    ``None`` is a supported state, not an error: the base install deliberately omits
    Docling, and a vault containing PDFs must still index its markdown. Callers skip
    PDFs when this returns ``None`` (logged once, at the scan boundary).
    """
    if not docling_available():
        log.info("pdf.parser_unavailable", reason="docling not installed", extra="vaultrag[pdf]")
        return None
    try:
        return DoclingPdfParser()
    except Exception as exc:  # a present-but-unusable Docling degrades, never crashes
        log.warning("pdf.parser_load_failed", error=str(exc))
        return None
