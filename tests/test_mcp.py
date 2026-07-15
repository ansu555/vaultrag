"""Step 10 verify: the FastMCP ``search``/``open`` tools in ``mcp/server.py``.

Every test drives the *real* MCP layer — FastMCP's in-memory client against the server
:func:`~vaultrag.mcp.build_mcp_server` returns — so schema validation, tool dispatch,
and error conversion are all exercised, not just the underlying functions. The
retrieval dependencies are the same fakes ``test_retrieval.py`` uses (orchestration is
Step 9's tested job; this layer's job is the MCP contract: tenancy refusal, defaults
flowing from settings, filter parsing, compact mode, and ``open``'s vault gates).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from vaultrag.chunking.tokens import count_tokens
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index.base import ScoredChunk, SearchFilters, SparseVector, VectorStoreError
from vaultrag.mcp import build_mcp_server
from vaultrag.mcp.server import _apply_cap
from vaultrag.models import Chunk, Document

PROJECT = "vaultrag-design"


# ---------------------------------------------------------------------------
# Test doubles (mirrors test_retrieval.py)
# ---------------------------------------------------------------------------
class FakeEmbedder:
    def __init__(self, *, dims: int = 4) -> None:
        self._dims = dims
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeSparse:
    def __init__(self) -> None:
        self.queries: list[str] = []

    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[], values=[]) for _ in texts]

    def embed_query(self, text: str) -> SparseVector:
        self.queries.append(text)
        return SparseVector(indices=[1], values=[1.0])


class FakeVectorStore:
    def __init__(self, hits: Sequence[ScoredChunk] = ()) -> None:
        self._hits = list(hits)
        self.calls: list[dict[str, object]] = []

    def ensure_collection(self) -> None:
        raise NotImplementedError

    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        vectors: Mapping[str, list[float]],
        *,
        embed_model: str,
        document: Document | None = None,
    ) -> int:
        raise NotImplementedError

    def delete_by_doc(self, project_id: str, doc_id: str) -> None:
        raise NotImplementedError

    def query(
        self,
        *,
        project_id: str,
        dense: list[float],
        sparse: SparseVector,
        limit: int,
        prefetch_limit: int = 100,
        filters: SearchFilters | None = None,
    ) -> list[ScoredChunk]:
        self.calls.append(
            {
                "project_id": project_id,
                "limit": limit,
                "prefetch_limit": prefetch_limit,
                "filters": filters,
            }
        )
        return self._hits

    def count(self, project_id: str | None = None) -> int:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DownVectorStore(FakeVectorStore):
    """Simulates Qdrant being unreachable at query time."""

    def query(self, **kwargs: object) -> list[ScoredChunk]:  # type: ignore[override]
        raise VectorStoreError("cannot reach Qdrant (is it running? `docker compose up -d qdrant`)")


def make_hit(
    *,
    point_id: str = "point-1",
    score: float = 1.0,
    text: str = "Dense catches paraphrase; BM25 catches exact tokens.",
    tokens: int = 10,
) -> ScoredChunk:
    return ScoredChunk(
        point_id=point_id,
        score=score,
        payload={
            "path": "projects/vaultrag/hybrid-search.md",
            "title": "Hybrid Search Details",
            "heading_path": ["Design"],
            "anchor": "Design",
            "text": text,
            "tokens": tokens,
            "doc_id": "doc-1",
        },
    )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
GUIDE = """\
---
title: Guide
---

# Guide

Intro paragraph.

## Setup

Install uv.
Run docker.

### Details

Nested content under Setup.

## Quick Start

Call search first.

```text
## Fake Heading
```
"""


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "drafts").mkdir()
    (root / "notes" / "guide.md").write_text(GUIDE, encoding="utf-8")
    (root / "drafts" / "hidden.md").write_text("# WIP\n", encoding="utf-8")
    (root / ".ragignore").write_text("drafts/\n", encoding="utf-8")
    (root / "aws-credentials.md").write_text("# Just a name test\n", encoding="utf-8")
    (root / "leaky.md").write_text(
        "# Oops\n\nkey is AKIAABCDEFGHIJKLMNOP somewhere\n", encoding="utf-8"
    )
    (root / "data.canvas").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.md").write_text("# Not yours\n", encoding="utf-8")
    (root / "escape.md").symlink_to(outside / "external.md")
    return root


def build(
    vault: Path,
    *,
    hits: Sequence[ScoredChunk] = (),
    store: FakeVectorStore | None = None,
    embedder: FakeEmbedder | None = None,
    sparse: FakeSparse | None = None,
    default_k: int = 10,
    default_budget_tokens: int = 2000,
) -> tuple[FastMCP, FakeVectorStore, FakeEmbedder, FakeSparse]:
    store = store if store is not None else FakeVectorStore(hits=hits)
    embedder = embedder or FakeEmbedder()
    sparse = sparse or FakeSparse()
    server = build_mcp_server(
        project_id=PROJECT,
        vault_path=vault,
        vector_store=store,
        embedder=QueryEmbedder(embedder),
        sparse=sparse,
        default_k=default_k,
        default_budget_tokens=default_budget_tokens,
    )
    return server, store, embedder, sparse


def call(server: FastMCP, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Round-trip one tool call through FastMCP's in-memory client."""

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            assert result.structured_content is not None
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
# search — MCP contract
# ---------------------------------------------------------------------------
def test_search_returns_cited_snippets(vault: Path) -> None:
    server, _, _, _ = build(vault, hits=[make_hit()])
    result = call(server, "search", {"query": "hybrid search"})
    assert result["query"] == "hybrid search"
    assert result["project_id"] == PROJECT
    snippet = result["snippets"][0]
    assert snippet["path"] == "projects/vaultrag/hybrid-search.md"
    assert snippet["anchor"] == "Design"
    assert snippet["text"].startswith("Dense catches")
    assert snippet["score"] == 1.0


def test_search_lists_both_tools(vault: Path) -> None:
    server, _, _, _ = build(vault)

    async def _run() -> list[str]:
        async with Client(server) as client:
            return [t.name for t in await client.list_tools()]

    names = asyncio.run(_run())
    assert set(names) == {"search", "open"}


def test_search_rejects_a_foreign_project(vault: Path) -> None:
    """Tenancy: the served scope is fixed at construction; a client can never widen it."""
    server, store, _, _ = build(vault, hits=[make_hit()])
    message = call_expecting_error(server, "search", {"query": "q", "project": "other-project"})
    assert "scoped to project" in message
    assert store.calls == []  # refused before any engine work


def test_search_accepts_naming_the_served_project(vault: Path) -> None:
    server, store, _, _ = build(vault, hits=[make_hit()])
    result = call(server, "search", {"query": "q", "project": PROJECT})
    assert len(result["snippets"]) == 1
    assert store.calls[0]["project_id"] == PROJECT


def test_search_defaults_flow_from_server_settings(vault: Path) -> None:
    """k/budget defaults are the composition root's (env-driven), not schema constants."""
    hits = [make_hit(point_id="a", tokens=20), make_hit(point_id="b", score=0.5, tokens=20)]
    server, store, _, _ = build(vault, hits=hits, default_k=3, default_budget_tokens=25)
    result = call(server, "search", {"query": "q"})
    assert store.calls[0]["limit"] == 3
    assert [s["chunk_id"] for s in result["snippets"]] == ["a"]
    assert result["truncated"] is True


def test_search_explicit_k_and_budget_override_defaults(vault: Path) -> None:
    hits = [make_hit(point_id="a", tokens=20), make_hit(point_id="b", score=0.5, tokens=20)]
    server, store, _, _ = build(vault, hits=hits, default_k=3, default_budget_tokens=25)
    result = call(server, "search", {"query": "q", "k": 7, "budget_tokens": 100})
    assert store.calls[0]["limit"] == 7
    assert len(result["snippets"]) == 2
    assert result["truncated"] is False


def test_search_parses_filters_into_search_filters(vault: Path) -> None:
    server, store, _, _ = build(vault)
    call(
        server,
        "search",
        {"query": "q", "filters": {"tags": ["component/qdrant"], "modified_after": 5}},
    )
    assert store.calls[0]["filters"] == SearchFilters(
        tags=("component/qdrant",), modified_after=5
    )


def test_search_rejects_unknown_filter_keys(vault: Path) -> None:
    """extra='forbid': a client cannot smuggle (or typo) a widening key like project_id."""
    server, store, _, _ = build(vault)
    call_expecting_error(server, "search", {"query": "q", "filters": {"project_id": "other"}})
    assert store.calls == []


def test_search_rejects_out_of_range_k(vault: Path) -> None:
    server, store, _, _ = build(vault)
    call_expecting_error(server, "search", {"query": "q", "k": 0})
    assert store.calls == []


def test_search_compact_mode_returns_one_line_previews(vault: Path) -> None:
    long_text = "First line of the chunk body.\n\nSecond paragraph that compact drops."
    server, _, _, _ = build(vault, hits=[make_hit(text=long_text, tokens=500)])
    result = call(server, "search", {"query": "q", "mode": "compact"})
    snippet = result["snippets"][0]
    assert snippet["text"] == "First line of the chunk body."
    assert snippet["tokens"] == count_tokens("First line of the chunk body.")
    assert result["total_tokens"] == snippet["tokens"]


def test_search_rejects_unknown_mode(vault: Path) -> None:
    server, _, _, _ = build(vault)
    call_expecting_error(server, "search", {"query": "q", "mode": "answer"})


def test_search_empty_query_short_circuits(vault: Path) -> None:
    server, store, embedder, sparse = build(vault, hits=[make_hit()])
    result = call(server, "search", {"query": "   "})
    assert result["snippets"] == []
    assert embedder.calls == 0
    assert sparse.queries == []
    assert store.calls == []


def test_search_surfaces_engine_failure_as_actionable_tool_error(vault: Path) -> None:
    server, _, _, _ = build(vault, store=DownVectorStore())
    message = call_expecting_error(server, "search", {"query": "q"})
    assert "search failed" in message
    assert "docker compose up -d qdrant" in message


# ---------------------------------------------------------------------------
# open — slicing
# ---------------------------------------------------------------------------
def test_open_whole_note(vault: Path) -> None:
    server, _, _, _ = build(vault)
    result = call(server, "open", {"path": "notes/guide.md"})
    expected = "\n".join(GUIDE.splitlines())
    assert result["text"] == expected
    assert result["line_start"] == 1
    assert result["line_end"] == result["total_lines"] == len(GUIDE.splitlines())
    assert result["truncated"] is False


def test_open_heading_returns_exactly_that_section(vault: Path) -> None:
    server, _, _, _ = build(vault)
    result = call(server, "open", {"path": "notes/guide.md", "heading": "Setup"})
    lines = GUIDE.splitlines()
    start = lines.index("## Setup") + 1  # 1-based
    end = lines.index("## Quick Start")  # line before the next same-level heading
    assert result["line_start"] == start
    assert result["line_end"] == end
    assert result["heading"] == "Setup"
    assert result["title"] == "Guide"
    assert result["text"].startswith("## Setup")
    assert "Install uv." in result["text"]
    assert "Nested content under Setup." in result["text"]  # lower-level child included
    assert "Call search first." not in result["text"]  # next sibling excluded


def test_open_heading_match_is_case_insensitive_and_sluggy(vault: Path) -> None:
    server, _, _, _ = build(vault)
    assert call(server, "open", {"path": "notes/guide.md", "heading": "setup"})[
        "heading"
    ] == "Setup"
    assert call(server, "open", {"path": "notes/guide.md", "heading": "quick-start"})[
        "heading"
    ] == "Quick Start"


def test_open_heading_inside_code_fence_is_not_a_heading(vault: Path) -> None:
    server, _, _, _ = build(vault)
    message = call_expecting_error(
        server, "open", {"path": "notes/guide.md", "heading": "Fake Heading"}
    )
    assert "not found" in message
    assert "Setup" in message  # the error lists the real headings for self-correction


def test_open_lines_span(vault: Path) -> None:
    server, _, _, _ = build(vault)
    result = call(server, "open", {"path": "notes/guide.md", "lines": [5, 7]})
    lines = GUIDE.splitlines()
    assert result["text"] == "\n".join(lines[4:7])
    assert result["line_start"] == 5
    assert result["line_end"] == 7


def test_open_lines_end_clamps_to_eof(vault: Path) -> None:
    server, _, _, _ = build(vault)
    result = call(server, "open", {"path": "notes/guide.md", "lines": [5, 9999]})
    assert result["line_end"] == result["total_lines"]
    assert result["truncated"] is False


def test_open_lines_validation(vault: Path) -> None:
    server, _, _, _ = build(vault)
    assert "pair" in call_expecting_error(server, "open", {"path": "notes/guide.md", "lines": [5]})
    assert "1 <= start <= end" in call_expecting_error(
        server, "open", {"path": "notes/guide.md", "lines": [0, 3]}
    )
    assert "only" in call_expecting_error(
        server, "open", {"path": "notes/guide.md", "lines": [10_000, 10_001]}
    )


def test_open_heading_and_lines_are_mutually_exclusive(vault: Path) -> None:
    server, _, _, _ = build(vault)
    message = call_expecting_error(
        server, "open", {"path": "notes/guide.md", "heading": "Setup", "lines": [1, 2]}
    )
    assert "not both" in message


def test_open_missing_note(vault: Path) -> None:
    server, _, _, _ = build(vault)
    assert "not found" in call_expecting_error(server, "open", {"path": "notes/nope.md"})


def test_open_truncates_a_huge_note_and_reports_paging_info(vault: Path) -> None:
    big = "\n".join(f"line {i} " + "x" * 40 for i in range(1, 3001))  # ~140 KB >> cap
    (vault / "big.md").write_text(big, encoding="utf-8")
    server, _, _, _ = build(vault)
    result = call(server, "open", {"path": "big.md"})
    assert result["truncated"] is True
    assert result["total_lines"] == 3000
    assert result["line_end"] < 3000
    assert result["tokens"] <= 8_000


def test_apply_cap_always_keeps_the_first_line() -> None:
    kept, truncated = _apply_cap(["y" * 100_000, "tail"], cap_tokens=10)
    assert kept == ["y" * 100_000]
    assert truncated is True


# ---------------------------------------------------------------------------
# open — the vault gates (CLAUDE.md §6: open must refuse what indexing refuses)
# ---------------------------------------------------------------------------
def test_open_blocks_path_traversal(vault: Path) -> None:
    message = call_expecting_error(
        build(vault)[0], "open", {"path": "../outside/external.md"}
    )
    assert "escapes the vault" in message


def test_open_blocks_absolute_paths(vault: Path) -> None:
    outside = (vault.parent / "outside" / "external.md").resolve()
    message = call_expecting_error(build(vault)[0], "open", {"path": str(outside)})
    assert "escapes the vault" in message


def test_open_blocks_symlink_escape(vault: Path) -> None:
    """A symlink inside the vault pointing outside resolves outside → refused."""
    message = call_expecting_error(build(vault)[0], "open", {"path": "escape.md"})
    assert "escapes the vault" in message


def test_open_blocks_secret_filenames(vault: Path) -> None:
    message = call_expecting_error(build(vault)[0], "open", {"path": "aws-credentials.md"})
    assert "secret guard" in message
    assert "name test" not in message  # never echoes content


def test_open_withholds_secret_bearing_content(vault: Path) -> None:
    message = call_expecting_error(build(vault)[0], "open", {"path": "leaky.md"})
    assert "withheld" in message
    assert "AKIAABCDEFGHIJKLMNOP" not in message  # names the rule, never the secret


def test_open_blocks_ragignored_paths(vault: Path) -> None:
    message = call_expecting_error(build(vault)[0], "open", {"path": "drafts/hidden.md"})
    assert ".ragignore" in message


def test_open_blocks_non_markdown(vault: Path) -> None:
    message = call_expecting_error(build(vault)[0], "open", {"path": "data.canvas"})
    assert "markdown" in message
