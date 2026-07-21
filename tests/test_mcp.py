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
import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from qdrant_client import QdrantClient

from tests.fakes import (
    PROJECT,
    DownVectorStore,
    FakeEmbedder,
    FakeSparse,
    FakeVectorStore,
    make_hit,
)
from vaultrag.chunking.tokens import count_tokens
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index import QdrantVectorStore
from vaultrag.index.base import ScoredChunk, SearchFilters
from vaultrag.indexer.pipeline import IngestContext
from vaultrag.indexer.scan import ExclusionReason
from vaultrag.mcp import build_mcp_server
from vaultrag.mcp.server import _EXCLUSION_MESSAGES, SearchFiltersParam, _apply_cap
from vaultrag.models import DocumentMetadata, Link, LinkKind, document_id
from vaultrag.retrieval import obsidian_uri
from vaultrag.store.sqlite import SQLiteMetaStore
from vaultrag.writeback import NoteWriter

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
    # Markdown living inside dirs the walk always prunes: real files, real .md, and the
    # index has never heard of them — `open` must not be the way in.
    (root / ".trash").mkdir()
    (root / ".trash" / "deleted.md").write_text("# Deleted secret plans\n", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "README.md").write_text("# Vendored\n", encoding="utf-8")
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
    parent_swap_enabled: bool = False,
    vault_name: str | None = None,
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
        parent_swap_enabled=parent_swap_enabled,
        vault_name=vault_name,
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
    assert snippet["anchor"] == "design"
    assert snippet["text"].startswith("Dense catches")
    assert snippet["score"] == 1.0


def test_search_stamps_obsidian_uris_when_a_vault_name_is_configured(vault: Path) -> None:
    """Stage 8 wired through the server: a served snippet carries a one-click obsidian:// URI."""
    server, _, _, _ = build(vault, hits=[make_hit(path="a/b.md")], vault_name="My Vault")
    result = call(server, "search", {"query": "q"})
    assert result["snippets"][0]["uri"] == obsidian_uri("My Vault", "a/b.md")


def test_search_omits_uris_when_no_vault_name_is_configured(vault: Path) -> None:
    server, _, _, _ = build(vault, hits=[make_hit()])  # vault_name defaults to None
    result = call(server, "search", {"query": "q"})
    assert result["snippets"][0]["uri"] is None


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
    # Distinct docs/text: this asserts the budget, not stage 7's near-dupe/cap on twins.
    hits = [
        make_hit(point_id="a", tokens=20, text="alpha body", doc_id="doc-a"),
        make_hit(point_id="b", score=0.5, tokens=20, text="beta body", doc_id="doc-b"),
    ]
    server, store, _, _ = build(vault, hits=hits, default_k=3, default_budget_tokens=25)
    result = call(server, "search", {"query": "q"})
    assert store.calls[0]["limit"] == 3
    assert [s["chunk_id"] for s in result["snippets"]] == ["a"]
    assert result["truncated"] is True


def test_search_explicit_k_and_budget_override_defaults(vault: Path) -> None:
    hits = [
        make_hit(point_id="a", tokens=20, text="alpha body", doc_id="doc-a"),
        make_hit(point_id="b", score=0.5, tokens=20, text="beta body", doc_id="doc-b"),
    ]
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
    assert store.calls[0]["filters"] == SearchFilters(tags=("component/qdrant",), modified_after=5)


def test_search_rejects_unknown_filter_keys(vault: Path) -> None:
    """extra='forbid': a client cannot smuggle (or typo) a widening key like project_id."""
    server, store, _, _ = build(vault)
    call_expecting_error(server, "search", {"query": "q", "filters": {"project_id": "other"}})
    assert store.calls == []


def test_filter_param_exposes_every_narrowing_filter_and_no_widening_one() -> None:
    """Tripwire on the MCP-boundary twin of SearchFilters.

    The twin exists to hold model-facing descriptions and `extra="forbid"`, but a
    duplicated field list rots silently in both directions: a filter added to the engine
    would become unreachable over MCP, and a `project` field appearing here would hand
    clients the tenancy switch the whole scoping design withholds (CLAUDE.md §6).
    """
    engine = {f.name for f in dataclasses.fields(SearchFilters)}
    exposed = set(SearchFiltersParam.model_fields)
    assert exposed == engine
    assert "project" not in exposed and "project_id" not in exposed


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


def test_search_compact_mode_budgets_the_previews_not_the_full_texts(vault: Path) -> None:
    """Compact must budget what it *returns*. Hits whose full text blows the budget but
    whose one-liner fits are the entire point of the mode: budgeting the full texts
    first would drop them before compaction and hand back one preview for a k=10 browse.
    """
    hits = [
        # Same first line (the preview) so the token math below holds, but distinct bodies +
        # docs so stage 7 doesn't collapse these as near-dupes before compaction sees them.
        make_hit(
            point_id=f"p{i}",
            score=1.0 - i / 10,
            text=f"Short line.\n\nrest body number {i}",
            doc_id=f"doc-{i}",
            tokens=500,
        )
        for i in range(5)
    ]
    server, _, _, _ = build(vault, hits=hits, default_budget_tokens=50)
    result = call(server, "search", {"query": "q", "mode": "compact"})
    assert [s["chunk_id"] for s in result["snippets"]] == ["p0", "p1", "p2", "p3", "p4"]
    assert result["total_tokens"] == 5 * count_tokens("Short line.")
    assert result["truncated"] is False


def test_search_compact_mode_still_honors_the_budget(vault: Path) -> None:
    """...but the budget is real: enough previews still truncate.

    Previews are capped at _COMPACT_LINE_CHARS (160 → ~40 tokens each), so a 100-token
    budget fits two of them and drops the rest.
    """
    # Distinct trailing token per hit (past the 160-char preview cut) so the previews stay
    # identical for the budget math while the full texts aren't near-dupes stage 7 collapses.
    hits = [
        make_hit(point_id=f"p{i}", score=1.0 - i / 10, text="x" * 400 + f" {i}", doc_id=f"doc-{i}")
        for i in range(5)
    ]
    server, _, _, _ = build(vault, hits=hits, default_budget_tokens=100)
    result = call(server, "search", {"query": "q", "mode": "compact"})
    assert [s["chunk_id"] for s in result["snippets"]] == ["p0", "p1"]
    assert result["total_tokens"] <= 100
    assert result["truncated"] is True


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
# search — parent-swap (stage 5, P1 Stage 2)
# ---------------------------------------------------------------------------
def test_search_parent_swap_returns_the_reconstructed_section(vault: Path) -> None:
    """With parent-swap on, a child hit comes back as its whole enclosing section, read
    live from the vault through ``open``'s own gated reader."""
    hit = make_hit(
        path="notes/guide.md",
        text="Install uv.",  # a small child; the reconstructed Setup section is larger
        tokens=3,
        heading_path=["Guide", "Setup"],
        anchor="setup",
    )
    server, _, _, _ = build(vault, hits=[hit], parent_swap_enabled=True)
    snip = call(server, "search", {"query": "how do I set up"})["snippets"][0]
    assert snip["anchor"] == "setup"
    assert "Install uv." in snip["text"]
    assert "Nested content under Setup." in snip["text"]  # the ### Details subsection rolls up
    assert "Quick Start" not in snip["text"]  # stops at the next same-level heading
    assert snip["chunk_id"] is None  # a reconstructed section, not an indexed point


def test_search_parent_swap_falls_back_when_the_note_is_secret_gated(vault: Path) -> None:
    """Parent-swap reuses ``open``'s gates: a section in a secret-bearing note is refused,
    so the child is kept rather than reconstructed (the read-side twin of open withholding)."""
    hit = make_hit(
        path="leaky.md",
        text="the original child body",
        heading_path=["Oops"],
        anchor="oops",
    )
    server, _, _, _ = build(vault, hits=[hit], parent_swap_enabled=True)
    snip = call(server, "search", {"query": "q"})["snippets"][0]
    assert snip["text"] == "the original child body"  # fell back to the child
    assert snip["chunk_id"] == "point-1"


def test_search_compact_mode_skips_parent_swap(vault: Path) -> None:
    """Compact is browse-by-preview: it must not pay for section reconstruction, so the
    one-liner is the child's first line, not a swapped-in parent."""
    hit = make_hit(
        path="notes/guide.md",
        text="Install uv.\nRun docker.",
        heading_path=["Guide", "Setup"],
        anchor="setup",
    )
    server, _, _, _ = build(vault, hits=[hit], parent_swap_enabled=True)
    snip = call(server, "search", {"query": "q", "mode": "compact"})["snippets"][0]
    assert snip["text"] == "Install uv."  # the child preview, no swap


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


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"lines": [1, 5]},
        {"heading": "Setup"},
    ],
    ids=["whole", "lines", "heading"],
)
def test_open_reports_the_note_title_in_every_mode(vault: Path, args: dict[str, Any]) -> None:
    """`title` describes the note, not the slice — which slice was asked for is irrelevant."""
    result = call(build(vault)[0], "open", {"path": "notes/guide.md", **args})
    assert result["title"] == "Guide"


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


@pytest.mark.parametrize(
    "given", ["Quick Start", "quick start", "quick-start", "Quick_Start", "  quick start  "]
)
def test_open_heading_match_is_forgiving(vault: Path, given: str) -> None:
    """Agents pass anchors in every one of these forms; all normalize to one key.

    ``Quick_Start`` is the case a ``\\W+``-only fold gets wrong — ``_`` is a word
    character, so it would survive and never match the space in 'Quick Start'.
    """
    result = call(build(vault)[0], "open", {"path": "notes/guide.md", "heading": given})
    assert result["heading"] == "Quick Start"


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
    message = call_expecting_error(build(vault)[0], "open", {"path": "../outside/external.md"})
    assert "escapes the vault" in message


def test_open_blocks_absolute_paths(vault: Path) -> None:
    outside = (vault.parent / "outside" / "external.md").resolve()
    message = call_expecting_error(build(vault)[0], "open", {"path": str(outside)})
    assert "escapes the vault" in message


def test_open_blocks_symlink_escape(vault: Path) -> None:
    """A symlink inside the vault pointing outside resolves outside → refused."""
    message = call_expecting_error(build(vault)[0], "open", {"path": "escape.md"})
    assert "escapes the vault" in message


def test_open_refuses_a_pdf_and_says_where_its_text_lives(vault: Path) -> None:
    """A PDF is indexable but not readable as text — returning its decoded bytes would
    hand the agent mojibake. Refuse, and point at the route that does work."""
    (vault / "handbook.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer\n")

    message = call_expecting_error(build(vault)[0], "open", {"path": "handbook.pdf"})

    assert "PDF" in message
    assert "search" in message  # actionable, not just "no"


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


@pytest.mark.parametrize(
    "path", [".trash/deleted.md", "node_modules/pkg/README.md", ".obsidian/workspace.md"]
)
def test_open_blocks_always_pruned_dirs(vault: Path, path: str) -> None:
    """`open` refuses what the walk prunes — otherwise it is a hole around the scanner.

    These are markdown files that pass every *other* gate (inside the vault, not
    .ragignored, no secret in the name), so nothing but the prune rule stops them being
    served — and the index has never seen them, which is exactly why an agent must not
    reach them through the tool that reads the vault directly.
    """
    message = call_expecting_error(build(vault)[0], "open", {"path": path})
    assert "never scans" in message


def test_open_has_a_refusal_message_for_every_indexer_gate() -> None:
    """Tripwire: every gate `exclusion_reason` can return has a refusal message.

    A gate added to the scanner without a message here would KeyError in front of an
    agent instead of refusing cleanly, so the two must move together.
    """
    assert set(get_args(ExclusionReason)) == set(_EXCLUSION_MESSAGES)


# ---------------------------------------------------------------------------
# write tools (P1 Stage 5) — registered only when a NoteWriter is supplied
# ---------------------------------------------------------------------------
def build_with_writer(
    vault: Path, **writer_kwargs: object
) -> tuple[FastMCP, SQLiteMetaStore, QdrantVectorStore]:
    """A server with the real write tools wired to a real store + in-memory Qdrant."""
    store = SQLiteMetaStore(vault.parent / "state" / "vaultrag.db")
    store.upsert_project(PROJECT, name=PROJECT, vault_path=str(vault))
    vstore = QdrantVectorStore(
        dims=4, collection="chunks", client=QdrantClient(":memory:"), sparse=WriteFakeSparse()
    )
    vstore.ensure_collection()
    writer = NoteWriter(
        IngestContext(
            project_id=PROJECT,
            vault_path=vault,
            store=store,
            vector_store=vstore,
            embedder=WriteFakeEmbedder(),
        ),
        vault_name="Vault",
        git_enabled=False,  # keep the MCP boundary tests off subprocess git
        **writer_kwargs,  # type: ignore[arg-type]
    )
    server = build_mcp_server(
        project_id=PROJECT,
        vault_path=vault,
        vector_store=vstore,
        embedder=QueryEmbedder(WriteFakeEmbedder()),
        sparse=WriteFakeSparse(),
        writer=writer,
    )
    return server, store, vstore


class WriteFakeSparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[Any]:
        from vaultrag.index import SparseVector

        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> Any:
        from vaultrag.index import SparseVector

        return SparseVector(indices=[1], values=[1.0])


class WriteFakeEmbedder:
    @property
    def model(self) -> str:
        return "fake-embed-model"

    @property
    def dims(self) -> int:
        return 4

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(len(t) + i) for i in range(4)] for t in texts]

    def close(self) -> None:
        pass


def test_write_tools_are_absent_without_a_writer(vault: Path) -> None:
    """A read-only server (no writer) exposes only search + open — no write surface."""
    server, _, _, _ = build(vault)

    async def _run() -> set[str]:
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    assert asyncio.run(_run()) == {"search", "open"}


def test_write_tools_are_registered_with_a_writer(vault: Path) -> None:
    server, _store, _vs = build_with_writer(vault)

    async def _run() -> set[str]:
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    assert asyncio.run(_run()) == {
        "search",
        "open",
        "write_note",
        "append_section",
        "patch_frontmatter",
    }


def test_write_note_tool_round_trips_and_returns_a_result(vault: Path) -> None:
    server, store, _vs = build_with_writer(vault)
    result = call(
        server,
        "write_note",
        {"title": "MCP Memory", "content": "## What\n\nremember this\n", "tags": ["memo"]},
    )
    assert result["action"] == "created"
    assert result["path"] == "agent-inbox/mcp-memory.md"
    assert result["indexed"] is True and result["chunks"] >= 1
    assert result["uri"] == obsidian_uri("Vault", result["path"])
    # It actually landed in the vault + index.
    assert (vault / result["path"]).is_file()
    assert store.get_document(PROJECT, result["path"]) is not None


def test_write_note_refusal_surfaces_as_a_tool_error(vault: Path) -> None:
    server, _store, _vs = build_with_writer(vault)
    message = call_expecting_error(
        server, "write_note", {"title": "Elsewhere", "content": "x\n", "folder": "decisions"}
    )
    assert "write allowlist" in message


def test_patch_frontmatter_conflict_surfaces_current_hash(vault: Path) -> None:
    server, _store, _vs = build_with_writer(vault)
    created = call(server, "write_note", {"title": "Lockme", "content": "## B\n\nbody\n"})
    message = call_expecting_error(
        server,
        "patch_frontmatter",
        {"path": created["path"], "updates": {"status": "x"}, "base_hash": "stale"},
    )
    assert created["hash"] in message  # 409 echoes the live hash to rebase on
    assert "body" in message  # …and the current content


# ---------------------------------------------------------------------------
# related (P2 Stage 3) — the MCP contract over the link graph
# ---------------------------------------------------------------------------
def build_with_graph(
    vault: Path,
    *,
    hits: Sequence[ScoredChunk] = (),
    vault_name: str | None = None,
) -> tuple[FastMCP, SQLiteMetaStore]:
    """A server wired to a *real* SQLite graph holding a two-note linked vault.

    Uses the real store rather than a fake so this exercises the same
    ``GraphStore`` implementation the CLI composes — the MCP layer's job here is the
    contract (path normalization, the not-indexed refusal, registration), and a fake store
    would let the two drift apart unnoticed.
    """
    store = SQLiteMetaStore(vault / ".state.db")
    store.upsert_project(PROJECT, name=PROJECT, vault_path=str(vault))
    guide = store.upsert_document(
        PROJECT,
        DocumentMetadata(path="notes/guide.md", title="Guide", hash="blake3:g", tags=["topic"]),
    )
    other = store.upsert_document(
        PROJECT,
        DocumentMetadata(path="notes/other.md", title="Other", hash="blake3:o", tags=["topic"]),
    )
    store.replace_links(
        PROJECT,
        other,
        [
            Link(kind=LinkKind.wikilink, target="guide"),
            Link(kind=LinkKind.wikilink, target="Ghost"),
        ],
    )
    store.reresolve_links(PROJECT, other)
    assert guide  # the graph is keyed on it
    server = build_mcp_server(
        project_id=PROJECT,
        vault_path=vault,
        vector_store=FakeVectorStore(hits=hits),
        embedder=QueryEmbedder(FakeEmbedder()),
        sparse=FakeSparse(),
        graph_store=store,
        vault_name=vault_name,
    )
    return server, store


def test_related_returns_the_neighbourhood_with_working_paths(vault: Path) -> None:
    """The plan's acceptance criterion: neighbours + tag-siblings, as usable paths."""
    server, store = build_with_graph(vault)
    try:
        result = call(server, "related", {"path": "notes/guide.md"})
        assert result["path"] == "notes/guide.md"
        assert result["title"] == "Guide"
        by_path = {n["path"]: n for n in result["notes"]}
        assert by_path["notes/other.md"]["relation"] == "backlink"
        assert result["truncated"] is False
    finally:
        store.close()


def test_related_returns_pointers_not_text(vault: Path) -> None:
    """Titles + paths only — the whole reason the tool is cheap enough to call freely."""
    server, store = build_with_graph(vault)
    try:
        note = call(server, "related", {"path": "notes/guide.md"})["notes"][0]
        assert set(note) == {"path", "title", "relation", "hops", "shared_tags", "uri"}
    finally:
        store.close()


def test_related_reports_dangling_links_for_the_note_asked_about(vault: Path) -> None:
    server, store = build_with_graph(vault)
    try:
        result = call(server, "related", {"path": "notes/other.md"})
        assert result["unresolved"] == ["Ghost"]
    finally:
        store.close()


def test_related_stamps_obsidian_uris(vault: Path) -> None:
    server, store = build_with_graph(vault, vault_name="My Vault")
    try:
        note = call(server, "related", {"path": "notes/guide.md"})["notes"][0]
        assert note["uri"] == obsidian_uri("My Vault", note["path"])
    finally:
        store.close()


def test_related_refuses_a_path_that_is_not_indexed(vault: Path) -> None:
    """An unindexed path is a mistake worth naming, not an empty neighbourhood."""
    server, store = build_with_graph(vault)
    try:
        message = call_expecting_error(server, "related", {"path": "notes/nope.md"})
        assert "not indexed" in message
    finally:
        store.close()


def test_related_refuses_a_path_that_escapes_the_vault(vault: Path) -> None:
    """Containment is enforced before any store query, exactly as `open` enforces it."""
    server, store = build_with_graph(vault)
    try:
        message = call_expecting_error(server, "related", {"path": "../outside/external.md"})
        assert "escapes the vault" in message
    finally:
        store.close()


def test_related_is_not_registered_without_a_graph_store(vault: Path) -> None:
    """No graph, no tool: a server must not advertise a capability it cannot serve."""
    server, _, _, _ = build(vault)

    async def _run() -> list[str]:
        async with Client(server) as client:
            return [t.name for t in await client.list_tools()]

    assert "related" not in asyncio.run(_run())


def test_related_is_registered_when_a_graph_store_is_given(vault: Path) -> None:
    server, store = build_with_graph(vault)
    try:

        async def _run() -> list[str]:
            async with Client(server) as client:
                return [t.name for t in await client.list_tools()]

        assert set(asyncio.run(_run())) == {"search", "open", "related"}
    finally:
        store.close()


def test_search_appends_graph_neighbours_at_no_token_cost(vault: Path) -> None:
    """Stage 6's append reaches the wire, and `budget_tokens` does not pay for it."""
    hit = make_hit(path="notes/guide.md", doc_id=document_id(PROJECT, "notes/guide.md"), tokens=7)
    server, store = build_with_graph(vault, hits=[hit])
    try:
        result = call(server, "search", {"query": "guide"})
        assert [n["path"] for n in result["related"]] == ["notes/other.md"]
        assert result["total_tokens"] == 7  # the snippet only — pointers are free
    finally:
        store.close()


def test_search_returns_no_neighbours_without_a_graph_store(vault: Path) -> None:
    server, _, _, _ = build(vault, hits=[make_hit()])
    assert call(server, "search", {"query": "q"})["related"] == []


def test_compact_mode_still_carries_graph_neighbours(vault: Path) -> None:
    """Pointers survive the compact re-pack — they were never in the budget to begin with."""
    hit = make_hit(path="notes/guide.md", doc_id=document_id(PROJECT, "notes/guide.md"))
    server, store = build_with_graph(vault, hits=[hit])
    try:
        result = call(server, "search", {"query": "guide", "mode": "compact"})
        assert [n["path"] for n in result["related"]] == ["notes/other.md"]
    finally:
        store.close()
