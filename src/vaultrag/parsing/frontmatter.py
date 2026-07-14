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


def split_frontmatter(path: str, raw_text: str) -> tuple[dict[str, Any], str]:
    """Split ``raw_text`` into ``(frontmatter dict, body)``. Never raises.

    Falls back to ``({}, raw_text)`` on malformed YAML, logging a warning with the
    path (never the content). When no ``---`` block is present, ``raw_text`` is
    returned verbatim rather than python-frontmatter's own ``.content``: that
    attribute ``.strip()``s whitespace at the document boundary, which would corrupt
    a body whose very first line is a 4-space-indented code block (turning it into
    an ordinary paragraph). This only needs to special-case the *no-frontmatter*
    path -- there the whole input *is* the body, no boundary-detection guesswork
    required; when a frontmatter block genuinely is present, ``post.content`` (the
    well-tested split) is used as-is.
    """
    try:
        post = fm.loads(raw_text)
    except yaml.YAMLError as exc:
        log.warning("parse.frontmatter_invalid", path=path, error=str(exc))
        return {}, raw_text
    if frontmatter_line_offset(raw_text) == 0:
        return post.metadata, raw_text
    return post.metadata, post.content


def frontmatter_line_offset(raw_text: str) -> int:
    """Lines consumed by a leading ``---`` YAML block, else 0.

    Mirrors python-frontmatter's own delimiter convention (opening ``---`` as the
    first line, next bare ``---`` as the close) so line numbers computed against the
    post-frontmatter body can be shifted back to file-relative for citations/
    ``open()``. An unterminated block is treated as "no frontmatter" (offset 0),
    matching how ``split_frontmatter`` degrades on malformed input.
    """
    lines = raw_text.splitlines()
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
