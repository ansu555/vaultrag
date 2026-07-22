"""FastMCP ``search`` + ``open`` — the P0 finish line (Step 10).

:func:`build_mcp_server` assembles a :class:`fastmcp.FastMCP` server around
already-constructed dependencies (the CLI's ``vaultrag mcp`` command is the composition
root that builds them from :class:`~vaultrag.config.Settings` and calls ``run()`` over
stdio). Keeping construction out of this module makes every tool testable through
FastMCP's in-memory client with fakes, no stdio or live services required.

Tool surface (``../Rag/reference/reference-mcp-tools.md``, arch §16):

* ``search(query, project?, k?, filters?, budget_tokens?, mode?)`` — wraps Step 9's
  :func:`vaultrag.retrieval.search`. Returns cited snippets, never synthesized answers
  (``../Rag/decisions/snippets-not-answers-by-default.md``); ``mode="compact"`` returns
  one-line previews for cheap browsing. ``mode="answer"`` is opt-in synthesis and lands
  at P3 per the roadmap (CLAUDE.md §4), so P0 does not advertise it in the schema.
* ``open(path, project?, heading?, lines?)`` — reads an exact slice of one note **straight
  from the vault** (the source of truth — always at least as fresh as the index the
  citation came from), not from Qdrant.
* ``related(path, project?, hops?, limit?)`` — the note's graph neighbourhood (links,
  backlinks, tag-siblings) as paths + titles, from P2 Stage 2's ``links`` table. Registered
  only when a graph store is supplied. Text-free by design: it is how an agent explores
  outward from a hit without spending context (``retrieval/graph.py``).
* ``list_projects()`` / ``overview(project?)`` — discovery and orientation (P3 Stage 2).
  ``overview`` returns a project's *shape* — a depth-2 counted folder map and its top tags —
  never a file listing, and is bounded by constants rather than by vault size
  (``../Rag/agent-inbox/overview-tool-shape.md``).

Tool descriptions are written *for models* — when to call, arg semantics, budget
guidance — treated as seriously as retrieval quality
(``../Rag/decisions/mcp-first-not-rest-first.md``).

Security posture:

* **Tenancy** — the server answers for a fixed set of :class:`ServedProject` bundles fixed
  at construction (P3 Stage 2: one process serves N — ``../Rag/projects/vaultrag/
  multi-tenant-architecture.md``). The per-call ``project`` argument *selects* among them
  and can never name one outside the set, so a client narrows and never widens
  (CLAUDE.md §6). Everything tenant-specific — the vault root ``open`` reads, the writer
  that accepts edits, the retrieval knobs — is carried on the selected bundle rather than
  captured once from a process-wide flag, which is what keeps two projects in one process
  from resolving each other's files.
* **``open`` enforces the same gates as indexing** — vault containment (a resolved
  path, including through symlinks, must stay under the vault root), the built-in
  secret guard (filename + content), and ``.ragignore``. PDFs are indexable but not
  text, so ``open`` refuses them and points at ``search``'s page-cited chunks instead;
  bare ``.excalidraw`` scenes are refused for the same reason, and an Excalidraw
  *wrapper* is served with its compressed payload trimmed off (P2 Stage 5). Without
  these, ``open`` would hand an agent exactly the files the scan boundary exists to
  withhold — or tens of kilobytes of base64 it can do nothing with.
* Retrieved vault text is returned as data, never executed or interpreted (§6).

Tools are deliberately **sync** functions: FastMCP runs them in a worker thread, which
is the "keep blocking I/O off the event loop" pattern CLAUDE.md §5 prescribes (verified
against fastmcp 3.4 — a sync tool executes off the event-loop thread).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from vaultrag.chunking.tokens import CHARS_PER_TOKEN, count_tokens
from vaultrag.embedding.base import EmbeddingError
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index.base import SearchFilters, SparseEmbedder, VectorStore, VectorStoreError
from vaultrag.indexer.scan import (
    ExclusionReason,
    exclusion_reason,
    is_binary_source,
    is_image_source,
    load_ignore_spec,
)
from vaultrag.indexer.secret_guard import scan_for_secrets
from vaultrag.logging import get_logger
from vaultrag.models import (
    BlockType,
    FolderCount,
    OpenResult,
    OverviewResult,
    ProjectSummary,
    RelatedResult,
    SearchResult,
    TagCount,
    WriteResult,
)
from vaultrag.parsing import heading_section, parse_document
from vaultrag.parsing.excalidraw import (
    is_excalidraw_source,
    is_scene_file,
    readable_text,
    scene_title,
)
from vaultrag.retrieval import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_K,
    DEFAULT_PARENT_MAX_TOKENS,
    CompressParams,
    GraphExpandParams,
    GraphStore,
    QueryLogParams,
    QueryLogSink,
    Reranker,
    RerankParams,
    VaultParentResolver,
    q_hash,
    related_notes,
)
from vaultrag.retrieval import search as retrieval_search
from vaultrag.retrieval.pipeline import pack_budget
from vaultrag.writeback import NoteWriter, WriteConflict, WriteRefused

log = get_logger(__name__)

# How much of a conflicting note's current content the 409 error echoes back, so the agent
# can rebase without a second round-trip while a huge note can't detonate its context.
_CONFLICT_PREVIEW_CHARS = 4_000

# Compact mode: one preview line per hit, hard-capped so a pathological first line
# can't defeat the point of the mode.
_COMPACT_LINE_CHARS = 160

# Compact mode retrieves with this stand-in budget so the *full-text* packer never
# drops hits an agent asked to browse; the caller's real budget is then applied to the
# compacted one-liners by `_compact` (same `pack_budget` rule), so `budget_tokens` and
# `truncated` mean the same thing in every mode.
_UNBOUNDED_BUDGET = 1_000_000_000

# Safety valve for `open` (not a budget feature): a whole-note request bigger than this
# is truncated with `truncated=True` + `total_lines`, so the agent can re-slice with
# heading=/lines= instead of receiving a context-detonating dump. Sized generously —
# "open the full section" is the tool's purpose and must not be starved (principle #7
# governs `search`; `open` is the explicit "give me the exact content" escape hatch).
_OPEN_CAP_TOKENS = 8_000

# Cap on how many headings a "heading not found" error lists back — enough to pick from,
# bounded so a heading-dense note can't blow up the agent's context on an error path.
_HEADING_LIST_LIMIT = 30

# exclusion_reason rule → the model-facing refusal text. One message per gate the scan
# boundary enforces; `open` must refuse the same set or it is a bypass (CLAUDE.md §6).
# Keyed by ExclusionReason, which makes a *misspelled* rule a mypy error; a *missing* one
# is caught by test_open_has_a_refusal_message_for_every_indexer_gate (mypy does not
# require dict literals to be exhaustive over a Literal).
_EXCLUSION_MESSAGES: dict[ExclusionReason, str] = {
    "pruned-dir": "is under a directory the indexer never scans (.git/.obsidian/.trash/…)",
    "secret-filename": "is blocked by the built-in secret guard",
    "ragignore": "is excluded by .ragignore and cannot be opened",
    "unsupported": "is not an indexable file (markdown, Excalidraw, PDF, or image)",
}


# `overview`'s bounds. Depth 2 is the taxonomy of an Obsidian vault (this repo's own design
# vault folds to 7 entries at depth 2); depth 3 starts listing contents, which is what
# `search` is for. Both caps make the response flat in vault size — the reasoning and the
# measurement are in `../Rag/agent-inbox/overview-tool-shape.md`.
_OVERVIEW_FOLDER_DEPTH = 2
_OVERVIEW_FOLDER_LIMIT = 50
_OVERVIEW_TAG_LIMIT = 20


@runtime_checkable
class OverviewStore(Protocol):
    """The slice of the metadata store ``overview`` reads — three per-project aggregates.

    Structurally satisfied by :class:`~vaultrag.store.sqlite.SQLiteMetaStore`, and declared
    narrowly for the same reason :class:`~vaultrag.retrieval.graph.GraphStore` is: the tool
    depends on three read methods rather than the whole ``MetaStore`` write surface, and a
    test can hand it a stub.

    Note what is *not* here: any vector-store call. ``overview`` is a SQLite-only tool, so
    it still answers "what is actually in this project?" on a deployment whose Qdrant is
    down — which is one of the states in which that question is most worth asking.
    """

    def document_count(self, project_id: str) -> int: ...

    def folder_counts(
        self, project_id: str, depth: int = 2, limit: int = 50
    ) -> list[tuple[str, int]]: ...

    def top_tags(self, project_id: str, limit: int = 20) -> list[tuple[str, int]]: ...


@dataclass(frozen=True, slots=True)
class ServedProject:
    """Everything one tenant needs, bundled — the unit ``build_mcp_server`` serves N of.

    This is the whole of P3 Stage 2's "one process serves N projects": the per-call
    ``project`` argument selects a bundle, and every tenant-specific decision downstream
    reads it off the bundle instead of off a process-wide flag. The split between what lives
    here and what is passed to :func:`build_mcp_server` once is not cosmetic:

    * **Per project** — ``vault_root`` (what ``open`` may read), ``writer`` (what may be
      edited, under whose write root), ``vault_name`` (citation URIs), and the retrieval
      knobs, because :class:`~vaultrag.projects.ProjectRegistry` resolves a full
      :class:`~vaultrag.config.Settings` per project and those are the parts of it a served
      request can differ on. ``embedder`` too: per-project embedding models are
      ``../Rag/projects/vaultrag/multi-tenant-architecture.md``'s explicit design (at a
      shared width — see ``projects._RESERVED_SETTINGS``).
    * **Shared** — the vector store, the sparse embedder, the reranker and the metadata
      store, all of which are already ``project_id``-scoped on every call (Qdrant's
      ``_build_filter`` makes it an unconditional ``must``) and would only waste memory
      duplicated per tenant.

    ``vault_root`` is resolved on construction: containment checks compare against it, and a
    relative or symlinked root would quietly weaken every one of them.
    """

    project_id: str
    vault_root: Path
    embedder: QueryEmbedder
    name: str | None = None
    vault_name: str | None = None
    writer: NoteWriter | None = None
    default_k: int = DEFAULT_K
    default_budget_tokens: int = DEFAULT_BUDGET_TOKENS
    rerank_params: RerankParams | None = None
    parent_swap_enabled: bool = False
    parent_max_tokens: int = DEFAULT_PARENT_MAX_TOKENS
    graph_params: GraphExpandParams = field(default_factory=GraphExpandParams)
    graph_enabled: bool = True
    compress_params: CompressParams | None = None
    # P3 Stage 3. Per-project because whether a tenant's queries are recorded — and
    # whether their text is kept — is a tenant policy; the sink they are written to is
    # shared, like every other project_id-scoped store. ``None`` means this project's
    # searches are not logged, even on a process that logs its neighbours'.
    query_log: QueryLogParams | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vault_root", Path(self.vault_root).resolve())

    @property
    def display_name(self) -> str:
        return self.name or self.project_id


#: What every tool calls to turn a caller's ``project`` argument into the bundle it is
#: allowed to use. There is exactly one implementation, closed over the served set inside
#: :func:`build_mcp_server`, and the helpers below take it rather than the set — so no
#: second place can grow its own idea of which projects a caller may reach.
_Select = Callable[[str | None], ServedProject]


class SearchFiltersParam(BaseModel):
    """Client-suppliable narrowing filters for ``search`` — the MCP-boundary twin of
    :class:`~vaultrag.index.base.SearchFilters`.

    Deliberately has no ``project`` field: filters can only narrow within the project
    this server is scoped to, never widen it (CLAUDE.md §6). ``extra="forbid"`` so a
    misspelled filter key fails loudly instead of silently not filtering.
    """

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(
        default_factory=list,
        description="Keep only chunks tagged with ANY of these (nested tags are "
        "expanded, so 'project' also matches notes tagged 'project/vaultrag').",
    )
    paths: list[str] = Field(
        default_factory=list,
        description="Keep only chunks from ANY of these exact vault-relative note paths.",
    )
    type: str | None = Field(
        default=None,
        description="Exact document type, e.g. 'note' (P0 indexes only notes).",
    )
    modified_after: int | None = Field(
        default=None,
        description="Keep only notes modified at/after this epoch-seconds timestamp (inclusive).",
    )
    modified_before: int | None = Field(
        default=None,
        description="Keep only notes modified before this epoch-seconds timestamp (exclusive).",
    )

    def to_filters(self) -> SearchFilters:
        """Convert to the engine-agnostic value object the read path takes."""
        return SearchFilters(
            tags=tuple(self.tags),
            paths=tuple(self.paths),
            type=self.type,
            modified_after=self.modified_after,
            modified_before=self.modified_before,
        )


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
    """Assemble the VaultRAG MCP server around already-built dependencies.

    ``projects`` is the set of tenants this process answers for — one
    :class:`ServedProject` each, carrying that project's vault root, writer, embedder and
    retrieval knobs (the CLI builds them from
    :class:`~vaultrag.projects.ProjectRegistry`-resolved settings, so the served defaults
    are per-project and env-configurable; the tool schema advertises the *parameters*, the
    running server owns their defaults). With exactly one, omitting a call's ``project``
    argument means that one and the surface behaves exactly as it did before P3 Stage 2;
    with several, ``project`` becomes required rather than silently defaulting to whichever
    happened to be first.

    The remaining arguments are what every tenant shares, because each is already
    ``project_id``-scoped per call:

    ``reranker`` is the stage-4 cross-encoder; ``None`` runs the fusion-only P0 path. The
    CLI builds it from settings and degrades to a no-op reranker if the model can't be
    loaded, so the server always comes up.

    ``graph_store`` turns on retrieval stage 6 **and** registers the ``related`` tool — both
    read P2 Stage 2's ``links`` table (``retrieval/graph.py``). ``None`` (a deployment with no
    metadata store, or one indexed before Stage 2) simply omits both: search still works, it
    just carries no ``related`` pointers, and no tool advertises a graph that isn't there.

    ``meta_store`` registers ``overview`` on the same principle — no store, no tool, rather
    than a tool that answers zero for everything. It is the same SQLite store ``graph_store``
    usually is; the two are separate parameters because they are separate *capabilities*
    (``related`` needs a link graph, ``overview`` needs document/tag aggregates) and a
    deployment can have one without the other.

    ``query_log_sink`` is where P3 Stage 3's per-search rows are written — usually that same
    SQLite store again, a third parameter for the same "separate capability" reason. Note
    that the server does not write them: it hands the sink to
    :func:`~vaultrag.retrieval.search`, which records in the **core pipeline** so Stage 4's
    REST surface inherits the identical row instead of growing a second, divergent copy.
    A project whose ``query_log`` is ``None`` is not logged even when a sink exists.

    Per-project stages, for reference: ``parent_swap_enabled`` turns on retrieval stage 5,
    where a retrieved child is swapped for its enclosing section reconstructed live from the
    vault through ``open``'s *own* gated reader — so parent-swap can never surface a file
    ``open`` would refuse (CLAUDE.md §6); it runs in ``snippets`` mode only. ``compress_params``
    tunes stage 7 (``retrieval/compress.py``), and ``vault_name`` stamps ``obsidian://open``
    citation URIs (stage 8). A project's ``writer`` turns on the P1 Stage 5 write tools for
    *that* project: with one, an agent can record what it learns and retrieve it the same
    session (the memory loop); without, that project is served read-only.
    """
    if not projects:
        raise ValueError("build_mcp_server needs at least one ServedProject to serve")
    by_id: dict[str, ServedProject] = {}
    for served in projects:
        if served.project_id in by_id:
            raise ValueError(f"duplicate project in served set: {served.project_id!r}")
        by_id[served.project_id] = served
    # The lone project is the implicit default; with several there is no defensible default,
    # so `project` becomes mandatory. Guessing here would mean a forgotten argument silently
    # queries the wrong vault — the one failure mode multi-tenancy must not have.
    solo = projects[0] if len(projects) == 1 else None
    served_ids = ", ".join(sorted(by_id))

    def _select(project: str | None) -> ServedProject:
        """Resolve a call's ``project`` argument to the bundle it may use, or refuse.

        The single enforcement point for CLAUDE.md §6's "a client can narrow, never widen":
        every tool routes through it, and nothing downstream takes a project id from the
        caller again.
        """
        if project is None:
            if solo is not None:
                return solo
            raise ToolError(
                f"this server serves several projects — pass `project` (one of: {served_ids})"
            )
        found = by_id.get(project)
        if found is None:
            raise ToolError(
                f"project {project!r} is not served by this server — tenancy is enforced "
                f"server-side. Available: {served_ids}."
            )
        return found

    if solo is not None:
        scope = f"the {solo.project_id!r} Obsidian vault"
        multi = ""
    else:
        scope = f"{len(by_id)} Obsidian vaults ({served_ids})"
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
        sel = _select(project)
        t0 = time.perf_counter()
        effective_budget = budget_tokens if budget_tokens is not None else sel.default_budget_tokens
        # Fresh resolver per call: its parse cache dedupes reads *within* one search but must
        # not persist across calls, or an edited note would resolve stale (open re-reads too).
        # Skipped in compact mode — browsing by preview shouldn't pay for section reconstruction.
        # Bound to *this* project's vault root, so a multi-project server can never
        # reconstruct a parent section out of a neighbouring tenant's files.
        parent_resolver = (
            VaultParentResolver(read_note=lambda p: _read_note(sel.vault_root, p)[1])
            if sel.parent_swap_enabled and mode == "snippets"
            else None
        )
        try:
            result = retrieval_search(
                query,
                project_id=sel.project_id,
                vector_store=vector_store,
                embedder=sel.embedder,
                sparse=sparse,
                k=k if k is not None else sel.default_k,
                filters=filters.to_filters() if filters is not None else None,
                reranker=reranker,
                rerank=sel.rerank_params,
                parent_resolver=parent_resolver,
                parent_max_tokens=sel.parent_max_tokens,
                # Per-project: a tenant with graph expansion off gets the fusion-only path
                # even though the process holds a link graph for its neighbours.
                graph_store=graph_store if sel.graph_enabled else None,
                graph=sel.graph_params,
                compress_params=sel.compress_params,
                vault_name=sel.vault_name,
                # P3 Stage 3. The mode label is stamped per call; the storage policy came
                # from this project's settings at startup. In compact mode the row records
                # what *retrieval* produced — `_compact` re-packs the previews afterwards,
                # so `budget_used` is the full-text total, which is the honest number for
                # "what did this query cost to serve".
                # A project that opted out gets no sink, rather than a sink plus a null
                # policy — exactly how `graph_enabled` withholds the graph store above.
                # One thing decides whether a row is written, so there is no state where
                # "don't log" and "log with defaults" are told apart by a second argument.
                query_log_sink=query_log_sink if sel.query_log is not None else None,
                query_log=sel.query_log.with_mode(mode) if sel.query_log is not None else None,
                # Compact mode budgets the *previews*, not the full texts: packing full
                # texts first would silently drop hits the agent asked to browse (k),
                # so retrieval runs unbounded and _compact re-packs to the real budget.
                budget_tokens=_UNBOUNDED_BUDGET if mode == "compact" else effective_budget,
            )
        except (VectorStoreError, EmbeddingError) as exc:
            # ToolError text reaches the agent verbatim — keep the actionable message
            # ("is Qdrant running? docker compose up -d qdrant") instead of masking it.
            raise ToolError(f"search failed: {exc}") from exc
        if mode == "compact":
            result = _compact(result, budget_tokens=effective_budget)
        log.info(
            "mcp.search",  # q_hash, never the raw query, at info level (CLAUDE.md §5)
            project=sel.project_id,
            # The same hash the query log stores, from the same function — one definition,
            # so a row in `vaultrag queries` and a line in the log are joinable. The DB may
            # additionally hold the text (P3 Decision 2); this stream never does.
            q_hash=q_hash(query),
            mode=mode,
            hits=len(result.snippets),
            related=len(result.related),
            total_tokens=result.total_tokens,
            truncated=result.truncated,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return result

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
        sel = _select(project)
        if heading is not None and lines is not None:
            raise ToolError("pass `heading` or `lines`, not both")
        if heading is not None and not heading.strip():
            raise ToolError("`heading` must be non-empty when given")

        # Resolved against the *selected* project's root — the failure mode this whole
        # per-call plumbing exists to prevent is one project reading another's files
        # through a root captured once at construction (p3-step-plan Stage 2 verify).
        rel, text = _read_note(sel.vault_root, path)
        source_lines = text.splitlines()
        total = len(source_lines)
        # Parse once, for every mode: `heading` needs the block tree, and `title` is
        # frontmatter the caller wants regardless of which slice they asked for.
        document = parse_document(path=rel, raw_text=text)
        # A drawing's name comes from its filename/frontmatter, not from the plugin's
        # `# Excalidraw Data` scaffolding heading — keep `open` agreeing with `search`.
        title = (
            scene_title(rel, text) if is_excalidraw_source(rel, text) else (document.metadata.title)
        )

        resolved_heading: str | None = None
        if lines is not None:
            start, end = _validate_lines(lines, total, rel)
        elif heading is not None:
            span = heading_section(document, heading, total)
            if span is None:
                # Name the real headings so the agent can retry without a second probe;
                # whole headings only (a char-truncated one would just get re-sent mangled).
                names = [b.text for b in document.blocks if b.type is BlockType.heading]
                available = ", ".join(names[:_HEADING_LIST_LIMIT]) or "(none)"
                raise ToolError(
                    f"heading {heading!r} not found in {rel!r}; headings here: {available}"
                )
            start, end, block = span
            resolved_heading = block.text
        else:
            start, end = 1, total

        slice_lines = source_lines[start - 1 : end]
        kept, truncated = _apply_cap(slice_lines)
        body = "\n".join(kept)
        line_end = start + len(kept) - 1 if kept else start - 1
        result = OpenResult(
            path=rel,
            title=title,
            heading=resolved_heading,
            line_start=start,
            line_end=line_end,
            total_lines=total,
            text=body,
            tokens=count_tokens(body),
            truncated=truncated,
        )
        log.info(
            "mcp.open",
            project=sel.project_id,
            path=rel,
            line_start=result.line_start,
            line_end=result.line_end,
            tokens=result.tokens,
            truncated=result.truncated,
        )
        return result

    _register_project_tools(
        mcp, projects=list(by_id.values()), select=_select, meta_store=meta_store
    )
    # Registered when the process holds a link graph *and* at least one served project uses
    # it; a call naming a graph-disabled project is refused inside the tool, the same
    # per-project shape the write tools use.
    if graph_store is not None and any(p.graph_enabled for p in projects):
        _register_related_tool(mcp, select=_select, graph_store=graph_store)
    # Registered when *any* served project is writable; a call naming a read-only one is
    # refused per-project. The alternative — hiding the tools whenever a single tenant is
    # read-only — would take the memory loop away from the projects that do have it.
    if any(p.writer is not None for p in projects):
        _register_write_tools(mcp, select=_select)

    return mcp


# ---------------------------------------------------------------------------
# Discovery + orientation (P3 Stage 2).
# ---------------------------------------------------------------------------
def _register_project_tools(
    mcp: FastMCP,
    *,
    projects: Sequence[ServedProject],
    select: _Select,
    meta_store: OverviewStore | None,
) -> None:
    """Attach ``list_projects`` (always) and ``overview`` (with a metadata store).

    ``list_projects`` is registered even on a single-project server: it is how an agent
    learns the ``project`` string to pass, and answering "just this one" is a real answer
    that costs a few tokens. ``overview`` follows ``related``'s rule — no store, no tool.
    """

    @mcp.tool
    def list_projects() -> list[ProjectSummary]:
        """List the projects (vaults) this server can search — start here if unsure.

        Every other tool's `project` argument must name one of these. On a single-project
        server the argument is optional and this returns that one project. `notes` is the
        indexed document count (null if this server keeps no metadata store); `writable`
        says whether write_note/append_section/patch_frontmatter accept that project.
        """
        summaries = [
            ProjectSummary(
                id=p.project_id,
                name=p.display_name,
                notes=None if meta_store is None else meta_store.document_count(p.project_id),
                writable=p.writer is not None,
            )
            for p in projects
        ]
        log.info("mcp.list_projects", projects=len(summaries))
        return summaries

    if meta_store is None:
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
        sel = select(project)
        # One over each cap, so "a cap bit" is observed rather than inferred from equality
        # (a vault with exactly 50 folders is not truncated, and must not say it is).
        folders = meta_store.folder_counts(
            sel.project_id, depth=_OVERVIEW_FOLDER_DEPTH, limit=_OVERVIEW_FOLDER_LIMIT + 1
        )
        tags = meta_store.top_tags(sel.project_id, limit=_OVERVIEW_TAG_LIMIT + 1)
        truncated = len(folders) > _OVERVIEW_FOLDER_LIMIT or len(tags) > _OVERVIEW_TAG_LIMIT
        result = OverviewResult(
            project=sel.project_id,
            name=sel.display_name,
            notes=meta_store.document_count(sel.project_id),
            folders=[
                FolderCount(path=path, notes=n) for path, n in folders[:_OVERVIEW_FOLDER_LIMIT]
            ],
            tags=[TagCount(tag=tag, notes=n) for tag, n in tags[:_OVERVIEW_TAG_LIMIT]],
            truncated=truncated,
        )
        log.info(
            "mcp.overview",
            project=sel.project_id,
            notes=result.notes,
            folders=len(result.folders),
            tags=len(result.tags),
            truncated=truncated,
        )
        return result


# ---------------------------------------------------------------------------
# related (P2 Stage 3) — registered only when a graph store is supplied.
# ---------------------------------------------------------------------------
def _register_related_tool(
    mcp: FastMCP,
    *,
    select: _Select,
    graph_store: GraphStore,
) -> None:
    """Attach ``related`` — graph exploration over P2 Stage 2's ``links`` table.

    Returns pointers (path + title + why), never text: following one is what ``open`` and
    ``search`` are for, and keeping text out means an agent can survey a neighbourhood for
    almost nothing. Everything it names came out of the index, which already applied every
    scan gate (``.ragignore``, the secret guard, containment), so there is no path here by
    which ``related`` can surface a note ``open`` would refuse.
    """

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
        sel = select(project)
        if not sel.graph_enabled:
            raise ToolError(
                f"project {sel.project_id!r} has graph expansion disabled, so it exposes no "
                "link graph — use `search` instead"
            )
        rel = _vault_relative(sel.vault_root, path)
        document = graph_store.get_document(sel.project_id, rel)
        if document is None:
            raise ToolError(
                f"{rel!r} is not indexed in project {sel.project_id!r} — pass a path exactly "
                "as a search citation gave it (the graph only knows indexed notes)"
            )
        notes, unresolved, truncated = related_notes(
            project_id=sel.project_id,
            doc_id=document.id,
            store=graph_store,
            hops=hops,
            limit=limit,
            max_tag_siblings=sel.graph_params.max_tag_siblings,
            vault_name=sel.vault_name,
        )
        log.info(
            "mcp.related",
            project=sel.project_id,
            path=rel,
            hops=hops,
            notes=len(notes),
            unresolved=len(unresolved),
            truncated=truncated,
        )
        return RelatedResult(
            path=rel,
            title=document.title,
            doc_id=document.id,
            hops=hops,
            notes=notes,
            unresolved=unresolved,
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# Write tools (P1 Stage 5) — registered only when a NoteWriter is supplied.
# ---------------------------------------------------------------------------
def _register_write_tools(mcp: FastMCP, *, select: _Select) -> None:
    """Attach ``write_note`` / ``append_section`` / ``patch_frontmatter`` to ``mcp``.

    Each tool is a thin boundary over the selected project's
    :class:`~vaultrag.writeback.NoteWriter`: it translates a
    :class:`~vaultrag.writeback.WriteRefused` into a plain ``ToolError`` (the message is
    written for the agent) and a :class:`~vaultrag.writeback.WriteConflict` into a 409-style
    error that echoes the current hash + content so the agent can rebase and retry — no
    second read needed (``../Rag/projects/vaultrag/write-back-design.md``).

    Each writer owns its own vault, write root, git repo and rate limiter, so a
    multi-project write is N independent commits rather than a distributed transaction
    (one project = one vault dir = one git repo — [[multi-tenant-architecture]]).
    """

    def _writer_for(project: str | None) -> NoteWriter:
        sel = select(project)  # refuses an unserved project before anything else
        if sel.writer is None:
            raise ToolError(
                f"project {sel.project_id!r} is served read-only — its write tools are "
                "disabled (--no-write, or VAULTRAG_WRITEBACK_ENABLED=false)"
            )
        return sel.writer

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
        writer = _writer_for(project)
        try:
            return writer.write_note(title=title, content=content, folder=folder, tags=tags)
        except WriteConflict as exc:
            raise ToolError(_conflict_message(exc)) from exc
        except WriteRefused as exc:
            raise ToolError(str(exc)) from exc

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
        writer = _writer_for(project)
        try:
            return writer.append_section(path=path, heading=heading, content=content)
        except WriteConflict as exc:
            raise ToolError(_conflict_message(exc)) from exc
        except WriteRefused as exc:
            raise ToolError(str(exc)) from exc

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
        writer = _writer_for(project)
        try:
            return writer.patch_frontmatter(path=path, updates=updates, base_hash=base_hash)
        except WriteConflict as exc:
            raise ToolError(_conflict_message(exc)) from exc
        except WriteRefused as exc:
            raise ToolError(str(exc)) from exc


def _conflict_message(exc: WriteConflict) -> str:
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


# ---------------------------------------------------------------------------
# search helpers
# ---------------------------------------------------------------------------
def _compact(result: SearchResult, *, budget_tokens: int) -> SearchResult:
    """Squash each snippet to a one-line preview (arch §10: 'title + 1-liner').

    The caller's budget is applied *here*, to the previews, using search's own
    :func:`~vaultrag.retrieval.pipeline.pack_budget` — so ``budget_tokens`` and
    ``truncated`` keep identical meaning across modes. Retrieval itself ran unbounded
    (``_UNBOUNDED_BUDGET``): budgeting full texts first would drop hits whose *preview*
    would have fit fine, which is the opposite of what "compact" is for.
    """
    compacted = []
    for snippet in result.snippets:
        line = _one_line(snippet.text)
        compacted.append(snippet.model_copy(update={"text": line, "tokens": count_tokens(line)}))
    packed, total, truncated = pack_budget(compacted, budget_tokens)
    return result.model_copy(
        update={
            "snippets": packed,
            "total_tokens": total,
            "truncated": result.truncated or truncated,
        }
    )


def _one_line(text: str, limit: int = _COMPACT_LINE_CHARS) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"
    return ""


# ---------------------------------------------------------------------------
# open helpers
# ---------------------------------------------------------------------------
def _vault_relative(vault_root: Path, path: str) -> str:
    """Normalize a client-supplied path to a vault-relative one, refusing escapes.

    The lookup-only twin of :func:`_read_note`'s first half — used by ``related``, which
    never opens the file and so must not require it to exist on disk (the index is what it
    queries). Containment is still enforced: ``'../../etc/passwd'`` is a mistake worth
    naming, not a silent "not indexed", and refusing it here means no path outside the
    vault ever reaches a store query.
    """
    if not path.strip():
        raise ToolError("`path` must be a non-empty vault-relative path")
    candidate = (vault_root / path).resolve()
    if not candidate.is_relative_to(vault_root):
        raise ToolError(f"path {path!r} escapes the vault — pass a vault-relative note path")
    return candidate.relative_to(vault_root).as_posix()


def _read_note(vault_root: Path, path: str) -> tuple[str, str]:
    """Resolve ``path`` inside the vault and read it, enforcing every indexing gate.

    ``open`` reads the vault directly, so it must refuse exactly what the scan
    boundary refuses (CLAUDE.md §6) — otherwise it is a bypass. Containment is checked
    here (``resolve()`` follows symlinks first, so a note pointing outside the vault is
    caught); the rest is delegated to :func:`~vaultrag.indexer.scan.exclusion_reason`,
    the *same* predicate ``walk_vault`` filters with, so the two can never drift apart
    into a hole. The whole-file content scan follows the read.

    ``.ragignore`` is re-read per call on purpose: an edit that adds an exclusion must
    take effect immediately, and a cached spec would keep serving a file the user just
    withdrew.
    """
    rel = _vault_relative(vault_root, path)
    candidate = vault_root / rel

    reason = exclusion_reason(rel, ignore=load_ignore_spec(vault_root))
    if reason is not None:
        if reason == "secret-filename":
            log.warning("mcp.open_blocked", path=rel, rule="filename")
        raise ToolError(f"{rel!r} {_EXCLUSION_MESSAGES[reason]}")
    if not candidate.is_file():
        raise ToolError(f"note not found: {rel!r}")

    # Indexable but not readable *as text*: a PDF's bytes decode to mojibake, and
    # handing an agent that is worse than a refusal it can act on. `search` still
    # returns its chunks (with a `page`), which is the way in — so say that, rather
    # than pretending the path doesn't exist.
    if is_image_source(rel):
        raise ToolError(
            f"{rel!r} is an image — `open` serves text only. Any text it contains was "
            f"OCR'd at index time and is searchable via `search`."
        )
    if is_binary_source(rel):
        raise ToolError(
            f"{rel!r} is a PDF — `open` serves text only. Its extracted text is "
            f"searchable via `search`, and each hit cites the page it came from."
        )

    # A bare `.excalidraw` is a JSON scene end to end — same situation as the PDF above,
    # arrived at differently: the bytes *are* text, they just aren't prose.
    if is_scene_file(rel):
        raise ToolError(
            f"{rel!r} is an Excalidraw scene — `open` serves prose, and this file is "
            f"scene JSON. Its labels and frame names are searchable via `search`."
        )

    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"could not read {rel!r}: {exc}") from exc

    rule = scan_for_secrets(text)
    if rule is not None:
        # Same posture as the scan boundary: withhold the whole file, name only the rule.
        log.warning("mcp.open_blocked", path=rel, rule=rule)
        raise ToolError(f"{rel!r} contains secret-like content ({rule}) and is withheld")

    # An Excalidraw *wrapper* does have prose worth serving (`## Text Elements`,
    # `## Element Links`) — just not the compressed scene below it. Cutting the payload
    # is a suffix trim, so every line `open` returns still has its on-disk number.
    if is_excalidraw_source(rel, text):
        text = readable_text(text)
    return rel, text


def _validate_lines(lines: list[int], total: int, rel: str) -> tuple[int, int]:
    """Validate a ``[start, end]`` request → a clamped 1-based inclusive span."""
    if len(lines) != 2:
        raise ToolError("`lines` must be a [start, end] pair, 1-based inclusive")
    start, end = lines
    if start < 1 or end < start:
        raise ToolError(f"invalid line span [{start}, {end}]: need 1 <= start <= end")
    if start > total:
        raise ToolError(f"{rel!r} has only {total} lines; requested span starts at {start}")
    return start, min(end, total)


def _apply_cap(
    slice_lines: list[str], cap_tokens: int = _OPEN_CAP_TOKENS
) -> tuple[list[str], bool]:
    """Keep leading lines until the token cap; always keep at least the first line.

    Same philosophy as search's budget packer: an oversized-but-requested first line
    beats an empty response.
    """
    budget_chars = cap_tokens * CHARS_PER_TOKEN  # inverse of count_tokens' heuristic
    kept: list[str] = []
    used = 0
    for line in slice_lines:
        cost = len(line) + 1
        if kept and used + cost > budget_chars:
            return kept, True
        kept.append(line)
        used += cost
    return kept, False
