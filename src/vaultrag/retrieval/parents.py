"""Parent-swap — retrieval stage 5 (P1 Stage 2): precision of children, coherence of parents.

Children are the only indexed vectors (small chunks rank precisely), but a lone child is
often too little context to answer from. Stage 5, after rerank, **swaps a surviving child
for its enclosing H1/H2 section** and **dedupes children sharing that section into one
parent snippet** — so the agent reads whole coherent sections, not scattered fragments
(``../Rag/projects/vaultrag/chunking-strategy.md``, arch §6 / §10-stage5).

The parent is **reconstructed live from the vault**, never stored: the chunker only records
a ``<path>#<anchor>`` pointer (:func:`~vaultrag.parsing.sections.parent_anchor`), and stage 5
slices the section out of the current file with the same
:func:`~vaultrag.parsing.sections.heading_section` machinery MCP ``open`` uses. That keeps
the vault the single source of truth (CLAUDE.md §6 #1) and the index a pure derived cache.

Policy (:func:`parent_swap`):

* group the reranked hits by their parent pointer ``(path, parent-anchor)``, derived from
  each hit's ``heading_path`` (so it works on any index that carries ``heading_path``);
* a group swaps only when its parent reconstructs, fits ``cap_tokens`` (the ~2k parent cap
  intersected with the response budget), **and is at least as large as its biggest child**
  (a parent smaller than the child means the child's ``heading_path`` spanned more than that
  one section — a tiny-sibling merge — so swapping would *lose* content); the parent takes
  the rank of the group's best child and the lower-ranked siblings are dropped (the dedupe);
* a group whose parent is missing, unresolved, over the cap, or smaller than the child
  **falls back to its children** unchanged — a child that already holds more beats a section
  that would shrink or drop it (arch §10-stage5).

The reconstruction source sits behind :class:`ParentResolver` (CLAUDE.md §5, interfaces
over vendors): :class:`VaultParentResolver` reads the real vault through the caller's gated
reader; tests inject a dict-backed fake. ``resolver is None`` at the call site means the
stage is disabled entirely and children pass through untouched (the P0 behavior).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from vaultrag.chunking.tokens import count_tokens
from vaultrag.index.base import ScoredChunk
from vaultrag.logging import get_logger
from vaultrag.models import Document, Snippet
from vaultrag.parsing import heading_section, parse_document
from vaultrag.parsing.sections import normalize_heading, parent_anchor

log = get_logger(__name__)

# The enclosing H1/H2 section is capped at ~2k tokens (chunking-strategy.md / arch §6): a
# section bigger than this is too coarse to swap in, so its children are kept instead. The
# effective swap cap is min(this, budget_tokens) — a parent that can't fit the response
# budget falls back to the child regardless (arch §10-stage5).
DEFAULT_PARENT_MAX_TOKENS = 2000

_KEY_SEP = "\x1f"  # unit separator — a path can't contain it, so (path, anchor) can't collide


@dataclass(frozen=True, slots=True)
class ParentSection:
    """One enclosing section, sliced live from the vault — the swapped-in parent snippet body.

    ``anchor``/``heading_path`` cite the *parent* heading (so a follow-up ``open`` reopens
    the section, not the original child), and ``tokens`` is measured on the reconstructed
    ``text`` so stage 5 can honor the size cap before committing to the swap.
    """

    path: str
    heading: str  # the parent heading's own text (as written)
    anchor: str  # normalized parent-heading key, for citations / open()
    heading_path: list[str]  # the parent section's own trail (ancestors + its heading)
    line_start: int  # 1-based source line span of the reconstructed section (for citations)
    line_end: int
    text: str
    tokens: int


@runtime_checkable
class ParentResolver(Protocol):
    """Reconstructs a child's enclosing section from the source of truth (the vault).

    ``resolve`` returns ``None`` whenever the section can't be produced — the note is gone,
    excluded, unreadable, or the heading no longer resolves — so stage 5 degrades to keeping
    the child rather than failing the search (CLAUDE.md §5, error handling).
    """

    def resolve(self, path: str, anchor: str) -> ParentSection | None:
        """Return the section ``anchor`` names in note ``path``, or ``None`` if unavailable."""
        ...


class VaultParentResolver:
    """Vault-backed :class:`ParentResolver`: read note → parse → slice the section's lines.

    Takes a ``read_note`` callable (vault-relative path → note text) rather than touching the
    filesystem itself, so it inherits *exactly* the caller's read gates — containment, the
    secret guard, ``.ragignore``, markdown-only. In the MCP server that callable is ``open``'s
    own gated reader, which is what guarantees parent-swap can never surface a file ``open``
    would refuse (CLAUDE.md §6). Any error from it means "no parent" (kept child), logged at
    debug. Parsed notes are memoized for the lifetime of one resolver so several parents in the
    same document parse once (the plan's ``(path, hash)`` cache, scoped to a single search).
    """

    def __init__(self, *, read_note: Callable[[str], str]) -> None:
        self._read_note = read_note
        self._cache: dict[str, tuple[Document, list[str]] | None] = {}

    def _document(self, path: str) -> tuple[Document, list[str]] | None:
        if path in self._cache:
            return self._cache[path]
        try:
            text = self._read_note(path)
        except Exception as exc:  # gate refusal, missing note, unreadable — all "no parent"
            log.debug("parent.read_failed", path=path, error=str(exc))
            self._cache[path] = None
            return None
        lines = text.splitlines()
        document = parse_document(path=path, raw_text=text)
        parsed = (document, lines)
        self._cache[path] = parsed
        return parsed

    def resolve(self, path: str, anchor: str) -> ParentSection | None:
        parsed = self._document(path)
        if parsed is None:
            return None
        document, lines = parsed
        span = heading_section(document, anchor, len(lines))
        if span is None:
            return None
        start, end, block = span
        body = "\n".join(lines[start - 1 : end])
        return ParentSection(
            path=path,
            heading=block.text,
            anchor=normalize_heading(block.text),
            heading_path=[*block.heading_path, block.text],
            line_start=start,
            line_end=end,
            text=body,
            tokens=count_tokens(body),
        )


def parent_swap(
    hits: Sequence[ScoredChunk],
    resolver: ParentResolver,
    child_to_snippet: Callable[[ScoredChunk], Snippet],
    *,
    cap_tokens: int,
) -> list[Snippet]:
    """Stage 5: collapse swappable same-section hits into parent snippets, in rank order.

    Every distinct parent pointer among ``hits`` is resolved once; a pointer becomes a *swap
    group* only when its section reconstructs as a genuine superset of its children — no bigger
    than ``cap_tokens`` and no smaller than the biggest child (see the guard below). Walking the
    hits in rank order, the first hit of a swap group is emitted as its parent snippet (inheriting
    that hit's score/position) and its lower-ranked siblings are dropped; every other hit passes
    through as its child snippet via ``child_to_snippet``. Non-swappable groups keep all their
    children, so nothing is ever silently lost — a group only shrinks when the parent it gains
    strictly contains what it dropped.
    """
    resolved: dict[str, ParentSection | None] = {}
    keys: list[str | None] = []
    max_child: dict[str, int] = {}
    for hit in hits:
        path, anchor = _parent_target(hit.payload)
        if path is None or anchor is None:
            keys.append(None)
            continue
        group_key = f"{path}{_KEY_SEP}{anchor}"
        keys.append(group_key)
        max_child[group_key] = max(max_child.get(group_key, 0), _hit_tokens(hit.payload))
        if group_key not in resolved:
            resolved[group_key] = resolver.resolve(path, anchor)

    # A group swaps only when its parent is a genuine superset: within the cap and no smaller
    # than its biggest child. The lower bound is what stops a tiny-sibling-merge child (whose
    # heading_path names only its first section) from being replaced by that one, smaller
    # section — the swap would drop the rest of the merged text.
    swappable = {
        key: section
        for key, section in resolved.items()
        if section is not None and max_child.get(key, 0) <= section.tokens <= cap_tokens
    }

    out: list[Snippet] = []
    emitted: set[str] = set()
    for hit, key in zip(hits, keys, strict=True):
        section = swappable.get(key) if key is not None else None
        if section is None or key is None:
            out.append(child_to_snippet(hit))
            continue
        if key in emitted:
            log.debug("parent.deduped", path=section.path, anchor=section.anchor)
            continue  # a higher-ranked sibling already became this section's parent snippet
        emitted.add(key)
        out.append(_parent_snippet(section, score=hit.score, rep_payload=hit.payload))
    return out


def _parent_target(payload: Mapping[str, object]) -> tuple[str | None, str | None]:
    """The ``(path, parent-anchor)`` a hit swaps toward, derived from its ``heading_path``.

    Recomputed from the payload rather than read from a stored ``parent_id`` so the stage
    works on any index carrying ``heading_path`` (which every index does) — the chunker's
    stored pointer and this share one :func:`parent_anchor` definition, so they agree.
    """
    path = payload.get("path")
    heading_path = payload.get("heading_path")
    hp = [str(h) for h in heading_path] if isinstance(heading_path, list) else []
    anchor = parent_anchor(hp)
    return (str(path) if isinstance(path, str) and path else None, anchor)


def _parent_snippet(
    section: ParentSection, *, score: float, rep_payload: Mapping[str, object]
) -> Snippet:
    """Build the swapped-in parent snippet: parent body + parent citation, child provenance.

    ``title``/``doc_id`` come from the representative (best-ranked) child's payload since a
    reconstructed section is not itself an indexed point; ``chunk_id`` is ``None`` for the
    same reason (there is no point to reopen — the ``path`` + ``anchor`` citation is how you
    reopen it).
    """
    return Snippet(
        path=section.path,
        title=_as_str(rep_payload.get("title")),
        heading_path=list(section.heading_path),
        anchor=section.anchor,
        line_start=section.line_start,
        line_end=section.line_end,
        text=section.text,
        score=score,
        tokens=section.tokens,
        doc_id=_as_str(rep_payload.get("doc_id")),
        chunk_id=None,
    )


def _hit_tokens(payload: Mapping[str, object]) -> int:
    """The child chunk's token count (payload ``tokens``), for the superset guard."""
    tokens = payload.get("tokens")
    return int(tokens) if isinstance(tokens, (int, float)) else 0


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
