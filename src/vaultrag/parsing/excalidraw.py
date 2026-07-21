"""Excalidraw scenes → :class:`~vaultrag.models.Document` (P2 Stage 5).

``parsing-pipeline.md``: "``.excalidraw``/``.excalidraw.md`` JSON → extract ``text``
elements + frame names as searchable text. Plugin's ``compressed-json`` blocks →
LZ-string decompress first." The optional VLM caption pass stays off — it costs GPU
or money (principle #2).

**Two file shapes, one parser.** A bare ``.excalidraw`` is the scene JSON. Obsidian's
Excalidraw plugin instead writes ``<name>.excalidraw.md``: a markdown wrapper whose
``## Drawing`` fence holds the scene, either as plain ``json`` (``excalidraw-plugin:
raw``) or as LZ-string-compressed base64 (``compressed-json``, the default).

**The wrapper is why this is not just "index the markdown".** That base64 blob is
routinely tens of kilobytes of one unbroken token; left alone it is parsed as a code
block and embedded like prose, spending the chunk budget of a whole note on something
no query can ever usefully match. Reading the scene and dropping the blob is a
correctness fix, not only a feature.

**Structure comes from frames.** Excalidraw frames are the drawing's own grouping, so
each frame becomes an H2 and the text inside it becomes that section's body — which
is exactly the shape the heading-tree chunker already knows how to cut (arch §6).
Within a group, elements are emitted in *reading* order (banded top-to-bottom, then
left-to-right) rather than the scene's z-order, so a row of boxes reads as a row.

Degradation is graceful at every step: an unreadable scene falls back to the markdown
wrapper (minus the blob), and a bare ``.excalidraw`` that will not parse raises
:class:`ExcalidrawError` for the pipeline to record as a failed file — never a crash
that takes the rest of the vault with it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from vaultrag.logging import get_logger
from vaultrag.models import Block, BlockType, DocType, Document, DocumentMetadata, Link
from vaultrag.parsing.frontmatter import (
    coerce_epoch,
    coerce_str_list,
    custom_frontmatter,
    split_frontmatter,
)
from vaultrag.parsing.markdown import parse_document
from vaultrag.parsing.obsidian import expand_nested_tags, extract_obsidian_features

log = get_logger(__name__)

__all__ = [
    "EXCALIDRAW_SUFFIX",
    "ExcalidrawError",
    "is_excalidraw_source",
    "is_scene_file",
    "parse_excalidraw",
    "readable_text",
    "scene_title",
]

EXCALIDRAW_SUFFIX = ".excalidraw"
_MARKDOWN_SUFFIX = ".excalidraw.md"

# The plugin's own frontmatter marker. Checked as well as the filename because the
# suffix is a convention a user can change, while this key is written by the plugin
# on every save — without it, a renamed drawing would silently index its base64 blob.
_PLUGIN_KEY = "excalidraw-plugin"

# ```compressed-json / ```json fence inside the markdown wrapper.
_DRAWING_FENCE_RE = re.compile(
    r"^[ \t]*(?P<ticks>`{3,}|~{3,})[ \t]*(?P<lang>compressed-json|json)[ \t]*$\n"
    r"(?P<body>.*?)^[ \t]*(?P=ticks)[ \t]*$",
    re.M | re.S,
)

# Elements laid out within this many pixels of each other count as one row, so a
# left-to-right row of boxes is emitted in reading order instead of by y to the pixel.
_ROW_BAND = 40.0


class ExcalidrawError(Exception):
    """An Excalidraw file could not be read as a scene (corrupt JSON / bad blob)."""


def is_excalidraw_source(path: str, text: str) -> bool:
    """Whether ``path``/``text`` is an Excalidraw scene rather than an ordinary note.

    Filename first (cheap), then the plugin's frontmatter key, which is what catches a
    drawing that was renamed away from the ``.excalidraw.md`` convention.
    """
    lowered = path.casefold()
    if lowered.endswith(EXCALIDRAW_SUFFIX) or lowered.endswith(_MARKDOWN_SUFFIX):
        return True
    if not lowered.endswith(".md"):
        return False
    return _PLUGIN_KEY in text[:2048]


def is_scene_file(path: str) -> bool:
    """Whether ``path`` is a bare ``.excalidraw`` — a scene with no prose wrapper.

    The distinction matters to ``open``: a wrapper has human-readable sections worth
    serving above its payload, a bare scene is machine data end to end.
    """
    lowered = path.casefold()
    return lowered.endswith(EXCALIDRAW_SUFFIX) and not lowered.endswith(".md")


def scene_title(path: str, raw_text: str) -> str:
    """The title this drawing is indexed under.

    Exists so ``open`` can report the same title ``search`` cites. Reading the wrapper
    as plain markdown would instead surface its first heading — the plugin's own
    ``# Excalidraw Data`` scaffolding — and the two tools would disagree about what one
    file is called.
    """
    frontmatter, _body = split_frontmatter(path, raw_text)
    return _title(frontmatter, path)


def readable_text(raw_text: str) -> str:
    """An Excalidraw wrapper cut down to what a human (or agent) can read.

    Everything from the ``## Drawing`` payload onward is removed. This is what ``open``
    serves: the blob is tens of kilobytes of unbroken base64, and handing that to an
    agent burns its context on bytes no reader can use.

    Deliberately a **suffix** cut, so every retained line keeps the number it has on
    disk — a citation or a ``lines=[a, b]`` slice into the kept region stays exactly
    true, which a mid-file excision would break.
    """
    m = _DRAWING_FENCE_RE.search(raw_text)
    if m is None:
        return raw_text
    head = raw_text[: m.start()].splitlines()
    # The plugin wraps the payload in `%%` (an Obsidian comment) and heads it with
    # `## Drawing`; both are scaffolding for the part being removed.
    while head and head[-1].strip() in {"", "%%", "## Drawing"}:
        head.pop()
    return "\n".join(head) + "\n" if head else ""


def parse_excalidraw(
    *,
    path: str,
    raw_text: str,
    content_hash: str | None = None,
    mtime: int | None = None,
) -> Document:
    """Parse one Excalidraw file into the same ``Document`` shape markdown produces.

    Raises :class:`ExcalidrawError` only for a bare ``.excalidraw`` whose JSON is
    unreadable — there is nothing else in that file to fall back to. The markdown
    wrapper always has a fallback (:func:`_fallback_document`).
    """
    if not path.casefold().endswith(".md"):
        return _document_from_scene(
            path=path,
            scene=_load_scene_json(path, raw_text),
            frontmatter={},
            extra_links=[],
            content_hash=content_hash,
            mtime=mtime,
        )

    frontmatter, body = split_frontmatter(path, raw_text)
    stripped, scene_text = _split_drawing(body)
    # Wikilinks in the wrapper's own `## Element Links` / `## Embedded Files` sections
    # are real edges (element → note, drawing → pasted image); the scene JSON does not
    # carry them, so they are collected from the stripped body and merged.
    extra_links, _tags, _dv = extract_obsidian_features(stripped)

    scene = _try_scene(path, scene_text)
    if scene is None:
        return _fallback_document(
            path=path, stripped=stripped, content_hash=content_hash, mtime=mtime
        )
    return _document_from_scene(
        path=path,
        scene=scene,
        frontmatter=frontmatter,
        extra_links=extra_links,
        content_hash=content_hash,
        mtime=mtime,
    )


# ---------------------------------------------------------------------------
# Scene recovery
# ---------------------------------------------------------------------------
def _split_drawing(body: str) -> tuple[str, str | None]:
    """Split the wrapper into (body without the drawing fence, the fence's contents)."""
    m = _DRAWING_FENCE_RE.search(body)
    if m is None:
        return body, None
    payload = m.group("body")
    if m.group("lang") == "compressed-json":
        payload = _decompress(payload)
    return (body[: m.start()] + body[m.end() :]), payload


def _decompress(payload: str) -> str:
    """LZ-string base64 → the scene JSON (``""`` when it cannot be decoded).

    All whitespace is stripped first: the plugin hard-wraps the blob across lines, and
    those newlines are not part of the base64 alphabet.
    """
    packed = "".join(payload.split())
    if not packed:
        return ""
    try:
        import lzstring

        return lzstring.LZString().decompressFromBase64(packed) or ""
    except Exception as exc:  # a truncated/edited blob must not fail the file
        log.warning("excalidraw.decompress_failed", error=str(exc))
        return ""


def _try_scene(path: str, text: str | None) -> dict[str, Any] | None:
    """Parse recovered scene text, or ``None`` to take the markdown fallback."""
    if not text or not text.strip():
        return None
    try:
        return _load_scene_json(path, text)
    except ExcalidrawError as exc:
        log.warning("excalidraw.scene_unreadable", path=path, error=str(exc))
        return None


def _load_scene_json(path: str, text: str) -> dict[str, Any]:
    try:
        scene = json.loads(text)
    except ValueError as exc:
        raise ExcalidrawError(f"{path!r}: scene is not valid JSON: {exc}") from exc
    if not isinstance(scene, dict):
        raise ExcalidrawError(f"{path!r}: scene JSON is not an object")
    return scene


# ---------------------------------------------------------------------------
# Scene → Document
# ---------------------------------------------------------------------------
def _document_from_scene(
    *,
    path: str,
    scene: dict[str, Any],
    frontmatter: dict[str, Any],
    extra_links: list[Link],
    content_hash: str | None,
    mtime: int | None,
) -> Document:
    elements = [e for e in _elements(scene) if not e.get("isDeleted")]
    frames = _in_reading_order([e for e in elements if e.get("type") == "frame"])
    texts = [e for e in elements if e.get("type") == "text"]

    blocks: list[Block] = []
    links: list[Link] = list(extra_links)
    tag_pool: list[str] = []
    dataview: dict[str, str] = {}

    def emit(block: Block) -> None:
        blocks.append(block)
        b_links, b_tags, b_dv = extract_obsidian_features(block.text)
        links.extend(b_links)
        tag_pool.extend(b_tags)
        dataview.update(b_dv)

    def emit_texts(group: list[dict[str, Any]], heading_path: list[str]) -> None:
        for element in _in_reading_order(group):
            text = _element_text(element)
            if text:
                emit(Block(type=BlockType.paragraph, text=text, heading_path=heading_path))

    frame_ids = {f.get("id") for f in frames if f.get("id") is not None}
    # Unframed text first: it is the drawing's own top level, and putting it under the
    # first frame's heading would attribute it to a group it is not in.
    emit_texts([t for t in texts if t.get("frameId") not in frame_ids], [])

    for frame in frames:
        name = _clean(frame.get("name")) or "Frame"
        emit(Block(type=BlockType.heading, text=name, level=2, heading_path=[]))
        emit_texts([t for t in texts if t.get("frameId") == frame.get("id")], [name])

    # An element's `link` property is a first-class Obsidian edge (right-click → link).
    for element in elements:
        raw_link = element.get("link")
        if isinstance(raw_link, str) and raw_link:
            e_links, _t, _d = extract_obsidian_features(raw_link)
            links.extend(e_links)

    fm_tags = coerce_str_list(frontmatter.get("tags"))
    metadata = DocumentMetadata(
        path=path,
        title=_title(frontmatter, path),
        type=DocType.excalidraw,
        hash=content_hash,
        tags=expand_nested_tags([*fm_tags, *tag_pool]),
        aliases=coerce_str_list(frontmatter.get("aliases")),
        frontmatter=_merged_frontmatter(frontmatter, dataview),
        created_ts=coerce_epoch(frontmatter.get("created")),
        modified_ts=coerce_epoch(frontmatter.get("modified")) or mtime,
    )
    return Document(metadata=metadata, blocks=blocks, links=_dedupe_links(links))


def _elements(scene: dict[str, Any]) -> list[dict[str, Any]]:
    raw = scene.get("elements")
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _element_text(element: dict[str, Any]) -> str:
    """A text element's content.

    ``originalText`` is preferred over ``text``: the latter carries the newlines
    Excalidraw inserts to wrap the label to its box width, which would put line breaks
    in the middle of indexed sentences.
    """
    for key in ("originalText", "text"):
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            return _clean(value)
    return ""


def _in_reading_order(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by banded ``y`` then ``x`` — a scene lists elements in z-order, not reading
    order, so without this a diagram is indexed in the order it happened to be drawn."""
    ordered = sorted(
        enumerate(elements),
        key=lambda pair: (
            round(_coord(pair[1], "y") / _ROW_BAND),
            _coord(pair[1], "x"),
            pair[0],  # stable for elements sharing a position (or carrying no geometry)
        ),
    )
    return [element for _ix, element in ordered]


def _coord(element: dict[str, Any], key: str) -> float:
    value = element.get(key)
    return float(value) if isinstance(value, int | float) else 0.0


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def _title(frontmatter: dict[str, Any], path: str) -> str:
    fm_title = frontmatter.get("title")
    if isinstance(fm_title, str) and fm_title.strip():
        return fm_title.strip()
    return _strip_suffixes(path.rsplit("/", 1)[-1])


def _strip_suffixes(name: str) -> str:
    """``Ingest Flow.excalidraw.md`` → ``Ingest Flow`` (a no-op on any other string).

    Longest suffix first, so the ``.md`` wrapper does not leave a stray ``.excalidraw``.
    """
    for suffix in (_MARKDOWN_SUFFIX, EXCALIDRAW_SUFFIX):
        if name.casefold().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _merged_frontmatter(frontmatter: dict[str, Any], dataview: dict[str, str]) -> dict[str, Any]:
    merged = custom_frontmatter(frontmatter)
    merged.pop(_PLUGIN_KEY, None)  # a storage-format marker, not vault metadata
    merged.update(dataview)
    return merged


def _dedupe_links(links: list[Link]) -> list[Link]:
    """First occurrence wins, in source order (`store.links.link_edges` dedupes again
    on the normalized key; this keeps the parsed document itself clean)."""
    out: list[Link] = []
    seen: set[tuple[str, str, str | None]] = set()
    for link in links:
        ident = (str(link.kind), link.target, link.anchor)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(link)
    return out


def _fallback_document(
    *, path: str, stripped: str, content_hash: str | None, mtime: int | None
) -> Document:
    """No readable scene: index the wrapper's markdown, minus the drawing blob.

    Still strictly better than leaving the file to the markdown parser, because the
    base64 payload is already gone by the time this runs.
    """
    log.info("excalidraw.scene_fallback", path=path)
    document = parse_document(path=path, raw_text=stripped, content_hash=content_hash, mtime=mtime)
    metadata = document.metadata.model_copy(
        update={
            "type": DocType.excalidraw,
            # `parse_document` falls back to the filename stem, which for a wrapper is
            # "Ingest Flow.excalidraw"; a title from frontmatter or a heading is untouched.
            "title": _strip_suffixes(document.metadata.title),
        }
    )
    return document.model_copy(update={"metadata": metadata})
