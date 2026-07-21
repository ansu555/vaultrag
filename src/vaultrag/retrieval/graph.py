"""Graph expand — retrieval stage 6 (P2 Stage 3), plus the ``related`` tool's core.

Stage 2 turned Obsidian's wikilinks into *data* (the ``links`` table). This module is its
first consumer: the vault's own link structure becomes a retrieval signal and an
exploration surface, with **no LLM anywhere** in it
(``../Rag/decisions/no-full-graphrag.md``, ``projects/vaultrag/knowledge-graph-integration.md``).

Two independent halves, both cheap and both pure SQLite:

1. **Authority boost** (:func:`authority_boost`) — a chunk whose document is linked *from
   other documents in the same result set* is, by the vault author's own hand, the note the
   others defer to. That is a genuine relevance signal fusion cannot see: BM25 and the
   bi-encoder score a chunk against the query in isolation, never against what the rest of
   the vault says about it. Stage 6 nudges such a chunk up.

2. **Neighbor append** (:func:`neighbor_notes`) — the 1-hop links/backlinks of the top few
   result documents, returned as **titles + paths only** in
   :attr:`~vaultrag.models.SearchResult.related`. Never text: a pointer costs no
   ``budget_tokens`` and can never displace a ranked snippet, which is the whole reason
   this is a separate field rather than more snippets (principle #7).

**Why the boost is expressed as a fraction of the result set's own score spread.** The
scores reaching stage 6 are on wildly different scales depending on what ran before it:
RRF fusion emits ``Σ 1/(60+rank)`` (~0.016 and down), the cross-encoder emits a sigmoid in
``[0, 1]``, and :class:`~vaultrag.retrieval.rerank.NoOpReranker` emits a constant 1.0. Any
absolute bonus would be a no-op on one scale and a wrecking ball on another. Scaling by
``max - min`` of the current result list makes the knob mean one scale-free thing:
:attr:`GraphExpandParams.boost` is *the largest share of the result set's total score range
that pure authority can buy*. The bound that follows is the reason this stage is safe
enough to ship on: **a chunk can only overtake another if it was already within
``boost x spread`` of it**, so at the default 0.25 authority settles a photo-finish and
never overturns a decisive one. Link structure adjusts the ranking; it does not replace it.

Measured 2026-07-21, and the boost curve is exactly the shape that bound predicts:

===========  =====================  ==================================
``boost``    Meridian corpus (72)   live design vault (58, stage 6 A/B)
===========  =====================  ==================================
0.10         MRR@5 0.892 (+0.000)   MRR@5 0.730 (+0.014)
**0.25**     MRR@5 0.892 (+0.000)   MRR@5 0.749 (**+0.033**)
0.50         MRR@5 0.891 (-0.000)   MRR@5 0.778 (+0.062)
1.00         MRR@5 0.741 (-0.151)   MRR@5 0.719 (+0.003)
===========  =====================  ==================================

Two readings, both of which shaped the default. **The stage earns its place on a real
vault** (+0.033 MRR@5 on the design vault at 0.25; expected note moves up in 8 cases, down
in 3) and is **neutral on the synthetic corpus** (up 4, down 3 — it reorders 65 of 72 lists
to no net effect). Same pattern as the rerank stage, for the same kind of reason: the
corpus is 27 notes and 57 links written one-correct-answer-per-query, so in-result-set
in-degree has little to work with, while the design vault has real hub structure. **And
past the bound it is actively destructive** — at 1.00 authority can overturn anything and
the corpus loses 0.151 MRR@5, which is the measurement the 0.25 default is buying distance
from. 0.50 measured better on the design vault, but 0.25 is the conservative point that is
non-negative on *both* sets, and it is a per-vault knob (``VAULTRAG_GRAPH_BOOST``) with the
sweep above as the evidence for raising it.

Stage 6 runs after parent-swap (stage 5) and before cite + pack (stages 7-8), per
``../Rag/projects/vaultrag/retrieval-pipeline.md``. It therefore reorders *within* the
``k`` the earlier stages kept; it cannot resurrect a chunk rerank already dropped. That is
the honest scope of a post-hoc signal, and it still matters: order decides MRR, and the
greedy budget packer keeps a prefix, so reordering decides what survives truncation.

The store sits behind the narrow :class:`GraphStore` protocol (CLAUDE.md §5, interfaces
over vendors) — stage 6 needs four read methods, not a whole
:class:`~vaultrag.store.base.MetaStore`, and tests inject a dict-backed fake.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from vaultrag.config import Settings
from vaultrag.logging import get_logger
from vaultrag.models import RelatedNote, Snippet
from vaultrag.retrieval.cite import obsidian_uri
from vaultrag.store.base import DocumentRow, LinkedDoc, LinkRow, NeighborDoc, SiblingDoc

log = get_logger(__name__)

# Ship-state and defaults, measured on the Stage 1 gate (see MEMORY.md / the module
# docstring above for the rule these express).
DEFAULT_GRAPH_EXPAND_ENABLED = True
DEFAULT_GRAPH_BOOST = 0.25
DEFAULT_GRAPH_TOP_DOCS = 3
DEFAULT_GRAPH_MAX_RELATED = 10
# Tag siblings get their own small cap, and it is not a style choice — measured on the live
# design vault, `related` on one note returned **49 entries: 1 link, 2 backlinks, and 46 tag
# siblings**. `tags` stores prefix-expanded tags (`a/b/c` → `a`, `a/b`, `a/b/c`), so every
# note under one `project/…` namespace is a "sibling" of every other with 3-4 tags in
# common. Unbounded, the weakest relation drowns the one the tool is named for. Capped at 5
# (the store already returns them most-shared-first) the same call answers with 3 links and
# the 5 strongest siblings, which is a neighbourhood rather than a table of contents.
DEFAULT_MAX_TAG_SIBLINGS = 5
# Relation precedence when the same note is reachable several ways — most specific first.
# A note both linked *and* back-linked is reported once, as `links_to`: the source note's
# own outgoing link is the more direct statement of intent.
Relation = Literal["links_to", "backlink", "neighbor", "tag_sibling"]
_RELATION_ORDER: tuple[Relation, ...] = ("links_to", "backlink", "neighbor", "tag_sibling")


@runtime_checkable
class GraphStore(Protocol):
    """The slice of the metadata store retrieval stage 6 and ``related`` actually read.

    Structurally satisfied by :class:`~vaultrag.store.sqlite.SQLiteMetaStore` — declared
    narrowly so the graph stages depend on four read methods rather than the whole
    ``MetaStore`` write surface, and so a test can hand them a dict.
    """

    def linked_docs(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        """Documents ``doc_id`` links *to* (resolved edges only, self-links excluded)."""
        ...

    def backlinks(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        """Documents that link *to* ``doc_id``."""
        ...

    def neighbors(
        self, project_id: str, doc_id: str, hops: int = 1, limit: int = 50
    ) -> list[NeighborDoc]:
        """Documents within ``hops`` link-hops, either direction."""
        ...

    def tag_siblings(self, project_id: str, doc_id: str, limit: int = 20) -> list[SiblingDoc]:
        """Documents sharing tags with ``doc_id``, most-shared first."""
        ...

    def outgoing_links(self, project_id: str, doc_id: str) -> list[LinkRow]:
        """Every edge out of ``doc_id``, resolved or not — the dangling-link signal."""
        ...

    def get_document(self, project_id: str, path: str) -> DocumentRow | None:
        """Resolve a vault-relative path to its indexed document row, or ``None``.

        Not used by stage 6 (which already has ``doc_id`` on every snippet) but needed by
        ``related``, whose input is a path an agent copied out of a citation. Included here
        so both graph features depend on one protocol rather than two overlapping ones.
        """
        ...


@dataclass(frozen=True, slots=True)
class GraphExpandParams:
    """Tunables for both graph features — all env-configurable (see
    :class:`~vaultrag.config.Settings`).

    ``boost`` is the maximum share of the result set's score spread that authority can add
    (0 disables the reorder while leaving the neighbor append on). ``top_docs`` is how many
    of the top-ranked documents seed the neighbor append; ``max_related`` caps how many
    pointers come back, so a hub note can't return half the vault.
    ``max_tag_siblings`` bounds the weakest relation so it cannot crowd out the links —
    see :data:`DEFAULT_MAX_TAG_SIBLINGS` for the measurement that forced it; it applies to
    ``related`` only (stage 6's append is links/backlinks, never tags).
    """

    boost: float = DEFAULT_GRAPH_BOOST
    top_docs: int = DEFAULT_GRAPH_TOP_DOCS
    max_related: int = DEFAULT_GRAPH_MAX_RELATED
    max_tag_siblings: int = DEFAULT_MAX_TAG_SIBLINGS


def graph_params_for(settings: Settings) -> GraphExpandParams:
    """The stage-6 tunables a config implies.

    Lives here rather than on :class:`~vaultrag.config.Settings` for the same reason
    :func:`~vaultrag.retrieval.rerank.rerank_params_for` does: ``config`` is a
    dependency-free leaf that everything imports, so it must import nothing back. One
    place maps settings → behaviour, so the served server and the eval harness cannot
    drift into measuring something other than what ships.
    """
    return GraphExpandParams(
        boost=settings.graph_boost,
        top_docs=settings.graph_top_docs,
        max_related=settings.graph_max_related,
        max_tag_siblings=settings.graph_max_tag_siblings,
    )


def graph_expand(
    snippets: list[Snippet],
    *,
    project_id: str,
    store: GraphStore | None,
    params: GraphExpandParams,
    vault_name: str | None = None,
) -> tuple[list[Snippet], list[RelatedNote]]:
    """Stage 6: reorder by link authority and collect neighbor pointers.

    ``store is None`` disables the stage entirely (the P1 behaviour) — snippets pass
    through untouched and no neighbors are returned. Returns
    ``(reordered_snippets, related_notes)``; the caller keeps them apart because the
    pointers must not enter the budget packer.

    Every store read is wrapped: the graph is a *derived cache* on the side of the read
    path, so a missing ``links`` table (a store from before Stage 2) or any query failure
    degrades to "no graph signal" rather than failing a search that would otherwise have
    worked (CLAUDE.md §6 #1 — the index is derived; the vault is truth).
    """
    if store is None or not snippets:
        return snippets, []
    try:
        boosted = authority_boost(snippets, project_id=project_id, store=store, params=params)
        related = neighbor_notes(
            boosted,
            project_id=project_id,
            store=store,
            params=params,
            vault_name=vault_name,
        )
    except Exception as exc:  # a degraded graph must not take the search down with it
        log.warning("graph.expand_failed", project=project_id, error=str(exc))
        return snippets, []
    return boosted, related


def authority_boost(
    snippets: Sequence[Snippet],
    *,
    project_id: str,
    store: GraphStore,
    params: GraphExpandParams,
) -> list[Snippet]:
    """Reorder ``snippets`` by how many *other* result documents link to each one.

    The in-degree is counted **only within the result set**, which is what makes this a
    query-conditional signal rather than a global PageRank: a note every result defers to
    *for this query* is authoritative here, whereas a vault-wide hub (an MOC, a daily note)
    would otherwise dominate every search it happened to touch.

    Costs one indexed ``linked_docs`` lookup per distinct result document (≤ ``k``, so
    ~10 sub-millisecond SQLite reads). Returns the snippets in the new order with their
    scores updated to the adjusted value — the same contract stage 4 already has, where
    the returned ``score`` is the score the stage actually ranked by, so a client sorting
    by ``score`` reproduces the order it was given.
    """
    if params.boost <= 0 or len(snippets) < 2:
        return list(snippets)

    doc_ids = _ordered_doc_ids(snippets)
    if len(doc_ids) < 2:
        return list(snippets)  # one document can't have authority over itself

    result_docs = set(doc_ids)
    indegree: dict[str, int] = dict.fromkeys(doc_ids, 0)
    for source in doc_ids:
        for target in store.linked_docs(project_id, source):
            # `linked_docs` already excludes self-links; the membership test is what keeps
            # the signal query-conditional (edges out of the result set are not evidence).
            if target.doc_id in result_docs and target.doc_id != source:
                indegree[target.doc_id] += 1

    max_in = max(indegree.values())
    if max_in == 0:
        return list(snippets)  # nothing links to anything here — no reorder, no cost

    scores = [s.score for s in snippets]
    spread = max(scores) - min(scores)
    if spread <= 0:
        # Every score identical (a NoOpReranker run, or a one-hit list): there is no
        # ranking information to preserve, so ordering by authority is strictly additive.
        # Fall back to the score's own magnitude to keep the bonus on the right scale.
        spread = abs(max(scores)) or 1.0

    unit = params.boost * spread / max_in
    adjusted = [
        s.model_copy(update={"score": s.score + unit * indegree.get(s.doc_id or "", 0)})
        for s in snippets
    ]
    # Stable sort: equal authority (the common case) keeps the incoming rank order, so the
    # stage only ever moves chunks the graph has something to say about.
    adjusted.sort(key=lambda s: s.score, reverse=True)
    log.debug(
        "graph.authority",
        project=project_id,
        docs=len(doc_ids),
        linked=sum(1 for v in indegree.values() if v),
        moved=sum(1 for a, b in zip(snippets, adjusted, strict=True) if a.path != b.path),
    )
    return adjusted


def neighbor_notes(
    snippets: Sequence[Snippet],
    *,
    project_id: str,
    store: GraphStore,
    params: GraphExpandParams,
    vault_name: str | None = None,
) -> list[RelatedNote]:
    """1-hop links + backlinks of the top ``params.top_docs`` result documents, as pointers.

    Notes already present in the result set are excluded — a pointer to something the agent
    is already reading is noise, and the point of the append is to show what is *adjacent to*
    the answer. Capped at ``params.max_related``, deduped by path, ``links_to`` before
    ``backlink`` (see :data:`_RELATION_ORDER`).
    """
    if params.max_related <= 0 or params.top_docs <= 0:
        return []

    seeds = _ordered_doc_ids(snippets)[: params.top_docs]
    already = {s.path for s in snippets}
    found: dict[str, RelatedNote] = {}
    for doc_id in seeds:
        edges: tuple[tuple[Relation, list[LinkedDoc]], ...] = (
            ("links_to", store.linked_docs(project_id, doc_id)),
            ("backlink", store.backlinks(project_id, doc_id)),
        )
        for relation, linked in edges:
            for doc in linked:
                if doc.path in already or doc.path in found:
                    continue
                found[doc.path] = _related_note(
                    path=doc.path,
                    title=doc.title,
                    relation=relation,
                    hops=1,
                    vault_name=vault_name,
                )
    return _ranked(found.values())[: params.max_related]


def related_notes(
    *,
    project_id: str,
    doc_id: str,
    store: GraphStore,
    hops: int = 1,
    limit: int = 50,
    max_tag_siblings: int = DEFAULT_MAX_TAG_SIBLINGS,
    vault_name: str | None = None,
) -> tuple[list[RelatedNote], list[str], bool]:
    """The ``related`` tool's core: one note's neighborhood as ``(notes, unresolved, truncated)``.

    Assembles outgoing links, backlinks, ``hops``-distant neighbors (only when ``hops >= 2``
    — at 1 hop every neighbor is already reported as the more specific ``links_to`` or
    ``backlink``) and at most ``max_tag_siblings`` shared-tag siblings, deduped by path in
    :data:`_RELATION_ORDER` precedence. ``unresolved`` is this note's own dangling wikilink
    targets, which is how an agent distinguishes "nothing links here" from "the links here
    are broken".

    Lives in ``retrieval`` rather than the MCP layer so the tool and the REST route (P3)
    share one definition of what "related" means, and so it is testable without FastMCP.
    """
    found: dict[str, RelatedNote] = {}

    def add(
        path: str,
        title: str | None,
        relation: Relation,
        hops: int | None,
        shared: int | None = None,
    ) -> None:
        if path in found:
            return  # first (most specific) relation wins — see _RELATION_ORDER
        found[path] = _related_note(
            path=path,
            title=title,
            relation=relation,
            hops=hops,
            shared_tags=shared,
            vault_name=vault_name,
        )

    for doc in store.linked_docs(project_id, doc_id):
        add(doc.path, doc.title, "links_to", 1)
    for doc in store.backlinks(project_id, doc_id):
        add(doc.path, doc.title, "backlink", 1)
    if hops >= 2:
        # `neighbors` walks *resolved* edges only, so a vault full of dangling links has a
        # sparser graph than its author thinks — which is exactly what `unresolved` reports.
        for neighbor in store.neighbors(project_id, doc_id, hops=hops, limit=limit * 2):
            if neighbor.hops >= 2:
                add(neighbor.path, neighbor.title, "neighbor", neighbor.hops)
    # Counted on what is *added*, not on what is fetched: a sibling that already arrived as a
    # link is deduped away, and letting it consume a slot would silently shrink the cap.
    added_siblings = 0
    for sibling in store.tag_siblings(project_id, doc_id, limit=limit):
        if added_siblings >= max_tag_siblings:
            break
        if sibling.path in found:
            continue
        add(sibling.path, sibling.title, "tag_sibling", None, sibling.shared_tags)
        added_siblings += 1

    unresolved = [
        row.dst_raw for row in store.outgoing_links(project_id, doc_id) if not row.resolved
    ]
    ranked = _ranked(found.values())
    return ranked[:limit], unresolved, len(ranked) > limit


def _related_note(
    *,
    path: str,
    title: str | None,
    relation: Relation,
    hops: int | None,
    shared_tags: int | None = None,
    vault_name: str | None,
) -> RelatedNote:
    """Build one pointer, stamped with its ``obsidian://`` URI when a vault name is known.

    Same rule as retrieval stage 8 (``retrieval/cite.py``): a URI is not text, so it costs
    no budget, and a pointer you can click is worth strictly more than one you cannot.
    """
    return RelatedNote(
        path=path,
        title=title,
        relation=relation,
        hops=hops,
        shared_tags=shared_tags,
        uri=obsidian_uri(vault_name, path) if vault_name else None,
    )


def _ranked(notes: Iterable[RelatedNote]) -> list[RelatedNote]:
    """Order pointers by relation specificity, then hop distance, then path (stable/total).

    Path is the final tiebreak so the output is fully deterministic — the eval gate and the
    tests both depend on two identical vaults producing byte-identical results.
    """
    return sorted(
        notes,
        key=lambda n: (
            _RELATION_ORDER.index(n.relation),
            n.hops if n.hops is not None else 99,
            -(n.shared_tags or 0),
            n.path,
        ),
    )


def _ordered_doc_ids(snippets: Sequence[Snippet]) -> list[str]:
    """Distinct ``doc_id`` s in rank order, first appearance wins.

    Several chunks of one note routinely come back; for a document-level graph signal the
    second occurrence carries no information and would double-count the note's authority.
    Snippets with no ``doc_id`` (an index predating the payload field) are skipped rather
    than grouped together under a shared empty key.
    """
    seen: list[str] = []
    for snippet in snippets:
        if snippet.doc_id and snippet.doc_id not in seen:
            seen.append(snippet.doc_id)
    return seen
