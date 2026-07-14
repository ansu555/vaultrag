# VaultRAG — implementation repo

This folder is the **code** for VaultRAG: a self-hosted, MCP-first RAG backend that
coding agents query instead of loading docs into context, and can write back into.

The **design and knowledge** for this system does **not** live here — it lives in an
Obsidian vault at `../Rag` (absolute: `/Users/anik/Desktop/Rag`). That vault is the
source of truth for *what to build and why*. This repo is the source of truth for the
*running code*. Keep them separate: the vault is a git repo of decisions; this is a git
repo of software.

> One-line mental model: **the vault decides, this repo executes, and the running index
> is a disposable cache derived from the vault.**

---

## 1. How to fetch info from the vault (read this before designing anything)

Almost every "how should X work?" question is already answered in `../Rag`. **Look there
first — do not re-derive a decision that already exists.**

### Until VaultRAG can index itself: grep the vault

VaultRAG is not yet running, so there is no `search` tool yet. Use ripgrep against the
vault directly:

```bash
# search the vault for a topic
rg -i "reranker" ../Rag --type md -l          # which notes mention it
rg -i -C2 "parent.child|parent swap" ../Rag   # with context

# read the canonical full design doc (long — grep it, don't always load it whole)
../Rag/_meta/vaultrag-architecture.md
```

The vault is organized so you can jump straight to the right note:

| You need… | Look in |
|---|---|
| The full canonical system design (single doc) | `../Rag/_meta/vaultrag-architecture.md` |
| A specific *decision* + its rationale ("why Qdrant?", "why not GraphRAG?") | `../Rag/decisions/*.md` |
| Stable technical shapes — DB schema, metadata payload, MCP tool signatures, REST API, docker-compose, tech stack | `../Rag/reference/*.md` |
| Per-subsystem design (chunking, retrieval, embedding, indexing, write-back…) | `../Rag/projects/vaultrag/*.md` |
| Tag taxonomy / component map | `../Rag/_meta/tags.md` |

Each subsystem in the architecture doc has a matching atomic note under
`projects/vaultrag/` and, where a real choice was made, a `decisions/` note. Prefer the
atomic note for a focused answer; fall back to the section in
`_meta/vaultrag-architecture.md` when you need the whole picture.

### Once VaultRAG indexes itself (dogfooding, ~end of P1)

The moment `search`/`open` work, register **this project's own design vault** as a
VaultRAG project and query it via MCP instead of grepping:

```bash
claude mcp add vaultrag -- uvx vaultrag mcp --project vaultrag-design
# then, from an agent session:
#   search("how does the heading-tree chunker merge tiny sections", project="vaultrag-design")
#   open("projects/vaultrag/chunking-strategy.md", heading="...")
```

Using the tool to build the tool is the best possible eval signal. When retrieval feels
bad while building, that is a real bug report — capture it (see §6, eval).

### The rule

- **Vault = source of truth.** If code and a vault decision disagree, the vault wins
  unless you're deliberately superseding it — and a superseded decision is a *new note in
  the vault*, not a silent code drift. Flag the divergence; don't just diverge.
- **Don't copy vault prose into code comments.** Cite the note path instead
  (e.g. `# see ../Rag/decisions/chunking-heading-tree-not-fixed-size.md`).

---

## 2. What we're building (orientation, not a spec — the vault is the spec)

Three deployable units, **one codebase**, one Docker image with a `ROLE` switch:

```
Obsidian vault(s)  →  INDEXER (watch→debounce→parse→chunk→embed→upsert)
                          │
                          ▼
                 Qdrant (dense HNSW + BM25 sparse, one collection, tenant-partitioned)
                 SQLite→Postgres (documents, chunks, links/graph, tags, jobs, api_keys, query_log)
                          │
                          ▼
                 QUERY SERVICE (FastAPI + FastMCP)
                 expand?→hybrid→filter→fuse→rerank→parent-swap→graph-expand→compress→cite
                          │
                          ▼
                 MCP (stdio + http)  ·  REST /v1  ·  write-back → git auto-commit → reindex
```

**Non-negotiable design principles** (full list: architecture doc §1):

1. **Vault = source of truth; every index/vector/edge is a derived cache.** DR = reindex.
2. **Local-first, $0 software.** Paid APIs (Voyage, Cohere) are optional plugins behind
   interfaces, never dependencies.
3. **MCP is the primary interface;** REST/CLI/SDK are thin wrappers over one core.
4. **Return snippets, not answers, by default.** The consuming agent is the LLM.
   `answer` mode is opt-in.
5. **Hybrid always.** BM25 + dense + reranker. Dev notes are full of identifiers and error
   codes — dense-only misses exact matches.
6. **Tenancy enforced server-side.** The `project_id` filter is injected from the API key
   scope; a client filter can narrow, never widen.
7. **Token frugality is a feature.** Every response respects `budget_tokens`.

### Default stack (do not re-litigate without a vault decision)

| Layer | Pick | Note |
|---|---|---|
| Vector + keyword | **Qdrant** (Docker) | one collection, named vectors `dense`+`bm25`, int8, payload-partitioned tenancy |
| Dense embeddings | **Qwen3-Embedding-0.6B** via Ollama or ONNX int8 | 1024d, Matryoshka-truncatable to 512; behind an `Embedder` interface |
| Sparse | **BM25 via FastEmbed → Qdrant sparse vectors** | zero extra infra |
| Reranker | **bge-reranker-v2-m3**, ONNX int8 | biggest accuracy lever |
| Metadata + graph | **SQLite (WAL)** → Postgres 16 | wikilink graph = `links` table + recursive CTEs |
| Markdown | **markdown-it-py + python-frontmatter + custom Obsidian rules** | wikilinks/embeds/callouts/dataview |
| PDF | **Docling** | markdown + page anchors |
| Jobs/queue | **in-process asyncio + SQLite `jobs` table** | idempotent by `(project, path, hash)` |
| File watch | **watchdog + BLAKE3 hashes + manifest diff** | rename = hash match → metadata-only |
| API / MCP | **FastAPI + FastMCP 2.x** (Python) | one image serves REST + MCP (stdio & http) |
| Deploy | **Docker Compose, one box** | ~3–4 GB RAM at 100k chunks |

**Rejected up front** (see `../Rag/decisions/`): LangChain/LlamaIndex as the framework,
Elasticsearch for BM25, collection-per-project, full GraphRAG, fine-tuned embeddings.

---

## 3. Repo layout (proposed — scaffold as you build P0)

Nothing is built yet. Target structure, mirroring the role-based single image:

```
vaultrag/
├── CLAUDE.md              # this file
├── MEMORY.md              # running progress log — UPDATE IT (see §7)
├── pyproject.toml         # uv-managed; entrypoint `vaultrag` → src/vaultrag/cli.py
├── docker-compose.yml     # qdrant + vaultrag-api + vaultrag-indexer (ROLE switch)
├── .ragignore             # example ignore file shipped for vaults
├── src/vaultrag/
│   ├── config.py          # settings (env-driven)
│   ├── cli.py             # `vaultrag mcp|reindex|eval|serve …`
│   ├── models.py          # Document{blocks, metadata, links}, Chunk, …
│   ├── parsing/           # frontmatter, wikilinks, embeds, callouts, dataview, code, pdf
│   ├── chunking/          # heading-tree chunker, parent/child, contextual prefix
│   ├── embedding/         # Embedder interface, ollama/onnx impls, (model,dims,hash) cache
│   ├── index/             # qdrant wrapper: collection, upsert, delete-by-doc, hybrid query
│   ├── store/             # sqlite/postgres DDL, jobs, links, tags, query_log, api_keys
│   ├── indexer/           # watchdog→debounce→manifest diff→job runner (worker pool)
│   ├── retrieval/         # hybrid→fuse(RRF)→rerank→parent-swap→graph→compress→cite
│   ├── api/               # FastAPI /v1 routes
│   ├── mcp/               # FastMCP tools (search/open/related/overview/write_note/…)
│   └── writeback/         # note writer, realpath containment, base_hash lock, git commit
├── tests/
│   └── eval/              # golden set + recall@k / MRR / nDCG harness (CI gate)
└── state/                 # sqlite + embed cache (gitignored)
```

Confirm exact shapes against `../Rag/reference/` before writing:
`reference-database-schema.md`, `reference-metadata-schema.md`, `reference-mcp-tools.md`,
`reference-rest-api.md`, `reference-technology-stack.md`, `reference-docker-compose.md`.

---

## 4. Build roadmap — ship in order

From architecture doc §26. **Do not start P2 before P0 works end-to-end** — retrieval
quality problems only appear under real agent traffic.

| Phase | Scope | Done when |
|---|---|---|
| **P0** (weekend) | vault scan → md parse → heading chunker → Qwen3 embed → Qdrant hybrid → FastMCP `search`/`open` → `claude mcp add` | usable from Claude Code |
| **P1** | watchdog + manifest/renames, reranker, parent-child, citations w/ `obsidian://` links, `write_note` + git auto-commit | daily-driver quality, memory loop closed |
| **P2** | Docling PDFs, Excalidraw/Mermaid extraction, OCR, `related`/graph expand, eval harness + CI gate, Prometheus | full-format coverage, measurable quality |
| **P3** | multi-project keys + tenancy hardening, compression/budget packer, query-log mining, optional `answer` mode, compose profiles (Grafana/Langfuse) | production posture |

**Current phase: P0 — nothing built yet.** See `MEMORY.md` for exactly where we are.

---

## 5. Coding conventions

- **Python 3.12+, managed with `uv`.** `uv add <pkg>`, `uv run vaultrag …`. No global pip.
- **Type everything.** Full type hints; run `mypy` (or `pyright`). Pydantic models for the
  `Document`/`Chunk`/config/API-schema boundary.
- **Async-first** for I/O (FastAPI, qdrant-client async, the job runner). Keep CPU-bound
  work (embedding, rerank) off the event loop (thread/process pool).
- **Interfaces over vendors.** `Embedder`, `Reranker`, `VectorStore`, `MetaStore` are
  protocols; Ollama/ONNX/Qdrant/SQLite are implementations. A paid API must be a config
  change, never a code change (principle #2).
- **Idempotency is a correctness requirement, not a nicety.** Jobs keyed `(project, path,
  hash)`; point IDs `uuid5(project, path, chunk_ix)`; embed cache keyed `(model, dims,
  chunk_hash)`. Re-running any step must not duplicate or corrupt.
- **Structured logging** (structlog JSON) — per-stage timings, `project`, `q_hash`. Never
  log raw query text at info level, never log secrets.
- **Lint/format:** `ruff` (lint + format). Keep it clean; CI should fail on lint errors.
- Match the style of surrounding code once it exists; this file describes intent, not a
  cage.

---

## 6. Guardrails (violating these is a bug, even if tests pass)

- **The index is derived; the vault is truth.** Never make the running index the only
  place some fact lives. `vaultrag reindex` from a bare vault must fully rebuild.
- **Secrets never enter the index.** Enforce `.ragignore` **and** built-in deny patterns
  (`.env*`, `id_rsa*`, `*.pem`, `*credentials*`, high-entropy key regex) at parse time. A
  RAG that memorizes API keys hands them to every agent — treat this as a first-class
  threat. Add a test that a planted fake secret is never indexed.
- **Tenancy is server-side.** The server always injects `must project_id ∈ key.scopes`.
  A client-supplied filter can only narrow. There is no code path where a client widens
  scope.
- **Write-back safety:** default write root is the vault's `agent-inbox/`; realpath must
  stay under the vault root (block path traversal); edits require `base_hash` (409 on
  mismatch, return current content); size + rate caps; `.ragignore`d paths are unwritable;
  every write auto-commits `agent(<name>): <what>` and triggers a synchronous reindex of
  that one file.
- **Retrieved vault text is untrusted data.** In `snippets` mode VaultRAG never executes
  or interprets it. `answer` mode wraps sources in delimited blocks.
- **Quality is CI-gated.** Any change to chunking params, embedding model, fusion weights,
  or rerank config must run the eval harness (recall@10 ≥ 0.9, MRR@5 ≥ 0.75 on the golden
  set). Regressions block merge. This is what makes model swaps safe instead of vibes.

---

## 7. Keep MEMORY.md current — this is how sessions hand off

`MEMORY.md` in this folder is the running log of what's been done and what's next. It is
the first thing a new agent session (or Anik) should read after this file.

**At the end of any working session, update `MEMORY.md`:**
- Bump the **Current state** block (phase, what works, what's next).
- Prepend a dated entry to the **Session log**: what changed, why, key files touched, and
  anything the next session needs to know (blockers, open questions, decisions made).
- If a design decision was made that isn't in the vault yet, note it here **and** flag that
  it should be written back to `../Rag/agent-inbox/` (or `decisions/` if Anik approves) —
  the vault, not MEMORY.md, is where durable decisions live.

Keep MEMORY.md a *log*, not a design doc. Deep rationale belongs in the vault.

---

## 8. Dev workflow / commands (fill in as the CLI lands)

```bash
uv sync                          # install deps
docker compose up -d qdrant      # local vector store
uv run vaultrag reindex          # (P0+) build the index from a vault
uv run vaultrag mcp --project X  # (P0+) serve MCP over stdio
uv run vaultrag serve            # (P0+) FastAPI + MCP over http
uv run pytest                    # unit tests
uv run pytest tests/eval         # (P2+) retrieval quality gate
ruff check . && mypy src         # lint + types
```

Commit style, since agents write here too: `agent(claude-code): <what changed>` for agent
commits; human edits commit normally. Branch before committing on `main`; commit/push only
when asked.
