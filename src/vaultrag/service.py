"""The transport-neutral service core — one implementation of every VaultRAG operation.

Until P3 Stage 4 there was one transport (MCP over stdio), so the operations lived inside
the FastMCP tool closures and "the core" meant :func:`vaultrag.retrieval.search` plus
whatever ``mcp/server.py`` wrapped around it. Adding the REST surface makes that shape
untenable: parent-resolver wiring, compact mode, the query-log policy, vault containment,
``overview``'s caps and the write-refusal translation are all *service* decisions, and a
second transport re-deriving any of them is how two front doors start answering
differently for the same question.

So this module owns them, and both transports are adapters over it:

* ``mcp/server.py`` — tool schemas (written for models), :class:`ToolError` translation.
* ``api/routes.py`` — HTTP verbs, status codes, RFC 7807 rendering.

Neither holds retrieval or safety logic. That is what makes the stage's acceptance test —
one query, MCP ``search`` and ``POST /v1/{project}/search`` return the same payload — a
statement about the *structure* rather than a coincidence two code paths currently share
(``../Rag/projects/vaultrag/p3-step-plan.md`` Stage 4; principle #3, MCP is primary and
REST is a wrapper over one core — ``../Rag/decisions/mcp-first-not-rest-first.md``).

The same reasoning already produced P3 Stage 3's placement: the query log records inside
``retrieval/pipeline.search`` precisely so that a new transport inherits the row instead of
growing its own. This module extends that rule from one table to the whole operation set.

Errors are transport-neutral too (:class:`ServiceError` and friends): the service says
*what kind* of refusal happened and writes the message for a model to read, and each
transport renders it — a ``ToolError`` string for MCP, a ``problem+json`` status for HTTP.
A service that raised ``ToolError`` would have made HTTP status codes guesswork, and one
that raised ``HTTPException`` would have leaked the web framework into stdio.

Security posture is unchanged and lives here now, in one place rather than per transport:

* **Tenancy** — the service answers for a fixed set of :class:`ServedProject` bundles fixed
  at construction (P3 Stage 2: one process serves N — ``../Rag/projects/vaultrag/
  multi-tenant-architecture.md``). :meth:`VaultRAGService.select` is the single enforcement
  point; a call's ``project`` argument *selects* among the served set and can never name one
  outside it, so a client narrows and never widens (CLAUDE.md §6).
* **``open`` enforces the same gates as indexing** — containment through symlinks, the
  built-in secret guard, ``.ragignore``, and the not-text refusals (PDF, image, bare
  Excalidraw scene). Without these, ``open`` would hand an agent exactly the files the scan
  boundary exists to withhold.
* Retrieved vault text is returned as data, never executed or interpreted (§6).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from vaultrag.chunking.tokens import CHARS_PER_TOKEN, count_tokens
from vaultrag.embedding.base import EmbeddingError
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.index.base import SearchFilters, SparseEmbedder, VectorStore, VectorStoreError
from vaultrag.indexer.pipeline import IngestContext, IngestOutcome, ingest_path, reindex_vault
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

__all__ = [
    "BackendUnavailable",
    "Conflict",
    "InvalidRequest",
    "NotFound",
    "OverviewStore",
    "Refused",
    "SearchFiltersParam",
    "ServedProject",
    "ServiceError",
    "UnknownProject",
    "VaultRAGService",
]

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

# exclusion_reason rule → the caller-facing refusal text. One message per gate the scan
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


# ---------------------------------------------------------------------------
# Transport-neutral errors.
# ---------------------------------------------------------------------------
class ServiceError(Exception):
    """Base for every refusal the service raises.

    The message is written **for the caller** — an agent reading a ``ToolError`` or an
    operator reading a ``problem+json`` ``detail`` — so it names the thing to do next
    rather than the internal state that went wrong. Subclasses carry no HTTP knowledge;
    the mapping to a status code lives in ``api/errors.py``, and to a ``ToolError`` in
    ``mcp/server.py``, because *which* transport is answering is exactly what the service
    must not know.
    """


class UnknownProject(ServiceError):
    """The caller named a project this process does not serve (or omitted a required one).

    Distinct from :class:`NotFound` on purpose: this is the tenancy boundary refusing, not
    a missing resource, and Stage 5 will make it the place a key's scopes are enforced.
    """


class NotFound(ServiceError):
    """A note, heading or indexed document the caller named does not exist."""


class InvalidRequest(ServiceError):
    """The arguments are malformed or contradictory (both ``heading`` and ``lines``, …)."""


class Refused(ServiceError):
    """The request is well-formed but a guardrail declines it.

    Covers both halves of the safety surface: the read gates (``.ragignore``, the secret
    guard, not-text files, a path escaping the vault) and the write gates (a read-only
    project, write-back's allowlist/size/rate caps).
    """


class Conflict(ServiceError):
    """An edit lost its optimistic lock — ``base_hash`` no longer matches the note.

    Carries the live hash and content so a caller can rebase without a second read
    (``../Rag/projects/vaultrag/write-back-design.md``). Structured rather than
    pre-rendered, because MCP wants one prose string and HTTP wants fields.
    """

    def __init__(self, *, path: str, current_hash: str, current_text: str) -> None:
        super().__init__(f"{path!r} changed since base_hash was taken — no silent overwrite")
        self.path = path
        self.current_hash = current_hash
        self.current_text = current_text


class BackendUnavailable(ServiceError):
    """A dependency the request needs is down (Qdrant, Ollama).

    Kept apart from every other error because it is the one class that is **not** the
    caller's fault and **is** worth retrying — the distinction a 4xx/5xx split exists to
    make, and one an agent should read as "the box is broken", not "ask differently".
    """


class SearchFiltersParam(BaseModel):
    """Client-suppliable narrowing filters — the boundary twin of
    :class:`~vaultrag.index.base.SearchFilters`.

    Lives here rather than in a transport so MCP's tool schema and the REST body are the
    *same* model: identical field names, identical validation, identical nested-tag
    semantics. A filter that narrowed differently over HTTP than over stdio would be a
    tenancy-adjacent divergence, and the cheapest way to not have one is to have one class.

    Deliberately has no ``project`` field: filters can only narrow within the project the
    call already selected, never widen it (CLAUDE.md §6). ``extra="forbid"`` so a
    misspelled filter key fails loudly instead of silently not filtering.
    """

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = PydanticField(
        default_factory=list,
        description="Keep only chunks tagged with ANY of these (nested tags are "
        "expanded, so 'project' also matches notes tagged 'project/vaultrag').",
    )
    paths: list[str] = PydanticField(
        default_factory=list,
        description="Keep only chunks from ANY of these exact vault-relative note paths.",
    )
    type: str | None = PydanticField(
        default=None,
        description="Exact document type, e.g. 'note' (P0 indexes only notes).",
    )
    modified_after: int | None = PydanticField(
        default=None,
        description="Keep only notes modified at/after this epoch-seconds timestamp (inclusive).",
    )
    modified_before: int | None = PydanticField(
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


@runtime_checkable
class OverviewStore(Protocol):
    """The slice of the metadata store ``overview`` reads — three per-project aggregates.

    Structurally satisfied by :class:`~vaultrag.store.sqlite.SQLiteMetaStore`, and declared
    narrowly for the same reason :class:`~vaultrag.retrieval.graph.GraphStore` is: the
    operation depends on three read methods rather than the whole ``MetaStore`` write
    surface, and a test can hand it a stub.

    Note what is *not* here: any vector-store call. ``overview`` is a SQLite-only operation,
    so it still answers "what is actually in this project?" on a deployment whose Qdrant is
    down — which is one of the states in which that question is most worth asking.
    """

    def document_count(self, project_id: str) -> int: ...

    def folder_counts(
        self, project_id: str, depth: int = 2, limit: int = 50
    ) -> list[tuple[str, int]]: ...

    def top_tags(self, project_id: str, limit: int = 20) -> list[tuple[str, int]]: ...


@dataclass(frozen=True, slots=True)
class ServedProject:
    """Everything one tenant needs, bundled — the unit a service serves N of.

    This is the whole of P3 Stage 2's "one process serves N projects": the per-call
    ``project`` argument selects a bundle, and every tenant-specific decision downstream
    reads it off the bundle instead of off a process-wide flag. The split between what lives
    here and what is passed to :class:`VaultRAGService` once is not cosmetic:

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
    # P3 Stage 4. The per-project ingest capability `POST /v1/{project}/reindex` needs.
    # ``None`` means this process cannot index that project — the ordinary state for a
    # read-only ``ROLE=api`` deployment, where the indexer is a separate container
    # ([[reference-docker-compose]]). It is the *same* context the writer already drives,
    # so a reindex and a write-back ingest cannot disagree about parsers or stores.
    ingest: IngestContext | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vault_root", Path(self.vault_root).resolve())

    @property
    def display_name(self) -> str:
        return self.name or self.project_id


class VaultRAGService:
    """Every VaultRAG operation, once, over a fixed set of tenants.

    Construction mirrors what P3 Stage 2 established about where state lives:
    ``projects`` is the per-tenant half (vault root, writer, embedder, retrieval knobs) and
    everything else is shared because it is already ``project_id``-scoped on each call.

    ``reranker`` is the stage-4 cross-encoder; ``None`` runs the fusion-only P0 path.

    ``graph_store`` turns on retrieval stage 6 **and** :meth:`related` — both read P2
    Stage 2's ``links`` table. ``None`` (a deployment with no metadata store, or one
    indexed before Stage 2) simply omits both: search still works, it just carries no
    ``related`` pointers.

    ``meta_store`` enables :meth:`overview` on the same principle — no store, no capability,
    rather than one that answers zero for everything. ``query_log_sink`` is where Stage 3's
    per-search rows land. All three are usually the same SQLite store, kept as separate
    parameters because they are separate *capabilities* and a deployment can have one
    without the others.

    The ``has_*`` properties exist so a transport can decide what to expose (MCP registers
    tools, HTTP returns 501) **without** re-deriving the rule from the constructor
    arguments — two transports disagreeing about whether ``related`` is available is the
    small version of the divergence this class exists to prevent.
    """

    def __init__(
        self,
        *,
        projects: Sequence[ServedProject],
        vector_store: VectorStore,
        sparse: SparseEmbedder,
        reranker: Reranker | None = None,
        graph_store: GraphStore | None = None,
        meta_store: OverviewStore | None = None,
        query_log_sink: QueryLogSink | None = None,
    ) -> None:
        if not projects:
            raise ValueError("VaultRAGService needs at least one ServedProject to serve")
        by_id: dict[str, ServedProject] = {}
        for served in projects:
            if served.project_id in by_id:
                raise ValueError(f"duplicate project in served set: {served.project_id!r}")
            by_id[served.project_id] = served
        self._by_id = by_id
        self._projects = tuple(projects)
        # The lone project is the implicit default; with several there is no defensible
        # default, so `project` becomes mandatory. Guessing here would mean a forgotten
        # argument silently queries the wrong vault — the one failure mode multi-tenancy
        # must not have.
        self._solo = self._projects[0] if len(self._projects) == 1 else None
        self._served_ids = ", ".join(sorted(by_id))
        self._vector_store = vector_store
        self._sparse = sparse
        self._reranker = reranker
        self._graph_store = graph_store
        self._meta_store = meta_store
        self._query_log_sink = query_log_sink
        # Single-flight for background reindexes, keyed by project. Held only while
        # starting/inspecting a thread, never across the reindex itself.
        self._reindex_lock = threading.Lock()
        self._reindexing: dict[str, threading.Thread] = {}

    # --- shape -------------------------------------------------------------
    @property
    def projects(self) -> tuple[ServedProject, ...]:
        """The served set, in the order given."""
        return self._projects

    @property
    def served_ids(self) -> str:
        """Sorted, comma-joined project ids — for error messages that name the options."""
        return self._served_ids

    @property
    def is_solo(self) -> bool:
        """True when one project is served and ``project`` may therefore be omitted."""
        return self._solo is not None

    @property
    def has_graph(self) -> bool:
        """Whether :meth:`related` can answer for at least one served project."""
        return self._graph_store is not None and any(p.graph_enabled for p in self._projects)

    @property
    def has_overview(self) -> bool:
        """Whether :meth:`overview` can answer (i.e. a metadata store was supplied)."""
        return self._meta_store is not None

    @property
    def has_writes(self) -> bool:
        """Whether at least one served project accepts writes."""
        return any(p.writer is not None for p in self._projects)

    # --- tenancy -----------------------------------------------------------
    def select(self, project: str | None) -> ServedProject:
        """Resolve a call's ``project`` argument to the bundle it may use, or refuse.

        The single enforcement point for CLAUDE.md §6's "a client can narrow, never widen":
        every operation routes through it, and nothing downstream takes a project id from
        the caller again. A second place that resolved a project would *be* the tenancy
        hole — which is why the P3 Stage 2 draft that had ``overview`` doing its own lookup
        was rewritten rather than left as a harmless duplicate.
        """
        if project is None:
            if self._solo is not None:
                return self._solo
            raise UnknownProject(
                f"this server serves several projects — pass `project` (one of: {self._served_ids})"
            )
        found = self._by_id.get(project)
        if found is None:
            raise UnknownProject(
                f"project {project!r} is not served by this server — tenancy is enforced "
                f"server-side. Available: {self._served_ids}."
            )
        return found

    # --- read operations ---------------------------------------------------
    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        k: int | None = None,
        filters: SearchFilters | None = None,
        budget_tokens: int | None = None,
        mode: str = "snippets",
    ) -> SearchResult:
        """Hybrid search over one project's notes → ranked, cited snippets.

        ``mode="compact"`` squashes each hit to a one-line preview for cheap browsing;
        every other mode returns full chunk text. Snippets are returned as **data** — the
        service never interprets what the vault said (CLAUDE.md §6).
        """
        sel = self.select(project)
        t0 = time.perf_counter()
        effective_budget = budget_tokens if budget_tokens is not None else sel.default_budget_tokens
        # Fresh resolver per call: its parse cache dedupes reads *within* one search but must
        # not persist across calls, or an edited note would resolve stale (open re-reads too).
        # Skipped in compact mode — browsing by preview shouldn't pay for section
        # reconstruction. Bound to *this* project's vault root, so a multi-project server can
        # never reconstruct a parent section out of a neighbouring tenant's files.
        parent_resolver = (
            VaultParentResolver(read_note=lambda p: self._read_note(sel.vault_root, p)[1])
            if sel.parent_swap_enabled and mode == "snippets"
            else None
        )
        try:
            result = retrieval_search(
                query,
                project_id=sel.project_id,
                vector_store=self._vector_store,
                embedder=sel.embedder,
                sparse=self._sparse,
                k=k if k is not None else sel.default_k,
                filters=filters,
                reranker=self._reranker,
                rerank=sel.rerank_params,
                parent_resolver=parent_resolver,
                parent_max_tokens=sel.parent_max_tokens,
                # Per-project: a tenant with graph expansion off gets the fusion-only path
                # even though the process holds a link graph for its neighbours.
                graph_store=self._graph_store if sel.graph_enabled else None,
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
                query_log_sink=self._query_log_sink if sel.query_log is not None else None,
                query_log=sel.query_log.with_mode(mode) if sel.query_log is not None else None,
                # Compact mode budgets the *previews*, not the full texts: packing full
                # texts first would silently drop hits the agent asked to browse (k),
                # so retrieval runs unbounded and _compact re-packs to the real budget.
                budget_tokens=_UNBOUNDED_BUDGET if mode == "compact" else effective_budget,
            )
        except (VectorStoreError, EmbeddingError) as exc:
            # The message reaches the caller verbatim — keep the actionable text
            # ("is Qdrant running? docker compose up -d qdrant") instead of masking it.
            raise BackendUnavailable(f"search failed: {exc}") from exc
        if mode == "compact":
            result = _compact(result, budget_tokens=effective_budget)
        log.info(
            "service.search",  # q_hash, never the raw query, at info level (CLAUDE.md §5)
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

    def open_note(
        self,
        path: str,
        *,
        project: str | None = None,
        heading: str | None = None,
        lines: Sequence[int] | None = None,
    ) -> OpenResult:
        """Read an exact slice of one note straight from the vault (the source of truth).

        Always at least as fresh as the index a citation came from, and gated exactly as
        the indexer gates its scan — see :meth:`_read_note`.
        """
        sel = self.select(project)
        if heading is not None and lines is not None:
            raise InvalidRequest("pass `heading` or `lines`, not both")
        if heading is not None and not heading.strip():
            raise InvalidRequest("`heading` must be non-empty when given")

        # Resolved against the *selected* project's root — the failure mode this whole
        # per-call plumbing exists to prevent is one project reading another's files
        # through a root captured once at construction (p3-step-plan Stage 2 verify).
        rel, text = self._read_note(sel.vault_root, path)
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
            start, end = _validate_lines(list(lines), total, rel)
        elif heading is not None:
            span = heading_section(document, heading, total)
            if span is None:
                # Name the real headings so the caller can retry without a second probe;
                # whole headings only (a char-truncated one would just get re-sent mangled).
                names = [b.text for b in document.blocks if b.type is BlockType.heading]
                available = ", ".join(names[:_HEADING_LIST_LIMIT]) or "(none)"
                raise NotFound(
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
            "service.open",
            project=sel.project_id,
            path=rel,
            line_start=result.line_start,
            line_end=result.line_end,
            tokens=result.tokens,
            truncated=result.truncated,
        )
        return result

    def related(
        self,
        path: str,
        *,
        project: str | None = None,
        hops: int = 1,
        limit: int = 50,
    ) -> RelatedResult:
        """One note's graph neighbourhood — links, backlinks, tag-siblings — as pointers.

        Returns paths + titles, never text: following one is what ``open`` and ``search``
        are for, and keeping text out means a caller can survey a neighbourhood for almost
        nothing. Everything named here came out of the index, which already applied every
        scan gate, so there is no path by which ``related`` surfaces a note ``open`` would
        refuse.
        """
        sel = self.select(project)
        if self._graph_store is None:
            raise Refused(
                "this server holds no link graph — it was built without a metadata store, "
                "or the index predates the links table. Use `search` instead."
            )
        if not sel.graph_enabled:
            raise Refused(
                f"project {sel.project_id!r} has graph expansion disabled, so it exposes no "
                "link graph — use `search` instead"
            )
        rel = _vault_relative(sel.vault_root, path)
        document = self._graph_store.get_document(sel.project_id, rel)
        if document is None:
            raise NotFound(
                f"{rel!r} is not indexed in project {sel.project_id!r} — pass a path exactly "
                "as a search citation gave it (the graph only knows indexed notes)"
            )
        notes, unresolved, truncated = related_notes(
            project_id=sel.project_id,
            doc_id=document.id,
            store=self._graph_store,
            hops=hops,
            limit=limit,
            max_tag_siblings=sel.graph_params.max_tag_siblings,
            vault_name=sel.vault_name,
        )
        log.info(
            "service.related",
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

    def list_projects(self) -> list[ProjectSummary]:
        """The projects this process serves — how a caller learns the ``project`` string.

        ``notes`` is null when this deployment keeps no metadata store; ``writable`` says
        whether the write operations accept that project.
        """
        summaries = [
            ProjectSummary(
                id=p.project_id,
                name=p.display_name,
                notes=(
                    None
                    if self._meta_store is None
                    else self._meta_store.document_count(p.project_id)
                ),
                writable=p.writer is not None,
            )
            for p in self._projects
        ]
        log.info("service.list_projects", projects=len(summaries))
        return summaries

    def overview(self, *, project: str | None = None) -> OverviewResult:
        """A project's *shape* — counted folder map + top tags + note count.

        A map, never a file listing, and bounded by constants rather than by vault size, so
        the response cost is flat however large the vault grows
        (``../Rag/agent-inbox/overview-tool-shape.md``).
        """
        sel = self.select(project)
        if self._meta_store is None:
            raise Refused(
                "this server keeps no metadata store, so it cannot describe a project's "
                "shape — use `search` instead."
            )
        # One over each cap, so "a cap bit" is observed rather than inferred from equality
        # (a vault with exactly 50 folders is not truncated, and must not say it is).
        folders = self._meta_store.folder_counts(
            sel.project_id, depth=_OVERVIEW_FOLDER_DEPTH, limit=_OVERVIEW_FOLDER_LIMIT + 1
        )
        tags = self._meta_store.top_tags(sel.project_id, limit=_OVERVIEW_TAG_LIMIT + 1)
        truncated = len(folders) > _OVERVIEW_FOLDER_LIMIT or len(tags) > _OVERVIEW_TAG_LIMIT
        result = OverviewResult(
            project=sel.project_id,
            name=sel.display_name,
            notes=self._meta_store.document_count(sel.project_id),
            folders=[
                FolderCount(path=path, notes=n) for path, n in folders[:_OVERVIEW_FOLDER_LIMIT]
            ],
            tags=[TagCount(tag=tag, notes=n) for tag, n in tags[:_OVERVIEW_TAG_LIMIT]],
            truncated=truncated,
        )
        log.info(
            "service.overview",
            project=sel.project_id,
            notes=result.notes,
            folders=len(result.folders),
            tags=len(result.tags),
            truncated=truncated,
        )
        return result

    # --- write operations --------------------------------------------------
    def _writer_for(self, project: str | None) -> NoteWriter:
        """The selected project's writer, or a refusal naming why it is read-only."""
        sel = self.select(project)  # refuses an unserved project before anything else
        if sel.writer is None:
            raise Refused(
                f"project {sel.project_id!r} is served read-only — its write operations are "
                "disabled (--no-write, or VAULTRAG_WRITEBACK_ENABLED=false)"
            )
        return sel.writer

    def write_note(
        self,
        *,
        title: str,
        content: str,
        folder: str | None = None,
        tags: Sequence[str] | None = None,
        project: str | None = None,
    ) -> WriteResult:
        """Create a new note in the vault, git-committed and reindexed synchronously.

        Each writer owns its own vault, write root, git repo and rate limiter, so a
        multi-project write is N independent commits rather than a distributed transaction
        (one project = one vault dir = one git repo — [[multi-tenant-architecture]]).
        """
        writer = self._writer_for(project)
        with _write_errors():
            return writer.write_note(
                title=title, content=content, folder=folder, tags=list(tags) if tags else None
            )

    def append_section(
        self,
        *,
        path: str,
        heading: str,
        content: str,
        project: str | None = None,
    ) -> WriteResult:
        """Append a Markdown block to a heading's section, then reindex that one file.

        Additive, so it needs no ``base_hash`` lock: it inserts at the section's end
        without rewriting the rest.
        """
        writer = self._writer_for(project)
        with _write_errors():
            return writer.append_section(path=path, heading=heading, content=content)

    def patch_frontmatter(
        self,
        *,
        path: str,
        updates: dict[str, Any],
        base_hash: str,
        project: str | None = None,
    ) -> WriteResult:
        """Set frontmatter keys on an existing note under an optimistic lock, then reindex.

        The only write that mutates in place, and so the only one that requires
        ``base_hash``; a mismatch raises :class:`Conflict` carrying the live hash + content.
        """
        writer = self._writer_for(project)
        with _write_errors():
            return writer.patch_frontmatter(path=path, updates=updates, base_hash=base_hash)

    # --- admin -------------------------------------------------------------
    def _ingest_for(self, project: str | None) -> tuple[ServedProject, IngestContext]:
        """The selected project's ingest context, or a refusal naming why there isn't one."""
        sel = self.select(project)
        if sel.ingest is None:
            raise Refused(
                f"project {sel.project_id!r} has no indexer in this process — reindexing "
                f"is the indexer role's job (VAULTRAG_ROLE=indexer, or `vaultrag reindex "
                f"--project {sel.project_id}`)"
            )
        return sel, sel.ingest

    def reindex_path(self, *, path: str, project: str | None = None) -> IngestOutcome:
        """Reconcile ONE vault-relative path against disk — synchronous, bounded work.

        The single-file primitive P1 Stage 4 built, exposed: it is fast enough to answer
        inside a request, and it is what "I just changed this note, pick it up now" means
        on a deployment with no watcher. Applies every scan gate, so this route can never
        index what a full reindex would withhold.
        """
        sel, ctx = self._ingest_for(project)
        _vault_relative(sel.vault_root, path)  # refuse traversal before touching the index
        try:
            outcome = ingest_path(ctx, path)
        except (VectorStoreError, EmbeddingError) as exc:
            raise BackendUnavailable(f"reindex failed: {exc}") from exc
        except OSError as exc:
            raise Refused(f"reindex failed for {path!r}: {exc}") from exc
        log.info(
            "service.reindex_path",
            project=sel.project_id,
            path=path,
            status=str(outcome.status),
        )
        return outcome

    def reindex_project(self, *, project: str | None = None, force: bool = False) -> bool:
        """Start a full boot-reconcile in a background thread. ``False`` = already running.

        Not synchronous, and not optional that it isn't: a full reindex of a real vault is
        minutes of embedding, which is not a request/response shape. Single-flight per
        project because two concurrent reconciles of one vault would race on the manifest
        for no benefit — the second one has nothing to add that the first is not already
        doing.

        The thread is a daemon: a reindex is idempotent and restartable, so a shutdown
        that interrupts one costs a re-run, whereas a non-daemon thread would hang the
        process on exactly the operation most likely to be long.
        """
        sel, ctx = self._ingest_for(project)
        with self._reindex_lock:
            running = self._reindexing.get(sel.project_id)
            if running is not None and running.is_alive():
                return False
            thread = threading.Thread(
                target=self._run_reindex,
                args=(sel.project_id, ctx, force),
                name=f"vaultrag-reindex-{sel.project_id}",
                daemon=True,
            )
            self._reindexing[sel.project_id] = thread
            thread.start()
        return True

    def reindex_running(self, *, project: str | None = None) -> bool:
        """Whether a background reindex is in flight for this project."""
        sel = self.select(project)
        with self._reindex_lock:
            running = self._reindexing.get(sel.project_id)
        return running is not None and running.is_alive()

    def _run_reindex(self, project_id: str, ctx: IngestContext, force: bool) -> None:
        """The background body. Never raises out of the thread — it logs and returns."""
        try:
            stats = reindex_vault(
                project_id=project_id,
                vault_path=ctx.vault_path,
                store=ctx.store,
                vector_store=ctx.vector_store,
                embedder=ctx.embedder,
                force=force,
                pdf_parser=ctx.pdf_parser,
                image_parser=ctx.image_parser,
            )
        except Exception as exc:
            log.error("service.reindex_failed", project=project_id, force=force, error=str(exc))
            return
        log.info(
            "service.reindexed",
            project=project_id,
            force=force,
            created=stats.created,
            updated=stats.updated,
            deleted=stats.deleted,
            renamed=stats.renamed,
            unchanged=stats.unchanged,
            failed=stats.failed,
        )

    # --- vault reading -----------------------------------------------------
    def _read_note(self, vault_root: Path, path: str) -> tuple[str, str]:
        """Instance hook so ``search``'s parent resolver reads through the same gates."""
        return _read_note(vault_root, path)


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
# write helpers
# ---------------------------------------------------------------------------
@contextmanager
def _write_errors() -> Iterator[None]:
    """Translate write-back's exceptions into the service vocabulary, once.

    Both edit operations and the create operation refuse for the same two reasons, and a
    per-call ``try/except`` in each transport is exactly the duplication this module
    exists to remove.
    """
    try:
        yield
    except WriteConflict as exc:
        raise Conflict(
            path=exc.path, current_hash=exc.current_hash, current_text=exc.current_text
        ) from exc
    except WriteRefused as exc:
        raise Refused(str(exc)) from exc


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
        raise InvalidRequest("`path` must be a non-empty vault-relative path")
    candidate = (vault_root / path).resolve()
    if not candidate.is_relative_to(vault_root):
        raise Refused(f"path {path!r} escapes the vault — pass a vault-relative note path")
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
            log.warning("service.open_blocked", path=rel, rule="filename")
        raise Refused(f"{rel!r} {_EXCLUSION_MESSAGES[reason]}")
    if not candidate.is_file():
        raise NotFound(f"note not found: {rel!r}")

    # Indexable but not readable *as text*: a PDF's bytes decode to mojibake, and
    # handing an agent that is worse than a refusal it can act on. `search` still
    # returns its chunks (with a `page`), which is the way in — so say that, rather
    # than pretending the path doesn't exist.
    if is_image_source(rel):
        raise Refused(
            f"{rel!r} is an image — `open` serves text only. Any text it contains was "
            f"OCR'd at index time and is searchable via `search`."
        )
    if is_binary_source(rel):
        raise Refused(
            f"{rel!r} is a PDF — `open` serves text only. Its extracted text is "
            f"searchable via `search`, and each hit cites the page it came from."
        )

    # A bare `.excalidraw` is a JSON scene end to end — same situation as the PDF above,
    # arrived at differently: the bytes *are* text, they just aren't prose.
    if is_scene_file(rel):
        raise Refused(
            f"{rel!r} is an Excalidraw scene — `open` serves prose, and this file is "
            f"scene JSON. Its labels and frame names are searchable via `search`."
        )

    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Refused(f"could not read {rel!r}: {exc}") from exc

    rule = scan_for_secrets(text)
    if rule is not None:
        # Same posture as the scan boundary: withhold the whole file, name only the rule.
        log.warning("service.open_blocked", path=rel, rule=rule)
        raise Refused(f"{rel!r} contains secret-like content ({rule}) and is withheld")

    # An Excalidraw *wrapper* does have prose worth serving (`## Text Elements`,
    # `## Element Links`) — just not the compressed scene below it. Cutting the payload
    # is a suffix trim, so every line `open` returns still has its on-disk number.
    if is_excalidraw_source(rel, text):
        text = readable_text(text)
    return rel, text


def _validate_lines(lines: list[int], total: int, rel: str) -> tuple[int, int]:
    """Validate a ``[start, end]`` request → a clamped 1-based inclusive span."""
    if len(lines) != 2:
        raise InvalidRequest("`lines` must be a [start, end] pair, 1-based inclusive")
    start, end = lines
    if start < 1 or end < start:
        raise InvalidRequest(f"invalid line span [{start}, {end}]: need 1 <= start <= end")
    if start > total:
        raise NotFound(f"{rel!r} has only {total} lines; requested span starts at {start}")
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
