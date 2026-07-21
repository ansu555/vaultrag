"""P2 Stage 4 verify: PDFs index end-to-end, carry pages, and degrade cleanly.

Two halves, deliberately separated:

* **Everything that does not need Docling** — the routing, the graceful skip, the
  secret gate, ``open``'s refusal, page propagation through the chunker — runs with a
  fake :class:`~vaultrag.parsing.pdf.PdfParser`. These are the tests CI actually runs,
  because the base install has no ``pdf`` extra (Decision 3).
* **The real Docling conversion** — marked ``requires_docling`` and skipped when the
  extra is absent. Run locally with ``uv sync --extra pdf``.

The split is the point: if the *only* PDF tests needed a 2 GB optional dependency, the
whole stage would be untested on every machine that ships it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from vaultrag.chunking.chunker import chunk_document
from vaultrag.embedding.base import Vector
from vaultrag.index import QdrantVectorStore, SparseVector
from vaultrag.indexer.pipeline import (
    IngestContext,
    IngestStatus,
    SkipReason,
    ingest_path,
    reindex_vault,
)
from vaultrag.indexer.scan import is_binary_source, iter_indexable_suffixes, walk_vault
from vaultrag.models import Block, BlockType, DocType, Document, DocumentMetadata
from vaultrag.parsing.pdf import DoclingPdfParser, docling_available
from vaultrag.store.sqlite import SQLiteMetaStore

PROJECT = "test-project"
DIMS = 4
FIXTURE_PDF = Path(__file__).parent / "fixtures" / "handbook.pdf"

requires_docling = pytest.mark.skipif(
    not docling_available(), reason="needs the `pdf` extra (uv sync --extra pdf)"
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class FakeSparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[1], values=[1.0])


class FakeEmbedder:
    @property
    def model(self) -> str:
        return "fake-embed-model"

    @property
    def dims(self) -> int:
        return DIMS

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [[float(len(t) + i) for i in range(DIMS)] for t in texts]

    def close(self) -> None:
        pass


class FakePdfParser:
    """Returns a fixed two-page Document — no Docling, no models, instant.

    Stands in for the converter everywhere the *wiring* is under test, so routing,
    skipping and secret-gating are covered on a base install.
    """

    def __init__(
        self, *, text: str = "The gateway returns MER-1422 when the quota is spent."
    ) -> None:
        self._text = text
        self.calls = 0

    def parse(
        self,
        *,
        path: str,
        data: bytes,
        content_hash: str | None = None,
        mtime: int | None = None,
    ) -> Document:
        self.calls += 1
        return Document(
            metadata=DocumentMetadata(
                path=path, title="Handbook", type=DocType.pdf, hash=content_hash, modified_ts=mtime
            ),
            blocks=[
                Block(type=BlockType.paragraph, text=_long("page one intro"), page=1),
                Block(type=BlockType.paragraph, text=self._text, page=2),
            ],
        )


def _long(seed: str) -> str:
    """A body long enough not to trip the chunker's tiny-sibling merge."""
    return " ".join(f"{seed.replace(' ', '')}{i}" for i in range(150))


def write_vault(tmp_path: Path, files: Mapping[str, str | bytes]) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    for rel_path, content in files.items():
        target = vault / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return vault


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    yield s
    s.close()


@pytest.fixture
def vector_store() -> QdrantVectorStore:
    vs = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=FakeSparse()
    )
    vs.ensure_collection()
    return vs


NOTE = "# A Note\n\n## Section\n\n" + _long("aaa") + "\n"
PDF_BYTES = FIXTURE_PDF.read_bytes()


# ---------------------------------------------------------------------------
# Scan routing (no Docling needed)
# ---------------------------------------------------------------------------
def test_pdf_is_indexable() -> None:
    assert ".pdf" in set(iter_indexable_suffixes())
    assert is_binary_source("docs/handbook.pdf")
    assert is_binary_source("docs/HANDBOOK.PDF")  # suffix match is case-insensitive
    assert not is_binary_source("docs/note.md")


def test_walk_does_not_content_scan_a_pdf(tmp_path: Path) -> None:
    """A PDF's bytes are not text: decoding them would make the secret scan meaningless.

    Its content gate runs after conversion instead (see the ingest tests below), so the
    guardrail moves rather than disappears.
    """
    vault = write_vault(tmp_path, {"handbook.pdf": PDF_BYTES, "note.md": NOTE})
    files, stats = walk_vault(vault)

    assert sorted(f.path for f in files) == ["handbook.pdf", "note.md"]
    assert stats.secrets_blocked == 0
    assert stats.indexable == 2


# ---------------------------------------------------------------------------
# Page propagation (no Docling needed)
# ---------------------------------------------------------------------------
def test_chunk_takes_the_page_it_starts_on() -> None:
    document = FakePdfParser().parse(path="h.pdf", data=b"", content_hash="h")
    chunks = chunk_document(document, project_id=PROJECT)

    assert [c.page for c in chunks] == [1, 2]
    assert all(c.type is DocType.pdf for c in chunks)


def test_markdown_chunks_have_no_page() -> None:
    """`page` is absent for notes, not zero — the payload omits it entirely."""
    from vaultrag.parsing.markdown import parse_document

    document = parse_document(path="note.md", raw_text=NOTE)
    chunks = chunk_document(document, project_id=PROJECT)

    assert chunks and all(c.page is None for c in chunks)


def test_a_chunk_spanning_a_page_break_reports_its_first_page() -> None:
    document = Document(
        metadata=DocumentMetadata(path="h.pdf", title="H", type=DocType.pdf),
        blocks=[
            Block(type=BlockType.paragraph, text="short a", page=2),
            Block(type=BlockType.paragraph, text="short b", page=3),
        ],
    )
    chunks = chunk_document(document, project_id=PROJECT)

    assert [c.page for c in chunks] == [2]


def test_a_block_with_no_page_does_not_erase_the_chunks_anchor() -> None:
    document = Document(
        metadata=DocumentMetadata(path="h.pdf", title="H", type=DocType.pdf),
        blocks=[
            Block(type=BlockType.paragraph, text="unpaged"),
            Block(type=BlockType.paragraph, text="paged", page=4),
        ],
    )
    chunks = chunk_document(document, project_id=PROJECT)

    assert [c.page for c in chunks] == [4]


# ---------------------------------------------------------------------------
# Ingest wiring (no Docling needed — FakePdfParser)
# ---------------------------------------------------------------------------
def _ctx(tmp_path: Path, store, vector_store, parser) -> IngestContext:  # type: ignore[no-untyped-def]
    store.upsert_project(PROJECT, name=PROJECT, vault_path=str(tmp_path / "vault"))
    return IngestContext(
        project_id=PROJECT,
        vault_path=(tmp_path / "vault").resolve(),
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        pdf_parser=parser,
    )


def test_pdf_indexes_end_to_end(tmp_path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    vault = write_vault(tmp_path, {"handbook.pdf": PDF_BYTES, "note.md": NOTE})
    parser = FakePdfParser()

    stats = reindex_vault(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        pdf_parser=parser,
    )

    assert stats.created == 2
    assert stats.failed == 0
    assert parser.calls == 1
    doc = store.get_document(PROJECT, "handbook.pdf")
    assert doc is not None and doc.type == DocType.pdf
    assert vector_store.count(PROJECT) == stats.chunks_written


def test_without_the_extra_pdfs_are_skipped_and_notes_still_index(  # type: ignore[no-untyped-def]
    tmp_path, store, vector_store
) -> None:
    """The base install must index a mixed vault, not fail on every PDF in it."""
    vault = write_vault(tmp_path, {"handbook.pdf": PDF_BYTES, "note.md": NOTE})

    stats = reindex_vault(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        pdf_parser=None,
    )

    assert stats.skipped == 1
    assert stats.failed == 0  # a skip is not a failure — reindex must still exit 0
    assert stats.created == 1
    assert store.get_document(PROJECT, "note.md") is not None
    assert store.get_document(PROJECT, "handbook.pdf") is None


def test_single_file_ingest_skips_a_pdf_without_a_parser(  # type: ignore[no-untyped-def]
    tmp_path, store, vector_store
) -> None:
    write_vault(tmp_path, {"handbook.pdf": PDF_BYTES})
    outcome = ingest_path(_ctx(tmp_path, store, vector_store, None), "handbook.pdf")

    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.no_parser


def test_a_secret_in_extracted_pdf_text_is_withheld(  # type: ignore[no-untyped-def]
    tmp_path, store, vector_store
) -> None:
    """CLAUDE.md §6: secrets never enter the index — including via a converted PDF.

    The walk cannot catch this one (a PDF has no text to scan), so the gate has to fire
    after conversion or it does not fire at all.
    """
    write_vault(tmp_path, {"handbook.pdf": PDF_BYTES})
    leaky = FakePdfParser(text="Credentials live at AKIAIOSFODNN7EXAMPLE in the appendix.")

    outcome = ingest_path(_ctx(tmp_path, store, vector_store, leaky), "handbook.pdf")

    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.secret
    assert store.get_document(PROJECT, "handbook.pdf") is None
    assert vector_store.count(PROJECT) == 0


def test_a_secret_carrying_pdf_is_skipped_not_failed_in_reindex(  # type: ignore[no-untyped-def]
    tmp_path, store, vector_store
) -> None:
    vault = write_vault(tmp_path, {"handbook.pdf": PDF_BYTES, "note.md": NOTE})
    leaky = FakePdfParser(text="Credentials live at AKIAIOSFODNN7EXAMPLE in the appendix.")

    stats = reindex_vault(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        pdf_parser=leaky,
    )

    assert stats.skipped == 1
    assert stats.failed == 0
    assert store.get_document(PROJECT, "handbook.pdf") is None


def test_an_unchanged_pdf_is_not_reconverted(tmp_path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    """Conversion costs ~0.6 s/page — the hash check must come *before* it."""
    write_vault(tmp_path, {"handbook.pdf": PDF_BYTES})
    parser = FakePdfParser()
    ctx = _ctx(tmp_path, store, vector_store, parser)

    assert ingest_path(ctx, "handbook.pdf").status is IngestStatus.indexed
    assert parser.calls == 1

    assert ingest_path(ctx, "handbook.pdf").status is IngestStatus.unchanged
    assert parser.calls == 1  # not converted a second time


# ---------------------------------------------------------------------------
# Real Docling conversion
# ---------------------------------------------------------------------------
@requires_docling
def test_docling_converts_the_fixture_with_page_anchors() -> None:
    document = DoclingPdfParser().parse(path="handbook.pdf", data=PDF_BYTES, content_hash="h")

    assert document.metadata.type is DocType.pdf
    assert document.blocks

    text = "\n".join(block.text for block in document.blocks)
    assert "MER-1422" in text  # page 2's identifier survived conversion
    assert "ninety days" in text  # page 3's content did too

    pages = {block.page for block in document.blocks if block.page is not None}
    assert pages, "no block carried a page number"
    assert max(pages) == 3  # the fixture is three pages

    # The page a piece of text lands on must be the page it was written on — the whole
    # point of the anchor. A wrong-but-present page is worse than none, and this is the
    # assertion that would catch an off-by-one in the line→page mapping.
    def page_of(needle: str) -> int | None:
        return next((b.page for b in document.blocks if needle in b.text), None)

    assert page_of("MER-1422") == 2
    assert page_of("ninety days") == 3
    assert page_of("MERIDIAN INGEST HANDBOOK") == 1


@requires_docling
def test_docling_pdf_chunks_carry_their_page(tmp_path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    vault = write_vault(tmp_path, {"handbook.pdf": PDF_BYTES})

    stats = reindex_vault(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        pdf_parser=DoclingPdfParser(),
    )

    assert stats.created == 1
    assert stats.chunks_written > 0
    assert vector_store.count(PROJECT) == stats.chunks_written


@requires_docling
def test_a_corrupt_pdf_raises_rather_than_indexing_garbage() -> None:
    from vaultrag.parsing.pdf import PdfError

    with pytest.raises(PdfError):
        DoclingPdfParser().parse(path="broken.pdf", data=b"%PDF-1.4 not really a pdf")
