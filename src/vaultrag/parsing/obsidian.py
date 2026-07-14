"""Obsidian-flavored markdown conventions markdown-it-py doesn't know about.

Wikilinks, embeds, inline tags, dataview inline fields, and callout headers are
community-plugin/Obsidian conventions layered on top of CommonMark, so they're
extracted here with dedicated regexes rather than as markdown-it tokenizer rules
(``../Rag/projects/vaultrag/parsing-pipeline.md``, arch §5). ``markdown.py`` calls
:func:`extract_obsidian_features` once per non-code block.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from vaultrag.models import Link, LinkKind

# ---------------------------------------------------------------------------
# Wikilinks & embeds: [[Note]] [[Note|Alias]] [[Note#Heading]] [[Note#^block]],
# and the embed form ![[...]] (transclusion -> an `embed` edge, never duplicated
# content -- the target is indexed once under its own path).
# ---------------------------------------------------------------------------
_WIKILINK_RE = re.compile(
    r"(?P<embed>!)?\[\[(?P<target>[^\]|#]*)(?:#(?P<anchor>[^\]|]+))?(?:\|(?P<alias>[^\]]+))?\]\]"
)


def extract_links(text: str) -> list[Link]:
    """Every wikilink/embed in ``text``, in source order, unresolved.

    ``anchor`` is stored verbatim (the raw text after ``#``, e.g. ``"Some Heading"``
    or ``"^blockid"``) -- resolving it to a slug is a retrieval-time concern
    (``Link.anchor`` docstring), not parsing's.
    """
    links: list[Link] = []
    for m in _WIKILINK_RE.finditer(text):
        anchor = m.group("anchor")
        alias = m.group("alias")
        links.append(
            Link(
                kind=LinkKind.embed if m.group("embed") else LinkKind.wikilink,
                target=(m.group("target") or "").strip(),
                anchor=anchor.strip() if anchor else None,
                alias=alias.strip() if alias else None,
            )
        )
    return links


def _mask_links(text: str) -> str:
    """Blank out wikilink/embed spans before tag/dataview extraction runs.

    Without this, a same-note heading link like ``[[#Overview]]`` would be
    misread as the tag ``#Overview``: nothing precedes its ``#`` but ``[``, which
    isn't a word character, so the tag regex's lookbehind alone can't rule it out.
    """
    return _WIKILINK_RE.sub(" ", text)


# ---------------------------------------------------------------------------
# Inline tags: #tag, #a/b (nested). The lookbehind excludes `#`/word-char/`/`
# immediately before the `#`, which is what keeps ATX headings (always `#<space>`)
# and wikilink anchors (`[[Note#Heading]]`, preceded by a word char) from matching.
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"(?<![#\w/])#([A-Za-z][A-Za-z0-9_/-]*)")


def extract_inline_tags(text: str) -> list[str]:
    """Raw ``#tag``/``#a/b`` occurrences in ``text``, in source order.

    Not yet prefix-expanded -- see :func:`expand_nested_tags`. Callers should mask
    wikilinks first; :func:`extract_obsidian_features` does.
    """
    return [m.group(1) for m in _TAG_RE.finditer(text)]


def expand_nested_tags(tags: Iterable[str]) -> list[str]:
    """``a/b/c`` -> ``[a, a/b, a/b/c]`` (prefix expansion for filtering).

    Dedupes across all input tags while preserving first-seen order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        prefix = ""
        for part in tag.split("/"):
            if not part:
                continue
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix not in seen:
                seen.add(prefix)
                out.append(prefix)
    return out


# ---------------------------------------------------------------------------
# Dataview inline fields: `Key:: value`, one per (optionally bulleted/task) line.
# ---------------------------------------------------------------------------
_DATAVIEW_RE = re.compile(r"(?m)^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s+)?([A-Za-z][\w ]*?)::\s*(.+?)\s*$")


def extract_dataview_fields(text: str) -> dict[str, str]:
    """``key:: value`` lines in ``text`` -> a dict (last occurrence wins on repeat)."""
    return {m.group(1).strip(): m.group(2).strip() for m in _DATAVIEW_RE.finditer(text)}


def extract_obsidian_features(text: str) -> tuple[list[Link], list[str], dict[str, str]]:
    """Links, raw inline tags, and dataview fields from one block of text.

    Links are extracted first, from the unmasked text; tags/dataview then run
    against a link-masked copy (see :func:`_mask_links`).
    """
    links = extract_links(text)
    masked = _mask_links(text)
    tags = extract_inline_tags(masked)
    dataview = extract_dataview_fields(masked)
    return links, tags, dataview


# ---------------------------------------------------------------------------
# Callout headers: `> [!type]`, `> [!type]+ Title` (expanded), `> [!type]- Title`
# (collapsed fold state -- ignored here, chunking/retrieval don't care).
# ---------------------------------------------------------------------------
_CALLOUT_RE = re.compile(r"^\[!(?P<type>[A-Za-z][\w-]*)\](?:[+-])?\s*(?P<title>.*)$")


def parse_callout_header(first_line: str) -> tuple[str, str] | None:
    """If ``first_line`` (a blockquote's first line, its ``>`` already stripped)
    opens a callout, return ``(type, title)``; else ``None`` (a plain blockquote)."""
    m = _CALLOUT_RE.match(first_line.strip())
    if not m:
        return None
    return m.group("type").lower(), m.group("title").strip()
