"""Frontmatter -> typed ``DocumentMetadata`` fields (parsing, Step 4).

YAML frontmatter maps onto the typed fields ``title``/``tags``/``aliases``/
``created_ts``/``modified_ts``; everything else is a "custom key" and stays in
``DocumentMetadata.frontmatter`` verbatim (``models.py``: "Custom frontmatter +
dataview keys preserved verbatim"). Malformed YAML degrades to "no frontmatter"
rather than failing the parse -- a vault has thousands of hand-edited notes, and one
bad ``---`` block must not take down ``vaultrag reindex`` (arch §4).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import frontmatter as fm
import structlog
import yaml

log = structlog.get_logger(__name__)

# Frontmatter keys promoted to their own DocumentMetadata field -- excluded from
# the generic `frontmatter` passthrough dict so a value never lives in two places.
_TYPED_KEYS = frozenset({"title", "tags", "aliases", "created", "modified"})

# U+FEFF, the byte-order mark. `read_text(encoding="utf-8")` does NOT strip it (only
# "utf-8-sig" does), so a BOM'd note arrives with an invisible character glued to the front
# of line 1 — and the frontmatter delimiter test is `lines[0].strip() != "---"`, which a
# leading BOM defeats. The whole `---` block then parses as prose: every tag is lost (so a
# server-side `filters.tags` silently stops matching the note) and the title becomes raw YAML
# in every citation. BOMs are routine on Windows-authored notes, so this is not exotic input.
_BOM = "﻿"


def strip_bom(text: str) -> str:
    """Drop a leading BOM. Safe for line anchors: it removes a character from line 1, never
    a line, so every ``line_start``/``line_end`` computed downstream is unchanged."""
    return text[1:] if text.startswith(_BOM) else text


def split_frontmatter(path: str, raw_text: str) -> tuple[dict[str, Any], str]:
    """Split ``raw_text`` into ``(frontmatter dict, body)``. Never raises.

    Falls back to ``({}, raw_text)`` on malformed YAML, logging a warning with the
    path (never the content). The body is never taken from python-frontmatter's
    ``.content``: that attribute is ``.strip()``-ed at both document boundaries,
    which (a) corrupts a no-frontmatter body whose first line is a 4-space-indented
    code block (turning it into an ordinary paragraph), and (b) drops blank lines
    between the closing ``---`` and the first content line -- shifting every block's
    ``line_start``/``line_end`` off the file by however many were stripped, which
    breaks the line anchors ``open(path, heading?, lines?)`` cites
    (``../Rag/reference/reference-mcp-tools.md``). Instead the body is sliced from
    ``raw_text`` at :func:`frontmatter_line_offset`, so body line *k* is file line
    ``offset + k`` by construction; python-frontmatter still parses the YAML itself.
    """
    raw_text = strip_bom(raw_text)
    try:
        post = fm.loads(raw_text)
    except yaml.YAMLError as exc:
        log.warning("parse.frontmatter_invalid", path=path, error=str(exc))
        return {}, raw_text
    offset = frontmatter_line_offset(raw_text)
    if offset == 0:
        return post.metadata, raw_text
    return post.metadata, "".join(raw_text.splitlines(keepends=True)[offset:])


def frontmatter_line_offset(raw_text: str) -> int:
    """Lines consumed by a leading ``---`` YAML block, else 0.

    Mirrors python-frontmatter's own delimiter convention (opening ``---`` as the
    first line, next bare ``---`` as the close) so line numbers computed against the
    post-frontmatter body can be shifted back to file-relative for citations/
    ``open()``. An unterminated block is treated as "no frontmatter" (offset 0),
    matching how ``split_frontmatter`` degrades on malformed input.
    """
    lines = strip_bom(raw_text).splitlines()
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def coerce_str_list(value: Any) -> list[str]:
    """Frontmatter values arrive as a YAML list, a comma-string, or a bare scalar."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def coerce_epoch(value: Any) -> int | None:
    """Frontmatter dates -> epoch seconds (UTC).

    PyYAML's implicit resolver auto-parses unquoted ``created: 2026-07-14`` into a
    `date`/`datetime`; quoted values arrive as `str`. Naive values are treated as
    UTC (no vault-wide timezone convention exists to do better).
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp())
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    return None


def custom_frontmatter(raw: dict[str, Any]) -> dict[str, Any]:
    """Frontmatter keys minus the ones promoted to typed ``DocumentMetadata`` fields."""
    return {k: v for k, v in raw.items() if k not in _TYPED_KEYS}
