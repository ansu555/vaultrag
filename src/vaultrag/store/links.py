"""Link-target normalization + the title/alias resolver (P2 Stage 2).

Pure functions — no SQL. :mod:`vaultrag.store.sqlite` owns the tables; this module
owns *what a link target means*, so the rule is testable on its own and identical
wherever it runs.

The graph itself is Obsidian's: `[[wikilinks]]` and `![[embeds]]`, parsed at ingest
into ``Document.links`` and persisted verbatim as ``links.dst_raw``. **Zero LLM
extraction** (``../Rag/decisions/no-full-graphrag.md``) — the human already drew the
edges.

**Resolution (P2 plan Decision 4).** A wikilink target is written the way a human
types it, so one raw string can mean four things. They are tried in strict
precedence, most-specific first:

===========  ==========================  ==========================================
tier         matches                     e.g. ``services/gateway-service.md``
===========  ==========================  ==========================================
``path``     full vault-relative path    ``[[services/gateway-service]]``
``basename`` filename stem               ``[[gateway-service]]``
``title``    frontmatter/first-heading   ``[[Gateway Service]]``
``alias``    a frontmatter alias         ``[[gw]]``
===========  ==========================  ==========================================

``basename`` outranks ``title`` because that is Obsidian's own semantics: `[[Note]]`
addresses the *file*, and a note's ``title`` (frontmatter, else first heading, else
the stem — see :func:`vaultrag.parsing.markdown.parse_document`) may be something
else entirely.

**Ambiguity is never guessed.** If a tier has two matches the target stays
unresolved (``dst_doc=NULL``) with its ``dst_raw`` intact, rather than silently
pointing at whichever row sorted first. Unresolved is honest and self-healing; a
wrong edge is a wrong retrieval that nobody ever notices.

Matching is normalized (:func:`normalize_key`) so ``[[Gateway Service]]``,
``[[gateway service]]`` and ``[[Gateway Service.md]]`` are one target.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from vaultrag.models import DocumentMetadata, Link


class DocKeyKind(StrEnum):
    """An addressable name for a document (``doc_keys.kind``), most specific first.

    Declaration order **is** the resolution precedence — :data:`KEY_PRECEDENCE`
    derives from it, so adding a tier here is enough to place it in the ladder.
    """

    path = "path"  # vault-relative path, extension stripped
    basename = "basename"  # filename stem — what Obsidian's [[Note]] actually addresses
    # ``note_title`` not ``title``: a ``title`` member would shadow ``str.title``
    # (StrEnum is a ``str`` subclass) — same trap as ``JobKind.ingest``. The stored
    # value is still "title".
    note_title = "title"  # frontmatter title / first heading / stem
    alias = "alias"  # frontmatter aliases


KEY_PRECEDENCE: tuple[DocKeyKind, ...] = tuple(DocKeyKind)


@dataclass(frozen=True, slots=True)
class DocKey:
    """One addressable name for a document (a ``doc_keys`` row, minus ids)."""

    kind: DocKeyKind
    key: str


@dataclass(frozen=True, slots=True)
class LinkEdge:
    """One outgoing edge ready to persist: the parsed link plus its lookup key.

    ``dst_raw`` is preserved exactly as written (an unresolved edge must still be
    reportable to a human); ``dst_key`` is the normalized form the resolver joins on.
    """

    kind: str  # models.LinkKind — wikilink | embed (| diagram, P2 Stage 5)
    dst_raw: str
    dst_key: str
    anchor: str | None = None


def normalize_key(raw: str) -> str:
    """Fold a link target (or a document name) to its match key.

    Case-insensitive, whitespace-collapsed, ``.md``-less, separator-normalized —
    so the four ways a human writes the same target all collide on one string.
    Returns ``""`` for anything that carries no target (callers skip those).
    """
    s = raw.strip()
    # A trailing backslash is an escape artifact, never part of a note name: inside a
    # markdown table Obsidian requires `[[Note\|alias]]`, and the wikilink regex splits
    # on the `|`, leaving the `\` stranded on the target.
    s = s.rstrip("\\")
    s = s.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    s = s.lstrip("/")
    if s[-3:].casefold() == ".md":
        s = s[:-3]
    return " ".join(s.split()).casefold()


def doc_keys_for(meta: DocumentMetadata) -> list[DocKey]:
    """Every name ``meta``'s document can be linked by, deduped within each tier.

    These are the rows behind ``doc_keys`` — a derived reverse index of facts that
    already live in ``documents``/frontmatter, kept as its own table so resolution is
    a single indexed lookup instead of four.
    """
    stem = PurePosixPath(meta.path).stem
    candidates: list[tuple[DocKeyKind, str]] = [
        (DocKeyKind.path, meta.path),
        (DocKeyKind.basename, stem),
        (DocKeyKind.note_title, meta.title),
        *((DocKeyKind.alias, a) for a in meta.aliases),
    ]
    out: list[DocKey] = []
    seen: set[tuple[DocKeyKind, str]] = set()
    for kind, raw in candidates:
        key = normalize_key(raw)
        if not key or (kind, key) in seen:
            continue
        seen.add((kind, key))
        out.append(DocKey(kind=kind, key=key))
    return out


def link_edges(links: Iterable[Link]) -> list[LinkEdge]:
    """``Document.links`` → the deduped edge set to persist for one document.

    Two things happen here:

    * **Same-note anchors are dropped.** ``[[#Some Heading]]`` parses with an empty
      target; it addresses a heading in the *current* note and carries no graph
      information, so it is not an edge.
    * **The edge set is a set, not a multiset.** Linking the same target three times
      is still one edge — nothing downstream (backlinks, neighbors, Stage 3's
      authority boost) is defined in terms of link *count*, and deduping is what
      makes re-ingest byte-identical (CLAUDE.md §5).
    """
    out: list[LinkEdge] = []
    seen: set[tuple[str, str, str | None]] = set()
    for link in links:
        key = normalize_key(link.target)
        if not key:
            continue
        ident = (str(link.kind), key, link.anchor)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(
            LinkEdge(
                kind=str(link.kind),
                dst_raw=link.target.strip(),
                dst_key=key,
                anchor=link.anchor,
            )
        )
    return out


def resolve_key(key: str, candidates: Mapping[DocKeyKind, Sequence[str]]) -> str | None:
    """Pick the document ``key`` addresses, or ``None`` if nothing/too much matches.

    ``candidates`` maps each tier to the doc ids matching ``key`` at that tier. The
    first non-empty tier in :data:`KEY_PRECEDENCE` decides — and decides *only* if it
    holds exactly one document. A tie is left unresolved rather than guessed, and a
    lower tier never breaks a higher tier's tie (a title collision is not settled by
    an alias; both notes are genuinely called that).
    """
    for kind in KEY_PRECEDENCE:
        matches = candidates.get(kind) or ()
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None  # ambiguous at the winning tier — don't guess
    return None
