"""P2 Stage 2 verify: the wikilink graph — resolution rules, edges, and the read side.

The plan's acceptance criteria, one test each: two notes linking each other land one
edge apiece; ``backlinks`` returns the reverse set; an unresolved ``[[Future Note]]``
stores raw then resolves once the target is indexed; deleting a note drops its outgoing
edges and NULLs the incoming ones; a reindex from a bare vault rebuilds the whole graph.

The resolution tests carry the most weight. Decision 4 says *never guess* on an
ambiguous target, and "never guess" is a claim about a failure path — so these assert
the ``None``, not just the happy match (the lesson from the Stage 1 silent-pass bug).
"""

from __future__ import annotations

import pytest

from vaultrag.models import DocumentMetadata, Link, LinkKind, document_id
from vaultrag.store import SQLiteMetaStore
from vaultrag.store.links import DocKeyKind, doc_keys_for, link_edges, normalize_key, resolve_key

PROJECT = "meridian"
OTHER_PROJECT = "decoy"


@pytest.fixture
def store(tmp_path):  # type: ignore[no-untyped-def]
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    s.upsert_project(PROJECT, name=PROJECT, vault_path=str(tmp_path))
    s.upsert_project(OTHER_PROJECT, name=OTHER_PROJECT, vault_path=str(tmp_path))
    yield s
    s.close()


def _meta(
    path: str,
    title: str | None = None,
    aliases: list[str] | None = None,
    tags: list[str] | None = None,
) -> DocumentMetadata:
    return DocumentMetadata(
        path=path,
        title=title or path.rsplit("/", 1)[-1].removesuffix(".md"),
        hash=f"blake3:{path}",
        aliases=aliases or [],
        tags=tags or [],
    )


def _add(store: SQLiteMetaStore, meta: DocumentMetadata, links: list[Link] | None = None) -> str:
    """Index one document + its edges, the way the pipeline's _write_graph does."""
    doc_id = store.upsert_document(PROJECT, meta)
    store.replace_links(PROJECT, doc_id, links or [])
    store.reresolve_links(PROJECT, doc_id)
    return doc_id


def _wiki(target: str, anchor: str | None = None) -> Link:
    return Link(kind=LinkKind.wikilink, target=target, anchor=anchor)


# ---------------------------------------------------------------------------
# Normalization + key derivation (pure, no SQL)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gateway Service", "gateway service"),
        ("gateway service", "gateway service"),
        ("Gateway Service.md", "gateway service"),
        ("Gateway  \t Service", "gateway service"),  # whitespace collapses
        ("  Gateway Service  ", "gateway service"),
        ("./services/Gateway.md", "services/gateway"),
        ("/services/Gateway", "services/gateway"),
        ("services\\Gateway", "services/gateway"),  # windows separators
        # `[[Note\|alias]]` in a markdown table: the regex splits on the pipe and leaves
        # the escape behind. Real vaults hit this on every table of links.
        ("Gateway Service\\", "gateway service"),
        ("NOTES.MD", "notes"),  # extension match is case-insensitive
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_key(raw: str, expected: str) -> None:
    assert normalize_key(raw) == expected


def test_doc_keys_cover_path_basename_title_and_aliases() -> None:
    keys = doc_keys_for(
        _meta("services/gateway-service.md", title="Gateway Service", aliases=["gw", "GW"])
    )
    by_kind = {(k.kind, k.key) for k in keys}
    assert (DocKeyKind.path, "services/gateway-service") in by_kind
    assert (DocKeyKind.basename, "gateway-service") in by_kind
    assert (DocKeyKind.note_title, "gateway service") in by_kind
    assert (DocKeyKind.alias, "gw") in by_kind
    # "gw" and "GW" normalize to one key — an alias list is a set of names, not a bag.
    assert sum(1 for k in keys if k.kind is DocKeyKind.alias) == 1


def test_doc_keys_skip_empty_names() -> None:
    keys = doc_keys_for(_meta("note.md", title="   ", aliases=["", "  "]))
    assert all(k.key for k in keys)
    assert not [k for k in keys if k.kind is DocKeyKind.alias]


def test_resolve_key_prefers_the_most_specific_tier() -> None:
    # The same key matches one doc by basename and a different one by title; Obsidian's
    # [[Note]] addresses the file, so basename wins.
    resolved = resolve_key(
        "gateway",
        {DocKeyKind.basename: ["doc-file"], DocKeyKind.note_title: ["doc-titled"]},
    )
    assert resolved == "doc-file"


def test_resolve_key_refuses_to_guess_on_a_tie() -> None:
    assert resolve_key("gateway", {DocKeyKind.note_title: ["doc-a", "doc-b"]}) is None


def test_a_lower_tier_never_breaks_a_higher_tiers_tie() -> None:
    # Two notes genuinely titled "Gateway"; a third aliases it. The alias must NOT be
    # promoted as the winner — the target is ambiguous and stays that way.
    assert (
        resolve_key(
            "gateway",
            {DocKeyKind.note_title: ["doc-a", "doc-b"], DocKeyKind.alias: ["doc-c"]},
        )
        is None
    )


def test_resolve_key_with_no_match_is_none() -> None:
    assert resolve_key("nothing", {}) is None


# ---------------------------------------------------------------------------
# Edge extraction
# ---------------------------------------------------------------------------
def test_link_edges_dedupe_and_preserve_raw() -> None:
    edges = link_edges([_wiki("Gateway Service"), _wiki("gateway service"), _wiki("Gateway")])
    assert len(edges) == 2  # first two collide on the normalized key
    assert edges[0].dst_raw == "Gateway Service"  # raw is what the human wrote
    assert edges[0].dst_key == "gateway service"


def test_link_edges_keep_distinct_anchors_apart() -> None:
    edges = link_edges([_wiki("Runbook", anchor="Rollback"), _wiki("Runbook", anchor="Rollout")])
    assert len(edges) == 2


def test_link_edges_drop_same_note_anchors() -> None:
    # [[#Overview]] parses with an empty target — it addresses this note, not the graph.
    assert link_edges([Link(kind=LinkKind.wikilink, target="", anchor="Overview")]) == []


def test_link_edges_keep_embeds_distinct_from_wikilinks() -> None:
    edges = link_edges([_wiki("Runbook"), Link(kind=LinkKind.embed, target="Runbook", anchor=None)])
    assert {e.kind for e in edges} == {"wikilink", "embed"}


# ---------------------------------------------------------------------------
# Persistence + the reverse lookup
# ---------------------------------------------------------------------------
def test_two_notes_linking_each_other_land_one_edge_each_way(store: SQLiteMetaStore) -> None:
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])
    b = _add(store, _meta("b.md", title="Beta"), [_wiki("Alpha")])

    assert [link.dst_doc for link in store.outgoing_links(PROJECT, a)] == [b]
    assert [link.dst_doc for link in store.outgoing_links(PROJECT, b)] == [a]
    assert [d.doc_id for d in store.backlinks(PROJECT, a)] == [b]
    assert [d.doc_id for d in store.backlinks(PROJECT, b)] == [a]


def test_backlinks_return_paths_titles_and_edge_kind(store: SQLiteMetaStore) -> None:
    target = _add(store, _meta("runbook.md", title="Rollback Runbook"))
    _add(
        store, _meta("incident.md", title="Incident"), [Link(kind=LinkKind.embed, target="Runbook")]
    )

    (back,) = store.backlinks(PROJECT, target)
    assert (back.path, back.title, back.kind) == ("incident.md", "Incident", "embed")


def test_anchor_is_preserved_on_the_edge(store: SQLiteMetaStore) -> None:
    _add(store, _meta("runbook.md"))
    src = _add(store, _meta("incident.md"), [_wiki("runbook", anchor="Rollback")])
    (edge,) = store.outgoing_links(PROJECT, src)
    assert edge.anchor == "Rollback"


def test_replace_links_is_idempotent(store: SQLiteMetaStore) -> None:
    _add(store, _meta("b.md", title="Beta"))
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])
    first = store.outgoing_links(PROJECT, a)

    store.replace_links(PROJECT, a, [_wiki("Beta")])
    assert store.outgoing_links(PROJECT, a) == first  # same ids, same rows — no churn


def test_removing_a_link_from_the_text_removes_the_edge(store: SQLiteMetaStore) -> None:
    _add(store, _meta("b.md", title="Beta"))
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])
    assert store.outgoing_links(PROJECT, a)

    store.replace_links(PROJECT, a, [])  # the note was edited; the wikilink is gone
    assert store.outgoing_links(PROJECT, a) == []


def test_a_note_can_be_linked_by_path_basename_title_or_alias(store: SQLiteMetaStore) -> None:
    target = _add(
        store,
        _meta("services/gateway-service.md", title="Gateway Service", aliases=["gw"]),
    )
    # Source names are deliberately unrelated: naming one src-gateway-service.md would
    # collide with the target's own basename and make the target legitimately ambiguous.
    for i, raw in enumerate(
        ("services/gateway-service", "gateway-service", "Gateway Service", "gw")
    ):
        src = _add(store, _meta(f"src{i}.md"), [_wiki(raw)])
        (edge,) = store.outgoing_links(PROJECT, src)
        assert edge.dst_doc == target, f"{raw!r} should resolve to the gateway note"


# ---------------------------------------------------------------------------
# Unresolved targets: stored raw, then re-resolved when the target appears
# ---------------------------------------------------------------------------
def test_an_unresolved_link_is_stored_raw_then_resolves_when_the_target_appears(
    store: SQLiteMetaStore,
) -> None:
    src = _add(store, _meta("a.md", title="Alpha"), [_wiki("Future Note")])

    (edge,) = store.outgoing_links(PROJECT, src)
    assert edge.dst_doc is None
    assert edge.dst_raw == "Future Note"  # the human's text survives
    assert not edge.resolved
    assert [link.dst_raw for link in store.unresolved_links(PROJECT)] == ["Future Note"]

    future = _add(store, _meta("future-note.md", title="Future Note"))

    (edge,) = store.outgoing_links(PROJECT, src)
    assert edge.dst_doc == future
    assert store.unresolved_links(PROJECT) == []
    assert [d.doc_id for d in store.backlinks(PROJECT, future)] == [src]


def test_an_ambiguous_target_stays_unresolved(store: SQLiteMetaStore) -> None:
    _add(store, _meta("one/gateway.md", title="Gateway"))
    _add(store, _meta("two/gateway.md", title="Gateway"))
    src = _add(store, _meta("a.md"), [_wiki("Gateway")])

    (edge,) = store.outgoing_links(PROJECT, src)
    assert edge.dst_doc is None, "two notes named Gateway — resolving either one is a guess"
    assert edge.dst_raw == "Gateway"


def test_deleting_one_of_two_collided_notes_resolves_the_tie(store: SQLiteMetaStore) -> None:
    _add(store, _meta("one/gateway.md", title="Gateway"))
    survivor = _add(store, _meta("two/gateway.md", title="Gateway"))
    src = _add(store, _meta("a.md"), [_wiki("Gateway")])
    assert store.outgoing_links(PROJECT, src)[0].dst_doc is None

    store.delete_document(PROJECT, "one/gateway.md")

    assert store.outgoing_links(PROJECT, src)[0].dst_doc == survivor


def test_a_new_higher_tier_match_repoints_an_already_resolved_edge(
    store: SQLiteMetaStore,
) -> None:
    titled = _add(store, _meta("notes/misc.md", title="Gateway"))
    src = _add(store, _meta("a.md"), [_wiki("Gateway")])
    assert store.outgoing_links(PROJECT, src)[0].dst_doc == titled

    # A file literally named Gateway.md now exists — Obsidian would point [[Gateway]]
    # at it, and so must we. A resolved edge is not frozen.
    by_file = _add(store, _meta("Gateway.md", title="Something Else"))
    assert store.outgoing_links(PROJECT, src)[0].dst_doc == by_file


# ---------------------------------------------------------------------------
# Delete / rename cleanup
# ---------------------------------------------------------------------------
def test_deleting_a_note_drops_outgoing_edges_and_nulls_incoming(store: SQLiteMetaStore) -> None:
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])
    b = _add(store, _meta("b.md", title="Beta"), [_wiki("Alpha")])
    assert store.outgoing_links(PROJECT, b)[0].dst_doc == a

    store.delete_document(PROJECT, "a.md")

    assert store.outgoing_links(PROJECT, a) == [], "the deleted note's own edges are gone"
    (edge,) = store.outgoing_links(PROJECT, b)
    assert edge.dst_doc is None, "an edge pointing at a deleted note dangles, it doesn't vanish"
    assert edge.dst_raw == "Alpha"


def test_a_deleted_note_coming_back_re_resolves_its_backlinks(store: SQLiteMetaStore) -> None:
    _add(store, _meta("a.md", title="Alpha"))
    b = _add(store, _meta("b.md", title="Beta"), [_wiki("Alpha")])
    store.delete_document(PROJECT, "a.md")
    assert store.outgoing_links(PROJECT, b)[0].dst_doc is None

    restored = _add(store, _meta("a.md", title="Alpha"))
    assert store.outgoing_links(PROJECT, b)[0].dst_doc == restored


def test_reingesting_a_note_keeps_its_backlinks(store: SQLiteMetaStore) -> None:
    # Re-ingest is an upsert, not a delete+insert: incoming edges must survive an edit.
    a = _add(store, _meta("a.md", title="Alpha"))
    b = _add(store, _meta("b.md", title="Beta"), [_wiki("Alpha")])

    _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])  # the note was edited

    assert [d.doc_id for d in store.backlinks(PROJECT, a)] == [b]


# ---------------------------------------------------------------------------
# Read side: neighbors + tag siblings
# ---------------------------------------------------------------------------
def test_neighbors_walk_both_directions(store: SQLiteMetaStore) -> None:
    _add(store, _meta("hub.md", title="Hub"))
    out = _add(store, _meta("out.md", title="Out"))
    inbound = _add(store, _meta("in.md", title="In"), [_wiki("Hub")])
    hub = _add(store, _meta("hub.md", title="Hub"), [_wiki("Out")])

    got = {n.doc_id: n.hops for n in store.neighbors(PROJECT, hub, hops=1)}
    assert got == {out: 1, inbound: 1}


def test_neighbors_reach_two_hops_and_report_the_shortest(store: SQLiteMetaStore) -> None:
    _add(store, _meta("c.md", title="Charlie"))
    _add(store, _meta("b.md", title="Bravo"), [_wiki("Charlie")])
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Bravo"), _wiki("Charlie")])

    got = {n.title: n.hops for n in store.neighbors(PROJECT, a, hops=2)}
    # Charlie is reachable at 1 (direct) and 2 (via Bravo) — the shorter one wins.
    assert got == {"Bravo": 1, "Charlie": 1}


def test_neighbors_terminate_on_a_cycle(store: SQLiteMetaStore) -> None:
    _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])
    _add(store, _meta("b.md", title="Beta"), [_wiki("Alpha")])
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])

    assert [n.title for n in store.neighbors(PROJECT, a, hops=3)] == ["Beta"]


def test_neighbors_exclude_the_starting_document(store: SQLiteMetaStore) -> None:
    _add(store, _meta("b.md", title="Beta"))
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])
    assert a not in {n.doc_id for n in store.neighbors(PROJECT, a, hops=2)}


def test_neighbors_respect_hops_and_limit(store: SQLiteMetaStore) -> None:
    _add(store, _meta("c.md", title="Charlie"))
    _add(store, _meta("b.md", title="Bravo"), [_wiki("Charlie")])
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Bravo")])

    assert [n.title for n in store.neighbors(PROJECT, a, hops=1)] == ["Bravo"]
    assert len(store.neighbors(PROJECT, a, hops=2, limit=1)) == 1
    assert store.neighbors(PROJECT, a, hops=0) == []


def test_unresolved_edges_are_not_walkable(store: SQLiteMetaStore) -> None:
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Nowhere")])
    assert store.neighbors(PROJECT, a, hops=2) == []


def test_tag_siblings_rank_by_shared_tag_count(store: SQLiteMetaStore) -> None:
    me = _add(store, _meta("me.md", tags=["ops", "runbook", "db"]))
    _add(store, _meta("close.md", tags=["ops", "runbook"]))
    _add(store, _meta("far.md", tags=["ops"]))
    _add(store, _meta("unrelated.md", tags=["frontend"]))

    got = [(s.path, s.shared_tags) for s in store.tag_siblings(PROJECT, me)]
    assert got == [("close.md", 2), ("far.md", 1)]


def test_tag_siblings_exclude_self_and_respect_limit(store: SQLiteMetaStore) -> None:
    me = _add(store, _meta("me.md", tags=["ops"]))
    _add(store, _meta("a.md", tags=["ops"]))
    _add(store, _meta("b.md", tags=["ops"]))

    siblings = store.tag_siblings(PROJECT, me, limit=1)
    assert len(siblings) == 1
    assert me not in {s.doc_id for s in store.tag_siblings(PROJECT, me)}


# ---------------------------------------------------------------------------
# Tenancy: the graph is per-project, like everything else (CLAUDE.md §6)
# ---------------------------------------------------------------------------
def test_the_graph_never_crosses_a_project_boundary(store: SQLiteMetaStore) -> None:
    # A decoy project holds a note with the exact name the other project links to.
    decoy_id = store.upsert_document(OTHER_PROJECT, _meta("beta.md", title="Beta"))

    _add(store, _meta("b.md", title="Beta"))
    a = _add(store, _meta("a.md", title="Alpha"), [_wiki("Beta")])

    (edge,) = store.outgoing_links(PROJECT, a)
    assert edge.dst_doc != decoy_id
    assert edge.dst_doc == document_id(PROJECT, "b.md")
    assert store.backlinks(OTHER_PROJECT, decoy_id) == []
    assert store.outgoing_links(OTHER_PROJECT, a) == []


def test_an_unresolved_link_does_not_resolve_to_another_projects_note(
    store: SQLiteMetaStore,
) -> None:
    src = _add(store, _meta("a.md"), [_wiki("Shared Name")])
    store.upsert_document(OTHER_PROJECT, _meta("shared-name.md", title="Shared Name"))

    assert store.outgoing_links(PROJECT, src)[0].dst_doc is None
