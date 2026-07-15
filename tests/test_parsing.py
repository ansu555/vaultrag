"""Step 4 verify: parse fixture notes -> assert blocks, metadata, links, atomicity.

The planted-secret assertions (:func:`test_secret_in_block_is_redacted`,
:func:`test_secret_never_leaks_via_dataview_extraction`,
:func:`test_secret_never_leaks_via_heading_path`,
:func:`test_multiline_secret_body_is_redacted_not_just_header_line`) are the Step 4
guardrail gate (``CLAUDE.md`` §6 / p0-step-plan.md Step 4): a secret must never
appear anywhere in the parsed ``Document`` -- not in block text, not smuggled into
``metadata.frontmatter`` via dataview extraction, and not smuggled into a
descendant's ``heading_path`` -- mirroring the whole-file gate ``test_scan.py``
already enforces one boundary earlier.
"""

from __future__ import annotations

import json

from vaultrag.models import BlockType, DocType, LinkKind
from vaultrag.parsing import parse_document

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _blob(doc) -> str:  # type: ignore[no-untyped-def]
    """Serialize the whole Document so "never appears anywhere" is one assertion."""
    return json.dumps(doc.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Frontmatter -> typed fields
# ---------------------------------------------------------------------------
def test_frontmatter_typed_fields_promoted() -> None:
    doc = parse_document(
        path="note.md",
        raw_text="""---
title: My Note
tags: [project/vaultrag, status/active]
aliases: [foo, bar]
created: 2026-07-14
custom_key: 42
---
body
""",
    )
    m = doc.metadata
    assert m.title == "My Note"
    assert "project/vaultrag" in m.tags and "status/active" in m.tags
    assert m.aliases == ["foo", "bar"]
    assert m.created_ts is not None
    # typed keys are promoted, not duplicated into the generic frontmatter bucket
    assert m.frontmatter == {"custom_key": 42}


def test_frontmatter_tags_as_comma_string() -> None:
    doc = parse_document(path="n.md", raw_text="---\ntags: a, b/c\n---\nbody\n")
    assert {"a", "b", "b/c"} <= set(doc.metadata.tags)


def test_frontmatter_aliases_as_single_scalar() -> None:
    doc = parse_document(path="n.md", raw_text="---\naliases: solo\n---\nbody\n")
    assert doc.metadata.aliases == ["solo"]


def test_content_hash_and_mtime_passthrough() -> None:
    doc = parse_document(path="n.md", raw_text="body", content_hash="deadbeef", mtime=1752000000)
    assert doc.metadata.hash == "deadbeef"
    assert doc.metadata.modified_ts == 1752000000


def test_title_falls_back_to_first_heading() -> None:
    doc = parse_document(path="n.md", raw_text="# Heading Title\n\nbody\n")
    assert doc.metadata.title == "Heading Title"


def test_title_falls_back_to_filename_stem() -> None:
    doc = parse_document(path="folder/plain-note.md", raw_text="just prose, no heading\n")
    assert doc.metadata.title == "plain-note"


def test_doc_type_is_always_note_in_p0() -> None:
    doc = parse_document(path="n.md", raw_text="body")
    assert doc.metadata.type == DocType.note


# ---------------------------------------------------------------------------
# Wikilinks & embeds
# ---------------------------------------------------------------------------
def test_wikilink_forms() -> None:
    doc = parse_document(
        path="n.md",
        raw_text=(
            "[[Note]] [[Note|Alias]] [[Note#Heading]] [[Note#^blockid]] [[#Same Note Heading]]\n"
        ),
    )
    links = doc.links
    assert len(links) == 5
    assert all(link.kind == LinkKind.wikilink for link in links)
    assert (links[0].target, links[0].anchor, links[0].alias) == ("Note", None, None)
    assert (links[1].target, links[1].anchor, links[1].alias) == ("Note", None, "Alias")
    assert (links[2].target, links[2].anchor, links[2].alias) == ("Note", "Heading", None)
    assert (links[3].target, links[3].anchor, links[3].alias) == ("Note", "^blockid", None)
    assert (links[4].target, links[4].anchor) == ("", "Same Note Heading")


def test_embed_recorded_as_embed_kind() -> None:
    doc = parse_document(path="n.md", raw_text="![[image.png]]\n")
    assert len(doc.links) == 1
    assert doc.links[0].kind == LinkKind.embed
    assert doc.links[0].target == "image.png"


def test_wikilink_anchor_not_mistaken_for_a_tag() -> None:
    # No target ([[#Heading]]) is the trickiest case: nothing but `[` precedes the
    # `#`, so only link-masking-before-tag-extraction saves this.
    doc = parse_document(path="n.md", raw_text="See [[#Overview]] and [[Note#Details]].\n")
    assert doc.metadata.tags == []


# ---------------------------------------------------------------------------
# Tags: inline + frontmatter merge, nested expansion
# ---------------------------------------------------------------------------
def test_nested_tags_expand_and_merge_with_frontmatter() -> None:
    doc = parse_document(
        path="n.md",
        raw_text="---\ntags: [project/vaultrag]\n---\nbody #status/active here\n",
    )
    assert set(doc.metadata.tags) == {"project", "project/vaultrag", "status", "status/active"}


def test_heading_hash_is_not_a_tag() -> None:
    doc = parse_document(path="n.md", raw_text="# Heading\n\nbody\n")
    assert doc.metadata.tags == []


def test_tag_requires_word_boundary_before_hash() -> None:
    # a `#` glued to a preceding word (URL fragment / mid-identifier) is not a tag
    doc = parse_document(path="n.md", raw_text="example.com/page#section and #realtag\n")
    assert doc.metadata.tags == ["realtag"]


# ---------------------------------------------------------------------------
# Callouts / blockquotes
# ---------------------------------------------------------------------------
def test_callout_parsed_with_type_and_text() -> None:
    doc = parse_document(path="n.md", raw_text="> [!warning] Careful\n> this is the body\n")
    (block,) = doc.blocks
    assert block.type == BlockType.callout
    assert block.callout_type == "warning"
    assert block.text == "Careful\nthis is the body"


def test_callout_fold_indicator_stripped() -> None:
    doc = parse_document(path="n.md", raw_text="> [!note]+ Title\n> body\n")
    assert doc.blocks[0].callout_type == "note"


def test_plain_blockquote_is_quote_not_callout() -> None:
    doc = parse_document(path="n.md", raw_text="> just a quote\n> more\n")
    (block,) = doc.blocks
    assert block.type == BlockType.quote
    assert block.callout_type is None


# ---------------------------------------------------------------------------
# Dataview inline fields
# ---------------------------------------------------------------------------
def test_dataview_inline_field_merged_into_frontmatter() -> None:
    doc = parse_document(path="n.md", raw_text="Status:: In Progress\n")
    assert doc.metadata.frontmatter["Status"] == "In Progress"


def test_dataview_field_in_bulleted_line() -> None:
    doc = parse_document(path="n.md", raw_text="- Priority:: High\n")
    assert doc.metadata.frontmatter["Priority"] == "High"


# ---------------------------------------------------------------------------
# Atomic blocks: code, tables, lists
# ---------------------------------------------------------------------------
def test_code_block_atomic_with_lang_and_fake_heading_ignored() -> None:
    doc = parse_document(
        path="n.md",
        raw_text='```python\n# not a real heading\nprint("hi")\n```\n',
    )
    (block,) = doc.blocks
    assert block.type == BlockType.code
    assert block.lang == "python"
    assert "# not a real heading" in block.text
    assert block.is_atomic


def test_indented_code_block_has_no_lang() -> None:
    doc = parse_document(path="n.md", raw_text="    indented code\n    second line\n")
    (block,) = doc.blocks
    assert block.type == BlockType.code
    assert block.lang is None


def test_table_is_one_atomic_block() -> None:
    doc = parse_document(path="n.md", raw_text="| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n")
    assert len(doc.blocks) == 1
    block = doc.blocks[0]
    assert block.type == BlockType.table
    assert block.is_atomic
    assert "| 3 | 4 |" in block.text


def test_nested_list_is_one_atomic_block() -> None:
    doc = parse_document(
        path="n.md",
        raw_text="1. first\n2. second\n   - nested bullet\n   - another\n",
    )
    assert len(doc.blocks) == 1
    assert doc.blocks[0].type == BlockType.list
    assert "nested bullet" in doc.blocks[0].text


def test_hr_produces_no_block() -> None:
    doc = parse_document(path="n.md", raw_text="para one\n\n---\n\npara two\n")
    assert [b.type for b in doc.blocks] == [BlockType.paragraph, BlockType.paragraph]


# ---------------------------------------------------------------------------
# heading_path trail
# ---------------------------------------------------------------------------
def test_heading_path_trail_and_sibling_reset() -> None:
    doc = parse_document(
        path="n.md",
        raw_text="# A\n\n## B\n\nunder b\n\n## C\n\nunder c\n",
    )
    by_text = {b.text: b for b in doc.blocks if b.type in (BlockType.heading, BlockType.paragraph)}
    assert by_text["A"].heading_path == []
    assert by_text["B"].heading_path == ["A"]
    assert by_text["under b"].heading_path == ["A", "B"]
    # C is a sibling of B, not nested under it -- must not inherit B as an ancestor
    assert by_text["C"].heading_path == ["A"]
    assert by_text["under c"].heading_path == ["A", "C"]


def test_multiline_paragraph_line_span() -> None:
    doc = parse_document(path="n.md", raw_text="line one\nline two\n")
    (block,) = doc.blocks
    assert block.text == "line one\nline two"
    assert (block.line_start, block.line_end) == (1, 2)


def test_line_numbers_offset_past_frontmatter() -> None:
    doc = parse_document(
        path="n.md",
        raw_text="---\ntitle: X\ntags: [a]\n---\n# First Heading\n",
    )
    heading = next(b for b in doc.blocks if b.type == BlockType.heading)
    # 4 frontmatter lines (--- / title / tags / ---) precede the heading on line 5.
    assert heading.line_start == 5
    assert heading.line_end == 5


def test_line_numbers_survive_blank_lines_after_frontmatter() -> None:
    """Blank lines between the closing ``---`` and the first content line must not
    shift line numbers: python-frontmatter's ``.content`` strips them, so a body
    sliced from it would report every block N lines early (N = blank lines) —
    corrupting the ``open(path, lines)`` anchors. The body must come from the raw
    text at the computed offset instead."""
    doc = parse_document(
        path="n.md",
        raw_text="---\ntitle: X\n---\n\n# Heading\n\nBody paragraph.\n",
    )
    heading = next(b for b in doc.blocks if b.type == BlockType.heading)
    paragraph = next(b for b in doc.blocks if b.type == BlockType.paragraph)
    assert (heading.line_start, heading.line_end) == (5, 5)
    assert (paragraph.line_start, paragraph.line_end) == (7, 7)


# ---------------------------------------------------------------------------
# Empty / malformed input never crashes (a vault is messy, real user data)
# ---------------------------------------------------------------------------
def test_empty_document_does_not_crash() -> None:
    doc = parse_document(path="folder/empty-note.md", raw_text="")
    assert doc.blocks == []
    assert doc.metadata.tags == []
    assert doc.metadata.title == "empty-note"


def test_malformed_frontmatter_degrades_gracefully() -> None:
    doc = parse_document(path="bad.md", raw_text="---\ntitle: [unclosed\n---\nbody text\n")
    assert doc.metadata.frontmatter == {}  # malformed YAML -> no frontmatter, not a crash


# ---------------------------------------------------------------------------
# Secret guard — the required Step 4 gate
# ---------------------------------------------------------------------------
def test_secret_in_block_is_redacted() -> None:
    doc = parse_document(path="leak.md", raw_text=f"# Note\n\nhere is a key {AWS_KEY} oops\n")
    assert AWS_KEY not in _blob(doc)
    assert "redacted" in doc.blocks[-1].text.lower()


def test_secret_never_leaks_via_dataview_extraction() -> None:
    # Without redact-before-extract ordering, this would be captured as a dataview
    # field literally named "api_key" holding the live key value.
    doc = parse_document(path="leak.md", raw_text=f"api_key:: {AWS_KEY}\n")
    assert AWS_KEY not in _blob(doc)
    assert doc.metadata.frontmatter == {}


def test_secret_never_leaks_via_heading_path() -> None:
    # Without redacting before pushing onto the heading stack, every descendant
    # block's heading_path would carry the raw secret forever.
    doc = parse_document(path="leak.md", raw_text=f"# {AWS_KEY}\n\nchild paragraph\n")
    assert AWS_KEY not in _blob(doc)
    child = next(b for b in doc.blocks if b.type == BlockType.paragraph)
    assert AWS_KEY not in child.heading_path[0]


def test_multiline_secret_body_is_redacted_not_just_header_line() -> None:
    # Only the BEGIN line self-identifies as a rule match; the base64 key body on
    # the following line does not. Block-level (not line-level) redaction is what
    # keeps the body from surviving alongside a "redacted" header line.
    body_marker = "MIIEabcSECRETBODYXYZ"
    pem = (
        f"```\n-----BEGIN RSA PRIVATE KEY-----\n{body_marker}\n-----END RSA PRIVATE KEY-----\n```\n"
    )
    doc = parse_document(path="leak.md", raw_text=pem)
    assert body_marker not in _blob(doc)


def test_secret_filename_convention_still_a_caller_concern() -> None:
    # parse_document has no opinion on filenames (that's Step 3's is_secret_filename
    # gate); it only guards block *content*. Documented here so the boundary is
    # explicit rather than assumed.
    doc = parse_document(path=".env", raw_text="OPENAI_API_KEY=sk-live-not-a-real-pattern\n")
    assert doc.metadata.path == ".env"
