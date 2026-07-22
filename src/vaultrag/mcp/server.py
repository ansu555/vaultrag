"""FastMCP ``search`` + ``open`` — the P0 finish line (Step 10), now a thin transport.

Since P3 Stage 4 this module holds **no** retrieval, tenancy or safety logic: every
operation lives once in :mod:`vaultrag.service`, and what remains here is the two things
that are genuinely MCP's own — the **tool schemas**, written for models, and the
translation of a :class:`~vaultrag.service.ServiceError` into a :class:`ToolError`. The
REST surface (``vaultrag.api``) is the same shape over the same service, which is what
makes "one query, both transports, identical payload" a structural property rather than a
coincidence (``../Rag/decisions/mcp-first-not-rest-first.md``, principle #3).

:func:`build_mcp_server` assembles a :class:`fastmcp.FastMCP` server around
already-constructed dependencies (``vaultrag.composition`` is the composition root that
builds them from :class:`~vaultrag.config.Settings`, and the CLI's ``vaultrag mcp``
command calls ``run()`` over stdio). Keeping construction out of this module makes every
tool testable through FastMCP's in-memory client with fakes, no stdio or live services
required.

Tool surface (``../Rag/reference/reference-mcp-tools.md``, arch §16):

* ``search(query, project?, k?, filters?, budget_tokens?, mode?)`` — returns cited
  snippets, never synthesized answers
  (``../Rag/decisions/snippets-not-answers-by-default.md``); ``mode="compact"`` returns
  one-line previews for cheap browsing. ``mode="answer"`` is opt-in synthesis and lands
  at P3 Stage 7 if it lands at all, so the schema does not advertise it.
* ``open(path, project?, heading?, lines?)`` — reads an exact slice of one note **straight
  from the vault** (the source of truth — always at least as fresh as the index the
  citation came from), not from Qdrant.
* ``related(path, project?, hops?, limit?)`` — the note's graph neighbourhood (links,
  backlinks, tag-siblings) as paths + titles, from P2 Stage 2's ``links`` table. Registered
  only when the service holds a graph. Text-free by design: it is how an agent explores
  outward from a hit without spending context.
* ``list_projects()`` / ``overview(project?)`` — discovery and orientation (P3 Stage 2).
  ``overview`` returns a project's *shape* — a depth-2 counted folder map and its top tags —
  never a file listing, and is bounded by constants rather than by vault size
  (``../Rag/agent-inbox/overview-tool-shape.md``).

Tool descriptions are written *for models* — when to call, arg semantics, budget
guidance — treated as seriously as retrieval quality. That is the reason this module is
still 700 lines after the logic moved out: the prose *is* the deliverable here.

Tools are deliberately **sync** functions: FastMCP runs them in a worker thread, which
is the "keep blocking I/O off the event loop" pattern CLAUDE.md §5 prescribes (verified
against fastmcp 3.4 — a sync tool executes off the event-loop thread).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from vaultrag.index.base import SparseEmbedder, VectorStore
from vaultrag.logging import get_logger
from vaultrag.models import (
    OpenResult,
    OverviewResult,
    ProjectSummary,
    RelatedResult,
    SearchResult,
    WriteResult,
)
from vaultrag.retrieval import GraphStore, QueryLogSink, Reranker
from vaultrag.service import (
    Conflict,
    OverviewStore,
    SearchFiltersParam,
    ServedProject,
    ServiceError,
    VaultRAGService,
)

log = get_logger(__name__)

# Re-exported for the transport's own callers (``vaultrag.mcp`` and the tests that hand
# this module fakes): both describe the *service*, and live there.
__all__ = [
    "OverviewStore",
    "SearchFiltersParam",
    "ServedProject",
    "build_mcp_server",
    "mcp_server_for",
]

# How much of a conflicting note's current content the 409 error echoes back, so the agent
# can rebase without a second round-trip while a huge note can't detonate its context.
_CONFLICT_PREVIEW_CHARS = 4_000


@contextmanager
def _as_tool_errors() -> Iterator[None]:
    """Render a :class:`~vaultrag.service.ServiceError` as MCP's own failure type.

    This is the whole of the transport's error responsibility. The service decides *that*
    a call is refused and writes the sentence the agent reads; MCP has exactly one failure
    channel, so every kind collapses to ``ToolError`` here — while HTTP, which has status
    codes, maps the same hierarchy onto them in ``api/errors.py``. A conflict is the one
    case with structure worth rendering, and it is rendered for a reader.
    """
    try:
        yield
    except Conflict as exc:
        raise ToolError(_conflict_message(exc)) from exc
    except ServiceError as exc:
        raise ToolError(str(exc)) from exc


def mcp_server_for(service: VaultRAGService) -> FastMCP:
    """Wrap an already-built service in the MCP tool surface.

    Which tools get registered follows the service's capabilities rather than this
    module's own reading of the constructor arguments — ``related`` needs a link graph,
    ``overview`` needs document/tag aggregates, the write tools need at least one writable
    project. No capability, no tool, rather than a tool that answers zero for everything.
    """
    if service.is_solo:
        scope = f"the {service.projects[0].project_id!r} Obsidian vault"
        multi = ""
    else:
        scope = f"{len(service.projects)} Obsidian vaults ({service.served_ids})"
        multi = (
            " This server holds several projects, so every tool takes a `project` "
            "argument — call `list_projects` first if you do not know which one you want."
        )
    mcp = FastMCP(
        name="vaultrag",
        instructions=(
            f"Cited-snippet retrieval over {scope}. Before "
            "designing, debugging, or answering questions about this project, call "
            "`search` first with one focused query — prefer that over reading many "
            "files. Follow a citation with `open` (its `path`, plus its `anchor` as "
            "`heading`) when the snippet alone is not enough context, or with `related` "
            f"(its `path`) to see what the vault links to it.{multi}"
        ),
    )

    @mcp.tool
    def search(
        query: Annotated[
            str,
            Field(
                description="Natural-language question OR exact identifier/error string. "
                "Search is hybrid (BM25 + dense), so verbatim tokens like "
                "'ERR_CONN_RESET' or 'docker-compose.prod.yml' rank as well as "
                "paraphrases. One focused query beats several vague ones."
            ),
        ],
        project: Annotated[
            str | None,
            Field(
                description="Which project/vault to search. Optional when this server "
                "serves exactly one (then it may only name that one); required when it "
                "serves several — call list_projects to see them. Never widens scope: a "
                "project this server does not serve is refused."
            ),
        ] = None,
        k: Annotated[
            int | None,
            Field(
                ge=1,
                le=100,
                description="Max snippets to return (server default applies when omitted).",
            ),
        ] = None,
        filters: Annotated[
            SearchFiltersParam | None,
            Field(
                description="Narrow by tags / paths / type / modified-time range. "
                "Filters only ever narrow results within this project."
            ),
        ] = None,
        budget_tokens: Annotated[
            int | None,
            Field(
                ge=1,
                description="Cap on total response tokens (server default applies when "
                "omitted). Lower it when your context is tight; the top hit is always "
                "kept even if it alone exceeds the budget.",
            ),
        ] = None,
        mode: Annotated[
            Literal["snippets", "compact"],
            Field(
                description="'snippets' (default) returns full cited chunk text — you "
                "are the LLM meant to read it. 'compact' returns one preview line per "
                "hit for cheap browsing; follow up with open() or a snippets-mode "
                "search. (LLM-synthesized 'answer' mode is opt-in and not served in P0.)"
            ),
        ] = "snippets",
    ) -> SearchResult:
        """Search the project's notes; returns ranked snippets cited by path + heading.

        Call this FIRST when you need project knowledge — decisions, designs, fixes,
        error strings. Every snippet carries `path`, `heading_path`, and `anchor`;
        follow up with open(path, heading=anchor) for the full section. `truncated`
        means lower-ranked hits existed but were dropped for `budget_tokens`.
        """
        with _as_tool_errors():
            return service.search(
                query,
                project=project,
                k=k,
                filters=filters.to_filters() if filters is not None else None,
                budget_tokens=budget_tokens,
                mode=mode,
            )

    @mcp.tool(name="open")
    def open_note(
        path: Annotated[
            str,
            Field(
                description="Vault-relative note path exactly as a search citation "
                "gave it, e.g. 'projects/vaultrag/chunking-strategy.md'."
            ),
        ],
        project: Annotated[
            str | None,
            Field(
                description="Which project's vault the path is in. Optional when this "
                "server serves one project; required when it serves several — pass the "
                "same project you searched, or the path will not resolve."
            ),
        ] = None,
        heading: Annotated[
            str | None,
            Field(
                description="Return just this heading's section (the heading line "
                "through the line before the next same-or-higher-level heading). "
                "Matching is forgiving: case-insensitive, 'quick-start' matches "
                "'Quick Start'. Pass a snippet's `anchor` here."
            ),
        ] = None,
        lines: Annotated[
            list[int] | None,
            Field(
                description="[start, end] line span, 1-based inclusive — for paging "
                "a long note (the response's total_lines tells you how far it goes). "
                "Mutually exclusive with `heading`."
            ),
        ] = None,
    ) -> OpenResult:
        """Read an exact slice of one note straight from the vault (source of truth).

        Use it to follow a search citation when the snippet isn't enough: pass the
        snippet's `path` and its `anchor` as `heading` to get that full section, or
        `lines=[start, end]` to page. Omit both for the whole note — very large notes
        come back with `truncated: true`; narrow with `heading`/`lines` then.
        """
        with _as_tool_errors():
            return service.open_note(path, project=project, heading=heading, lines=lines)

    _register_project_tools(mcp, service)
    # Registered when the process holds a link graph *and* at least one served project uses
    # it; a call naming a graph-disabled project is refused inside the tool, the same
    # per-project shape the write tools use.
    if service.has_graph:
        _register_related_tool(mcp, service)
    # Registered when *any* served project is writable; a call naming a read-only one is
    # refused per-project. The alternative — hiding the tools whenever a single tenant is
    # read-only — would take the memory loop away from the projects that do have it.
    if service.has_writes:
        _register_write_tools(mcp, service)

    return mcp


def build_mcp_server(
    *,
    projects: Sequence[ServedProject],
    vector_store: VectorStore,
    sparse: SparseEmbedder,
    reranker: Reranker | None = None,
    graph_store: GraphStore | None = None,
    meta_store: OverviewStore | None = None,
    query_log_sink: QueryLogSink | None = None,
) -> FastMCP:
    """Build the service from raw dependencies, then wrap it in the MCP tool surface.

    Kept as the dependency-shaped entry point because that is what a test wants to hand
    fakes to. Production goes through ``vaultrag.composition``, which builds one service
    and gives it to *both* transports — the arrangement that makes a REST/MCP divergence
    impossible rather than merely tested for. See :class:`~vaultrag.service.VaultRAGService`
    for what is per-project and what is shared.
    """
    return mcp_server_for(
        VaultRAGService(
            projects=projects,
            vector_store=vector_store,
            sparse=sparse,
            reranker=reranker,
            graph_store=graph_store,
            meta_store=meta_store,
            query_log_sink=query_log_sink,
        )
    )


# ---------------------------------------------------------------------------
# Discovery + orientation (P3 Stage 2).
# ---------------------------------------------------------------------------
def _register_project_tools(mcp: FastMCP, service: VaultRAGService) -> None:
    """Attach ``list_projects`` (always) and ``overview`` (when the service has one).

    ``list_projects`` is registered even on a single-project server: it is how an agent
    learns the ``project`` string to pass, and answering "just this one" is a real answer
    that costs a few tokens. ``overview`` follows ``related``'s rule — no capability, no
    tool.
    """

    @mcp.tool
    def list_projects() -> list[ProjectSummary]:
        """List the projects (vaults) this server can search — start here if unsure.

        Every other tool's `project` argument must name one of these. On a single-project
        server the argument is optional and this returns that one project. `notes` is the
        indexed document count (null if this server keeps no metadata store); `writable`
        says whether write_note/append_section/patch_frontmatter accept that project.
        """
        with _as_tool_errors():
            return service.list_projects()

    if not service.has_overview:
        return

    @mcp.tool
    def overview(
        project: Annotated[
            str | None,
            Field(
                description="Which project to describe. Optional when this server serves "
                "exactly one; required when it serves several."
            ),
        ] = None,
    ) -> OverviewResult:
        """Orient yourself in a vault: how many notes, which folders, which tags.

        Call this once at the start of a session on an unfamiliar project, instead of
        listing files. It returns the vault's *shape* — folders (2 levels deep) and tags,
        each with a note count — not a file listing, so it stays small however large the
        vault is. Use what it returns as `search` filters: a folder path narrows by
        `filters.paths`, a tag by `filters.tags`. To see actual content, use `search`.
        """
        with _as_tool_errors():
            return service.overview(project=project)


# ---------------------------------------------------------------------------
# related (P2 Stage 3) — registered only when the service holds a link graph.
# ---------------------------------------------------------------------------
def _register_related_tool(mcp: FastMCP, service: VaultRAGService) -> None:
    """Attach ``related`` — graph exploration over P2 Stage 2's ``links`` table."""

    @mcp.tool
    def related(
        path: Annotated[
            str,
            Field(
                description="Vault-relative note path exactly as a search citation gave "
                "it, e.g. 'projects/vaultrag/chunking-strategy.md'."
            ),
        ],
        project: Annotated[
            str | None,
            Field(
                description="Which project's graph to explore. Optional when this server "
                "serves one project; required when it serves several."
            ),
        ] = None,
        hops: Annotated[
            int,
            Field(
                ge=1,
                le=2,
                description="Link distance to explore. 1 (default) = direct links and "
                "backlinks. 2 also returns notes one further hop out — broader, noisier, "
                "and usually only worth it when 1 hop came back thin.",
            ),
        ] = 1,
        limit: Annotated[
            int,
            Field(ge=1, le=200, description="Max related notes to return."),
        ] = 50,
    ) -> RelatedResult:
        """Explore the note graph around one note: links, backlinks, and tag-siblings.

        Call this after a search hit to find what the vault itself connects to that note —
        cheaper and more precise than another search, because it follows the author's own
        wikilinks rather than guessing at similarity. Returns paths + titles only (no text,
        no token budget); follow anything interesting with open() or search(). Each entry's
        `relation` says why it is there: `links_to` (the note points at it), `backlink` (it
        points at the note), `neighbor` (2 hops away), `tag_sibling` (no link, shared tags —
        the weakest signal, so only the few strongest are returned). `unresolved` lists
        wikilinks in the note that point at nothing indexed — a broken link, or a note not
        written yet.
        """
        with _as_tool_errors():
            return service.related(path, project=project, hops=hops, limit=limit)


# ---------------------------------------------------------------------------
# Write tools (P1 Stage 5) — registered only when some project is writable.
# ---------------------------------------------------------------------------
def _register_write_tools(mcp: FastMCP, service: VaultRAGService) -> None:
    """Attach ``write_note`` / ``append_section`` / ``patch_frontmatter`` to ``mcp``.

    Each tool is a thin boundary over the service's write operations: a
    :class:`~vaultrag.service.Refused` becomes a plain ``ToolError`` (the message is
    written for the agent) and a :class:`~vaultrag.service.Conflict` becomes a 409-style
    error that echoes the current hash + content so the agent can rebase and retry — no
    second read needed (``../Rag/projects/vaultrag/write-back-design.md``).
    """

    @mcp.tool
    def write_note(
        title: Annotated[
            str,
            Field(
                description="Human-readable note title. Becomes the H1/frontmatter title and "
                "the slugified filename (e.g. 'JWT refresh decision' → 'jwt-refresh-decision.md'). "
                "VaultRAG adds provenance frontmatter (author, via: vaultrag, created)."
            ),
        ],
        content: Annotated[
            str,
            Field(
                description="The note body in Markdown (no frontmatter — VaultRAG writes that). "
                "For agent memory, record what/why/files-touched so a future search retrieves it."
            ),
        ],
        folder: Annotated[
            str | None,
            Field(
                description="Vault-relative folder to create the note in. Defaults to the "
                "write root (agent-inbox/). Must be within the server's write allowlist."
            ),
        ] = None,
        tags: Annotated[
            list[str] | None,
            Field(description="Optional tags written to frontmatter (e.g. ['decision', 'auth'])."),
        ] = None,
        project: Annotated[
            str | None,
            Field(
                description="Which project's vault to write into. Optional when this "
                "server serves one project; required when it serves several."
            ),
        ] = None,
    ) -> WriteResult:
        """Create a new note in the vault (defaults to agent-inbox/), committed + indexed.

        Use this to persist a decision/fix/finding so future sessions can `search` it — the
        line that turns this index into long-term memory. Refuses to overwrite an existing
        note: pick a different title, or use append_section/patch_frontmatter. The result's
        `hash` is the `base_hash` for a later patch; `uri` opens the note in Obsidian.
        """
        with _as_tool_errors():
            return service.write_note(
                title=title, content=content, folder=folder, tags=tags, project=project
            )

    @mcp.tool
    def append_section(
        path: Annotated[
            str,
            Field(
                description="Vault-relative path of the existing note to edit, exactly as a "
                "search citation gave it (e.g. 'agent-inbox/auth-notes.md')."
            ),
        ],
        heading: Annotated[
            str,
            Field(
                description="Heading whose section to append to (matched leniently, like open's "
                "`heading`: 'quick-start' matches 'Quick Start'). The block is inserted at the "
                "end of that section, before the next same-or-higher heading."
            ),
        ],
        content: Annotated[
            str,
            Field(description="Markdown block to append to the section."),
        ],
        project: Annotated[
            str | None,
            Field(
                description="Which project's vault the note is in. Optional when this "
                "server serves one project; required when it serves several."
            ),
        ] = None,
    ) -> WriteResult:
        """Append a Markdown block to a heading's section in an existing note, then reindex.

        Additive: it inserts at the section's end without rewriting the rest, so it needs no
        base_hash lock. Use it to grow a running note (a log, a checklist). Fails if the
        heading isn't found (the error lists the note's real headings).
        """
        with _as_tool_errors():
            return service.append_section(
                path=path, heading=heading, content=content, project=project
            )

    @mcp.tool
    def patch_frontmatter(
        path: Annotated[
            str,
            Field(
                description="Vault-relative path of the existing note whose frontmatter to edit."
            ),
        ],
        updates: Annotated[
            dict[str, Any],
            Field(
                description="Frontmatter keys to set/overwrite, e.g. {'status': 'accepted', "
                "'tags': ['decision']}. Body text is left untouched."
            ),
        ],
        base_hash: Annotated[
            str,
            Field(
                description="The note's current content hash (the `hash` a prior search/open/"
                "write returned). Guards against clobbering a concurrent edit: if it no longer "
                "matches, the call fails and returns the current hash + content to rebase on."
            ),
        ],
        project: Annotated[
            str | None,
            Field(
                description="Which project's vault the note is in. Optional when this "
                "server serves one project; required when it serves several."
            ),
        ] = None,
    ) -> WriteResult:
        """Set frontmatter keys on an existing note under an optimistic lock, then reindex.

        Pass the `base_hash` you last saw. On a mismatch (someone else edited the note) the
        call fails with the current hash and content — re-read, merge, and retry with the new
        base_hash. This is the only write tool that requires the lock (it mutates in place).
        """
        with _as_tool_errors():
            return service.patch_frontmatter(
                path=path, updates=updates, base_hash=base_hash, project=project
            )


def _conflict_message(exc: Conflict) -> str:
    """The 409 text: name the live hash and echo (bounded) current content to rebase on."""
    preview = exc.current_text[:_CONFLICT_PREVIEW_CHARS]
    elided = (
        ""
        if len(exc.current_text) <= _CONFLICT_PREVIEW_CHARS
        else "\n… (truncated; open the note for the rest)"
    )
    return (
        f"{exc.path!r} changed since base_hash was taken — no silent overwrite. Its current "
        f"hash is {exc.current_hash}. Re-read, merge your change, and retry with "
        f"base_hash={exc.current_hash}. Current content:\n---\n{preview}{elided}"
    )
