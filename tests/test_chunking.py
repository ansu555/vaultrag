"""Step 5 verify: chunk fixtures -> assert token bounds, prefix content, stable
chunk_hash, correct parent/sibling links (p0-step-plan.md Step 5).

Chunks are built end-to-end through the real Step 4 parser (``parse_document``)
rather than hand-built ``Document``/``Block`` objects, so these tests exercise the
same ``heading_path`` shape the chunker actually receives in the pipeline.
"""

from __future__ import annotations

from vaultrag.chunking import HARD_MAX_TOKENS, MERGE_MIN_TOKENS, TARGET_TOKENS, chunk_document
from vaultrag.models import Chunk, chunk_point_id, document_id
from vaultrag.parsing import parse_document

PROJECT = "proj1"


def _chunks(path: str, raw_text: str) -> list[Chunk]:
    doc = parse_document(path=path, raw_text=raw_text)
    return chunk_document(doc, project_id=PROJECT)


# ---------------------------------------------------------------------------
# Basic shape: one section -> one chunk
# ---------------------------------------------------------------------------
def test_single_section_becomes_one_chunk_with_heading_path_and_anchor() -> None:
    chunks = _chunks(
        "notes/my-note.md",
        "---\ntags: [demo]\n---\n# My Heading\n\nJust a paragraph of content here.\n",
    )
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.heading_path == ["My Heading"]
    assert chunk.anchor == "My Heading"
    assert "Just a paragraph of content here." in chunk.text
    assert chunk.tags == ["demo"]


def test_contextual_prefix_format() -> None:
    chunks = _chunks("notes/my-note.md", "---\ntags: [demo]\n---\n# My Heading\n\nBody text.\n")
    assert chunks[0].contextual_prefix == "«proj1 ▸ notes/my-note ▸ My Heading»\nTags: demo"


def test_contextual_prefix_omits_tags_line_when_no_tags() -> None:
    chunks = _chunks("plain.md", "Just a plain paragraph, no heading at all.\n")
    assert chunks[0].contextual_prefix == "«proj1 ▸ plain»"
    assert "Tags:" not in chunks[0].contextual_prefix


def test_root_level_content_before_any_heading_has_no_anchor() -> None:
    chunks = _chunks("plain.md", "Just a plain paragraph, no heading at all.\n")
    assert len(chunks) == 1
    assert chunks[0].heading_path == []
    assert chunks[0].anchor is None


def test_heading_with_no_direct_content_contributes_no_standalone_chunk() -> None:
    # "Overview" has nothing directly under it before "Details" -- it should only
    # appear as an ancestor in the descendant's heading_path, not its own chunk.
    chunks = _chunks("n.md", "# Overview\n\n## Details\n\nSome content under Details.\n")
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Overview", "Details"]


def test_empty_document_yields_no_chunks() -> None:
    chunks = _chunks("empty.md", "---\ntitle: Empty\n---\n")
    assert chunks == []


# ---------------------------------------------------------------------------
# Atomic blocks are never split, even when oversized
# ---------------------------------------------------------------------------
def test_oversized_atomic_code_block_is_not_split() -> None:
    huge_code = "a" * 3000  # ~750 tokens at 4 chars/token, well over the hard max
    chunks = _chunks("n.md", f"## Big\n\n```python\n{huge_code}\n```\n")
    assert len(chunks) == 1
    assert chunks[0].tokens > HARD_MAX_TOKENS  # exceeds hard max but wasn't split
    assert chunks[0].text == huge_code
    assert chunks[0].has_code is True
    assert chunks[0].langs == ["python"]


# ---------------------------------------------------------------------------
# Tiny-sibling merge
# ---------------------------------------------------------------------------
def test_tiny_sibling_sections_merge_into_one_chunk() -> None:
    chunks = _chunks(
        "n.md",
        "# Root\n\n## TermOne\nshort text a.\n\n## TermTwo\nshort text b.\n",
    )
    assert len(chunks) == 1
    merged = chunks[0]
    assert merged.heading_path == ["Root", "TermOne"]  # earlier piece's identity wins
    assert "short text a." in merged.text
    assert "short text b." in merged.text
    assert merged.tokens > 0


def test_root_content_does_not_merge_with_first_top_level_heading() -> None:
    # Both heading_path == [] (root) and heading_path == ["Root"] have an empty
    # `[:-1]`, so a naive parent check would wrongly treat them as siblings.
    chunks = _chunks("n.md", "Intro paragraph before any heading.\n\n# Root\nshort.\n")
    assert len(chunks) == 2
    assert chunks[0].heading_path == []
    assert chunks[1].heading_path == ["Root"]


# ---------------------------------------------------------------------------
# Token bounds within one big section
# ---------------------------------------------------------------------------
def test_large_section_splits_at_target_and_respects_hard_max() -> None:
    paragraphs = [f"[P{i}] " + ("filler sentence padding words here. " * 12) for i in range(6)]
    body = "## Big Section\n\n" + "\n\n".join(paragraphs) + "\n"
    chunks = _chunks("n.md", body)

    assert len(chunks) > 1  # 6 * ~115 tok >> hard max, so it must have split
    for chunk in chunks:
        assert chunk.tokens <= HARD_MAX_TOKENS
        assert chunk.heading_path == ["Big Section"]

    # every paragraph shows up exactly once, in order, across the chunk sequence
    all_text = "\n\n".join(c.text for c in chunks)
    positions = [all_text.index(f"[P{i}]") for i in range(6)]
    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# prev_id / next_id stitching
# ---------------------------------------------------------------------------
def test_prev_next_stitching_across_sibling_chunks() -> None:
    filler = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do. " * 8
    body = f"# Root\n\n## A\n{filler}\n\n## B\n{filler}\n\n## C\n{filler}\n"
    chunks = _chunks("n.md", body)

    assert len(chunks) == 3
    assert chunks[0].prev_id is None
    assert chunks[0].next_id == chunks[1].point_id
    assert chunks[1].prev_id == chunks[0].point_id
    assert chunks[1].next_id == chunks[2].point_id
    assert chunks[2].prev_id == chunks[1].point_id
    assert chunks[2].next_id is None


# ---------------------------------------------------------------------------
# parent_id: deferred to P1 (parent-swap not built yet)
# ---------------------------------------------------------------------------
def test_parent_id_is_none_in_p0() -> None:
    chunks = _chunks("n.md", "# H\n\nbody\n")
    assert all(c.parent_id is None for c in chunks)


# ---------------------------------------------------------------------------
# Identity: doc_id / point_id / chunk_hash
# ---------------------------------------------------------------------------
def test_doc_id_and_point_id_are_deterministic() -> None:
    chunks = _chunks("notes/foo.md", "# H\n\nbody\n")
    assert chunks[0].doc_id == document_id(PROJECT, "notes/foo.md")
    assert chunks[0].point_id == chunk_point_id(PROJECT, "notes/foo.md", 0)


def test_chunk_hash_is_stable_across_reruns() -> None:
    raw = "# H\n\nsome body text\n"
    first = _chunks("n.md", raw)
    second = _chunks("n.md", raw)
    assert [c.chunk_hash for c in first] == [c.chunk_hash for c in second]
    assert [c.model_dump(mode="json") for c in first] == [c.model_dump(mode="json") for c in second]


def test_target_and_hard_max_constants_are_sane() -> None:
    assert 0 < MERGE_MIN_TOKENS < TARGET_TOKENS < HARD_MAX_TOKENS
