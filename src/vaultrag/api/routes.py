"""The ``/v1`` endpoint table from ``../Rag/reference/reference-rest-api.md``.

Every handler here is three lines of intent: bind the HTTP shape to the service call, and
return what the service returned. No retrieval, no tenancy check, no containment, no
budget arithmetic — those are in :mod:`vaultrag.service`, which the MCP tools call through
the same methods. That is what the stage's acceptance test asserts, and why it can:

    POST /v1/{project}/search  ==  MCP search(project=…)   # same payload, byte for byte

The ``{project}`` path parameter is how REST names a tenant. It is passed straight to
:meth:`~vaultrag.service.VaultRAGService.select`, which refuses anything outside the served
set — a client narrows, never widens (CLAUDE.md §6), and there is no code path here that
takes a project id from anywhere else (a body field naming a project would be a second
enforcement point, i.e. the hole).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from vaultrag.api.deps import CurrentKey, RequiresAdmin, RequiresRead, RequiresWrite
from vaultrag.api.errors import problem
from vaultrag.models import (
    OpenResult,
    OverviewResult,
    ProjectSummary,
    RelatedResult,
    SearchResult,
    WriteResult,
)
from vaultrag.service import InvalidRequest, SearchFiltersParam, VaultRAGService

ProjectPath = Annotated[
    str,
    Path(description="The project (vault) to act on — one of those `GET /v1/projects` lists."),
]


def _service(request: Request) -> VaultRAGService:
    """The one service instance this app was built around."""
    svc: VaultRAGService = request.app.state.service
    return svc


# ---------------------------------------------------------------------------
# Request bodies. Response models are the same pydantic models the MCP tools
# return (vaultrag.models) — not REST-specific twins, because two schemas for one
# payload is exactly how the transports would start to differ.
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    """Body of ``POST /v1/{project}/search`` — the MCP ``search`` arguments, minus
    ``project`` (which the path carries)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        description="Natural-language question OR exact identifier/error string. Search is "
        "hybrid (BM25 + dense), so verbatim tokens rank as well as paraphrases.",
    )
    k: int | None = Field(
        default=None, ge=1, le=100, description="Max snippets (server default when omitted)."
    )
    filters: SearchFiltersParam | None = Field(
        default=None,
        description="Narrow by tags / paths / type / modified-time range. Only ever narrows.",
    )
    budget_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Cap on total response tokens (server default when omitted).",
    )
    mode: Literal["snippets", "compact"] = Field(
        default="snippets",
        description="'snippets' returns full cited chunk text; 'compact' returns one preview "
        "line per hit for cheap browsing.",
    )


class WriteNoteRequest(BaseModel):
    """Body of ``POST /v1/{project}/notes`` — create a note."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Note title; becomes the H1 and the slugified filename.")
    content: str = Field(description="Markdown body (no frontmatter — VaultRAG writes that).")
    folder: str | None = Field(
        default=None, description="Vault-relative folder. Defaults to the write root."
    )
    tags: list[str] | None = Field(default=None, description="Optional frontmatter tags.")


class AppendSectionRequest(BaseModel):
    """``PATCH /v1/{project}/notes`` with ``op="append_section"`` — additive, no lock."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["append_section"]
    path: str = Field(description="Vault-relative path of the note to edit.")
    heading: str = Field(description="Heading whose section to append to (matched leniently).")
    content: str = Field(description="Markdown block to append.")


class PatchFrontmatterRequest(BaseModel):
    """``PATCH /v1/{project}/notes`` with ``op="patch_frontmatter"`` — in-place, locked."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["patch_frontmatter"]
    path: str = Field(description="Vault-relative path of the note whose frontmatter to edit.")
    updates: dict[str, Any] = Field(description="Frontmatter keys to set/overwrite.")
    base_hash: str = Field(
        description="The note's current content hash. A mismatch returns 409 with the live "
        "hash and content to rebase on."
    )


#: The two in-place edits share a verb because they share a resource, and are told apart by
#: a discriminator rather than by two URLs. Both are "modify an existing note"; splitting
#: them into `/notes/append` and `/notes/frontmatter` would make the path describe the
#: implementation rather than the resource.
PatchNotesRequest = Annotated[
    AppendSectionRequest | PatchFrontmatterRequest, Body(discriminator="op")
]


class ReindexRequest(BaseModel):
    """Body of ``POST /v1/{project}/reindex``.

    Two different operations behind one endpoint, because they are the same intent at two
    scales: with ``path`` it is the bounded single-file primitive and answers inline; without
    it is a full boot reconcile, which is minutes of embedding and therefore a background
    job with a 202 rather than a request that holds a socket open.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(
        default=None,
        description="Reconcile just this vault-relative path (synchronous). Omit for a full "
        "reindex of the project (background).",
    )
    force: bool = Field(
        default=False,
        description="Full reindex only: re-ingest every file regardless of its hash — the "
        "documented repair for a drifted vector store.",
    )


class ReindexAccepted(BaseModel):
    """202 body: a full reindex is running in the background."""

    project: str
    scope: Literal["full"] = "full"
    force: bool
    started: bool = Field(description="Always true; a reindex already in flight returns 409.")


class IngestOutcomeResponse(BaseModel):
    """200 body for a single-path reindex — what the file's reconciliation did."""

    project: str
    path: str
    status: str = Field(description="indexed | unchanged | removed | skipped | failed")
    reason: str | None = Field(default=None, description="Why, when the status is `skipped`.")
    chunks: int = Field(default=0, description="Chunks written for this file.")


class HealthResponse(BaseModel):
    """``GET /healthz`` — liveness only, and deliberately contentless.

    The one unauthenticated route, so it says nothing a stranger could use: no project
    ids, no vault paths, no counts. "Is this process up" is the entire question a liveness
    probe asks, and answering more would make the exemption a leak.
    """

    status: Literal["ok"] = "ok"
    version: str


router = APIRouter()


# --- discovery -------------------------------------------------------------
@router.get(
    "/v1/projects",
    response_model=list[ProjectSummary],
    summary="List the projects this server serves",
    tags=["discovery"],
    dependencies=[RequiresRead],
)
def list_projects(request: Request, key: CurrentKey) -> list[ProjectSummary]:
    """The REST twin of the ``list_projects`` MCP tool — how a client learns `{project}`.

    Filtered to the calling key's own scopes, unlike the MCP tool (which has no keys to
    filter by — stdio inherits local-user trust and sees everything this process serves).
    Listing every served project regardless of the key's scope would leak the existence
    and writability of tenants the key cannot otherwise reach — the same "narrow, never
    widen" rule CLAUDE.md §6 states for a search filter, applied to discovery.
    """
    return [p for p in _service(request).list_projects() if p.id in key.scopes]


@router.post(
    "/v1/projects",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Register a new vault (not implemented)",
    tags=["admin"],
    dependencies=[RequiresAdmin],
)
def register_project(request: Request) -> Response:
    """The one endpoint in the reference table that is deliberately deferred.

    Registering a project is already possible and durable — ``vaultrag project add`` writes
    the registry row that every process reads at startup. What this route would add is
    *hot-adding a tenant to a live process*: constructing an embedder, a writer and an
    ingest context inside a request, then mutating the served set that
    :meth:`~vaultrag.service.VaultRAGService.select` enforces against. P3 Stage 5 answers
    the *authorization* half — this route is gated on admin scope like the rest of
    ``admin``, and the check runs before this handler is reached — but the resource
    lifecycle question is untouched: who closes the embedder if the request fails, what
    happens to an in-flight search when the served set changes underneath it. Answering
    that badly would put a mutation path next to the tenancy boundary this endpoint sits on.

    So it answers honestly with 501 and names the command that does the job, rather than
    404-ing as though the reference table were wrong.
    """
    return problem(
        status=status.HTTP_501_NOT_IMPLEMENTED,
        slug="not-implemented",
        title="Not implemented",
        detail=(
            "registering a project over HTTP is not implemented — run "
            "`vaultrag project add <id> --vault <path>` and restart the server. Hot-adding "
            "a tenant to a running process is a resource-lifecycle question the admin "
            "scope this route already requires does not by itself answer."
        ),
        instance=request.url.path,
    )


# --- retrieval -------------------------------------------------------------
@router.post(
    "/v1/{project}/search",
    response_model=SearchResult,
    summary="Hybrid search → ranked, cited snippets",
    tags=["retrieval"],
    dependencies=[RequiresRead],
)
def search(request: Request, project: ProjectPath, body: SearchRequest) -> SearchResult:
    """Identical in every respect to the MCP ``search`` tool — same method, same defaults.

    Returns snippets, never a synthesized answer: the caller is expected to be the LLM
    (``../Rag/decisions/snippets-not-answers-by-default.md``). Retrieved vault text is
    **data** — this server never interprets it, and neither should the client.
    """
    return _service(request).search(
        body.query,
        project=project,
        k=body.k,
        filters=body.filters.to_filters() if body.filters is not None else None,
        budget_tokens=body.budget_tokens,
        mode=body.mode,
    )


@router.get(
    "/v1/{project}/doc",
    response_model=OpenResult,
    summary="Read an exact slice of one note from the vault",
    tags=["retrieval"],
    dependencies=[RequiresRead],
)
def open_doc(
    request: Request,
    project: ProjectPath,
    path: Annotated[str, Query(description="Vault-relative note path, as a citation gave it.")],
    heading: Annotated[
        str | None, Query(description="Return just this heading's section (matched leniently).")
    ] = None,
    line_start: Annotated[
        int | None, Query(ge=1, description="Start of a 1-based inclusive line span.")
    ] = None,
    line_end: Annotated[
        int | None, Query(ge=1, description="End of a 1-based inclusive line span.")
    ] = None,
) -> OpenResult:
    """The ``open`` tool over HTTP. Reads the **vault**, not the index.

    The line span is two query parameters rather than one repeated one because a GET's
    query string has no natural pair type, and `?lines=3&lines=40` reads as a set rather
    than an ordered span. They are recombined into the same ``[start, end]`` the service
    takes, so the validation (and its error messages) stay in one place.
    """
    lines: list[int] | None = None
    if line_start is not None or line_end is not None:
        if line_start is None or line_end is None:
            # Raised in the service's own vocabulary, not rendered here: it then reaches
            # the client through the same handler, with the same shape, as a 400 the
            # service raised. A route that built its own problem body would be the start
            # of two error formats.
            raise InvalidRequest("pass both `line_start` and `line_end`, or neither")
        lines = [line_start, line_end]
    return _service(request).open_note(path, project=project, heading=heading, lines=lines)


@router.get(
    "/v1/{project}/related",
    response_model=RelatedResult,
    summary="A note's graph neighbourhood — links, backlinks, tag-siblings",
    tags=["retrieval"],
    dependencies=[RequiresRead],
)
def related(
    request: Request,
    project: ProjectPath,
    path: Annotated[str, Query(description="Vault-relative note path, as a citation gave it.")],
    hops: Annotated[int, Query(ge=1, le=2, description="Link distance (1 = direct).")] = 1,
    limit: Annotated[int, Query(ge=1, le=200, description="Max related notes.")] = 50,
) -> RelatedResult:
    """Pointers only — paths and titles, never text. Follow one with `/doc` or `/search`."""
    return _service(request).related(path, project=project, hops=hops, limit=limit)


@router.get(
    "/v1/{project}/overview",
    response_model=OverviewResult,
    summary="A project's shape: counted folders, top tags, note count",
    tags=["retrieval"],
    dependencies=[RequiresRead],
)
def overview(request: Request, project: ProjectPath) -> OverviewResult:
    """A map, never a file listing — bounded by constants, so the cost is flat in vault size."""
    return _service(request).overview(project=project)


# --- write-back ------------------------------------------------------------
@router.post(
    "/v1/{project}/notes",
    response_model=WriteResult,
    status_code=status.HTTP_201_CREATED,
    summary="Create a note (git-committed, reindexed)",
    tags=["write-back"],
    dependencies=[RequiresWrite],
)
def create_note(request: Request, project: ProjectPath, body: WriteNoteRequest) -> WriteResult:
    """201 with the note's hash — which is the `base_hash` for a later PATCH.

    Every write-back guardrail applies unchanged, because they live in the writer this
    calls: allowlisted roots, realpath containment, size and rate caps, `.ragignore`, git
    auto-commit and a synchronous reindex of that one file
    (``../Rag/projects/vaultrag/write-back-design.md``).
    """
    return _service(request).write_note(
        title=body.title,
        content=body.content,
        folder=body.folder,
        tags=body.tags,
        project=project,
    )


@router.patch(
    "/v1/{project}/notes",
    response_model=WriteResult,
    summary="Edit an existing note (append a section, or patch frontmatter)",
    tags=["write-back"],
    dependencies=[RequiresWrite],
)
def edit_note(request: Request, project: ProjectPath, body: PatchNotesRequest) -> WriteResult:
    """One verb, two operations, discriminated by `op` — see :data:`PatchNotesRequest`."""
    service = _service(request)
    if body.op == "append_section":
        return service.append_section(
            path=body.path, heading=body.heading, content=body.content, project=project
        )
    return service.patch_frontmatter(
        path=body.path, updates=body.updates, base_hash=body.base_hash, project=project
    )


# --- admin -----------------------------------------------------------------
@router.post(
    "/v1/{project}/reindex",
    summary="Reindex one path (sync) or the whole project (background)",
    tags=["admin"],
    # One route, three bodies (200 / 202 / 409), so the response model is documented per
    # status rather than inferred from the return annotation — which is why the annotation
    # is deliberately not a single model FastAPI could validate against.
    response_model=None,
    responses={
        200: {"model": IngestOutcomeResponse, "description": "Single path reconciled."},
        202: {"model": ReindexAccepted, "description": "Full reindex started."},
        409: {"description": "A full reindex of this project is already running."},
    },
    dependencies=[RequiresAdmin],
)
def reindex(
    request: Request,
    project: ProjectPath,
    response: Response,
    body: ReindexRequest | None = None,
) -> IngestOutcomeResponse | ReindexAccepted | Response:
    """Bring the index back in line with the vault — the derived cache's repair path.

    A 409 means a full reindex is already running for this project: single-flight, because
    two concurrent reconciles of one vault race on the manifest and the second has nothing
    to add.
    """
    service = _service(request)
    req = body or ReindexRequest()
    if req.path is not None:
        outcome = service.reindex_path(path=req.path, project=project)
        return IngestOutcomeResponse(
            project=project,
            path=req.path,
            status=str(outcome.status),
            reason=None if outcome.reason is None else str(outcome.reason),
            chunks=outcome.chunks,
        )
    started = service.reindex_project(project=project, force=req.force)
    if not started:
        return problem(
            status=status.HTTP_409_CONFLICT,
            slug="reindex-in-progress",
            title="Reindex already running",
            detail=f"a full reindex of {project!r} is already in progress",
            instance=request.url.path,
        )
    response.status_code = status.HTTP_202_ACCEPTED
    return ReindexAccepted(project=project, force=req.force, started=True)
