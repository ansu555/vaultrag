"""Images → :class:`~vaultrag.models.Document` via OCR (P2 Stage 6).

A vault accumulates screenshots, photographed whiteboards and exported diagrams whose
only content is text nobody typed. ``parsing-pipeline.md``: "RapidOCR primary, Tesseract
fallback; OCR'd text emitted as a searchable Document". Nothing downstream branches on
image-ness — the parser emits the same ``Document{blocks, metadata, links}`` markdown
does, so chunking / embedding / indexing never learn images exist (arch §5), exactly as
Stage 4 did for PDFs.

**An image with no text is a no-op, not an empty document.** OCR on a photo, an icon or
a gradient returns nothing, and indexing that as a zero-block document would put an
unretrievable row in the store and a "result" with no content in front of an agent.
:class:`OcrImageParser` raises :class:`NoTextFound` instead and the pipeline skips the
file — the same "a skip is not a failure" rule Stage 4 established for a missing extra.

**Two engines behind one protocol** (:class:`OcrEngine`): **RapidOCR** is primary — it
ships with the Docling stack Stage 4 already installs, so the ``pdf`` extra buys OCR for
free — and **Tesseract** is the fallback for installs that have it natively.
:func:`load_image_parser` picks the first that loads and returns ``None`` when neither
does, the ``NoOpReranker`` degradation pattern.

**The optional VLM caption pass stays off and unbuilt** (P2 plan Stage 6): describing an
image needs a GPU or a paid API, and VaultRAG's default install is neither
(``CLAUDE.md`` principle #2). ``ocr_enabled`` gates this parser; there is no
``vlm_caption_enabled`` code path to turn on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from vaultrag.logging import get_logger
from vaultrag.models import Block, BlockType, DocType, Document, DocumentMetadata

log = get_logger(__name__)

__all__ = [
    "IMAGE_SUFFIXES",
    "NoTextFound",
    "OcrEngine",
    "OcrError",
    "OcrImageParser",
    "OcrLine",
    "RapidOcrEngine",
    "TesseractOcrEngine",
    "load_image_parser",
    "load_ocr_engine",
    "rapidocr_available",
    "tesseract_available",
]

# Raster formats worth OCR'ing. Deliberately excludes `.svg` (text already, and a
# markdown-adjacent parser would serve it better) and `.gif` (animation frames are a
# different problem than a still).
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})

# Recognitions below this confidence are dropped. OCR on a noisy region emits
# high-entropy garbage ("l1|_", "^~"), and garbage in the index is worse than a gap:
# it never matches a real query but it does consume a chunk and pollute BM25's
# vocabulary. 0.5 is RapidOCR's own default text score.
_MIN_CONFIDENCE = 0.5

# Two recognitions belong to the same visual line when their vertical centres fall in
# the same band. Expressed in pixels because that is what OCR boxes are in.
_ROW_BAND = 12.0


class OcrError(Exception):
    """An image could not be read (corrupt file, engine failure)."""


class NoTextFound(Exception):
    """The image carries no recognizable text — skip it, do not index an empty document.

    Distinct from :class:`OcrError`: nothing went wrong. A photo with no writing in it
    is the *expected* case for most images in a vault.
    """


@dataclass(frozen=True, slots=True)
class OcrLine:
    """One recognized run of text, with enough geometry to restore reading order."""

    text: str
    confidence: float
    x: float
    y: float


@runtime_checkable
class OcrEngine(Protocol):
    """Image bytes → recognized lines. The seam RapidOCR and Tesseract sit behind."""

    name: str

    def read(self, data: bytes) -> list[OcrLine]:
        """Recognize text. Returns ``[]`` when the image holds none (never raises for that)."""


@runtime_checkable
class ImageParser(Protocol):
    """Convert image bytes into the same ``Document`` shape markdown produces."""

    def parse(
        self,
        *,
        path: str,
        data: bytes,
        content_hash: str | None = None,
        mtime: int | None = None,
    ) -> Document:
        """Parse one image. Raises :class:`NoTextFound` when it carries no text."""


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------
class RapidOcrEngine:
    """:class:`OcrEngine` backed by RapidOCR (ONNX PP-OCR models, Apache-2.0).

    The engine is built once and reused: construction loads three ONNX models, so a
    per-image engine would pay that on every image in a vault.
    """

    name = "rapidocr"

    def __init__(self, engine: Any | None = None) -> None:
        self._engine = engine if engine is not None else _build_rapidocr()

    def read(self, data: bytes) -> list[OcrLine]:
        try:
            result = self._engine(data)
        except Exception as exc:
            raise OcrError(f"rapidocr failed: {exc}") from exc

        # A textless image comes back with `txts`/`boxes` set to None rather than empty
        # tuples, so this is a normal outcome to handle, not a defensive check.
        texts = getattr(result, "txts", None) or ()
        scores = getattr(result, "scores", None) or ()
        boxes = getattr(result, "boxes", None)
        boxes = list(boxes) if boxes is not None else []

        lines: list[OcrLine] = []
        for ix, text in enumerate(texts):
            cleaned = _clean(text)
            if not cleaned:
                continue
            score = float(scores[ix]) if ix < len(scores) else 1.0
            x, y = _box_origin(boxes[ix]) if ix < len(boxes) else (0.0, float(ix))
            lines.append(OcrLine(text=cleaned, confidence=score, x=x, y=y))
        return lines


class TesseractOcrEngine:
    """:class:`OcrEngine` backed by Tesseract via ``pytesseract`` (the plan's fallback).

    Used only when RapidOCR is absent. Tesseract reports per-word boxes and confidences,
    so words are grouped back into lines by its own ``block/par/line`` numbering rather
    than by geometry — the engine already knows the grouping, and re-deriving it from
    coordinates would be strictly worse.
    """

    name = "tesseract"

    def __init__(self, pytesseract: Any | None = None) -> None:
        self._pt = pytesseract if pytesseract is not None else _import_pytesseract()

    def read(self, data: bytes) -> list[OcrLine]:
        import io

        from PIL import Image

        try:
            image = Image.open(io.BytesIO(data))
            raw = self._pt.image_to_data(image, output_type=self._pt.Output.DICT)
        except Exception as exc:
            raise OcrError(f"tesseract failed: {exc}") from exc

        grouped: dict[tuple[int, int, int], list[tuple[str, float, int, int]]] = {}
        for ix, word in enumerate(raw.get("text", [])):
            cleaned = _clean(word)
            if not cleaned:
                continue
            try:
                conf = float(raw["conf"][ix])
            except (KeyError, IndexError, TypeError, ValueError):
                conf = -1.0
            if conf < 0:  # Tesseract's "no confidence" sentinel
                continue
            key = (raw["block_num"][ix], raw["par_num"][ix], raw["line_num"][ix])
            grouped.setdefault(key, []).append(
                (cleaned, conf / 100.0, int(raw["left"][ix]), int(raw["top"][ix]))
            )

        lines: list[OcrLine] = []
        for words in grouped.values():
            text = _clean(" ".join(w[0] for w in words))
            if not text:
                continue
            lines.append(
                OcrLine(
                    text=text,
                    confidence=sum(w[1] for w in words) / len(words),
                    x=float(min(w[2] for w in words)),
                    y=float(min(w[3] for w in words)),
                )
            )
        return lines


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class OcrImageParser:
    """:class:`ImageParser` that turns recognized lines into a searchable document."""

    def __init__(self, engine: OcrEngine) -> None:
        self._engine = engine

    def parse(
        self,
        *,
        path: str,
        data: bytes,
        content_hash: str | None = None,
        mtime: int | None = None,
    ) -> Document:
        lines = [ln for ln in self._engine.read(data) if ln.confidence >= _MIN_CONFIDENCE]
        if not lines:
            raise NoTextFound(path)

        text = "\n".join(ln.text for ln in _in_reading_order(lines))
        title = Path(path).stem
        log.info("image.ocr", path=path, engine=self._engine.name, lines=len(lines))

        # One paragraph block: an image has no heading structure to give the chunker, so
        # inventing one (a heading per band, say) would fabricate a hierarchy the source
        # does not have. The filename is the only real title an image carries.
        return Document(
            metadata=DocumentMetadata(
                path=path,
                title=title,
                type=DocType.image,
                hash=content_hash,
                modified_ts=mtime,
            ),
            blocks=[Block(type=BlockType.paragraph, text=text, heading_path=[])],
            links=[],
        )


def _in_reading_order(lines: list[OcrLine]) -> list[OcrLine]:
    """Sort by banded ``y`` then ``x``.

    Detection order follows the model's box ranking, not the page's layout, so a
    two-column screenshot would otherwise interleave its columns. Banding ``y`` keeps a
    single visual line together when its boxes differ by a pixel or two.
    """
    return sorted(lines, key=lambda ln: (round(ln.y / _ROW_BAND), ln.x))


def _box_origin(box: Any) -> tuple[float, float]:
    """Top-left ``(x, y)`` of a 4-point OCR polygon, defensively."""
    try:
        points = [(float(p[0]), float(p[1])) for p in box]
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)
    if not points:
        return (0.0, 0.0)
    return (min(p[0] for p in points), min(p[1] for p in points))


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Availability / composition root
# ---------------------------------------------------------------------------
def _build_rapidocr() -> Any:
    from rapidocr import RapidOCR

    return RapidOCR()


def _import_pytesseract() -> Any:
    import pytesseract

    # `pytesseract` imports fine without the `tesseract` binary and only fails at call
    # time, so probe the binary here — availability must mean "will work".
    pytesseract.get_tesseract_version()
    return pytesseract


def rapidocr_available() -> bool:
    """Whether RapidOCR is importable. Import-only — loads no models."""
    from importlib.util import find_spec

    try:
        return find_spec("rapidocr") is not None
    except (ImportError, ValueError):  # a broken/partial install is "not available"
        return False


def tesseract_available() -> bool:
    """Whether ``pytesseract`` *and* the Tesseract binary it shells out to are present."""
    try:
        _import_pytesseract()
    except Exception:
        return False
    return True


def load_ocr_engine() -> OcrEngine | None:
    """The first OCR engine that loads: RapidOCR, else Tesseract, else ``None``."""
    if rapidocr_available():
        try:
            return RapidOcrEngine()
        except Exception as exc:  # present-but-unusable degrades, never crashes
            log.warning("image.rapidocr_load_failed", error=str(exc))
    if tesseract_available():
        try:
            return TesseractOcrEngine()
        except Exception as exc:
            log.warning("image.tesseract_load_failed", error=str(exc))
    return None


def load_image_parser(*, enabled: bool = True) -> ImageParser | None:
    """Composition-root helper: build the image parser, or ``None`` if unavailable.

    ``None`` is a supported state, not an error — a base install has no OCR engine and a
    vault full of screenshots must still index its markdown. Callers skip images when
    this returns ``None`` (logged once, at the scan boundary), the same contract
    :func:`~vaultrag.parsing.pdf.load_pdf_parser` has.
    """
    if not enabled:
        return None
    engine = load_ocr_engine()
    if engine is None:
        log.info(
            "image.parser_unavailable",
            reason="no OCR engine installed",
            extra="vaultrag[pdf] (RapidOCR) or tesseract",
        )
        return None
    return OcrImageParser(engine)
