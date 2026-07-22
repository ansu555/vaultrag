"""P3 Stage 4 verify: the REST transport, and that it is genuinely a *transport*.

The stage's real acceptance criterion is the **parity** block below: for one query, the
MCP tool and the HTTP endpoint must return the same payload. Any divergence means logic
leaked out of :mod:`vaultrag.service` and into a transport, which is the failure the whole
service extraction exists to prevent — so those tests drive **one** service instance
through both front doors and compare, rather than building two stacks that happen to agree.

Everything else here is the surface the reference note specifies and the security model
requires: every endpoint answers, errors are RFC 7807 ``problem+json``, auth is required on
everything but ``/healthz``, and tenancy refuses from the same single enforcement point it
always did.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP
from qdrant_client import QdrantClient

from tests.fakes import PROJECT, FakeEmbedder, FakeSparse, FakeVectorStore, make_hit
from tests.test_graph import FakeGraph
from vaultrag.api import PROBLEM_MEDIA_TYPE, create_app
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index import QdrantVectorStore
from vaultrag.index.base import ScoredChunk
from vaultrag.indexer.pipeline import IngestContext
from vaultrag.mcp import mcp_server_for
from vaultrag.service import ServedProject, VaultRAGService
from vaultrag.store.base import DocumentRow
from vaultrag.store.sqlite import SQLiteMetaStore
from vaultrag.writeback import NoteWriter

KEY = "test-key-abcdefghijklmnop"
AUTH = {"Authorization": f"Bearer {KEY}"}

GUIDE = """\
---
title: Guide
tags: [howto]
---

# Guide

Intro paragraph.

## Setup

Install uv.
Run docker.

## Quick Start

Call search first.
"""


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "guide.md").write_text(GUIDE, encoding="utf-8")
    (root / "drafts").mkdir()
    (root / "drafts" / "hidden.md").write_text("# WIP\n", encoding="utf-8")
    (root / ".ragignore").write_text("drafts/\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.md").write_text("# Not yours\n", encoding="utf-8")
    return root


class _StubOverviewStore:
    """The three aggregates ``overview`` reads, and nothing else."""

    def __init__(
        self,
        *,
        notes: int = 2,
        folders: Sequence[tuple[str, int]] = (("notes", 1), ("drafts", 1)),
        tags: Sequence[tuple[str, int]] = (("howto", 1),),
    ) -> None:
        self._notes = notes
        self._folders = list(folders)
        self._tags = list(tags)

    def document_count(self, project_id: str) -> int:
        return self._notes

    def folder_counts(
        self, project_id: str, depth: int = 2, limit: int = 50
    ) -> list[tuple[str, int]]:
        return self._folders[:limit]

    def top_tags(self, project_id: str, limit: int = 20) -> list[tuple[str, int]]:
        return self._tags[:limit]


def graph_with_one_neighbour() -> FakeGraph:
    """`notes/guide` links to `notes/other` — enough that `related` returns something.

    Reuses ``test_graph``'s ``FakeGraph`` rather than growing a second stand-in: two
    drifting fakes of one protocol is how a suite goes green against an interface the real
    code no longer has (the reason ``tests/fakes.py`` exists at all).
    """

    def row(doc_id: str, title: str) -> DocumentRow:
        return DocumentRow(
            id=doc_id,
            project_id=PROJECT,
            path=f"{doc_id}.md",
            hash="h",
            title=title,
            type="note",
            mtime=None,
            status="indexed",
        )

    return FakeGraph(
        edges={"notes/guide": ["notes/other"]},
        titles={"notes/guide": "Guide", "notes/other": "Other"},
        docs={"notes/guide.md": row("notes/guide", "Guide")},
    )


def build_service(
    vault: Path,
    *,
    hits: Sequence[ScoredChunk] = (),
    projects: Sequence[ServedProject] | None = None,
    meta_store: object | None = None,
    graph_store: object | None = None,
    vector_store: object | None = None,
) -> VaultRAGService:
    """One service — the thing both transports are then wrapped around."""
    served = (
        list(projects)
        if projects is not None
        else [
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                name="Design Vault",
            )
        ]
    )
    return VaultRAGService(
        projects=served,
        vector_store=vector_store or FakeVectorStore(hits=hits),  # type: ignore[arg-type]
        sparse=FakeSparse(),
        meta_store=meta_store,  # type: ignore[arg-type]
        graph_store=graph_store,  # type: ignore[arg-type]
    )


def client_for(service: VaultRAGService, *, api_key: str | None = KEY) -> TestClient:
    # raise_server_exceptions=False so the catch-all 500 handler is exercised as a client
    # would see it, rather than the exception propagating into the test.
    return TestClient(create_app(service, api_key=api_key), raise_server_exceptions=False)


def build_both(service: VaultRAGService) -> tuple[TestClient, FastMCP]:
    """The parity harness: one service, two transports over it."""
    return client_for(service), mcp_server_for(service)


def call_tool(server: FastMCP, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            assert result.structured_content is not None
            return result.structured_content

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# PARITY — the stage's acceptance criterion
# ---------------------------------------------------------------------------
def test_mcp_search_and_rest_search_return_the_same_payload(vault: Path) -> None:
    """The whole point of P3 Stage 4, asserted directly.

    One service, one query, two transports. If these ever differ, something moved out of
    ``vaultrag/service.py`` and into a transport — which is the divergence the extraction
    exists to make impossible rather than merely unlikely.
    """
    service = build_service(vault, hits=[make_hit(), make_hit(path="notes/guide.md", score=0.5)])
    rest, mcp = build_both(service)

    via_mcp = call_tool(mcp, "search", {"query": "hybrid search", "project": PROJECT})
    via_rest = rest.post(
        f"/v1/{PROJECT}/search", json={"query": "hybrid search"}, headers=AUTH
    ).json()

    assert via_rest == via_mcp
    assert via_rest["snippets"], "the fixture must actually return hits, or parity is vacuous"


def test_parity_holds_for_every_search_argument(vault: Path) -> None:
    """Parity is not just the happy path: k, filters, budget and mode must all land the same.

    **Asserted on what each transport *asked the store for*, not only on what came back.**
    Comparing responses alone is vacuous here for a reason worth writing down: the fake
    vector store returns a preset hit list regardless of the filter, so a transport that
    dropped `filters` on the floor would return an identical payload and pass. Verified by
    mutation — deleting the filter argument from the REST route left a response-only
    parity test green and fails this one. The recorded ``query()`` kwargs are the real
    evidence that one set of arguments reached the engine.
    """
    hits = [make_hit(path=f"notes/n{i}.md", score=1.0 - i / 10) for i in range(5)]
    store = FakeVectorStore(hits=hits)
    service = build_service(vault, vector_store=store)
    rest, mcp = build_both(service)

    args: dict[str, Any] = {
        "query": "docker compose",
        "k": 3,
        "budget_tokens": 300,
        "filters": {"tags": ["howto"], "paths": ["notes/n1.md"], "type": "note"},
        "mode": "compact",
    }
    via_mcp = call_tool(mcp, "search", {**args, "project": PROJECT})
    via_rest = rest.post(f"/v1/{PROJECT}/search", json=args, headers=AUTH).json()

    assert via_rest == via_mcp
    assert len(store.calls) == 2
    assert store.calls[0] == store.calls[1]
    # And the filter genuinely travelled, rather than both transports dropping it alike.
    filters = store.calls[0]["filters"]
    assert filters is not None
    assert filters.tags == ("howto",)  # type: ignore[union-attr]
    assert filters.paths == ("notes/n1.md",)  # type: ignore[union-attr]


def test_mcp_open_and_rest_doc_return_the_same_payload(vault: Path) -> None:
    service = build_service(vault)
    rest, mcp = build_both(service)

    via_mcp = call_tool(
        mcp, "open", {"path": "notes/guide.md", "heading": "Setup", "project": PROJECT}
    )
    via_rest = rest.get(
        f"/v1/{PROJECT}/doc",
        params={"path": "notes/guide.md", "heading": "Setup"},
        headers=AUTH,
    ).json()
    assert via_rest == via_mcp
    assert "Install uv." in via_rest["text"]


def test_open_line_span_is_the_same_slice_through_both_transports(vault: Path) -> None:
    """REST splits `lines` into two query params; the slice must still be identical."""
    service = build_service(vault)
    rest, mcp = build_both(service)
    via_mcp = call_tool(
        mcp, "open", {"path": "notes/guide.md", "lines": [1, 4], "project": PROJECT}
    )
    via_rest = rest.get(
        f"/v1/{PROJECT}/doc",
        params={"path": "notes/guide.md", "line_start": 1, "line_end": 4},
        headers=AUTH,
    ).json()
    assert via_rest == via_mcp


def test_mcp_related_and_rest_related_return_the_same_payload(vault: Path) -> None:
    service = build_service(vault, graph_store=graph_with_one_neighbour())
    rest, mcp = build_both(service)
    via_mcp = call_tool(mcp, "related", {"path": "notes/guide.md", "project": PROJECT})
    via_rest = rest.get(
        f"/v1/{PROJECT}/related", params={"path": "notes/guide.md"}, headers=AUTH
    ).json()
    assert via_rest == via_mcp
    assert via_rest["notes"], "the stub graph must return a neighbour, or parity is vacuous"


def test_mcp_overview_and_rest_overview_return_the_same_payload(vault: Path) -> None:
    service = build_service(vault, meta_store=_StubOverviewStore())
    rest, mcp = build_both(service)
    via_mcp = call_tool(mcp, "overview", {"project": PROJECT})
    via_rest = rest.get(f"/v1/{PROJECT}/overview", headers=AUTH).json()
    assert via_rest == via_mcp
    assert via_rest["notes"] == 2


def test_mcp_list_projects_and_rest_list_projects_return_the_same_payload(vault: Path) -> None:
    service = build_service(vault, meta_store=_StubOverviewStore())
    rest, mcp = build_both(service)
    via_mcp = call_tool(mcp, "list_projects", {})
    via_rest = rest.get("/v1/projects", headers=AUTH).json()
    # The MCP tool returns a bare list; FastMCP wraps it in a `result` key for structured
    # content, which is the transport's envelope, not a payload difference.
    assert via_rest == via_mcp["result"]


def test_one_query_writes_one_query_log_row_whichever_transport_asked(vault: Path) -> None:
    """P3 Stage 3's placement, verified from the other side.

    The query log records in the *core pipeline* precisely so a new transport inherits the
    row. This is that promise cashed: the same query over REST and over MCP produces two
    rows that differ only in the fields that genuinely are per-call.
    """
    from vaultrag.retrieval import QueryLogParams

    rows: list[dict[str, Any]] = []

    class _Sink:
        def log_query(self, **kwargs: Any) -> None:
            rows.append(kwargs)

    service = VaultRAGService(
        projects=[
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(FakeEmbedder()),
                query_log=QueryLogParams(),
            )
        ],
        vector_store=FakeVectorStore(hits=[make_hit()]),
        sparse=FakeSparse(),
        query_log_sink=_Sink(),  # type: ignore[arg-type]
    )
    rest, mcp = build_both(service)

    call_tool(mcp, "search", {"query": "same question", "project": PROJECT})
    rest.post(f"/v1/{PROJECT}/search", json={"query": "same question"}, headers=AUTH)

    assert len(rows) == 2
    volatile = {"latency_ms"}
    assert {k: v for k, v in rows[0].items() if k not in volatile} == {
        k: v for k, v in rows[1].items() if k not in volatile
    }


# ---------------------------------------------------------------------------
# The endpoint table answers (reference-rest-api.md)
# ---------------------------------------------------------------------------
def test_every_endpoint_in_the_reference_table_is_routed(vault: Path) -> None:
    """A 404 from a missing route and a 404 from a missing note are indistinguishable to a
    client, so the reference table is checked against the *router*, not against responses.

    The plan allows exactly one deliberately-deferred endpoint (answering 501); this test
    is what keeps "deferred" from silently becoming "forgotten".
    """
    service = build_service(vault, meta_store=_StubOverviewStore())
    app: FastAPI = create_app(service, api_key=KEY)
    # Read from the OpenAPI document rather than `app.routes`: that is the contract a
    # client actually sees, and it stays correct however FastAPI nests included routers
    # internally.
    paths = app.openapi()["paths"]
    routed = {(path, verb.upper()) for path, ops in paths.items() for verb in ops}
    expected = {
        ("/v1/{project}/search", "POST"),
        ("/v1/{project}/doc", "GET"),
        ("/v1/{project}/related", "GET"),
        ("/v1/{project}/overview", "GET"),
        ("/v1/{project}/notes", "POST"),
        ("/v1/{project}/notes", "PATCH"),
        ("/v1/{project}/reindex", "POST"),
        ("/v1/projects", "POST"),
        ("/v1/projects", "GET"),
        ("/healthz", "GET"),
        ("/metrics", "GET"),
    }
    assert expected <= routed


def test_register_project_answers_501_and_names_the_command_that_works(vault: Path) -> None:
    """The one deferred endpoint. 501 is an honest answer; a 404 would be a lie."""
    rest = client_for(build_service(vault))
    response = rest.post("/v1/projects", json={"id": "x"}, headers=AUTH)
    assert response.status_code == 501
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert "vaultrag project add" in response.json()["detail"]


def test_healthz_answers_without_a_key_and_leaks_nothing(vault: Path) -> None:
    """The one unauthenticated route, so it must say nothing a stranger could use."""
    rest = client_for(build_service(vault))
    response = rest.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "version"}
    assert PROJECT not in json.dumps(body)


# ---------------------------------------------------------------------------
# Auth (reference-security-model: the HTTP transport always requires a key)
# ---------------------------------------------------------------------------
UNAUTHENTICATED_ROUTES = [
    ("get", "/v1/projects"),
    ("post", f"/v1/{PROJECT}/search"),
    ("get", f"/v1/{PROJECT}/doc?path=notes/guide.md"),
    ("get", f"/v1/{PROJECT}/related?path=notes/guide.md"),
    ("get", f"/v1/{PROJECT}/overview"),
    ("post", f"/v1/{PROJECT}/notes"),
    ("patch", f"/v1/{PROJECT}/notes"),
    ("post", f"/v1/{PROJECT}/reindex"),
    ("post", "/v1/projects"),
    ("get", "/metrics"),
]


@pytest.mark.parametrize(("verb", "path"), UNAUTHENTICATED_ROUTES)
def test_every_route_but_healthz_refuses_an_unauthenticated_request(
    vault: Path, verb: str, path: str
) -> None:
    """The boring one that matters: no key, no answer — on every route, including the
    ones that would have refused anyway for another reason.

    Parameterized over the whole table rather than a sample, because "we forgot to
    authenticate the new endpoint" is precisely the mistake a sample misses.
    """
    rest = client_for(build_service(vault))
    response = getattr(rest, verb)(path)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_a_wrong_key_is_refused_and_never_echoed(vault: Path) -> None:
    rest = client_for(build_service(vault))
    response = rest.get("/v1/projects", headers={"Authorization": "Bearer wrong-key-here"})
    assert response.status_code == 401
    assert "wrong-key-here" not in response.text


def test_a_malformed_authorization_header_is_refused(vault: Path) -> None:
    rest = client_for(build_service(vault))
    assert rest.get("/v1/projects", headers={"Authorization": KEY}).status_code == 401
    assert rest.get("/v1/projects", headers={"Authorization": "Basic x"}).status_code == 401
    assert rest.get("/v1/projects", headers={"Authorization": "Bearer "}).status_code == 401


def test_an_absent_configured_key_is_minted_rather_than_left_open(vault: Path) -> None:
    """A missing VAULTRAG_API_KEY must never mean "no auth" — that is the one thing the
    security model forbids. It means an ephemeral key, announced."""
    app = create_app(build_service(vault), api_key=None)
    assert app.state.api_key
    assert app.state.key_was_minted is True
    rest = TestClient(app)
    assert rest.get("/v1/projects").status_code == 401
    assert (
        rest.get(
            "/v1/projects", headers={"Authorization": f"Bearer {app.state.api_key}"}
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Errors are problem+json, always
# ---------------------------------------------------------------------------
def test_an_unserved_project_is_refused_by_the_same_enforcement_point(vault: Path) -> None:
    """Tenancy: the path parameter selects within the served set and can never widen it."""
    rest = client_for(build_service(vault))
    response = rest.post("/v1/other-tenant/search", json={"query": "x"}, headers=AUTH)
    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "urn:vaultrag:problem:unknown-project"
    assert PROJECT in body["detail"]  # names what *is* served, as the MCP refusal does


def test_a_missing_note_is_404_problem_json(vault: Path) -> None:
    rest = client_for(build_service(vault))
    response = rest.get(f"/v1/{PROJECT}/doc", params={"path": "notes/nope.md"}, headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["type"] == "urn:vaultrag:problem:not-found"


def test_a_ragignored_note_is_403_not_404(vault: Path) -> None:
    """The gate refused it; it exists. Saying 404 would be a small lie that costs a user
    an afternoon wondering why their file "isn't there"."""
    rest = client_for(build_service(vault))
    response = rest.get(f"/v1/{PROJECT}/doc", params={"path": "drafts/hidden.md"}, headers=AUTH)
    assert response.status_code == 403
    assert ".ragignore" in response.json()["detail"]


def test_a_path_escaping_the_vault_is_refused(vault: Path) -> None:
    """`open`'s containment gate, over HTTP. The same check, from the same function."""
    rest = client_for(build_service(vault))
    response = rest.get(
        f"/v1/{PROJECT}/doc", params={"path": "../outside/external.md"}, headers=AUTH
    )
    assert response.status_code == 403
    assert "escapes the vault" in response.json()["detail"]


def test_contradictory_arguments_are_400_not_500(vault: Path) -> None:
    rest = client_for(build_service(vault))
    response = rest.get(
        f"/v1/{PROJECT}/doc",
        params={"path": "notes/guide.md", "heading": "Setup", "line_start": 1, "line_end": 2},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["type"] == "urn:vaultrag:problem:invalid-request"


def test_half_a_line_span_is_rejected_with_a_usable_message(vault: Path) -> None:
    rest = client_for(build_service(vault))
    response = rest.get(
        f"/v1/{PROJECT}/doc", params={"path": "notes/guide.md", "line_start": 2}, headers=AUTH
    )
    assert response.status_code == 400
    assert "line_end" in response.json()["detail"]


def test_a_schema_violation_is_422_problem_json_with_the_field(vault: Path) -> None:
    rest = client_for(build_service(vault))
    response = rest.post(f"/v1/{PROJECT}/search", json={"quiry": "typo"}, headers=AUTH)
    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "urn:vaultrag:problem:validation"
    assert body["errors"], "pydantic's per-field detail is kept, not summarized away"


def test_a_misspelled_filter_key_fails_loudly(vault: Path) -> None:
    """`extra="forbid"` on the shared filter model — a silently-ignored filter would return
    unfiltered results that look right."""
    rest = client_for(build_service(vault))
    response = rest.post(
        f"/v1/{PROJECT}/search", json={"query": "x", "filters": {"tag": ["howto"]}}, headers=AUTH
    )
    assert response.status_code == 422


def test_an_unknown_route_returns_problem_json_not_html(vault: Path) -> None:
    """A JSON API that answers HTML on 404 is the error that costs a client the most."""
    rest = client_for(build_service(vault))
    response = rest.get("/v1/nope/nothing", headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)


def test_a_down_backend_is_503_not_500(vault: Path) -> None:
    """A dependency being down is not the caller's fault and *is* worth retrying — the
    distinction the 4xx/5xx split exists to make."""
    from tests.fakes import DownVectorStore

    service = build_service(vault, vector_store=DownVectorStore())
    rest = client_for(service)
    response = rest.post(f"/v1/{PROJECT}/search", json={"query": "x"}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["type"] == "urn:vaultrag:problem:backend-unavailable"


def test_related_without_a_graph_refuses_rather_than_answering_empty(vault: Path) -> None:
    rest = client_for(build_service(vault))  # no graph_store
    response = rest.get(f"/v1/{PROJECT}/related", params={"path": "notes/guide.md"}, headers=AUTH)
    assert response.status_code == 403


def test_overview_without_a_metadata_store_refuses(vault: Path) -> None:
    rest = client_for(build_service(vault))  # no meta_store
    assert rest.get(f"/v1/{PROJECT}/overview", headers=AUTH).status_code == 403


# ---------------------------------------------------------------------------
# Write-back over HTTP
# ---------------------------------------------------------------------------
class _WriteFakeSparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[Any]:
        from vaultrag.index import SparseVector

        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> Any:
        from vaultrag.index import SparseVector

        return SparseVector(indices=[1], values=[1.0])


class _WriteFakeEmbedder:
    @property
    def model(self) -> str:
        return "fake-embed-model"

    @property
    def dims(self) -> int:
        return 4

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def writable(vault: Path) -> tuple[TestClient, VaultRAGService, SQLiteMetaStore]:
    """A service whose one project has a real writer over a real store + in-memory Qdrant."""
    store = SQLiteMetaStore(vault.parent / "state" / "vaultrag.db")
    store.upsert_project(PROJECT, name=PROJECT, vault_path=str(vault))
    vstore = QdrantVectorStore(
        dims=4, collection="chunks", client=QdrantClient(":memory:"), sparse=_WriteFakeSparse()
    )
    vstore.ensure_collection()
    ctx = IngestContext(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vstore,
        embedder=_WriteFakeEmbedder(),
    )
    writer = NoteWriter(ctx, vault_name="Vault", git_enabled=False)
    service = VaultRAGService(
        projects=[
            ServedProject(
                project_id=PROJECT,
                vault_root=vault,
                embedder=QueryEmbedder(_WriteFakeEmbedder()),
                writer=writer,
                ingest=ctx,
            )
        ],
        vector_store=vstore,
        sparse=_WriteFakeSparse(),
        meta_store=store,
    )
    return client_for(service), service, store


def test_creating_a_note_returns_201_and_the_hash_a_patch_will_need(
    writable: tuple[TestClient, VaultRAGService, SQLiteMetaStore],
) -> None:
    rest, _, _ = writable
    response = rest.post(
        f"/v1/{PROJECT}/notes",
        json={"title": "JWT refresh decision", "content": "We chose rotation.", "tags": ["auth"]},
        headers=AUTH,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["path"].endswith("jwt-refresh-decision.md")
    assert body["hash"]


def test_appending_a_section_needs_no_lock(
    writable: tuple[TestClient, VaultRAGService, SQLiteMetaStore],
) -> None:
    rest, _, _ = writable
    created = rest.post(
        f"/v1/{PROJECT}/notes",
        json={"title": "Running log", "content": "## Entries\n\nfirst\n"},
        headers=AUTH,
    ).json()
    response = rest.patch(
        f"/v1/{PROJECT}/notes",
        json={
            "op": "append_section",
            "path": created["path"],
            "heading": "Entries",
            "content": "second",
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["action"] == "appended"


def test_a_stale_base_hash_is_409_with_what_is_needed_to_rebase(
    writable: tuple[TestClient, VaultRAGService, SQLiteMetaStore],
) -> None:
    """The 409's whole job: one round-trip to recover, not two.

    This is the one error whose *body* is load-bearing — an RFC 7807 extension member
    carrying the live hash, so a client can merge and retry without re-reading the note.
    """
    rest, _, _ = writable
    created = rest.post(
        f"/v1/{PROJECT}/notes", json={"title": "Locked", "content": "body"}, headers=AUTH
    ).json()
    response = rest.patch(
        f"/v1/{PROJECT}/notes",
        json={
            "op": "patch_frontmatter",
            "path": created["path"],
            "updates": {"status": "accepted"},
            "base_hash": "0" * 16,
        },
        headers=AUTH,
    )
    assert response.status_code == 409
    body = response.json()
    assert body["type"] == "urn:vaultrag:problem:write-conflict"
    assert body["current_hash"] == created["hash"]
    assert "body" in body["current_text"]


def test_a_read_only_project_refuses_every_write_endpoint(vault: Path) -> None:
    rest = client_for(build_service(vault))  # no writer
    created = rest.post(f"/v1/{PROJECT}/notes", json={"title": "x", "content": "y"}, headers=AUTH)
    assert created.status_code == 403
    assert "read-only" in created.json()["detail"]
    patched = rest.patch(
        f"/v1/{PROJECT}/notes",
        json={"op": "append_section", "path": "notes/guide.md", "heading": "Setup", "content": "z"},
        headers=AUTH,
    )
    assert patched.status_code == 403


def test_a_write_naming_another_tenant_in_the_path_is_refused(
    writable: tuple[TestClient, VaultRAGService, SQLiteMetaStore],
) -> None:
    """There is no route by which a write reaches a project the server does not serve."""
    rest, _, _ = writable
    response = rest.post(
        "/v1/other-tenant/notes", json={"title": "x", "content": "y"}, headers=AUTH
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# reindex (admin)
# ---------------------------------------------------------------------------
def test_reindexing_one_path_answers_inline(
    writable: tuple[TestClient, VaultRAGService, SQLiteMetaStore],
) -> None:
    rest, _, _ = writable
    response = rest.post(f"/v1/{PROJECT}/reindex", json={"path": "notes/guide.md"}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "notes/guide.md"
    assert body["status"] == "indexed"
    assert body["chunks"] > 0


def test_reindexing_a_path_outside_the_vault_is_refused(
    writable: tuple[TestClient, VaultRAGService, SQLiteMetaStore],
) -> None:
    rest, _, _ = writable
    response = rest.post(
        f"/v1/{PROJECT}/reindex", json={"path": "../outside/external.md"}, headers=AUTH
    )
    assert response.status_code == 403


def test_a_full_reindex_is_accepted_as_a_background_job(
    writable: tuple[TestClient, VaultRAGService, SQLiteMetaStore],
) -> None:
    """202, not 200: minutes of embedding is not a request/response shape."""
    rest, service, _ = writable
    response = rest.post(f"/v1/{PROJECT}/reindex", json={}, headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {
        "project": PROJECT,
        "scope": "full",
        "force": False,
        "started": True,
    }
    _drain_reindex(service)


def test_reindex_without_an_indexer_refuses_and_names_the_alternative(vault: Path) -> None:
    """A read-only api role has no ingest context — the ordinary state when the indexer is
    a separate container."""
    rest = client_for(build_service(vault))
    response = rest.post(f"/v1/{PROJECT}/reindex", json={}, headers=AUTH)
    assert response.status_code == 403
    assert "vaultrag reindex" in response.json()["detail"]


def _drain_reindex(service: VaultRAGService) -> None:
    """Join the background reindex thread so a test never leaves one writing to a tmp db."""
    import time

    for _ in range(200):
        if not service.reindex_running(project=PROJECT):
            return
        time.sleep(0.05)
    raise AssertionError("background reindex did not finish")


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------
def test_metrics_serves_the_same_registry_as_the_standalone_exporter(vault: Path) -> None:
    """Two front doors, one set of numbers (P2 Decision 5). A series present on one and
    absent on the other would mean the mount built its own registry."""
    from prometheus_client import generate_latest

    from vaultrag.metrics import registry

    rest = client_for(build_service(vault))
    response = rest.get("/metrics", headers=AUTH)
    assert response.status_code == 200
    assert response.text == generate_latest(registry()).decode("utf-8")
    assert "vaultrag_searches_total" in response.text


def test_metrics_requires_the_key(vault: Path) -> None:
    """The standalone loopback exporter stays keyless for Prometheus; this one does not,
    because it is on the same surface as the vault content."""
    rest = client_for(build_service(vault))
    assert rest.get("/metrics").status_code == 401


# ---------------------------------------------------------------------------
# Multi-tenancy over HTTP
# ---------------------------------------------------------------------------
def test_the_path_parameter_selects_the_right_vault(tmp_path: Path) -> None:
    """The P3 Stage 2 failure mode, re-checked at the new transport: two projects with the
    *same* relative path must resolve to genuinely different files."""
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    for root, marker in ((alpha_root, "ALPHA"), (beta_root, "BETA")):
        root.mkdir()
        (root / "same.md").write_text(f"# {marker}\n", encoding="utf-8")

    service = VaultRAGService(
        projects=[
            ServedProject(
                project_id="alpha",
                vault_root=alpha_root,
                embedder=QueryEmbedder(FakeEmbedder()),
            ),
            ServedProject(
                project_id="beta", vault_root=beta_root, embedder=QueryEmbedder(FakeEmbedder())
            ),
        ],
        vector_store=FakeVectorStore(hits=[]),
        sparse=FakeSparse(),
    )
    rest = client_for(service)
    alpha = rest.get("/v1/alpha/doc", params={"path": "same.md"}, headers=AUTH).json()
    beta = rest.get("/v1/beta/doc", params={"path": "same.md"}, headers=AUTH).json()
    assert "ALPHA" in alpha["text"]
    assert "BETA" in beta["text"]
    assert alpha["text"] != beta["text"]


def test_a_filter_cannot_widen_scope_to_another_project(vault: Path) -> None:
    """The filter model has no `project` field, so there is no key to smuggle one in."""
    rest = client_for(build_service(vault))
    response = rest.post(
        f"/v1/{PROJECT}/search",
        json={"query": "x", "filters": {"project": "other-tenant"}},
        headers=AUTH,
    )
    assert response.status_code == 422
