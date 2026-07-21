"""P2 Stage 3 verify: graph expand (retrieval stage 6) + the ``related`` tool's core.

The plan's acceptance criteria, one test each: ``related`` returns a note's neighbours and
tag-siblings with working paths; the stage-6 boost lifts an authoritative-but-lower-ranked
chunk; appended neighbours are titles + paths and cost zero ``budget_tokens``.

Weighted toward the *bounds*, not the happy path. A ranking stage that reorders is only
safe because of what it cannot do — it cannot exceed its share of the score spread, it
cannot count links from outside the result set, and it cannot take a search down when the
graph is broken. Each of those is a claim about a failure path, so each is asserted
directly (the lesson from the Stage 1 silent-pass bug: assert the failure mode, not just
that the happy path is green).

The last section runs the real SQLite store rather than the fake, because a protocol the
production store does not actually satisfy is a green suite against an interface nobody
implements.
"""

from __future__ import annotations

import pytest

from vaultrag.config import Settings
from vaultrag.models import DocumentMetadata, Link, LinkKind, RelatedNote, Snippet
from vaultrag.retrieval.graph import (
    GraphExpandParams,
    GraphStore,
    authority_boost,
    graph_expand,
    graph_params_for,
    neighbor_notes,
    related_notes,
)
from vaultrag.store import SQLiteMetaStore
from vaultrag.store.base import DocumentRow, LinkedDoc, LinkRow, NeighborDoc, SiblingDoc

PROJECT = "meridian"


# ---------------------------------------------------------------------------
# Fake graph store — a dict-backed stand-in for the GraphStore protocol slice
# ---------------------------------------------------------------------------
class FakeGraph:
    """Structural :class:`GraphStore` stand-in: edges as ``{src: [dst, …]}``.

    Records every call so a test can assert stage 6's *cost* (one lookup per distinct
    result document, not per snippet) as well as its output.
    """

    def __init__(
        self,
        edges: dict[str, list[str]] | None = None,
        *,
        titles: dict[str, str] | None = None,
        siblings: dict[str, list[SiblingDoc]] | None = None,
        far: dict[str, list[NeighborDoc]] | None = None,
        dangling: dict[str, list[str]] | None = None,
        docs: dict[str, DocumentRow] | None = None,
    ) -> None:
        self.edges = edges or {}
        self.titles = titles or {}
        self.siblings = siblings or {}
        self.far = far or {}
        self.dangling = dangling or {}
        self.docs = docs or {}
        self.calls: list[tuple[str, str]] = []

    def _path(self, doc_id: str) -> str:
        return f"{doc_id}.md"

    def _title(self, doc_id: str) -> str:
        return self.titles.get(doc_id, doc_id.title())

    def linked_docs(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        self.calls.append(("linked_docs", doc_id))
        return [
            LinkedDoc(doc_id=d, path=self._path(d), title=self._title(d), kind="wikilink")
            for d in self.edges.get(doc_id, [])
            if d != doc_id  # the real store excludes self-links; the fake must too
        ]

    def backlinks(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        self.calls.append(("backlinks", doc_id))
        return [
            LinkedDoc(doc_id=src, path=self._path(src), title=self._title(src), kind="wikilink")
            for src, dsts in sorted(self.edges.items())
            if doc_id in dsts and src != doc_id
        ]

    def neighbors(
        self, project_id: str, doc_id: str, hops: int = 1, limit: int = 50
    ) -> list[NeighborDoc]:
        self.calls.append(("neighbors", doc_id))
        return self.far.get(doc_id, [])[:limit]

    def tag_siblings(self, project_id: str, doc_id: str, limit: int = 20) -> list[SiblingDoc]:
        self.calls.append(("tag_siblings", doc_id))
        return self.siblings.get(doc_id, [])[:limit]

    def outgoing_links(self, project_id: str, doc_id: str) -> list[LinkRow]:
        self.calls.append(("outgoing_links", doc_id))
        rows = [
            LinkRow(
                id=f"{doc_id}->{d}",
                project_id=project_id,
                src_doc=doc_id,
                dst_doc=d,
                dst_raw=d,
                dst_key=d,
                kind="wikilink",
            )
            for d in self.edges.get(doc_id, [])
        ]
        rows += [
            LinkRow(
                id=f"{doc_id}->?{raw}",
                project_id=project_id,
                src_doc=doc_id,
                dst_doc=None,
                dst_raw=raw,
                dst_key=raw.casefold(),
                kind="wikilink",
            )
            for raw in self.dangling.get(doc_id, [])
        ]
        return rows

    def get_document(self, project_id: str, path: str) -> DocumentRow | None:
        self.calls.append(("get_document", path))
        return self.docs.get(path)


class ExplodingGraph(FakeGraph):
    """A store whose every read fails — the degraded-graph case."""

    def linked_docs(self, project_id: str, doc_id: str) -> list[LinkedDoc]:
        raise RuntimeError("no such table: links")


def snip(doc_id: str, score: float, *, path: str | None = None, tokens: int = 10) -> Snippet:
    return Snippet(
        path=path or f"{doc_id}.md",
        title=doc_id.title(),
        text=f"body of {doc_id}",
        score=score,
        tokens=tokens,
        doc_id=doc_id,
    )


def paths(items: list[Snippet] | list[RelatedNote]) -> list[str]:
    return [i.path for i in items]


# ---------------------------------------------------------------------------
# Protocol conformance — the real store must actually satisfy GraphStore
# ---------------------------------------------------------------------------
def test_the_sqlite_store_satisfies_the_graph_store_protocol(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Stage 6 is typed against ``GraphStore``; production passes a ``SQLiteMetaStore``.

    If those two ever drift the suite would still be green against ``FakeGraph`` while the
    server broke, which is the whole failure mode ``runtime_checkable`` lets us pin here.
    """
    store = SQLiteMetaStore(tmp_path / "vaultrag.db")
    try:
        assert isinstance(store, GraphStore)
    finally:
        store.close()


def test_the_fake_satisfies_the_same_protocol() -> None:
    assert isinstance(FakeGraph(), GraphStore)


# ---------------------------------------------------------------------------
# authority_boost — the stage-6 reorder
# ---------------------------------------------------------------------------
def test_the_boost_lifts_a_chunk_other_result_docs_link_to() -> None:
    """The plan's acceptance criterion: an authoritative-but-lower-ranked chunk rises.

    ``c`` ranks below ``a`` on score alone, but both other results link to it — the vault's
    author saying "this is the note those defer to", which no query-vs-chunk scorer can see.
    """
    snippets = [snip("a", 1.0), snip("c", 0.9), snip("b", 0.5)]
    graph = FakeGraph({"a": ["c"], "b": ["c"]})
    out = authority_boost(
        snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=1.0)
    )
    assert paths(out) == ["c.md", "a.md", "b.md"]


def test_authority_breaks_near_ties_but_cannot_overturn_a_clear_win() -> None:
    """The precise safety bound, and the reason stage 6 could ship on.

    A chunk gains at most ``boost x spread``, so it can only overtake the top hit if it was
    already within that of it. At the shipped 0.25 that is a quarter of the result set's
    whole score range: authority settles a photo-finish, it never overturns a decisive one.
    Both halves are asserted from one fixture so the bound can't be half-satisfied.
    """
    graph = FakeGraph({"a": ["c"]})  # `c` is the only cited doc — maximum authority
    near = [snip("a", 1.0), snip("c", 0.95), snip("z", 0.0)]  # gap 0.05 < 0.25 * 1.0
    far = [snip("a", 1.0), snip("c", 0.60), snip("z", 0.0)]  # gap 0.40 > 0.25 * 1.0
    params = GraphExpandParams(boost=0.25)
    assert paths(authority_boost(near, project_id=PROJECT, store=graph, params=params))[0] == "c.md"
    assert paths(authority_boost(far, project_id=PROJECT, store=graph, params=params))[0] == "a.md"


def test_the_boost_cannot_exceed_its_share_of_the_score_spread() -> None:
    """The bound that makes stage 6 safe to ship on: link structure *adjusts* the ranking.

    At the shipped 0.25 the maximally-authoritative chunk gains a quarter of the spread, so
    a bottom hit cannot leapfrog a top one — measured 2026-07-21, a boost of 1.0 (the full
    spread) costs -0.151 MRR@5 on the corpus gate, which is exactly what this bound prevents.
    """
    snippets = [snip("a", 1.0), snip("b", 0.9), snip("c", 0.5)]
    graph = FakeGraph({"a": ["c"], "b": ["c"]})
    out = authority_boost(
        snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=0.25)
    )
    assert paths(out) == ["a.md", "b.md", "c.md"]  # 0.5 + 0.25*0.5 = 0.625 < 0.9
    assert out[2].score == pytest.approx(0.625)


def test_the_boost_only_counts_links_from_inside_the_result_set() -> None:
    """What makes the signal query-conditional rather than a global hub score.

    ``c`` is linked from ``zz``, a document nowhere in these results. If out-of-set edges
    counted, every vault MOC would dominate every search that grazed it.
    """
    snippets = [snip("a", 1.0), snip("c", 0.5)]
    graph = FakeGraph({"zz": ["c"], "a": []})
    out = authority_boost(
        snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=1.0)
    )
    assert paths(out) == ["a.md", "c.md"]
    assert [s.score for s in out] == [1.0, 0.5]  # untouched, not merely un-reordered


def test_a_self_link_confers_no_authority() -> None:
    snippets = [snip("a", 1.0), snip("b", 0.5)]
    graph = FakeGraph({"b": ["b"]})
    out = authority_boost(
        snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=1.0)
    )
    assert paths(out) == ["a.md", "b.md"]


def test_more_incoming_links_beat_fewer() -> None:
    """In-degree is a count, not a flag: three citers outrank one, one outranks none."""
    snippets = [
        snip("a", 1.0),
        snip("b", 1.0),
        snip("c", 1.0),
        snip("e", 0.95),  # 3 citers
        snip("d", 0.90),  # 1 citer
        snip("f", 0.50),  # 0 citers
    ]
    graph = FakeGraph({"a": ["e", "d"], "b": ["e"], "c": ["e"]})
    out = paths(
        authority_boost(
            snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=1.0)
        )
    )
    assert out[0] == "e.md"
    assert out.index("d.md") < out.index("b.md")  # 1 citer beats 0
    assert out[-1] == "f.md"


def test_equal_authority_preserves_the_incoming_rank_order() -> None:
    """Stable sort: the stage only moves chunks the graph has something to say about."""
    snippets = [snip(x, 1.0) for x in ("a", "b", "c", "d")]
    out = authority_boost(
        snippets, project_id=PROJECT, store=FakeGraph(), params=GraphExpandParams(boost=1.0)
    )
    assert paths(out) == ["a.md", "b.md", "c.md", "d.md"]


def test_flat_scores_still_order_by_authority() -> None:
    """A NoOpReranker run gives every hit score 1.0, so ``max - min`` is 0.

    With no ranking information to preserve, ordering by authority is strictly additive —
    the fallback must not silently turn the stage into a no-op precisely when fusion had
    nothing to say.
    """
    snippets = [snip("a", 1.0), snip("b", 1.0), snip("c", 1.0)]
    graph = FakeGraph({"a": ["c"], "b": ["c"]})
    out = authority_boost(
        snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=0.25)
    )
    assert paths(out)[0] == "c.md"


def test_boost_zero_disables_the_reorder_entirely() -> None:
    snippets = [snip("a", 1.0), snip("c", 0.5)]
    graph = FakeGraph({"a": ["c"]})
    out = authority_boost(
        snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=0.0)
    )
    assert paths(out) == ["a.md", "c.md"]
    assert not graph.calls  # and costs nothing — no query is even issued


def test_a_single_result_document_is_not_queried_at_all() -> None:
    """Several chunks of one note can't confer authority on each other."""
    graph = FakeGraph({"a": ["a"]})
    out = authority_boost(
        [snip("a", 1.0, path="a.md"), snip("a", 0.9, path="a.md")],
        project_id=PROJECT,
        store=graph,
        params=GraphExpandParams(boost=1.0),
    )
    assert len(out) == 2
    assert not graph.calls


def test_authority_is_counted_per_document_not_per_chunk() -> None:
    """Two chunks of one note must not double-count that note's outgoing links."""
    snippets = [snip("a", 1.0, path="a.md"), snip("a", 0.95, path="a.md"), snip("t", 0.5)]
    graph = FakeGraph({"a": ["t"]})
    authority_boost(snippets, project_id=PROJECT, store=graph, params=GraphExpandParams(boost=0.25))
    assert [c for c in graph.calls if c[0] == "linked_docs"] == [
        ("linked_docs", "a"),
        ("linked_docs", "t"),
    ]


def test_snippets_without_a_doc_id_are_left_alone() -> None:
    """An index predating the payload's ``doc_id`` must degrade, not crash or mis-group."""
    plain = Snippet(path="x.md", text="t", score=0.9, tokens=5)
    out = authority_boost(
        [snip("a", 1.0), plain],
        project_id=PROJECT,
        store=FakeGraph({"a": []}),
        params=GraphExpandParams(boost=1.0),
    )
    assert paths(out) == ["a.md", "x.md"]


# ---------------------------------------------------------------------------
# neighbor_notes — the stage-6 append
# ---------------------------------------------------------------------------
def test_neighbours_come_back_as_pointers_with_no_text() -> None:
    """Titles + paths only: the append must cost zero budget_tokens (arch §10-stage6)."""
    graph = FakeGraph({"a": ["n1"], "n2": ["a"]})
    out = neighbor_notes(
        [snip("a", 1.0)], project_id=PROJECT, store=graph, params=GraphExpandParams()
    )
    assert paths(out) == ["n1.md", "n2.md"]
    assert [n.relation for n in out] == ["links_to", "backlink"]
    assert all(not hasattr(n, "text") and not hasattr(n, "tokens") for n in out)


def test_neighbours_exclude_notes_already_in_the_results() -> None:
    """A pointer to something the agent is already reading is noise."""
    graph = FakeGraph({"a": ["b", "n1"]})
    out = neighbor_notes(
        [snip("a", 1.0), snip("b", 0.9)],
        project_id=PROJECT,
        store=graph,
        params=GraphExpandParams(),
    )
    assert paths(out) == ["n1.md"]


def test_neighbours_are_seeded_by_only_the_top_docs() -> None:
    graph = FakeGraph({"a": ["n1"], "b": ["n2"], "c": ["n3"]})
    out = neighbor_notes(
        [snip("a", 1.0), snip("b", 0.9), snip("c", 0.8)],
        project_id=PROJECT,
        store=graph,
        params=GraphExpandParams(top_docs=2),
    )
    assert paths(out) == ["n1.md", "n2.md"]


def test_neighbours_are_capped_so_a_hub_cannot_flood_the_response() -> None:
    graph = FakeGraph({"a": [f"n{i}" for i in range(50)]})
    out = neighbor_notes(
        [snip("a", 1.0)],
        project_id=PROJECT,
        store=graph,
        params=GraphExpandParams(max_related=3),
    )
    assert len(out) == 3


def test_a_note_reachable_twice_is_reported_once() -> None:
    graph = FakeGraph({"a": ["n1"], "b": ["n1"]})
    out = neighbor_notes(
        [snip("a", 1.0), snip("b", 0.9)],
        project_id=PROJECT,
        store=graph,
        params=GraphExpandParams(),
    )
    assert paths(out) == ["n1.md"]


# ---------------------------------------------------------------------------
# graph_expand — the composed stage, including its degradation
# ---------------------------------------------------------------------------
def test_no_store_means_the_stage_is_skipped_entirely() -> None:
    snippets = [snip("a", 0.5), snip("b", 1.0)]
    out, related = graph_expand(
        snippets, project_id=PROJECT, store=None, params=GraphExpandParams()
    )
    assert out is snippets  # untouched, not merely equal
    assert related == []


def test_a_broken_graph_degrades_instead_of_failing_the_search() -> None:
    """The links table is a derived cache; a store from before Stage 2 has none of it.

    Losing the graph must cost the *graph signal*, never the search — the vault is the
    source of truth and retrieval still works without any of this (CLAUDE.md §6 #1).
    """
    snippets = [snip("a", 1.0), snip("b", 0.5)]
    out, related = graph_expand(
        snippets, project_id=PROJECT, store=ExplodingGraph(), params=GraphExpandParams()
    )
    assert paths(out) == ["a.md", "b.md"]
    assert related == []


def test_graph_expand_stamps_obsidian_uris_on_pointers() -> None:
    graph = FakeGraph({"a": ["n1"]})
    _, related = graph_expand(
        [snip("a", 1.0)],
        project_id=PROJECT,
        store=graph,
        params=GraphExpandParams(),
        vault_name="My Vault",
    )
    assert related[0].uri == "obsidian://open?vault=My%20Vault&file=n1.md"


def test_graph_params_come_from_settings() -> None:
    settings = Settings(
        graph_boost=0.4, graph_top_docs=5, graph_max_related=7, graph_max_tag_siblings=2
    )
    assert graph_params_for(settings) == GraphExpandParams(
        boost=0.4, top_docs=5, max_related=7, max_tag_siblings=2
    )


# ---------------------------------------------------------------------------
# related_notes — the tool's core
# ---------------------------------------------------------------------------
def test_related_reports_links_backlinks_and_tag_siblings() -> None:
    graph = FakeGraph(
        {"me": ["out"], "in": ["me"]},
        siblings={"me": [SiblingDoc(doc_id="sib", path="sib.md", title="Sib", shared_tags=2)]},
    )
    notes, unresolved, truncated = related_notes(project_id=PROJECT, doc_id="me", store=graph)
    assert [(n.path, n.relation) for n in notes] == [
        ("out.md", "links_to"),
        ("in.md", "backlink"),
        ("sib.md", "tag_sibling"),
    ]
    assert unresolved == []
    assert truncated is False


def test_a_mutually_linked_note_is_reported_once_as_the_more_specific_relation() -> None:
    graph = FakeGraph({"me": ["peer"], "peer": ["me"]})
    notes, _, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph)
    assert [(n.path, n.relation) for n in notes] == [("peer.md", "links_to")]


def test_tag_siblings_carry_their_overlap_count_and_no_hop_distance() -> None:
    """A shared tag is not a link hop — reporting one would misdescribe the graph."""
    graph = FakeGraph(
        siblings={"me": [SiblingDoc(doc_id="s", path="s.md", title="S", shared_tags=3)]}
    )
    notes, _, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph)
    assert notes[0].hops is None
    assert notes[0].shared_tags == 3


def test_one_hop_does_not_query_the_recursive_neighbour_walk() -> None:
    """At 1 hop every neighbour is already a link or a backlink; the CTE would be waste."""
    graph = FakeGraph({"me": ["out"]})
    related_notes(project_id=PROJECT, doc_id="me", store=graph, hops=1)
    assert not [c for c in graph.calls if c[0] == "neighbors"]


def test_two_hops_adds_the_farther_notes_below_the_direct_ones() -> None:
    graph = FakeGraph(
        {"me": ["out"]},
        far={"me": [NeighborDoc(doc_id="far", path="far.md", title="Far", hops=2)]},
    )
    notes, _, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph, hops=2)
    assert [(n.path, n.relation, n.hops) for n in notes] == [
        ("out.md", "links_to", 1),
        ("far.md", "neighbor", 2),
    ]


def test_a_two_hop_walk_never_demotes_a_direct_link_to_neighbour() -> None:
    """``neighbors`` returns 1-hop docs too; they must keep the more specific relation."""
    graph = FakeGraph(
        {"me": ["out"]},
        far={"me": [NeighborDoc(doc_id="out", path="out.md", title="Out", hops=1)]},
    )
    notes, _, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph, hops=2)
    assert [(n.path, n.relation) for n in notes] == [("out.md", "links_to")]


def test_dangling_links_are_reported_separately_as_hygiene() -> None:
    """ "Nothing links here" and "the links here are broken" are different answers."""
    graph = FakeGraph({"me": []}, dangling={"me": ["Future Note", "Typoo"]})
    notes, unresolved, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph)
    assert notes == []
    assert unresolved == ["Future Note", "Typoo"]


def test_tag_siblings_cannot_crowd_out_the_links() -> None:
    """Measured on the live design vault: 46 tag siblings beside 3 real links.

    `tags` is prefix-expanded, so one shared `project/…` namespace makes every note a weak
    sibling of every other. Unbounded, the relation the tool is named for gets buried — so
    siblings get their own small cap while links stay bounded only by `limit`.
    """
    graph = FakeGraph(
        {"me": ["l1", "l2"]},
        siblings={
            "me": [
                SiblingDoc(doc_id=f"s{i}", path=f"s{i}.md", title=f"S{i}", shared_tags=4)
                for i in range(40)
            ]
        },
    )
    notes, _, _ = related_notes(
        project_id=PROJECT, doc_id="me", store=graph, limit=50, max_tag_siblings=5
    )
    kinds = [n.relation for n in notes]
    assert kinds.count("links_to") == 2  # every link survives
    assert kinds.count("tag_sibling") == 5  # siblings are capped


def test_a_sibling_that_is_also_a_link_does_not_consume_a_sibling_slot() -> None:
    """The cap counts what is *added*: dedupe must not silently shrink it."""
    graph = FakeGraph(
        {"me": ["s0"]},
        siblings={
            "me": [
                SiblingDoc(doc_id=f"s{i}", path=f"s{i}.md", title=f"S{i}", shared_tags=4)
                for i in range(5)
            ]
        },
    )
    notes, _, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph, max_tag_siblings=2)
    assert [(n.path, n.relation) for n in notes] == [
        ("s0.md", "links_to"),
        ("s1.md", "tag_sibling"),
        ("s2.md", "tag_sibling"),
    ]


def test_related_is_capped_and_says_so() -> None:
    graph = FakeGraph({"me": [f"n{i}" for i in range(10)]})
    notes, _, truncated = related_notes(project_id=PROJECT, doc_id="me", store=graph, limit=4)
    assert len(notes) == 4
    assert truncated is True


def test_related_output_is_deterministic() -> None:
    """The gate and the tests both assume two identical vaults produce identical output."""
    graph = FakeGraph({"me": ["c", "a", "b"]})
    first, _, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph)
    second, _, _ = related_notes(project_id=PROJECT, doc_id="me", store=graph)
    assert paths(first) == paths(second) == ["a.md", "b.md", "c.md"]


# ---------------------------------------------------------------------------
# End-to-end against the real SQLite graph (not the fake)
# ---------------------------------------------------------------------------
@pytest.fixture
def live(tmp_path):  # type: ignore[no-untyped-def]
    """A real store holding a small linked vault: hub <- a, b; a -> b; c shares a tag."""
    store = SQLiteMetaStore(tmp_path / "vaultrag.db")
    store.upsert_project(PROJECT, name=PROJECT, vault_path=str(tmp_path))

    def add(path: str, tags: list[str], links: list[str], dangling: tuple[str, ...] = ()) -> str:
        meta = DocumentMetadata(
            path=path,
            title=path.removesuffix(".md").title(),
            hash=f"blake3:{path}",
            tags=tags,
        )
        doc_id = store.upsert_document(PROJECT, meta)
        store.replace_links(
            PROJECT,
            doc_id,
            [Link(kind=LinkKind.wikilink, target=t) for t in [*links, *dangling]],
        )
        store.reresolve_links(PROJECT, doc_id)
        return doc_id

    add("hub.md", ["topic"], [])
    add("a.md", ["topic"], ["hub", "b"], dangling=["Unwritten Note"])
    add("b.md", ["topic"], ["hub"])
    add("c.md", ["topic"], [])
    yield store
    store.close()


def test_related_over_the_real_store_returns_working_paths(live: SQLiteMetaStore) -> None:
    """The plan's acceptance criterion, against real SQL: neighbours + siblings, real paths."""
    hub = live.get_document(PROJECT, "hub.md")
    assert hub is not None
    notes, unresolved, _ = related_notes(project_id=PROJECT, doc_id=hub.id, store=live)
    by_relation = {n.path: n.relation for n in notes}
    assert by_relation["a.md"] == "backlink"
    assert by_relation["b.md"] == "backlink"
    assert by_relation["c.md"] == "tag_sibling"  # no link, shared tag
    assert unresolved == []


def test_related_surfaces_a_dangling_wikilink_from_the_real_store(live: SQLiteMetaStore) -> None:
    a = live.get_document(PROJECT, "a.md")
    assert a is not None
    _, unresolved, _ = related_notes(project_id=PROJECT, doc_id=a.id, store=live)
    assert unresolved == ["Unwritten Note"]


def test_the_boost_lifts_the_real_hub(live: SQLiteMetaStore) -> None:
    """Stage 6 end-to-end on real edges: ``hub`` is cited by both other results, so it rises."""
    ids = {p: live.get_document(PROJECT, p) for p in ("hub.md", "a.md", "b.md", "c.md")}
    assert all(row is not None for row in ids.values())
    snippets = [
        Snippet(path=p, text="t", score=s, tokens=5, doc_id=ids[p].id)  # type: ignore[union-attr]
        for p, s in (("a.md", 1.0), ("b.md", 0.9), ("hub.md", 0.5), ("c.md", 0.1))
    ]
    out = authority_boost(
        snippets, project_id=PROJECT, store=live, params=GraphExpandParams(boost=1.0)
    )
    # hub is cited by both a and b; b is cited by a; c by nobody.
    assert paths(out) == ["hub.md", "b.md", "a.md", "c.md"]


def test_the_graph_is_scoped_to_its_project(live: SQLiteMetaStore) -> None:
    """Tenancy: another project's queries see none of this graph (CLAUDE.md §6)."""
    hub = live.get_document(PROJECT, "hub.md")
    assert hub is not None
    notes, _, _ = related_notes(project_id="other-project", doc_id=hub.id, store=live)
    assert notes == []
