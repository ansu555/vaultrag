"""Markdown -> normalized ``Document`` (P0 Step 4).

markdown-it-py tokenizes CommonMark + GFM tables (the ``"default"`` preset; arch §5,
``../Rag/projects/vaultrag/parsing-pipeline.md``). Wikilinks/embeds/tags/callouts/
dataview are Obsidian conventions markdown-it doesn't know about, layered on top by
``obsidian.py``. Code fences, tables, lists, and blockquotes are kept atomic here --
the chunker (Step 5) must never split them, so the parser never fragments them into
sub-blocks either.

Every block's text is checked against the built-in secret guard
(``indexer.secret_guard.scan_for_secrets``) at the point it's extracted -- before it
can reach ``heading_stack`` (and thus descendants' ``heading_path``) or the
wikilink/tag/dataview regexes. The vault scan (Step 3) already withholds an entire
file the instant any secret pattern appears in it, so in the normal pipeline this
never fires; it exists as a block-level backstop for direct/standalone use of this
parser (tests, a future watch-mode, programmatic callers) that don't route through
the scan gate first. Redaction is block-level, not line-level: a PEM private key's
body lines don't self-identify (only the ``-----BEGIN...`` header line matches a
rule), so scanning line-by-line would strip the header and leave the key body
searchable. Quarantining the whole block closes that gap.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

import structlog
from markdown_it import MarkdownIt
from markdown_it.token import Token

from vaultrag.indexer.secret_guard import scan_for_secrets
from vaultrag.models import Block, BlockType, DocType, Document, DocumentMetadata, Link
from vaultrag.parsing.frontmatter import (
    coerce_epoch,
    coerce_str_list,
    custom_frontmatter,
    frontmatter_line_offset,
    split_frontmatter,
)
from vaultrag.parsing.mermaid import extract_diagram_links, is_mermaid_lang
from vaultrag.parsing.obsidian import (
    expand_nested_tags,
    extract_obsidian_features,
    parse_callout_header,
)

log = structlog.get_logger(__name__)

# typographer (smart quotes/dashes) and raw html both mutate source text away from
# what's on disk -- off, so search stays exact-match faithful to the vault (arch §1
# principle #5: dev notes are full of identifiers BM25 must match verbatim).
_MD = MarkdownIt("default", {"typographer": False, "html": False})

_REDACTED_TEXT = "[redacted: secret content withheld from index]"

# *_open tokens whose `.map` spans the whole container (including nested children)
# -- sliced straight from source lines instead of walked, so the chunker never sees
# a fragment of a table/list/quote (arch §6, atomic blocks).
_CONTAINER_OPEN_TO_TYPE = {
    "table_open": BlockType.table,
    "bullet_list_open": BlockType.list,
    "ordered_list_open": BlockType.list,
    "blockquote_open": BlockType.quote,  # callouts are a blockquote sub-case
}


def parse_document(
    *,
    path: str,
    raw_text: str,
    content_hash: str | None = None,
    mtime: int | None = None,
) -> Document:
    """Parse one markdown file's raw text into a normalized :class:`Document`.

    ``path`` is the vault-relative POSIX path (the stable document key);
    ``content_hash``/``mtime`` come straight from the scan step's ``ScannedFile``
    (Step 3) and land in ``DocumentMetadata.hash``/``modified_ts``.
    """
    fm_raw, body = split_frontmatter(path, raw_text)
    line_offset = frontmatter_line_offset(raw_text)
    source_lines = body.splitlines()

    tokens = _MD.parse(body)
    blocks, links, inline_tags, dataview_fields = _build_blocks(
        tokens, source_lines, line_offset, path
    )

    fm_tags = coerce_str_list(fm_raw.get("tags"))
    tags = expand_nested_tags([*fm_tags, *inline_tags])
    aliases = coerce_str_list(fm_raw.get("aliases"))
    title = _resolve_title(fm_raw.get("title"), blocks, path)

    custom_fm = custom_frontmatter(fm_raw)
    custom_fm.update(dataview_fields)

    metadata = DocumentMetadata(
        path=path,
        title=title,
        type=DocType.note,
        hash=content_hash,
        tags=tags,
        aliases=aliases,
        frontmatter=custom_fm,
        created_ts=coerce_epoch(fm_raw.get("created")),
        modified_ts=coerce_epoch(fm_raw.get("modified")) or mtime,
    )
    return Document(metadata=metadata, blocks=blocks, links=links)


def _resolve_title(fm_title: Any, blocks: list[Block], path: str) -> str:
    """Frontmatter ``title`` wins; else the first heading; else the filename stem."""
    if isinstance(fm_title, str) and fm_title.strip():
        return fm_title.strip()
    for block in blocks:
        if block.type == BlockType.heading:
            return block.text
    return PurePosixPath(path).stem


# ---------------------------------------------------------------------------
# Token walk -> blocks
# ---------------------------------------------------------------------------
def _build_blocks(
    tokens: list[Token],
    source_lines: list[str],
    line_offset: int,
    path: str,
) -> tuple[list[Block], list[Link], list[str], dict[str, str]]:
    """Walk the flat markdown-it token stream into source-ordered ``Block``s.

    Also accumulates the document-level link/tag/dataview pools by running the
    Obsidian regexes over each non-code block's (already-redacted) text.
    """
    blocks: list[Block] = []
    links: list[Link] = []
    tag_pool: list[str] = []
    dataview: dict[str, str] = {}
    heading_stack: list[tuple[int, str]] = []

    def current_path() -> list[str]:
        return [text for _, text in heading_stack]

    def collect(block: Block) -> None:
        blocks.append(block)
        if block.type == BlockType.code:
            # Code contents aren't prose; never mined for links/tags/fields. A Mermaid
            # fence is the one exception, and only for *diagram* edges (P2 Stage 5): its
            # node labels are graph data a human drew, while a `[[...]]` or `#tag` that
            # happens to sit in code is still a code literal, not a link.
            if is_mermaid_lang(block.lang):
                links.extend(extract_diagram_links(block.text))
            return
        b_links, b_tags, b_dv = extract_obsidian_features(block.text)
        links.extend(b_links)
        tag_pool.extend(b_tags)
        dataview.update(b_dv)

    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]

        if t.type == "heading_open":
            level = int(t.tag[1:])
            text = _redact(tokens[i + 1].content, path, BlockType.heading)
            # A new heading closes every open heading at its level or deeper (a
            # sibling H2 replaces the previous H2; a lower H1 closes any open H2/H3).
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            ancestors = current_path()  # this heading's own trail, ancestors only
            heading_stack.append((level, text))
            start, end = _line_span(t.map, line_offset)
            collect(
                Block(
                    type=BlockType.heading,
                    text=text,
                    level=level,
                    heading_path=ancestors,
                    line_start=start,
                    line_end=end,
                )
            )
            i += 3  # heading_open, inline, heading_close
            continue

        if t.type == "paragraph_open":
            text = _redact(tokens[i + 1].content, path, BlockType.paragraph)
            start, end = _line_span(t.map, line_offset)
            collect(
                Block(
                    type=BlockType.paragraph,
                    text=text,
                    heading_path=current_path(),
                    line_start=start,
                    line_end=end,
                )
            )
            i += 3  # paragraph_open, inline, paragraph_close
            continue

        if t.type in ("fence", "code_block"):
            lang = t.info.strip().split()[0] if t.info and t.info.strip() else None
            text = _redact(t.content.rstrip("\n"), path, BlockType.code)
            start, end = _line_span(t.map, line_offset)
            collect(
                Block(
                    type=BlockType.code,
                    text=text,
                    lang=lang,
                    heading_path=current_path(),
                    line_start=start,
                    line_end=end,
                )
            )
            i += 1
            continue

        if t.type == "hr":
            i += 1  # a thematic break carries no searchable text
            continue

        if t.type in _CONTAINER_OPEN_TO_TYPE:
            close_idx = _find_close_index(tokens, i)
            start, end = _line_span(t.map, line_offset)
            raw = "\n".join(source_lines[t.map[0] : t.map[1]]).rstrip() if t.map else ""
            block_type = _CONTAINER_OPEN_TO_TYPE[t.type]
            callout_type: str | None = None
            if t.type == "blockquote_open":
                block_type, callout_type, raw = _resolve_blockquote(raw)
            text = _redact(raw, path, block_type)
            collect(
                Block(
                    type=block_type,
                    text=text,
                    callout_type=callout_type,
                    heading_path=current_path(),
                    line_start=start,
                    line_end=end,
                )
            )
            i = close_idx + 1
            continue

        i += 1  # unhandled token (e.g. an orphan inline) -- no content to capture

    return blocks, links, tag_pool, dataview


def _resolve_blockquote(raw: str) -> tuple[BlockType, str | None, str]:
    """Strip ``>`` markers; if the first line opens a callout, split type/title out."""
    stripped_lines = [_strip_quote_marker(line) for line in raw.splitlines()]
    if not stripped_lines:
        return BlockType.quote, None, raw
    header = parse_callout_header(stripped_lines[0])
    if header is None:
        return BlockType.quote, None, "\n".join(stripped_lines).strip()
    callout_type, title = header
    rest = [title, *stripped_lines[1:]] if title else stripped_lines[1:]
    return BlockType.callout, callout_type, "\n".join(rest).strip()


def _strip_quote_marker(line: str) -> str:
    s = line.lstrip()
    if s.startswith(">"):
        s = s[1:]
        if s.startswith(" "):
            s = s[1:]
    return s


def _find_close_index(tokens: list[Token], open_idx: int) -> int:
    """Index of the token that closes ``tokens[open_idx]``'s container.

    markdown-it's flat token stream is a well-formed tree flattened by nesting
    ``.level``, so the next token back at the *same* level as the opener is always
    its matching closer, regardless of what nests in between (verified empirically
    against markdown-it-py's token output for nested/mixed lists).
    """
    level = tokens[open_idx].level
    j = open_idx + 1
    n = len(tokens)
    while j < n and tokens[j].level != level:
        j += 1
    return min(j, n - 1)


def _line_span(map_: list[int] | None, line_offset: int) -> tuple[int | None, int | None]:
    """0-indexed ``[start, end)`` (body-relative) -> 1-indexed inclusive, file-relative."""
    if not map_:
        return None, None
    start, end = map_
    return line_offset + start + 1, line_offset + end


def _redact(text: str, path: str, block_type: BlockType) -> str:
    """Return ``text`` unchanged, or a fixed placeholder if it trips the secret guard.

    Called at the point each block's text is first extracted -- before it can reach
    ``heading_stack`` (leaking into descendants' ``heading_path``) or the wikilink/
    tag/dataview regexes (which could otherwise turn a leaked ``api_key: <value>``
    line into a dataview field literally named ``api_key``). Never logs the matched
    text itself, only the rule name and block type (``CLAUDE.md`` §6).
    """
    rule = scan_for_secrets(text)
    if rule is None:
        return text
    log.warning("parse.secret_redacted", path=path, rule=rule, block_type=block_type)
    return _REDACTED_TEXT
