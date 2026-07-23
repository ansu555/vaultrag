# MCP tools

The agent-facing surface. This is what Claude Code actually sees.

```bash
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp
```

Server name: `vaultrag`. Transport: **stdio** (also available over HTTP via
`vaultrag serve`).

---

## Which tools appear

Registration follows **capability**, not configuration guesswork. No capability, no tool —
rather than a tool that answers zero for everything.

| Tool | Registered when |
|---|---|
| `search` | Always. |
| `open` | Always. |
| `list_projects` | Always — even on a single-project server, because it is how an agent learns the `project` string to pass. |
| `overview` | The process holds a metadata store. |
| `related` | The process holds a link graph. |
| `write_note`, `append_section`, `patch_frontmatter` | **Any** served project is writable. A call naming a read-only project is refused per-project. |

Hiding the write tools whenever a *single* tenant is read-only would take the memory loop
away from the projects that do have it — so the check is per-call, not per-server.

## The `project` argument

Every tool takes `project`. It is:

- **optional** on a single-project server (and may then only name *that* project);
- **required** when the server holds several.

It **never widens scope**: a project this server does not serve is refused. A real but
unscoped project answers "insufficient scope"; a nonexistent one answers "unknown project".

## Server instructions

The server ships instructions the client sees on connect:

> Cited-snippet retrieval over *\<the vault(s)\>*. Before designing, debugging, or
> answering questions about this project, call `search` first with one focused query —
> prefer that over reading many files. Follow a citation with `open` (its `path`, plus its
> `anchor` as `heading`) when the snippet alone is not enough context, or with `related`
> (its `path`) to see what the vault links to it.

---

## `search`

**Search the project's notes; returns ranked snippets cited by path + heading.**

Call this **first** when you need project knowledge — decisions, designs, fixes, error
strings.

### Parameters

| Name | Type | Default | Description |
|---|---|---|---|
| `query` | `str` | *required* | Natural-language question **or** exact identifier/error string. Search is hybrid, so verbatim tokens like `ERR_CONN_RESET` or `docker-compose.prod.yml` rank as well as paraphrases. **One focused query beats several vague ones.** |
| `project` | `str?` | — | Which project/vault to search. |
| `k` | `int?` (1–100) | `10` | Max snippets to return. |
| `filters` | `object?` | — | Narrow by tags / paths / type / modified-time. **Only ever narrows.** |
| `budget_tokens` | `int?` (≥1) | `2000` | Cap on total response tokens. The top hit is always kept even if it alone exceeds the budget. |
| `mode` | `"snippets"` \| `"compact"` | `"snippets"` | See [modes](#response-modes). |

### `filters` object

| Field | Type | Semantics |
|---|---|---|
| `tags` | `string[]` | Keep chunks tagged with **any** of these. Nested tags expand, so `project` also matches `project/vaultrag`. |
| `paths` | `string[]` | Keep chunks from **any** of these exact vault-relative paths. |
| `type` | `string` | Exact document type, e.g. `note`. |
| `modified_after` | `int` | Epoch seconds, inclusive. |
| `modified_before` | `int` | Epoch seconds, exclusive. |

There is deliberately **no `project` field** here, and unknown keys are **rejected** — a
misspelled filter fails loudly instead of silently not filtering.

### Response — `SearchResult`

```jsonc
{
  "query": "how does the chunker merge tiny sections",
  "project_id": "vaultrag-design",
  "snippets": [
    {
      "path": "projects/vaultrag/chunking-strategy.md",
      "title": "Chunking strategy",
      "heading_path": ["Chunking strategy", "Sizing"],
      "anchor": "Sizing",
      "line_start": 42,
      "line_end": 71,
      "page": null,                       // PDFs only
      "text": "…the actual chunk text…",
      "score": 0.0163,
      "tokens": 287,
      "uri": "obsidian://open?vault=Rag&file=projects%2F…",
      "doc_id": "…",
      "chunk_id": "…"
    }
  ],
  "related": [                            // graph pointers — cost NO budget_tokens
    { "path": "decisions/chunking-heading-tree-not-fixed-size.md",
      "title": "Heading-tree chunking…", "hops": 1, "uri": "obsidian://…" }
  ],
  "total_tokens": 1287,
  "truncated": false
}
```

| Field | Meaning |
|---|---|
| `snippets[].heading_path` | The H1 ▸ H2 trail. This is the citation. |
| `snippets[].anchor` | Pass this straight to `open(heading=…)`. |
| `snippets[].uri` | Clickable `obsidian://` link. `null` when no vault display name is configured. |
| `related[]` | 1-hop link neighbours of the top hits. **Titles and paths only, never text** — so a pointer can never displace a result. |
| `truncated` | Lower-ranked hits existed but were dropped for `budget_tokens`. |

### Response modes

| Mode | Returns | Use when |
|---|---|---|
| `snippets` | Full cited chunk text. | You are the LLM meant to read it. **Default.** |
| `compact` | One preview line per hit. | Cheap browsing — then follow up with `open()` or a `snippets` search. |

`answer` mode (LLM-synthesized) is opt-in in the design and **not served**.

### Usage patterns

```
✅  search("why did we reject collection-per-project")
✅  search("MERIDIAN_DEDUPE_TTL_SECONDS")          # bare identifier — BM25 finds it
✅  search("auth", filters={"tags":["decision"]})   # narrow to decision notes
✅  search("deploy steps", mode="compact", k=20)    # browse cheaply, then open()

❌  search("tell me everything about the project")  # too vague; hybrid needs a target
❌  three separate vague searches                   # one focused query beats several
```

---

## `open`

**Read an exact slice of one note straight from the vault (source of truth).**

Use it to follow a search citation when the snippet isn't enough.

| Name | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | *required* | Vault-relative path exactly as a citation gave it. |
| `project` | `str?` | — | Pass the same project you searched, or the path will not resolve. |
| `heading` | `str?` | — | Return just this heading's section. Matching is **forgiving**: case-insensitive, and `quick-start` matches `Quick Start`. Pass a snippet's `anchor` here. |
| `lines` | `[int, int]?` | — | 1-based inclusive span, for paging. **Mutually exclusive with `heading`.** |

Omit both for the whole note. Very large notes come back with `truncated: true` — narrow
with `heading` or `lines` then.

### Response — `OpenResult`

```jsonc
{
  "path": "projects/vaultrag/chunking-strategy.md",
  "title": "Chunking strategy",
  "heading": "Sizing",          // the resolved heading, when one was requested
  "line_start": 42,
  "line_end": 71,
  "total_lines": 210,           // how far the note goes — use it to page
  "text": "## Sizing\n…",
  "tokens": 287,
  "truncated": false
}
```

A heading section is the heading line through the line **before** the next
same-or-higher-level heading.

```
search("chunk sizing")
  → snippet{ path: "…/chunking-strategy.md", anchor: "Sizing" }
open(path="…/chunking-strategy.md", heading="Sizing")
  → the whole section
```

---

## `related`

**Explore the note graph around one note: links, backlinks, and tag-siblings.**

Call this after a search hit to find what the vault itself connects to that note — cheaper
and more precise than another search, because it follows the **author's own wikilinks**
rather than guessing at similarity.

| Name | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | *required* | Vault-relative path as a citation gave it. |
| `project` | `str?` | — | Which project's graph. |
| `hops` | `int` (1–2) | `1` | 1 = direct links and backlinks. 2 also returns one further hop — broader, noisier, usually only worth it when 1 hop came back thin. |
| `limit` | `int` (1–200) | `50` | Max related notes. |

### Response — `RelatedResult`

```jsonc
{
  "path": "projects/vaultrag/chunking-strategy.md",
  "title": "Chunking strategy",
  "doc_id": "…",
  "hops": 1,
  "notes": [
    { "path": "decisions/chunking-heading-tree-not-fixed-size.md",
      "title": "Heading-tree chunking…", "relation": "backlink", "hops": 1 },
    { "path": "projects/vaultrag/retrieval-pipeline.md",
      "title": "Retrieval pipeline", "relation": "links_to", "hops": 1 },
    { "path": "projects/vaultrag/embedding-pipeline.md",
      "title": "Embedding pipeline", "relation": "tag_sibling", "shared_tags": 3 }
  ],
  "unresolved": ["Some Note That Does Not Exist"],
  "truncated": false
}
```

| `relation` | Meaning |
|---|---|
| `links_to` | The note points at it. |
| `backlink` | It points at the note. |
| `neighbor` | Two hops away. |
| `tag_sibling` | No link, shared tags. **The weakest signal**, so only the few strongest are returned. |

`unresolved` lists wikilinks in the note that point at nothing indexed — a broken link, or
a note not written yet. That is genuinely useful information about the vault.

**Paths and titles only, never text**, so it costs no token budget. Follow anything
interesting with `open()` or `search()`.

> Tag siblings are capped at 5 by default (`graph_max_tag_siblings`). Dogfooding found a
> call returning 49 entries — 1 link, 2 backlinks, and **46 tag siblings** — because tags
> are prefix-expanded and one shared `project/…` namespace makes every note a weak sibling
> of every other. Raise the cap on a vault with a flat, meaningful tag vocabulary.

---

## `overview`

**Orient yourself in a vault: how many notes, which folders, which tags.**

Call this **once at the start of a session on an unfamiliar project**, instead of listing
files.

| Name | Type | Default |
|---|---|---|
| `project` | `str?` | — |

### Response — `OverviewResult`

```jsonc
{
  "project": "vaultrag-design",
  "name": "VaultRAG design vault",
  "notes": 67,
  "folders": [
    { "path": "projects/vaultrag", "notes": 23 },
    { "path": "decisions", "notes": 13 },
    { "path": "reference", "notes": 7 }
  ],
  "tags": [
    { "tag": "project/vaultrag", "notes": 40 },
    { "tag": "decision", "notes": 13 }
  ],
  "truncated": false
}
```

It returns the vault's **shape** — folders two levels deep and tags, each with a note count
— **not a file listing**. So it stays small however large the vault is; the cost is flat in
vault size.

Use what it returns as `search` filters: a folder path narrows by `filters.paths`, a tag by
`filters.tags`.

---

## `list_projects`

**List the projects (vaults) this server can search — start here if unsure.**

No parameters.

```jsonc
[
  { "id": "vaultrag-design", "name": "VaultRAG design vault", "notes": 67, "writable": true },
  { "id": "work",            "name": "Work notes",            "notes": 412, "writable": false }
]
```

| Field | Meaning |
|---|---|
| `id` | What every other tool's `project` argument must be. |
| `notes` | Indexed document count. `null` if this server keeps no metadata store. |
| `writable` | Whether the write tools accept this project. |

> **Over MCP this is unfiltered; over REST it is filtered to the calling key's scopes.**
> MCP has no keys to filter by — stdio inherits local-user trust for everything. REST is
> the first transport with a narrower caller, and listing every served project to an
> unscoped key would leak the existence and writability of tenants it cannot reach.

---

## Write tools — the memory loop

Registered when at least one served project is writable. This is the line that turns the
index into long-term memory: an agent persists what it learned, and a future session can
`search` it.

Every write runs the same gauntlet — containment, allowlist, index gates, markdown-only,
size and rate caps, optimistic lock — then writes, **git-commits**, and **synchronously
reindexes that one file** so it is searchable in the same session. Full detail:
[security §write-back](security.md#write-back-safety).

### `write_note`

**Create a new note in the vault (defaults to `agent-inbox/`), committed + indexed.**

| Name | Type | Default | Description |
|---|---|---|---|
| `title` | `str` | *required* | Becomes the H1/frontmatter title and the slugified filename (`JWT refresh decision` → `jwt-refresh-decision.md`). |
| `content` | `str` | *required* | Markdown body — **no frontmatter**, VaultRAG writes that. |
| `folder` | `str?` | write root | Vault-relative folder. Must be within the write allowlist. |
| `tags` | `string[]?` | — | Written to frontmatter. |
| `project` | `str?` | — | Which vault to write into. |

VaultRAG adds provenance frontmatter: `author`, `via: vaultrag`, `created`.

**Refuses to overwrite an existing note.** Pick a different title, or use
`append_section` / `patch_frontmatter`.

For agent memory, record **what / why / files-touched** so a future search retrieves it.

### `append_section`

**Append a Markdown block to a heading's section in an existing note, then reindex.**

| Name | Type | Description |
|---|---|---|
| `path` | `str` | Vault-relative path of the existing note. |
| `heading` | `str` | Heading whose section to append to. Matched leniently, like `open`'s `heading`. |
| `content` | `str` | The block to append. |
| `project` | `str?` | — |

Inserts at the section's end, before the next same-or-higher heading.

**Additive, so it needs no `base_hash`** — two concurrent appends both survive. Fails if
the heading isn't found, and the error **lists the note's real headings**.

Use it to grow a running note: a log, a checklist, a findings list.

### `patch_frontmatter`

**Set frontmatter keys on an existing note under an optimistic lock, then reindex.**

| Name | Type | Description |
|---|---|---|
| `path` | `str` | Vault-relative path. |
| `updates` | `object` | Keys to set/overwrite, e.g. `{"status":"accepted","tags":["decision"]}`. Body text untouched. |
| `base_hash` | `str` | **Required.** The note's current content hash — the `hash` a prior search/open/write returned. |
| `project` | `str?` | — |

**The only write tool that takes the lock**, because it mutates in place.

On a mismatch the call fails and the error **echoes the current hash and the current
content**, so the agent can rebase and retry without a second read:

```
'<path>' changed since base_hash was taken — no silent overwrite. Its current hash
is <hash>. Re-read, merge your change, and retry with base_hash=<hash>.
Current content:
---
…
```

### Response — `WriteResult`

```jsonc
{
  "path": "agent-inbox/jwt-refresh-decision.md",
  "hash": "b3…",              // the base_hash for a later patch
  "bytes_written": 1843,
  "committed": true,
  "commit": "a3f21c9",
  "indexed": true,            // the synchronous reindex succeeded → searchable NOW
  "chunks": 4,
  "uri": "obsidian://open?vault=Rag&file=agent-inbox%2F…",
  "warning": null
}
```

`committed: false` means a non-git vault or a skipped commit — **the write still landed**.
`indexed: false` with a `warning` means the vault write succeeded but the index lagged
(e.g. Qdrant down). The write is durable in the vault before indexing is attempted; there
is no path where a write is reported successful and isn't on disk.

---

## Errors

Every service error becomes a plain `ToolError` whose message is written **for the agent**
— it names what went wrong and what to do next, rather than leaking a stack trace.

| Situation | What the agent sees |
|---|---|
| Unknown project | "unknown project 'x'" + the served list. |
| Project not writable | Refusal naming the project. |
| Path outside the write allowlist | Refusal naming the allowed roots. |
| Heading not found (`append_section`) | Refusal **listing the note's real headings**. |
| `base_hash` mismatch | 409-shaped message with the current hash **and** current content. |
| Rate cap exceeded | Refusal; retry after the window clears. |
| Note contains secrets | Refused — unwritable **and** never indexed. |

---

## Registration variants

```bash
# every registered project (default)
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp

# exactly one project
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp --project mynotes

# read-only — no write tools registered at all
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp --no-write
```

Generic MCP client config:

```json
{
  "mcpServers": {
    "vaultrag": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/vaultrag", "vaultrag", "mcp"]
    }
  }
}
```

> **stdout is the JSON-RPC channel.** All logs go to stderr and the FastMCP banner is
> disabled. If you wrap this in a shell script, that script must not echo.

---

## Related reading

- [REST API](rest-api.md) — the same operations over HTTP
- [Retrieval pipeline](retrieval.md) — what `search` actually does
- [Security model](security.md) — tenancy and write-back guardrails
- [CLI reference](cli-reference.md#vaultrag-mcp) — the `mcp` command's flags
