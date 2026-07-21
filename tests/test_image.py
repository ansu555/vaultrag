"""P2 Stage 6 verify: images OCR end-to-end, degrade cleanly, and stay gated.

Split the same way ``test_pdf.py`` is, and for the same reason: everything that tests
the *wiring* — routing, the per-format graceful skip, the secret gate on converted text,
``open``'s refusal, the no-text no-op — runs against a fake :class:`OcrEngine`, so it is
covered on an install with no OCR engine at all. Only the tests that exercise real
recognition are marked ``requires_ocr``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from vaultrag.embedding.base import Vector
from vaultrag.index import QdrantVectorStore, SparseVector
from vaultrag.indexer.pipeline import (
    IngestContext,
    IngestStatus,
    SkipReason,
    ingest_path,
    reindex_vault,
)
from vaultrag.indexer.scan import (
    is_binary_source,
    is_image_source,
    iter_indexable_suffixes,
    walk_vault,
)
from vaultrag.models import DocType
from vaultrag.parsing.image import (
    IMAGE_SUFFIXES,
    NoTextFound,
    OcrImageParser,
    OcrLine,
    load_image_parser,
    rapidocr_available,
    tesseract_available,
)
from vaultrag.store.sqlite import SQLiteMetaStore

PROJECT = "test-project"
DIMS = 4
FIXTURES = Path(__file__).parent / "fixtures"
TEXT_IMAGE = FIXTURES / "diagram-shot.png"
BLANK_IMAGE = FIXTURES / "blank-shot.png"

requires_ocr = pytest.mark.skipif(
    not (rapidocr_available() or tesseract_available()),
    reason="needs an OCR engine (uv sync --extra pdf, or install tesseract)",
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


class FakeOcrEngine:
    """Returns fixed lines — no models, instant. Geometry is real so ordering is testable."""

    name = "fake-ocr"

    def __init__(self, lines: list[OcrLine] | None = None) -> None:
        self._lines = lines if lines is not None else [OcrLine(_long("shot"), 0.98, 10.0, 10.0)]
        self.calls = 0

    def read(self, data: bytes) -> list[OcrLine]:
        self.calls += 1
        return list(self._lines)


def _long(seed: str) -> str:
    """A body long enough not to trip the chunker's tiny-sibling merge."""
    return " ".join(f"{seed}{i}" for i in range(150))


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
    # `ingest_path` is the single-file primitive and does not register the project the
    # way `reindex_vault` does — without this the FK on `documents.project_id` fails.
    s.upsert_project(PROJECT, name=PROJECT, vault_path=str(tmp_path / "vault"))
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
IMAGE_BYTES = TEXT_IMAGE.read_bytes()


# ---------------------------------------------------------------------------
# Scan routing
# ---------------------------------------------------------------------------
def test_images_are_indexable_binary_sources() -> None:
    suffixes = set(iter_indexable_suffixes())
    assert suffixes >= IMAGE_SUFFIXES
    assert is_image_source("shots/diagram.png")
    assert is_image_source("shots/DIAGRAM.PNG")  # suffix match is case-insensitive
    assert not is_image_source("docs/handbook.pdf")
    # An image is binary *as well as* an image: its bytes are not its text, so the walk
    # must not content-scan it (that gate runs after OCR instead).
    assert is_binary_source("shots/diagram.png")


def test_walk_does_not_content_scan_an_image(tmp_path: Path) -> None:
    """Image bytes decode to mojibake; scanning that is meaningless, not safe."""
    vault = write_vault(tmp_path, {"shot.png": IMAGE_BYTES, "note.md": NOTE})
    files, stats = walk_vault(vault)
    assert {f.path for f in files} == {"shot.png", "note.md"}
    assert stats.secrets_blocked == 0


# ---------------------------------------------------------------------------
# Parser behaviour (fake engine — no OCR install needed)
# ---------------------------------------------------------------------------
def test_ocr_lines_become_one_searchable_document() -> None:
    engine = FakeOcrEngine([OcrLine("Gateway rejects large payloads", 0.99, 5.0, 5.0)])
    doc = OcrImageParser(engine).parse(path="shot.png", data=b"x", content_hash="h", mtime=7)

    assert doc.metadata.type is DocType.image
    assert doc.metadata.title == "shot"  # the filename is the only title an image has
    assert doc.metadata.hash == "h"
    assert doc.metadata.modified_ts == 7
    assert [b.text for b in doc.blocks] == ["Gateway rejects large payloads"]


def test_text_is_emitted_in_reading_order_not_detection_order() -> None:
    """Detection order follows the model's box ranking, not the page's layout."""
    engine = FakeOcrEngine(
        [
            OcrLine("bottom line", 0.9, 10.0, 200.0),
            OcrLine("top right", 0.9, 400.0, 10.0),
            OcrLine("top left", 0.9, 10.0, 12.0),  # same band as "top right", left of it
        ]
    )
    doc = OcrImageParser(engine).parse(path="shot.png", data=b"x")
    assert doc.blocks[0].text == "top left\ntop right\nbottom line"


def test_an_image_with_no_text_raises_rather_than_indexing_an_empty_document() -> None:
    with pytest.raises(NoTextFound):
        OcrImageParser(FakeOcrEngine([])).parse(path="photo.png", data=b"x")


def test_low_confidence_recognitions_are_dropped() -> None:
    """OCR on a noisy region emits garbage; garbage in the index is worse than a gap."""
    engine = FakeOcrEngine(
        [OcrLine("real text here", 0.95, 0.0, 0.0), OcrLine("l1|_ ^~", 0.11, 0.0, 40.0)]
    )
    doc = OcrImageParser(engine).parse(path="shot.png", data=b"x")
    assert doc.blocks[0].text == "real text here"


def test_an_image_of_only_garbage_is_a_no_op() -> None:
    with pytest.raises(NoTextFound):
        OcrImageParser(FakeOcrEngine([OcrLine("l1|_", 0.05, 0.0, 0.0)])).parse(
            path="noise.png", data=b"x"
        )


def test_load_image_parser_returns_none_when_disabled() -> None:
    """`ocr_enabled=False` must skip images even on a box that has an engine."""
    assert load_image_parser(enabled=False) is None


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------
def _ctx(tmp_path: Path, store, vector_store, **kw):  # type: ignore[no-untyped-def]
    return IngestContext(
        project_id=PROJECT,
        vault_path=tmp_path / "vault",
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        **kw,
    )


def test_an_image_indexes_end_to_end(tmp_path: Path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    write_vault(tmp_path, {"shot.png": IMAGE_BYTES})
    ctx = _ctx(tmp_path, store, vector_store, image_parser=OcrImageParser(FakeOcrEngine()))

    outcome = ingest_path(ctx, "shot.png")
    assert outcome.status is IngestStatus.indexed
    assert outcome.chunks >= 1
    doc = store.get_document(PROJECT, "shot.png")
    assert doc is not None and doc.type == DocType.image


def test_an_image_is_skipped_when_no_ocr_engine_is_installed(
    tmp_path: Path,
    store,  # type: ignore[no-untyped-def]
    vector_store,  # type: ignore[no-untyped-def]
) -> None:
    write_vault(tmp_path, {"shot.png": IMAGE_BYTES})
    outcome = ingest_path(_ctx(tmp_path, store, vector_store), "shot.png")
    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.no_parser


def test_a_textless_image_is_skipped_not_failed(tmp_path: Path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    write_vault(tmp_path, {"shot.png": IMAGE_BYTES})
    ctx = _ctx(tmp_path, store, vector_store, image_parser=OcrImageParser(FakeOcrEngine([])))

    outcome = ingest_path(ctx, "shot.png")
    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.no_text
    assert store.get_document(PROJECT, "shot.png") is None


def test_an_image_that_loses_its_text_is_evicted(tmp_path: Path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    """Editing text out of an indexed image must remove it, not strand a stale document."""
    vault = write_vault(tmp_path, {"shot.png": IMAGE_BYTES})
    ctx = _ctx(tmp_path, store, vector_store, image_parser=OcrImageParser(FakeOcrEngine()))
    assert ingest_path(ctx, "shot.png").status is IngestStatus.indexed

    (vault / "shot.png").write_bytes(BLANK_IMAGE.read_bytes())  # new hash → re-parsed
    blanked = _ctx(tmp_path, store, vector_store, image_parser=OcrImageParser(FakeOcrEngine([])))
    outcome = ingest_path(blanked, "shot.png")

    assert outcome.status is IngestStatus.removed
    assert outcome.reason is SkipReason.no_text
    assert store.get_document(PROJECT, "shot.png") is None


def test_ocr_text_carrying_a_secret_is_never_indexed(tmp_path: Path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    """A screenshot of a `.env` is exactly as leaky as the file (CLAUDE.md §6).

    The walk cannot catch this — image bytes are not text — so the gate has to run on
    the *converted* output, the same placement Stage 4 established for PDFs.
    """
    write_vault(tmp_path, {"shot.png": IMAGE_BYTES})
    leaked = FakeOcrEngine(
        [OcrLine("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIbKb7MDENGbPxRfiCYEXAMPLEKEY", 0.99, 0.0, 0.0)]
    )
    ctx = _ctx(tmp_path, store, vector_store, image_parser=OcrImageParser(leaked))

    outcome = ingest_path(ctx, "shot.png")
    assert outcome.status is IngestStatus.skipped
    assert outcome.reason is SkipReason.secret
    assert store.get_document(PROJECT, "shot.png") is None


def test_a_textless_image_is_skipped_by_the_reconcile_path_too(
    tmp_path: Path,
    store,  # type: ignore[no-untyped-def]
    vector_store,  # type: ignore[no-untyped-def]
) -> None:
    """`reindex_vault` handles `NoTextFound` separately from `ingest_path` — pin both.

    Found live, not in a unit test: `ingest_path` caught it, the reconcile did not, so a
    single blank screenshot made `reindex` report `failed: 1` and park a dead job.
    """
    write_vault(tmp_path, {"blank.png": BLANK_IMAGE.read_bytes(), "note.md": NOTE})
    stats = reindex_vault(
        project_id=PROJECT,
        vault_path=tmp_path / "vault",
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        image_parser=OcrImageParser(FakeOcrEngine([])),
    )
    assert stats.failed == 0
    assert stats.skipped == 1
    assert stats.created == 1  # the note still indexed
    assert store.get_document(PROJECT, "blank.png") is None


def test_images_are_skipped_while_pdfs_still_index(tmp_path: Path, store, vector_store) -> None:  # type: ignore[no-untyped-def]
    """The per-format guard: OCR missing must not turn every image into a dead job.

    A single "any parser loaded?" test would let images through to the worker the moment
    the `pdf` extra happened to be installed, and each one would fail rather than skip.
    """
    write_vault(tmp_path, {"shot.png": IMAGE_BYTES, "note.md": NOTE})
    stats = reindex_vault(
        project_id=PROJECT,
        vault_path=tmp_path / "vault",
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
        pdf_parser=object(),  # PDF support present…
        image_parser=None,  # …OCR absent
    )
    assert stats.failed == 0
    assert stats.skipped == 1
    assert stats.created == 1  # the note still indexed


def test_a_vault_of_notes_and_images_still_indexes_its_notes(
    tmp_path: Path,
    store,  # type: ignore[no-untyped-def]
    vector_store,  # type: ignore[no-untyped-def]
) -> None:
    write_vault(tmp_path, {"shot.png": IMAGE_BYTES, "note.md": NOTE})
    stats = reindex_vault(
        project_id=PROJECT,
        vault_path=tmp_path / "vault",
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
    )
    assert stats.failed == 0
    assert stats.skipped == 1
    assert store.get_document(PROJECT, "note.md") is not None


# ---------------------------------------------------------------------------
# Real OCR (needs an engine)
# ---------------------------------------------------------------------------
@requires_ocr
def test_real_ocr_reads_the_fixture_in_reading_order() -> None:
    parser = load_image_parser()
    assert parser is not None
    doc = parser.parse(path="diagram-shot.png", data=IMAGE_BYTES)

    text = doc.blocks[0].text
    assert "MER-1422" in text  # an identifier BM25 can match exactly
    assert "Meridian" in text
    # Drawn bottom-first; the upper band must still come out first.
    assert text.index("Meridian") < text.index("Retention")


@requires_ocr
def test_real_ocr_on_a_blank_image_finds_nothing() -> None:
    parser = load_image_parser()
    assert parser is not None
    with pytest.raises(NoTextFound):
        parser.parse(path="blank-shot.png", data=BLANK_IMAGE.read_bytes())
