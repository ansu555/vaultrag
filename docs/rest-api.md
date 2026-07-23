# REST API — `/v1`

The same operations as [MCP](mcp-tools.md), over HTTP. Both transports are handed the
**same** `VaultRAGService` instance, so a divergence between them is impossible by
construction rather than merely tested for.

```bash
vaultrag serve --role api          # 127.0.0.1:8000 by default
```

Interactive docs (OpenAPI): `http://127.0.0.1:8000/docs`

---

## Authentication

**Every `/v1` route and `/metrics` requires a bearer key.** `/healthz` is the one
exception.

```bash
curl -H "Authorization: Bearer $VAULTRAG_KEY" http://127.0.0.1:8000/v1/projects
```

Keys are `id.secret`, argon2-hashed at rest, scoped per project, and rate-limited
independently. Mint them with [`vaultrag key add`](cli-reference.md#vaultrag-key).

On a first start with an empty `api_keys` table, one admin-everywhere key is minted and
**printed once** at startup, so there is always a way in.

### Two questions, asked in order

| | Question | Failure |
|---|---|---|
| **Authentication** | Is this a key this server minted and has not revoked? | **401** + `WWW-Authenticate: Bearer realm="vaultrag"` |
| **Authorization** | Does it hold at least this level on the project this route names? | **403** |
| *(and)* | Is it within its rate limit? | **429** |

401 means re-authenticating might help; 403 means it would not. RFC 9110 draws exactly
that line, and so does this API.

### Scope levels

`read` < `write` < `admin`, each implying the ones before it.

| Level | Grants |
|---|---|
| `read` | `/v1/projects`, `search`, `doc`, `related`, `overview` |
| `write` | + `POST /v1/{project}/notes`, `PATCH /v1/{project}/notes` |
| `admin` | + `POST /v1/{project}/reindex`, `POST /v1/projects` |

Scope is checked **router-wide** — a new route is authenticated by default — plus a
per-route `require_scope(level)` declaration.

---

## Endpoints

| Method | Path | Scope | Purpose |
|---|---|---|---|
| `GET` | [`/healthz`](#get-healthz) | *none* | Liveness. |
| `GET` | [`/metrics`](#get-metrics) | read | Prometheus exposition. |
| `GET` | [`/v1/projects`](#get-v1projects) | read | List projects the key can reach. |
| `POST` | [`/v1/projects`](#post-v1projects) | admin | **501** — deliberately deferred. |
| `POST` | [`/v1/{project}/search`](#post-v1projectsearch) | read | Hybrid search → cited snippets. |
| `GET` | [`/v1/{project}/doc`](#get-v1projectdoc) | read | Read a slice of one note. |
| `GET` | [`/v1/{project}/related`](#get-v1projectrelated) | read | Graph neighbourhood. |
| `GET` | [`/v1/{project}/overview`](#get-v1projectoverview) | read | Project shape. |
| `POST` | [`/v1/{project}/notes`](#post-v1projectnotes) | write | Create a note. |
| `PATCH` | [`/v1/{project}/notes`](#patch-v1projectnotes) | write | Append a section, or patch frontmatter. |
| `POST` | [`/v1/{project}/reindex`](#post-v1projectreindex) | admin | Reconcile one path, or the project. |

---

### `GET /healthz`

Liveness. **The one unauthenticated route, and deliberately contentless.**

```json
{ "status": "ok", "version": "0.0.0" }
```

It says nothing a stranger could use — no project ids, no vault paths, no counts. "Is this
process up" is the entire question a liveness probe asks, and answering more would make the
exemption a leak. The compose healthcheck is its intended caller.

---

### `GET /metrics`

Prometheus exposition of the **same registry** the standalone exporter serves. Requires a
key.

> Two front doors, one set of numbers. Prometheus scrapes the **keyless** exporter on port
> 9464, which is bound to the compose network and never published to the host — putting a
> credential in `prometheus.yml` to read numbers that carry no vault content is plumbing
> for nothing.

---

### `GET /v1/projects`

Lists the projects this server serves, **filtered to the calling key's scopes**.

```jsonc
[
  { "id": "mynotes", "name": "My notes", "notes": 412, "writable": true }
]
```

> The MCP twin is **not** filtered — stdio has no keys and inherits local-user trust for
> everything. REST is the first transport with a narrower caller, and returning every
> served project to an unscoped key would leak the existence and writability of tenants it
> cannot otherwise reach: the same "narrow, never widen" rule, applied at discovery instead
> of at search.

---

### `POST /v1/projects`

**`501 Not Implemented`, on purpose.**

```jsonc
{
  "type": "urn:vaultrag:problem:not-implemented",
  "title": "Not implemented",
  "status": 501,
  "detail": "registering a project over HTTP is not implemented — run `vaultrag project add <id> --vault <path>` and restart the server. …"
}
```

Registering a project is already possible and durable — `vaultrag project add` writes the
row every process reads at startup. What this route would add is **hot-adding a tenant to a
live process**: constructing an embedder, a writer and an ingest context inside a request,
then mutating the served set the tenancy check enforces against.

The *authorization* half is answered (this route requires admin scope, checked before the
handler runs). The *resource-lifecycle* half is not: who closes the embedder if the request
fails, what happens to an in-flight search when the served set changes underneath it.
Answering that badly would put a mutation path next to the tenancy boundary.

So it answers honestly and names the command that does the job, rather than 404-ing as
though the spec were wrong.

---

### `POST /v1/{project}/search`

Hybrid search → ranked, cited snippets. Identical in every respect to the MCP `search`
tool — same method, same defaults.

**Request:**

```jsonc
{
  "query": "why did we reject collection-per-project",
  "k": 10,                       // 1–100, optional
  "budget_tokens": 2000,         // ≥1, optional
  "mode": "snippets",            // "snippets" | "compact"
  "filters": {                   // optional; only ever narrows
    "tags": ["decision"],
    "paths": ["decisions/x.md"],
    "type": "note",
    "modified_after": 1750000000,
    "modified_before": 1760000000
  }
}
```

Unknown keys are **rejected** (`extra="forbid"`), so a misspelled filter fails loudly
instead of silently not filtering. There is no `project` field in `filters` — the path
carries it.

**Response** — `SearchResult`, exactly as [documented for MCP](mcp-tools.md#response--searchresult):
`snippets[]`, `related[]`, `total_tokens`, `truncated`.

Returns snippets, never a synthesized answer. Retrieved vault text is **data** — this
server never interprets it, and neither should the client.

```bash
curl -H "Authorization: Bearer $VAULTRAG_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"query":"how does chunking work","k":5}' \
     http://127.0.0.1:8000/v1/mynotes/search
```

---

### `GET /v1/{project}/doc`

Reads the **vault**, not the index.

| Query param | Type | Notes |
|---|---|---|
| `path` | `string` | **Required.** Vault-relative path as a citation gave it. |
| `heading` | `string` | Return just this heading's section. Matched leniently. |
| `line_start` | `int ≥1` | Start of a 1-based inclusive span. |
| `line_end` | `int ≥1` | End of the span. |

`line_start` and `line_end` are two parameters rather than one repeated one, because a GET
query string has no natural pair type and `?lines=3&lines=40` reads as a *set* rather than
an ordered span. Pass **both or neither** — one alone is a `400`.

**Response** — `OpenResult`: `path`, `title`, `heading`, `line_start`, `line_end`,
`total_lines`, `text`, `tokens`, `truncated`.

```bash
curl -H "Authorization: Bearer $VAULTRAG_KEY" \
  'http://127.0.0.1:8000/v1/mynotes/doc?path=decisions/x.md&heading=Rationale'
```

---

### `GET /v1/{project}/related`

| Query param | Type | Default |
|---|---|---|
| `path` | `string` | *required* |
| `hops` | `int` 1–2 | `1` |
| `limit` | `int` 1–200 | `50` |

**Pointers only** — paths and titles, never text. Follow one with `/doc` or `/search`.

**Response** — `RelatedResult`: `path`, `title`, `doc_id`, `hops`, `notes[]`,
`unresolved[]`, `truncated`.

---

### `GET /v1/{project}/overview`

No parameters. Returns the project's shape: note count, folders two levels deep, top tags —
**a map, never a file listing**, so the cost is flat in vault size.

**Response** — `OverviewResult`: `project`, `name`, `notes`, `folders[]`, `tags[]`,
`truncated`.

---

### `POST /v1/{project}/notes`

Create a note. **`201 Created`.**

```jsonc
{
  "title": "JWT refresh decision",
  "content": "We chose sliding-window refresh because …",
  "folder": "agent-inbox",      // optional; must be in the write allowlist
  "tags": ["decision", "auth"]  // optional
}
```

**Response** — `WriteResult`. The returned `hash` **is** the `base_hash` for a later
`PATCH`.

Every write-back guardrail applies unchanged, because they live in the writer this calls:
allowlisted roots, realpath containment, size and rate caps, `.ragignore`, the secret scan,
git auto-commit, and a synchronous reindex of that one file.

---

### `PATCH /v1/{project}/notes`

**One verb, two operations, discriminated by `op`.** Both are "modify an existing note";
splitting them into `/notes/append` and `/notes/frontmatter` would make the path describe
the implementation rather than the resource.

**Append a section** — additive, no lock:

```jsonc
{
  "op": "append_section",
  "path": "agent-inbox/running-log.md",
  "heading": "Findings",
  "content": "- Qdrant payload indexes are a no-op in `:memory:` mode."
}
```

**Patch frontmatter** — in-place, **locked**:

```jsonc
{
  "op": "patch_frontmatter",
  "path": "decisions/x.md",
  "updates": { "status": "accepted" },
  "base_hash": "b3a91f…"
}
```

A stale `base_hash` returns **409** carrying the live hash *and* up to 4 000 characters of
current content, so a client rebases without a second read.

**Response** — `WriteResult`.

---

### `POST /v1/{project}/reindex`

Two operations behind one endpoint, because they are the same intent at two scales.

```jsonc
{ "path": "notes/x.md" }      // single path — synchronous
{ }                            // whole project — background
{ "force": true }              // whole project, ignore hashes — the drift repair
```

| Status | Body | When |
|---|---|---|
| **200** | `IngestOutcomeResponse` — `{project, path, status, reason?, chunks}` | A `path` was given. `status` ∈ `indexed \| unchanged \| removed \| skipped \| failed`. |
| **202** | `ReindexAccepted` — `{project, scope:"full", force, started:true}` | Full reindex started in the background. |
| **409** | problem | A full reindex of this project is already running. |
| **403** | problem | This process has no ingest context (read-only deployment). |

**Single-flight**, because two concurrent reconciles of one vault race on the manifest and
the second has nothing to add. A full reindex is minutes of embedding, which is why it is a
background job with a 202 rather than a request holding a socket open.

---

## Errors — RFC 9457 problem details

Every error is `application/problem+json`:

```jsonc
{
  "type": "urn:vaultrag:problem:unknown-project",
  "title": "Unknown project",
  "status": 404,
  "detail": "unknown project 'typo' — served: mynotes, work",
  "instance": "/v1/typo/search"
}
```

| Status | `type` slug | Raised when |
|---|---|---|
| **400** | `invalid-request` | Malformed arguments the service rejects. |
| **401** | *(auth)* | No key, unknown key, or a revoked one. Carries `WWW-Authenticate`. |
| **403** | *(scope)* | Authenticated, but the key lacks the level on this project. |
| **403** | `refused` | Authenticated and scoped, but a guardrail declines — write allowlist, size cap, secret content. |
| **404** | `unknown-project` | The project is not served here. |
| **404** | `not-found` | The note/path does not exist. |
| **409** | `write-conflict` | `base_hash` mismatch. Body echoes the live hash and content. |
| **409** | `reindex-in-progress` | Single-flight collision. |
| **422** | `validation` | FastAPI request-model validation. |
| **429** | *(rate limit)* | This key, but not right now. |
| **501** | `not-implemented` | `POST /v1/projects`. |
| **503** | `backend-unavailable` | Qdrant or Ollama unreachable. |
| **500** | `internal` | Anything unmapped. |

Mapping is by `isinstance`, ordered most-specific first, so a future error subclass
inherits its parent's status rather than silently falling through to 500.

### 403 vs 404 on an unscoped project

A key with no entry for project `X` cannot, from the scopes map alone, tell "X exists, you
can't see it" from "X doesn't exist". The resolution is the more honest of the two
defensible answers:

- The scope check consults the **served set** first. A **real but unscoped** project →
  **403 `insufficient-scope`**.
- A **nonexistent** project falls through to the service's own **404 `unknown-project`**.

That is identical to what MCP already answers for the same typo, under a strictly weaker
trust model — so this leaks nothing MCP does not already leak to a caller who is by
definition more trusted.

---

## Network posture

`api_host` defaults to `127.0.0.1`, and the compose file publishes the port
**loopback-only**. Remote access is Tailscale or WireGuard, not an open port.

Binding wider is a decision that must be typed out:

```bash
vaultrag serve --host 0.0.0.0
```

Inside a container the loopback default would make the port unreachable even from the
compose network, so the compose file widens the bind **inside** the container and narrows
it again with the loopback-only publish. Both halves of that decision are written out in
`docker-compose.yml` rather than one being an invisible image default.

---

## Related reading

- [MCP tools](mcp-tools.md) — the same operations, agent-shaped
- [Security model](security.md) — keys, scopes, tenancy, write-back
- [CLI reference](cli-reference.md#vaultrag-serve) — the `serve` command
- [Operations](operations.md) — deploying the api role
