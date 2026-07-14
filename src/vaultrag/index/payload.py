"""Qdrant point payload for one chunk.

Field list: ``../Rag/reference/reference-metadata-schema.md`` (arch §7). The payload is
what filters (``project_id``, ``tags``, ``path``, ``type``, ``modified_ts``) run against
and what the read path (Step 9) turns into cited snippets, so it is built in one place
rather than inline in the engine wrapper.

Two fields are impl-level readings of that reference, not departures from it:

* ``text`` — the reference enumerates *metadata* and omits the chunk body, but nothing
  else persists it (the SQLite ``chunks`` table holds structure only:
  ``../Rag/reference/reference-database-schema.md``) and Step 9 must build snippets with
  ``text`` straight from the hits. The payload is the only place it can live.
* ``embed_ver`` — the reference names it without defining it. Here it is
  :data:`EMBED_VER`: the version of the embedding *recipe* (contextual-prefix format,
  truncation, normalization). Together with ``embed_model`` it makes a re-embed
  incremental and per-project — points whose ``(embed_model, embed_ver)`` is stale can be
  found and refreshed without a flag day, which is the whole reason the reference wants
  both per point.
"""

from __future__ import annotations

from typing import Any

from vaultrag.models import Chunk, Document

# Bump when a change to how ``Chunk.embed_text`` is built (prefix format, truncation,
# normalization) invalidates stored vectors even though ``embed_model`` is unchanged.
EMBED_VER = 1

# A frontmatter value is kept only if it is a scalar or a flat list of scalars, and only
# up to this length: the payload's job is filtering, and arbitrary nested YAML (or a
# pasted essay in a property) bloats every point for something no filter can use. This is
# the concrete reading of the reference's "frontmatter{...select keys}".
_MAX_FRONTMATTER_STR = 512
_SCALARS = (str, int, float, bool)


def _keep_frontmatter_value(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) <= _MAX_FRONTMATTER_STR
    if isinstance(value, _SCALARS) or value is None:
        return True
    if isinstance(value, list):
        return all(_keep_frontmatter_value(v) for v in value)
    return False


def select_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Filter frontmatter down to the filterable keys worth carrying on every point."""
    return {k: v for k, v in frontmatter.items() if _keep_frontmatter_value(v)}


def chunk_payload(
    chunk: Chunk,
    *,
    embed_model: str,
    document: Document | None = None,
) -> dict[str, Any]:
    """Build the payload for ``chunk``'s point.

    ``document`` supplies the doc-level fields ``Chunk`` doesn't carry (``title``,
    ``links_out``, ``frontmatter``). It is ignored unless its path matches the chunk's, so
    a caller upserting several documents at once can't cross-contaminate payloads.
    """
    payload: dict[str, Any] = {
        # identity / tenancy — project_id is the tenant-partitioning field and is
        # non-negotiable: every query filters on it server-side (CLAUDE.md §6).
        "project_id": chunk.project_id,
        "doc_id": chunk.doc_id,
        "path": chunk.path,
        "chunk_ix": chunk.ix,
        # content + citation anchors
        "text": chunk.text,
        "heading_path": list(chunk.heading_path),
        "anchor": chunk.anchor,
        # filters
        "tags": list(chunk.tags),
        "type": str(chunk.type),
        "langs": list(chunk.langs),
        "has_code": chunk.has_code,
        "created_ts": chunk.created_ts,
        "modified_ts": chunk.modified_ts,
        # bookkeeping / migration
        "chunk_hash": chunk.chunk_hash,
        "tokens": chunk.tokens,
        "embed_model": embed_model,
        "embed_ver": EMBED_VER,
    }
    if chunk.page is not None:  # PDFs only (P2) — absent rather than null on notes
        payload["page"] = chunk.page

    if document is not None and document.path == chunk.path:
        payload["title"] = document.metadata.title
        payload["links_out"] = [link.target for link in document.links]
        payload["frontmatter"] = select_frontmatter(document.metadata.frontmatter)
    return payload
