"""P3 Stage 2 verify: one process serves N projects, and no tenant can reach another's.

This is the stage's real acceptance criterion, and it is written adversarially on purpose.
Everything here runs against **one** ``build_mcp_server`` holding **two registered
projects** backed by one SQLite db and one Qdrant collection — the deployment shape
``../Rag/projects/vaultrag/multi-tenant-architecture.md`` describes — and each test asks
whether a caller scoped to ``alpha`` can obtain something belonging to ``beta``.

It extends P2's decoy-tenant test (``test_index.py``, where the decoy was a hand-inserted
payload) in the way the plan asks: **the decoy is now a real registered project** with its
own vault on disk, its own documents, its own link graph and its own writer. The failure
mode that matters is not "the Qdrant filter is missing" — that has been an unconditional
``must`` since P0 and is tested there — it is the new one this stage introduces: a *vault
root, writer or graph captured once at construction* while ``project`` varies per call, so
``open("notes/guide.md", project="alpha")`` quietly reads beta's copy.

Every fixture below therefore makes the two projects **maximally confusable**: the same
relative paths, the same note titles, the same chunk text, and the same dense vector.
A test that passes because the two vaults happened to differ proves nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from qdrant_client import QdrantClient

from vaultrag.config import Settings
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index import QdrantVectorStore, SparseVector
from vaultrag.indexer.pipeline import IngestContext
from vaultrag.mcp import ServedProject, build_mcp_server
from vaultrag.models import Chunk, DocumentMetadata, Link, LinkKind
from vaultrag.projects import ProjectRegistry, UnknownProjectError
from vaultrag.store.sqlite import SQLiteMetaStore
from vaultrag.writeback import NoteWriter

ALPHA = "alpha"
BETA = "beta"
DIMS = 4

# The same relative path in both vaults, holding different content: the single most direct
# probe of "which root did `open` resolve against?".
SHARED_PATH = "notes/guide.md"
ALPHA_TEXT = "# Guide\n\nAlpha's own private guide.\n"
BETA_TEXT = "# Guide\n\nBeta's own private guide.\n"

# The same relative path again, this time inside the default write root — the write tools
# only accept `agent-inbox/`, so the "which vault did the edit land in?" probe needs a path
# that is both shared and writable.
SHARED_INBOX_PATH = "agent-inbox/log.md"
INBOX_TEXT = "# Log\n\n## Entries\n\nfirst\n"


class _Sparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[1], values=[1.0]) for _ in texts]

    def embed_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[1], values=[1.0])


class _Embedder:
    """One vector for everything — so tenancy, not similarity, decides every result."""

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def dims(self) -> int:
        return DIMS

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def close(self) -> None:
        pass


def _chunk(project: str, doc_id: str, path: str, text: str, ix: int = 0) -> Chunk:
    return Chunk(
        project_id=project,
        doc_id=doc_id,
        path=path,
        ix=ix,
        text=text,
        heading_path=["Guide"],
        anchor="guide",
        line_start=1,
        line_end=3,
        tags=["topic", "shared"],
        # Identical across tenants on purpose: same text, same hash, same vector — the
        # tenant filter is then the only thing that can tell the two chunks apart.
        chunk_hash=f"hash-{path}-{ix}-{project}",
        tokens=8,
        contextual_prefix=f"{project} ▸ {path} ▸ Guide",
    )


@pytest.fixture()
def world(tmp_path: Path) -> Any:
    """Two registered projects in one store + one collection, deliberately identical.

    Yields ``(server, store, vaults)`` where ``vaults`` maps project id → vault root.
    """
    vaults = {}
    for pid, text in ((ALPHA, ALPHA_TEXT), (BETA, BETA_TEXT)):
        root = tmp_path / pid
        (root / "notes").mkdir(parents=True)
        (root / SHARED_PATH).write_text(text, encoding="utf-8")
        (root / "agent-inbox").mkdir()
        (root / SHARED_INBOX_PATH).write_text(INBOX_TEXT, encoding="utf-8")
        vaults[pid] = root
    # Only beta has this note: proof that a miss in alpha is a *miss*, not a silent read.
    (vaults[BETA] / "notes" / "beta-only.md").write_text("# Beta Only\n\nsecret\n", "utf-8")

    store = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    base = Settings(
        project_id="unused",
        vault_path=tmp_path,
        state_dir=tmp_path / "state",
        embed_dims=DIMS,
    )
    registry = ProjectRegistry(store, base)
    for pid in (ALPHA, BETA):
        registry.add(pid, vaults[pid], name=f"{pid.title()} Vault")

    vector_store = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=_Sparse()
    )
    vector_store.ensure_collection()

    # Identical text, identical vector, identical path — different tenants.
    for pid in (ALPHA, BETA):
        doc_id = store.upsert_document(
            pid,
            DocumentMetadata(
                path=SHARED_PATH, title="Guide", hash=f"blake3:{pid}", tags=["topic", "shared"]
            ),
        )
        chunk = _chunk(pid, doc_id, SHARED_PATH, f"{pid} guide body about deployment")
        vector_store.upsert_chunks(
            [chunk], {chunk.chunk_hash: [1.0, 0.0, 0.0, 0.0]}, embed_model="fake-model"
        )
    # Beta alone gets a second document, a folder alpha does not have, and a link graph.
    beta_extra = store.upsert_document(
        BETA,
        DocumentMetadata(
            path="decisions/why-beta.md", title="Why Beta", hash="blake3:b2", tags=["decision"]
        ),
    )
    store.replace_links(BETA, beta_extra, [Link(kind=LinkKind.wikilink, target="Guide")])
    store.reresolve_links(BETA, beta_extra)

    served = [
        ServedProject(
            project_id=pid,
            vault_root=vaults[pid],
            embedder=QueryEmbedder(_Embedder()),
            name=f"{pid.title()} Vault",
            vault_name=pid,
            writer=NoteWriter(
                IngestContext(
                    project_id=pid,
                    vault_path=vaults[pid],
                    store=store,
                    vector_store=vector_store,
                    embedder=_Embedder(),
                ),
                git_enabled=False,
                vault_name=pid,
            ),
        )
        for pid in (ALPHA, BETA)
    ]
    server = build_mcp_server(
        projects=served,
        vector_store=vector_store,
        sparse=_Sparse(),
        graph_store=store,
        meta_store=store,
    )
    yield server, store, vaults
    store.close()


def call(server: FastMCP, tool: str, args: dict[str, Any]) -> Any:
    async def _run() -> Any:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return result.structured_content

    return asyncio.run(_run())


def call_expecting_error(server: FastMCP, tool: str, args: dict[str, Any]) -> str:
    async def _run() -> str:
        async with Client(server) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool(tool, args)
            return str(excinfo.value)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# The core isolation claim, tool by tool
# ---------------------------------------------------------------------------
def test_search_in_one_project_never_returns_the_others_chunk(world: Any) -> None:
    """The decoy is a *registered* project whose chunk is textually and vectorially
    identical — so only the tenant filter can separate them (plan Stage 2 verify)."""
    server, _store, _vaults = world
    alpha = call(server, "search", {"query": "deployment", "project": ALPHA})
    beta = call(server, "search", {"query": "deployment", "project": BETA})
    # Non-vacuity: both decoys really are in the one collection and really do match, so a
    # clean alpha result is the filter working, not beta's chunk being absent.
    assert alpha["snippets"] and beta["snippets"]
    assert {s["text"] for s in beta["snippets"]} != {s["text"] for s in alpha["snippets"]}
    for snippet in alpha["snippets"]:
        assert snippet["text"].startswith(ALPHA)
    for snippet in beta["snippets"]:
        assert snippet["text"].startswith(BETA)
    assert alpha["project_id"] == ALPHA


def test_each_project_sees_only_its_own_copy_of_a_shared_path(world: Any) -> None:
    """Both vaults hold ``notes/guide.md``. ``open`` must resolve against the *selected*
    project's root — the failure mode a construction-time vault_root would produce."""
    server, _store, _vaults = world
    assert (
        "Alpha's own private guide"
        in call(server, "open", {"path": SHARED_PATH, "project": ALPHA})["text"]
    )
    assert (
        "Beta's own private guide"
        in call(server, "open", {"path": SHARED_PATH, "project": BETA})["text"]
    )


def test_open_cannot_read_a_note_that_exists_only_in_the_other_vault(world: Any) -> None:
    """A miss must be a miss. If this returns content, one root is serving both tenants."""
    message = call_expecting_error(
        world[0], "open", {"path": "notes/beta-only.md", "project": ALPHA}
    )
    assert "not found" in message
    assert (
        "secret" in call(world[0], "open", {"path": "notes/beta-only.md", "project": BETA})["text"]
    )


def test_related_explores_only_the_selected_projects_graph(world: Any) -> None:
    """Beta's ``why-beta.md`` backlinks its guide; alpha's identically-pathed guide has no
    neighbours. One shared ``links`` table, two answers."""
    server, _store, _vaults = world
    assert call(server, "related", {"path": SHARED_PATH, "project": ALPHA})["notes"] == []
    beta = call(server, "related", {"path": SHARED_PATH, "project": BETA})
    assert [n["path"] for n in beta["notes"]] == ["decisions/why-beta.md"]


def test_related_refuses_a_document_indexed_only_under_the_other_project(world: Any) -> None:
    message = call_expecting_error(
        world[0], "related", {"path": "decisions/why-beta.md", "project": ALPHA}
    )
    assert "not indexed in project 'alpha'" in message


def test_overview_counts_only_the_selected_projects_notes(world: Any) -> None:
    """The aggregates are per-tenant, from one shared table."""
    server, _store, _vaults = world
    alpha = call(server, "overview", {"project": ALPHA})
    beta = call(server, "overview", {"project": BETA})
    assert alpha["notes"] == 1
    assert beta["notes"] == 2
    assert [f["path"] for f in alpha["folders"]] == ["notes"]
    assert sorted(f["path"] for f in beta["folders"]) == ["decisions", "notes"]
    assert "decision" in {t["tag"] for t in beta["tags"]}
    assert "decision" not in {t["tag"] for t in alpha["tags"]}


def test_a_write_lands_in_the_named_projects_vault_and_nowhere_else(world: Any) -> None:
    """Each project owns its vault dir, write root and git repo, so a multi-project write
    is N independent commits rather than a transaction (plan Stage 2 open question)."""
    server, _store, vaults = world
    result = call(
        server, "write_note", {"title": "Alpha Memo", "content": "body", "project": ALPHA}
    )
    written = vaults[ALPHA] / result["path"]
    assert written.is_file()
    assert not (vaults[BETA] / result["path"]).exists()


def test_appending_through_one_project_cannot_touch_the_others_file(world: Any) -> None:
    """Same relative path in both vaults; the edit must apply to exactly one."""
    server, _store, vaults = world
    before = (vaults[BETA] / SHARED_INBOX_PATH).read_text(encoding="utf-8")
    call(
        server,
        "append_section",
        {
            "path": SHARED_INBOX_PATH,
            "heading": "Entries",
            "content": "appended",
            "project": ALPHA,
        },
    )
    assert "appended" in (vaults[ALPHA] / SHARED_INBOX_PATH).read_text(encoding="utf-8")
    assert (vaults[BETA] / SHARED_INBOX_PATH).read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# The selection layer itself: refuse rather than guess
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search", {"query": "deployment"}),
        ("open", {"path": SHARED_PATH}),
        ("related", {"path": SHARED_PATH}),
        ("overview", {}),
        ("write_note", {"title": "T", "content": "c"}),
    ],
)
def test_omitting_project_on_a_multi_project_server_is_refused(
    world: Any, tool: str, args: dict[str, Any]
) -> None:
    """No implicit default with several tenants: a forgotten argument must fail loudly,
    not silently query whichever project was registered first."""
    message = call_expecting_error(world[0], tool, args)
    assert "serves several projects" in message
    assert ALPHA in message and BETA in message  # the error names the choices


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search", {"query": "deployment", "project": "gamma"}),
        ("open", {"path": SHARED_PATH, "project": "gamma"}),
        ("related", {"path": SHARED_PATH, "project": "gamma"}),
        ("overview", {"project": "gamma"}),
        ("write_note", {"title": "T", "content": "c", "project": "gamma"}),
    ],
)
def test_naming_an_unserved_project_is_refused_by_every_tool(
    world: Any, tool: str, args: dict[str, Any]
) -> None:
    """CLAUDE.md §6: a client narrows, never widens. There is no tool-shaped exception."""
    message = call_expecting_error(world[0], tool, args)
    assert "not served by this server" in message


def test_list_projects_reports_both_served_projects(world: Any) -> None:
    server, _store, _vaults = world
    rows = call(server, "list_projects", {})["result"]
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == {ALPHA, BETA}
    assert by_id[ALPHA]["name"] == "Alpha Vault"
    assert by_id[ALPHA]["notes"] == 1
    assert by_id[BETA]["notes"] == 2
    assert all(row["writable"] for row in rows)


def test_a_read_only_project_is_named_in_the_refusal_not_hidden(tmp_path: Path) -> None:
    """A mixed server keeps the write tools for the tenants that have them, and tells the
    others why — rather than hiding the memory loop from everyone."""
    roots = {}
    store = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    for pid in (ALPHA, BETA):
        roots[pid] = tmp_path / pid
        roots[pid].mkdir()
        # The documents FK points at `projects`, so a writer needs its project row to exist.
        store.upsert_project(pid, name=pid, vault_path=str(roots[pid]))
    vector_store = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=_Sparse()
    )
    vector_store.ensure_collection()
    server = build_mcp_server(
        projects=[
            ServedProject(
                project_id=ALPHA,
                vault_root=roots[ALPHA],
                embedder=QueryEmbedder(_Embedder()),
                writer=NoteWriter(
                    IngestContext(
                        project_id=ALPHA,
                        vault_path=roots[ALPHA],
                        store=store,
                        vector_store=vector_store,
                        embedder=_Embedder(),
                    ),
                    git_enabled=False,
                ),
            ),
            ServedProject(  # no writer
                project_id=BETA,
                vault_root=roots[BETA],
                embedder=QueryEmbedder(_Embedder()),
            ),
        ],
        vector_store=vector_store,
        sparse=_Sparse(),
    )
    try:
        message = call_expecting_error(
            server, "write_note", {"title": "T", "content": "c", "project": BETA}
        )
        assert "served read-only" in message
        assert call(server, "write_note", {"title": "T", "content": "c", "project": ALPHA})
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Construction-time invariants
# ---------------------------------------------------------------------------
def test_a_server_with_no_projects_is_a_construction_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_mcp_server(projects=[], vector_store=object(), sparse=_Sparse())  # type: ignore[arg-type]


def test_two_bundles_with_the_same_id_are_rejected(tmp_path: Path) -> None:
    """Otherwise one silently shadows the other and half the calls go to the wrong vault."""
    bundle = ServedProject(
        project_id=ALPHA, vault_root=tmp_path, embedder=QueryEmbedder(_Embedder())
    )
    with pytest.raises(ValueError, match="duplicate project"):
        build_mcp_server(
            projects=[bundle, bundle],
            vector_store=object(),  # type: ignore[arg-type]
            sparse=_Sparse(),
        )


def test_a_served_projects_vault_root_is_resolved_on_construction(tmp_path: Path) -> None:
    """Containment compares against this root; a relative or symlinked one weakens every
    check that uses it, so it is normalised once rather than at each call site."""
    nested = tmp_path / "real"
    nested.mkdir()
    link = tmp_path / "link"
    link.symlink_to(nested)
    bundle = ServedProject(project_id=ALPHA, vault_root=link, embedder=QueryEmbedder(_Embedder()))
    assert bundle.vault_root == nested.resolve()


# ---------------------------------------------------------------------------
# Registry lifecycle under two tenants
# ---------------------------------------------------------------------------
def test_deleting_a_project_cascades_its_rows_and_leaves_the_other_intact(world: Any) -> None:
    """Plan Stage 2 verify — the FK cascade must be tenant-scoped, not global."""
    _server, store, _vaults = world
    assert store.document_count(BETA) == 2
    assert store.delete_project(BETA) is True
    assert store.document_count(BETA) == 0
    assert store.document_count(ALPHA) == 1  # the neighbour is untouched
    assert [row.id for row in store.list_projects()] == [ALPHA]


def test_resolving_an_unregistered_project_names_the_fix(tmp_path: Path) -> None:
    store = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    store.init_schema()
    registry = ProjectRegistry(
        store, Settings(project_id="p", vault_path=tmp_path, state_dir=tmp_path / "state")
    )
    try:
        with pytest.raises(UnknownProjectError, match="vaultrag project add"):
            registry.resolve("nope")
    finally:
        store.close()


def test_parser_version_is_tracked_per_project_not_per_deployment(world: Any) -> None:
    """Plan Stage 2 verify: "a project with a stale PARSER_VERSION warns independently of
    its neighbours". ``index_state`` is keyed by ``project_id``, so upgrading one tenant's
    parser marker must leave the other's stale — otherwise one project's ``reindex --force``
    would silently clear the warning that tells the operator to re-force the others.
    """
    _server, store, _vaults = world
    store.set_parser_version(ALPHA, 1)
    store.set_parser_version(BETA, 4)
    assert store.get_parser_version(ALPHA) == 1
    assert store.get_parser_version(BETA) == 4
    store.delete_project(BETA)
    assert store.get_parser_version(ALPHA) == 1, "the neighbour's marker survives a delete"
    assert store.get_parser_version(BETA) is None  # cascaded with its project


def test_the_shared_embed_cache_survives_deleting_one_project(world: Any) -> None:
    """The cache is keyed ``(model, dims, chunk_hash)`` with no tenant in it — deliberately
    shared, so two vaults holding the same boilerplate embed it once. Dropping a project
    must not evict it out from under the others (``store.delete_project``)."""
    _server, store, _vaults = world
    store.put_cached_vector("fake-model", DIMS, "hash-shared", b"\x00\x01\x02\x03")
    store.delete_project(BETA)
    assert store.get_cached_vectors("fake-model", DIMS, ["hash-shared"]) == {
        "hash-shared": b"\x00\x01\x02\x03"
    }
