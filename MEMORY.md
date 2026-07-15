# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** **P0 is CODE-COMPLETE.** Steps 0 (scaffold) + 1 (models) + 2 (store) + 3 (scan)
  + 4 (parsing) + 5 (chunking) + 6 (embedding) + 7 (Qdrant vector store) + 8 (reindex
  pipeline) + 9 (hybrid retrieval read path) + **10 (FastMCP `search`/`open` + `vaultrag
  mcp` — the P0 finish line) DONE.** Next up: **P1** (watchdog + manifest/renames,
  reranker, parent-child, citations w/ `obsidian://`, `write_note` + git auto-commit) —
  but first, the one P0 acceptance step still unrun: **dogfood via `claude mcp add`**
  (blocked on Docker, see Open questions).
- **What works:** the project builds and runs. `uv sync` installs; `uv run vaultrag reindex`
  builds a real index end-to-end (`--project`/`--vault` flags, env defaults otherwise);
  **`uv run vaultrag mcp --project X --vault Y` serves `search`+`open` over real stdio**
  (verified by a spawned-subprocess round-trip, not just in-memory tests); `ruff check .`,
  `ruff format --check .`, `mypy src` (33 files), and `pytest` (216 tests: 8 CLI + 7 models
  + 13 store + 13 scan + 34 parsing + 15 chunking + 22 embedding + 42 index + 7
  indexer-pipeline + 10 retrieval + 45 mcp) are all green. A full-code audit of Steps 0-9
  (2026-07-15) fixed two defects (frontmatter line-number drift; CLI store-construction
  failure paths); a second full audit of Steps 0-10 (2026-07-16, see session log) fixed
  nine more — including a **security-grade `open()` gate bypass** — and ratified the rest.
  Env-driven `config.py` (pydantic-settings, `VAULTRAG_` prefix) + structlog JSON
  logging wired. `models.py` holds the normalized `Document{metadata, blocks, links}` / `Block`
  / `Chunk` shape, the `Snippet`/`SearchResult` return contract, and deterministic
  `chunk_point_id` + `document_id`. `store/` has the `MetaStore` protocol + `SQLiteMetaStore`
  (WAL): P0 tables `projects, documents, chunks, jobs, embed_cache, tags`, content-addressed
  idempotent upserts, FK cascade delete, embed-cache keyed `(model, dims, chunk_hash)`.
  `indexer/` has the vault scan: `scan.py` (walk → BLAKE3 → `.ragignore` + secret guard →
  `diff_manifest` → `ScanDiff{creates,updates,deletes,renames,unchanged}`) and
  `secret_guard.py` (non-overridable filename + content secret blocking). `parsing/` turns one
  file's raw text into a `Document`: `markdown.py` (`parse_document`, markdown-it-py
  `"default"` preset tokenizer walk → source-ordered `Block`s; code/table/list/blockquote kept
  atomic; heading-trail `heading_path`; file-relative `line_start`/`line_end`), `obsidian.py`
  (wikilink/embed regex, inline-tag regex + nested `a/b` expansion, dataview `key:: value`,
  callout header parsing), `frontmatter.py` (YAML → typed fields, malformed-YAML degrades to
  no-frontmatter rather than raising). `chunking/` turns a `Document` into its ordered
  `Chunk` children: `chunker.py` (`chunk_document`: heading-path section grouping → greedy
  ~300-tok-target/512-hard-max packing at block granularity → single-pass tiny-sibling merge
  ≥120 tok → contextual prefix «Project ▸ folder/note ▸ H1 ▸ H2» + tags line → `prev_id`/
  `next_id` stitching via deterministic `chunk_point_id`), `tokens.py` (`count_tokens`: ~4
  chars/token heuristic — see decisions below). `embedding/` turns `Chunk.embed_text` into
  dense vectors behind the swappable `Embedder` protocol: `base.py` (`Embedder` protocol,
  `Vector`, `EmbeddingError`, float32 `serialize_vector`/`deserialize_vector` for the cache
  BLOB, `fit_dims` = Matryoshka truncate-to-`dims` + L2-normalize), `ollama.py`
  (`OllamaEmbedder`: sync httpx `POST /api/embed`, batch 64, dim/count validation, typed
  errors on unreachable-Ollama / model-not-found, injectable client for tests, `from_settings`),
  `cache.py` (`embed_chunks(embedder, store, chunks)` → cache-through on the Step 2
  `embed_cache` table keyed `(model, dims, chunk_hash)`, de-duped by hash, 0 model calls when
  all cached; `QueryEmbedder` = LRU over single-query embeds for the read path). `index/` is the
  vector store: `base.py` (`VectorStore` + `SparseEmbedder` protocols, `SparseVector`,
  `VectorStoreError`), `payload.py` (`chunk_payload` → the per-point payload, `EMBED_VER`,
  `select_frontmatter`), `sparse.py` (`Bm25Embedder`: FastEmbed `Qdrant/bm25`, lazy-loaded so
  import/construct never hits the network), `qdrant.py` (`QdrantVectorStore`: one `chunks`
  collection, named vectors `dense`(cosine, int8-quantized, on-disk originals, HNSW
  m=16/ef_construct=128) + sparse `bm25`(IDF modifier), payload indexes incl. `project_id`
  `is_tenant=true`; `ensure_collection`/`upsert_chunks`/`delete_by_doc`/`count`,
  `from_settings`). `docker-compose.yml` ships the Qdrant service. `indexer/pipeline.py` is
  the reindex orchestration (`reindex_vault`, `ReindexStats`): scans (Step 3) →
  creates/updates get parsed (Step 4) → chunked (Step 5) → embedded via `embed_chunks`
  (Step 6) → upserted to both stores (Step 7); renames are delete-at-old-path +
  ingest-at-new-path; deletes remove the doc + its points. Every (re)ingest deletes a
  document's existing chunks/points *before* writing fresh ones (no stranded tail on a
  shrunk note), and a document's `hash` column commits *last*, after chunks+vectors are
  fully upserted (a half-written doc keeps `hash=None`, which Step 3's `diff_manifest`
  already treats as "needs reprocessing" — so a crash mid-ingest self-heals on the next
  `reindex` instead of looking falsely done). One `jobs` row per changed path
  (`enqueue_job`/`set_job_state`); a bad file is parked `dead` and logged, never aborts the
  rest of the run — retry/backoff is explicitly left to the *live-watch* trigger (watchdog,
  P1), which doesn't exist yet; this is the boot-reconcile/manual trigger, run serially.
  `cli.py`'s `reindex` command wires `Settings` → `SQLiteMetaStore`/`QdrantVectorStore`/
  `OllamaEmbedder` `.from_settings(...)` → `reindex_vault`, prints a one-line summary, exits
  1 on `VectorStoreError`/`EmbeddingError`/`OSError` or if any file failed. `index/base.py`
  now also carries the Step 9 read-side shapes: `SearchFilters` (engine-agnostic
  tags/paths/type/modified-range narrowing — no `project_id` field, since a client can
  never set/widen tenancy) and `ScoredChunk` (point id + RRF score + payload), plus
  `VectorStore.query(...)` in the protocol. `QdrantVectorStore.query` runs one Query API
  call: dense `Prefetch` (k=`prefetch_limit`, default 100) + sparse `Prefetch` (same k,
  skipped entirely when the sparse vector has zero terms — a pure-stopword query still
  works dense-only) → `FusionQuery(fusion=RRF)` → top `limit`; both prefetch branches carry
  the *same* filter (`project_id` always-`must` + `_build_filter`'s translation of
  `SearchFilters`), never a post-fusion filter, so a narrow query can't be starved of
  candidates before fusion sees them. `retrieval/pipeline.py` (`search`, exported from
  `retrieval/__init__.py`) is the Step 9 orchestration: embeds the (stripped) query dense
  via `QueryEmbedder.embed_query` + sparse via `Bm25Embedder.embed_query` → one
  `vector_store.query(...)` call → `_to_snippet` builds each `Snippet` straight from the
  hit's payload (no SQLite round-trip — `text`/`heading_path`/`anchor`/`doc_id`/`tokens`
  already live there per the Step 7 payload divergence) → `_pack_budget` greedily keeps
  already-RRF-ranked hits under `budget_tokens`, **always keeping the top hit even if it
  alone exceeds budget** (an oversized best match beats an empty response), setting
  `truncated=True` iff a lower-ranked hit existed but didn't fit. An empty/whitespace query
  short-circuits to an empty `SearchResult` with zero embed calls. Rerank/parent-swap/graph-
  expand/MMR-compression (arch §10 stages 4-7) are explicitly P1+, not built here.
  `mcp/server.py` is Step 10: `build_mcp_server(project_id, vault_path, vector_store,
  embedder, sparse, default_k, default_budget_tokens) -> FastMCP` — a pure assembly
  function over already-built deps (the CLI's `mcp` command is the composition root), so
  every tool is testable through FastMCP's in-memory `Client` with fakes, no stdio or live
  services. Tools: **`search(query, project?, k?, filters?, budget_tokens?, mode?)`** wraps
  Step 9's `retrieval.search` (tenancy: `project` may only *name* the served project, else
  `ToolError` before any engine work; `SearchFiltersParam` is the pydantic MCP-boundary twin
  of `SearchFilters` with `extra="forbid"` and no `project` field; `mode="compact"` returns
  one-line previews; `mode="answer"` is P3 and not in the schema; engine failures become
  `ToolError`s that keep the actionable "is Qdrant running?" text; logs `q_hash` (blake3,
  12 chars) never raw query text). **`open(path, heading?, lines?)`** reads an exact slice
  straight from the vault (the source of truth, always ≥ as fresh as the index) and
  **enforces every gate indexing enforces** via the shared `exclusion_reason` predicate —
  containment (incl. symlink escape), always-pruned dirs, secret filenames, `.ragignore`,
  markdown-only — plus a whole-file `scan_for_secrets`; heading resolution goes through
  `parsing/sections.py` (`heading_section`/`normalize_heading`) so a `## fake heading`
  inside a code fence can never match and P1's write-back reuses one definition of a
  section's bounds. `cli.py`'s `mcp` command wires `Settings` → `QdrantVectorStore`/
  `OllamaEmbedder`/`QueryEmbedder`/`Bm25Embedder` → `build_mcp_server` →
  `run(transport="stdio", show_banner=False)`, exits 1 cleanly on a bad vault path or
  misconfig, closes both clients in `finally`. **`logging.py` writes to stderr only** (via a
  `_StderrProxy` resolving `sys.stderr` at write time) — stdout is the JSON-RPC channel.
  Deps added: `blake3`, `pathspec`, `markdown-it-py`, `python-frontmatter`, `httpx`,
  `qdrant-client`, `fastembed`, **`fastmcp>=3.4.4`**, (`types-pyyaml` dev-only).
- **What's next:** **P1** (`CLAUDE.md` §4): watchdog + manifest/renames, reranker,
  parent-child, citations w/ `obsidian://` links, `write_note` + git auto-commit. Before
  that, close the one P0 acceptance step that hasn't run (needs Docker — see Open
  questions): `docker compose up -d qdrant` → `uv run vaultrag reindex --project
  vaultrag-design --vault ../Rag` → `claude mcp add vaultrag -- uv run --directory
  /Users/anik/Desktop/vaultrag vaultrag mcp --project vaultrag-design --vault
  /Users/anik/Desktop/Rag` → from a Claude Code session, `search("how does the heading-tree
  chunker merge tiny sections")` should return cited snippets. That dogfood is also the best
  eval signal we have (repo `CLAUDE.md` §1): when retrieval feels bad while building P1,
  that's a real bug report — capture it.
- **Design source of truth:** `../Rag` (Obsidian vault). Grep it before designing; see
  `CLAUDE.md` §1.
- **Open questions / blockers:**
  - **BLOCKER (env, not code): Docker Desktop will not start on this box**, so the
    Qdrant-dependent dogfood above is the one P0 acceptance step never run. Symptom: the
    `com.docker.backend` process runs but never creates
    `~/.docker/run/docker.sock`, so every `docker` command hangs/fails; `docker context ls`
    shows `desktop-linux` pointing at that missing socket. Its stderr log's last entries are
    a burst of `FILE_ERROR_NO_SPACE` from 2026-07-15T12:00Z — the disk was full then, but
    now reads 117Gi free, so that's a stale cause, not the current one. Tried across three
    sessions: `pkill -f com.docker.backend` + `open -a Docker`, plus repeated polling — all
    failed (two-strike rule → stopped). **Anik: this needs a hands-on Docker Desktop restart
    (or reinstall) from the GUI.** Nothing in `src/` is implicated: Ollama is up (v0.32.0,
    `qwen3-embedding:0.6b`) and the MCP server itself is verified over real stdio without
    Qdrant. Everything Qdrant-side was verified live in the Step 7/8/9 sessions when Docker
    last worked.
  - `search`/`open` response JSON shapes are unspecified in
    `../Rag/reference/reference-mcp-tools.md` (params only) — Step 10 has now hardened the
    concrete contract in code (`SearchResult`/`Snippet`/**`OpenResult`**, `models.py`), and
    it is what the MCP tools actually return. **Ready to promote to `reference/`** — the
    shapes are no longer speculative.
  - **Vault write-backs pending Anik's review** (both in `../Rag/agent-inbox/`):
    - `payload-carries-chunk-text.md` (proposed) — the payload reference omits `text`, but
      Step 7 stores it and Step 9's `_to_snippet` depends on reading it directly from the
      payload (no SQLite round-trip), which makes this divergence load-bearing rather than
      theoretical. Ratify into `reference-metadata-schema.md` or correct it.
    - **`fastmcp-3x-not-2x.md` (new, proposed)** — `reference-technology-stack.md` says
      "FastMCP 2.x"; Step 10 shipped on **3.4.4** (what `uv add fastmcp` resolves to today).
      Verified empirically against 3.4.4: bare `@mcp.tool`, `run(transport="stdio",
      show_banner=False)`, in-memory `Client(server)` for tests, `ToolError` text reaching
      the client verbatim, and sync tools auto-running in anyio worker threads. Ratify the
      version bump or pin 2.x.

---

## How to write an entry

Prepend newest first. Template:

```
### YYYY-MM-DD — <short title>
- **Did:** <what changed>
- **Why:** <reason / which vault note it implements>
- **Files:** <key paths touched>
- **Next / handoff:** <what the next session should pick up; blockers; decisions made>
```

---

## Session log

### 2026-07-16 — P0 Step 10 (FastMCP `search`/`open` + `vaultrag mcp`) + full Steps 0-10 audit — **P0 CODE-COMPLETE**
- **Did:** Two things Anik asked for in one session: (1) implemented **Step 10**, the P0
  finish line; (2) audited the **entire** P0 codebase (Steps 0-10) and fixed everything
  substandard, then ran the suite.
- **Step 10 shipped:** `mcp/server.py` (`build_mcp_server` + `search`/`open` tools +
  `SearchFiltersParam`), `mcp/__init__.py`, `models.py` (+`OpenResult`),
  `parsing/sections.py` (new — `heading_section`/`normalize_heading`), `cli.py`'s `mcp`
  command wired to real stdio, `logging.py` switched to stderr-only. Details in **Current
  state → What works**. Tool descriptions are written *for models* (when to call, arg
  semantics, budget guidance) per `../Rag/decisions/mcp-first-not-rest-first.md`.
- **Audit method:** every `src/vaultrag` module read end-to-end against `CLAUDE.md` §5/§6 and
  the vault design notes, **plus** the `/code-review` skill at high effort (8 parallel finder
  angles — line-by-line, removed-behavior, cross-file, reuse, simplification, efficiency,
  altitude, conventions — then a verify pass triaging each candidate). Nine defects fixed,
  each with a regression test that **fails against the pre-fix code** (spot-verified by
  reverting the fix and re-running, e.g. the pruned-dir gate):
  1. **`open()` bypassed the always-pruned-dir gate — security-grade** (`mcp/server.py`).
     `_read_note` checked secret-filename + `.ragignore` + suffix, but **not**
     `_ALWAYS_PRUNE_DIRS`. So `open(".trash/deleted.md")`, `open(".obsidian/workspace.md")`,
     `open("node_modules/pkg/README.md")` **served files the indexer never scans** — real
     markdown, passing every other gate, that `search` can by construction never cite. `open`
     reads the vault directly, so any gate it doesn't repeat is a hole around the scanner
     (CLAUDE.md §6). **Fixed at the right altitude rather than by adding a fourth check:**
     extracted `exclusion_reason(rel_path, *, ignore) -> ExclusionReason | None` into
     `indexer/scan.py` as the single source of truth for path-level gates; `walk_vault` now
     filters with it (mapping reason → its stats counter) and `_read_note` refuses with it.
     The two can no longer drift apart. `ExclusionReason` is a `Literal`, so a *misspelled*
     key in the MCP layer's refusal-message map is a mypy error (verified by injecting one),
     and a *missing* one is caught by `test_open_has_a_refusal_message_for_every_indexer_gate`
     (mypy does not require dict literals to be exhaustive over a Literal — checked, not
     assumed).
  2. **`mode="compact"` applied `budget_tokens` to the full texts, before compaction**
     (`mcp/server.py`). A `k=10` browse where each chunk is 500 tokens against a 2000-token
     budget returned **4 previews instead of 10** — hits were dropped for the size of text
     compact mode was about to throw away. Fixed: compact retrieves with `_UNBOUNDED_BUDGET`
     and `_compact` re-packs the one-liners through search's own **now-public** `pack_budget`,
     so `budget_tokens`/`truncated` mean the same thing in both modes.
  3. **`open()` returned `title=None` in whole-note and `lines=` modes** (`mcp/server.py`) —
     only the `heading=` path parsed the document. `title` describes the *note*, not the
     slice. Fixed by parsing once up front for every mode (also removes a redundant parse).
  4. **`_norm_heading` didn't fold `_`** — `\W+` keeps underscores (a `\w` char), so
     `heading="Quick_Start"` never matched `## Quick Start`. Fixed to `[\W_]+` in the new
     `parsing/sections.py`.
  5. **`QdrantClient(url=...)` fired a blocking version-check GET from its constructor**
     (`index/qdrant.py`) — turning "construct a store" into "reach the network", stalling
     `vaultrag mcp` startup and printing a warning **to stdout, the JSON-RPC channel**, when
     Qdrant is down. Fixed with `check_compatibility=False`; this also makes `cli.py`'s
     "neither client connects at construction" comment true rather than aspirational.
  6. **`Bm25Embedder`'s lazy `_encoder` init raced under FastMCP's worker-thread pool**
     (`index/sparse.py`) — two concurrent first searches both see `None` and race two
     HuggingFace fetches into one cache dir. Fixed with a double-checked `threading.Lock`
     (steady state stays lock-free).
  7. **`_keep_frontmatter_value` recursed into lists** (`index/payload.py`), so
     `[[1,2],[3]]` passed despite the module contract saying "a flat list of scalars" —
     carrying nested structure no Qdrant filter can match onto every point in the note.
     Fixed to a flat-scalar check (per-element length rules now apply inside lists too).
  8. **`_apply_cap` hardcoded `cap_tokens * 4`** — a silent duplicate of `count_tokens`'
     heuristic that would drift the moment it's tuned. Fixed by publishing
     `CHARS_PER_TOKEN` from `chunking/tokens.py` and sharing it.
  9. **`QueryEmbedder`'s docstring claimed "not thread-safe by construction"**
     (`embedding/cache.py`) — false, and newly load-bearing now that the MCP server calls it
     from concurrent worker threads. `lru_cache` locks its own bookkeeping; the only race is
     two threads missing the same query, costing one duplicate call that stores an identical
     vector. Truthed up (a lock here would serialize every embed to prevent a harmless
     repeat).
- **Also cleaned up:** duplicated test doubles (`FakeEmbedder`/`FakeSparse`/`FakeVectorStore`/
  `make_hit`) were copy-pasted across `test_retrieval.py` and `test_mcp.py` → extracted to
  `tests/fakes.py` (two drifting copies of a protocol stand-in is how a suite goes green
  against an interface the real code no longer has). **`ruff format` had evidently never been
  run**: the code was hand-wrapped at ~88 chars while `pyproject.toml` declares
  `line-length = 100`, so the formatter touched 9 files. Kept — the repo now matches its own
  declared style (CLAUDE.md §5) — but it's why this diff is noisier than the fix list.
  `ruff format --check .` is now clean and worth keeping in CI next to `ruff check`.
- **Reviewed and deliberately NOT changed** (each considered, with a reason): eager BM25
  warm-up before stdio binds (rejected — costs offline resilience for a one-time first-call
  delay); per-call `.ragignore` reload (kept — freshness *is* the safety property: an
  exclusion the user just added must take effect now); the `_StderrProxy` (kept — the
  altitude angle endorsed it); `reindex`'s structured logs going to stdout (intended — it's a
  CLI, not a protocol server); `SearchFiltersParam` as a hand-maintained twin of
  `SearchFilters` (kept for its model-facing descriptions + `extra="forbid"`, but a
  field-parity tripwire test now fails if the engine gains a filter the MCP layer doesn't
  expose, or if a `project` field ever appears here). One candidate was **refuted** on
  inspection: "`Bm25Embedder` is never closed" — it has no `close()`.
- **Why:** Anik: *"pls audit the code which till p0 entirely… is it absolutely perfect the
  code implementation or not. if its not upto the standars then do take the nesscery steps.
  and after all of this run the test also. do all of this after implimenting step 10 and 11"*
  (the plan's steps are 0-indexed; "10 and 11" = plan Step 10, the last one).
- **Files:** `src/vaultrag/mcp/{server,__init__}.py` (new), `src/vaultrag/parsing/sections.py`
  (new), `tests/fakes.py` (new), `tests/test_mcp.py` (new, 45 tests), plus
  `src/vaultrag/{models,cli,logging}.py`, `src/vaultrag/indexer/{scan,__init__}.py`,
  `src/vaultrag/index/{qdrant,sparse,payload}.py`, `src/vaultrag/embedding/cache.py`,
  `src/vaultrag/chunking/tokens.py`, `src/vaultrag/retrieval/pipeline.py`,
  `src/vaultrag/parsing/__init__.py`, `tests/{test_cli,test_index,test_retrieval}.py`,
  `pyproject.toml`/`uv.lock` (+`fastmcp>=3.4.4`), `MEMORY.md`,
  `../Rag/agent-inbox/fastmcp-3x-not-2x.md` (new, vault).
- **Decisions (impl-level):** (1) **FastMCP 3.4.4, not the stack note's 2.x** — a real
  divergence, written back to `../Rag/agent-inbox/fastmcp-3x-not-2x.md` (proposed) rather
  than drifted silently (CLAUDE.md §1). Every 3.x API used was verified empirically, not
  assumed from training data. (2) **Tools are deliberately sync** — FastMCP runs them in a
  worker thread, which *is* the "keep blocking I/O off the event loop" pattern CLAUDE.md §5
  prescribes (probed against 3.4 to confirm, and it's what makes fix #6 necessary).
  (3) **`build_mcp_server` takes built dependencies; the CLI is the composition root** —
  which is what lets all 45 MCP tests drive the real server through FastMCP's in-memory
  client with fakes, no stdio and no live services. (4) **`heading_section` lives in
  `parsing/`, not `mcp/`** — "which lines belong to this heading" is a document-structure
  question, and P1's write-back (`append_section`) needs the identical definition of a
  section's bounds. (5) **`_OPEN_CAP_TOKENS = 8000` is a safety valve, not a budget** —
  principle #7 governs `search`; `open` is the explicit "give me the exact content" escape
  hatch and must not be starved, so it truncates generously with `truncated=True` +
  `total_lines` so the agent can re-slice.
- **Verify (all green):** `uv run pytest` **216 passed** (was 200 pre-audit, 168 pre-Step-10):
  8 CLI + 7 models + 13 store + 13 scan + 34 parsing + 15 chunking + 22 embedding + 42 index +
  7 indexer-pipeline + 10 retrieval + 45 mcp. `uv run ruff check .` clean, `ruff format
  --check .` clean, `uv run mypy src` clean (33 files).
- **Live verify — REAL STDIO ROUND-TRIP PASSED (the P0 "done when"):** spawned an actual
  `uv run vaultrag mcp --project smoke --vault <tmp>` **subprocess** and drove it over real
  stdin/stdout with a `StdioTransport` client (not the in-memory client the unit tests use):
  `list_tools()` → `['open', 'search']`; `open(path, heading="quick-start")` → resolved
  `heading='Quick Start'`, `title='Guide'`, correct body; `search` with Qdrant pointed at a
  dead port → `ToolError: search failed: … [Errno 61] Connection refused` (actionable, not a
  hang), **and the session kept serving afterwards**. This also proves the stderr-only
  logging fix: FastEmbed's HuggingFace download chatter, its tqdm bar, and every structlog
  line all went to stderr while stdout stayed clean JSON-RPC — any one of them on stdout
  would have broken the handshake outright.
- **Live verify — NOT DONE (blocked, env not code):** the Qdrant-dependent dogfood
  (`reindex ../Rag` as `vaultrag-design` → `claude mcp add` → search it from Claude Code).
  **Docker Desktop will not start on this box** — see **Open questions** for the diagnosis,
  what was tried, and the exact commands to finish it once Docker is back. Ollama is up.
- **Next / handoff:** **P0 is code-complete; P1 is next** (`CLAUDE.md` §4). Two things for
  Anik first: (a) restart Docker Desktop by hand, then run the dogfood commands in **Current
  state → What's next** — that's the last P0 acceptance step *and* the first real eval
  signal; (b) rule on the two `../Rag/agent-inbox/` notes (`payload-carries-chunk-text.md`,
  `fastmcp-3x-not-2x.md`). Also ready: promote the now-hardened `SearchResult`/`Snippet`/
  `OpenResult` shapes into `../Rag/reference/reference-mcp-tools.md` (it still specifies
  params only). **Nothing committed this session** — still on `main`, working tree dirty
  (branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — Full audit of Steps 0-9; two defects found and fixed
- **Did:** Read every `src/vaultrag` module end-to-end and audited against `CLAUDE.md` §5/§6
  and the vault design notes (chunk thresholds, filters-inside-prefetch, tenancy, secret
  guard, idempotency ordering all conform). Two real defects found, both fixed with
  regression tests:
  1. **Frontmatter line-number drift** (`parsing/frontmatter.py`): `split_frontmatter`
     returned python-frontmatter's `post.content` when a `---` block was present, but
     `frontmatter.parse` **always** `.strip()`s content — so blank lines between the
     closing `---` and the first content line (the most common Obsidian layout) were
     silently dropped, shifting every block's `line_start`/`line_end` early by that count.
     Confirmed empirically (heading reported line 4, actually 5). This corrupts the
     citation anchors `open(path, heading?, lines?)` (Step 10, `reference-mcp-tools.md`)
     depends on. The prior docstring claimed only the *no-frontmatter* path needed
     protecting — wrong. Fix: the body is now always sliced from `raw_text` at
     `frontmatter_line_offset` (`"".join(splitlines(keepends=True)[offset:])`), so body
     line *k* = file line *offset+k* by construction; python-frontmatter still parses the
     YAML. The pre-existing offset test passed by luck (no blank line after `---`); added
     `test_line_numbers_survive_blank_lines_after_frontmatter`.
  2. **CLI `reindex` construction/`sqlite3.Error` failure paths** (`cli.py`): the three
     stores were built *outside* the `try`, and `sqlite3.Error` wasn't in the except tuple
     — an unwritable `VAULTRAG_STATE_DIR` or unopenable db file tracebacked instead of the
     documented clean exit-1-with-message. Fix: construction moved inside the guarded
     region (`store`/`vector_store`/`embedder` start as `None`; `finally` closes only what
     was built), `sqlite3.Error` added to the catch (justified in a comment: the CLI is
     the composition root, the one place that knows the MetaStore is SQLite). Added
     `test_reindex_fails_gracefully_when_state_dir_is_a_file` and
     `..._when_db_is_unopenable` (db path is a directory → `sqlite3.OperationalError`).
- **Audited and deliberately NOT changed** (each checked, judged as-designed or not worth
  it at P0): payload carries `text` (already a flagged divergence pending Anik);
  embed-cache keyed on body hash so a prefix-only change (e.g. heading rename above an
  unchanged body) reuses the stale vector — ratified vault design, `EMBED_VER` is the
  recipe-level invalidation lever; `put_cached_vector` commits per vector and `scan.py`
  reads each file twice (secret scan + hash) — perf nits dominated by embedding cost;
  `config.py` retrieval defaults duplicated as `retrieval/pipeline.py` constants — Step 10
  wires `Settings` through and should collapse them; python-frontmatter accepts a
  `----` (4+ dash) boundary where `frontmatter_line_offset` doesn't — diverges only on
  that exotic form and fails safe (frontmatter indexed as body text, line numbers stay
  correct).
- **Why:** Anik asked for a Steps-0-9 audit before Step 10 (code was written by
  Sonnet + Anik). Both fixes protect Step 10's `open()` contract and the CLI's stated
  error contract.
- **Files:** `src/vaultrag/parsing/frontmatter.py` (body slicing), `src/vaultrag/cli.py`
  (guarded construction + `sqlite3.Error`), `tests/test_parsing.py` (+1),
  `tests/test_cli.py` (+2), `MEMORY.md`.
- **Verify (all green):** `uv run pytest` **168 passed** (was 165), `uv run ruff check .`
  clean, `uv run mypy src` clean (31 files). Line fix re-verified by direct probe: blank
  line after `---` → heading at 5/paragraph at 7 (was 4/6); two blank lines → 6 (was 4);
  no-frontmatter leading indented-code case still intact.
- **Next / handoff:** Unchanged — **Step 10 (FastMCP `search` + `open` + `vaultrag mcp`)**
  per Current state. `open()` can now trust `line_start`/`line_end`. Nothing committed
  this session (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 9: hybrid retrieval read path (`retrieval/`, `VectorStore.query`)
- **Did:** Completed Step 9. Extended `index/base.py` with the read side of the `VectorStore`
  protocol (write side landed at Step 7): `SearchFilters` (frozen dataclass — `tags`/`paths`
  any-of, `type` exact, `modified_after`/`modified_before` range; deliberately has no
  `project_id` field, since a client filter can only narrow tenancy, never set or widen it),
  `ScoredChunk` (`point_id`, RRF `score`, `payload`), and `VectorStore.query(project_id, dense,
  sparse, limit, prefetch_limit=100, filters=None) -> list[ScoredChunk]`. Implemented it in
  `index/qdrant.py`: one `client.query_points` call with a dense `Prefetch` and a sparse
  `Prefetch` (skipped when the sparse vector has zero terms, e.g. a pure-stopword query — dense
  alone still carries it rather than sending a doomed empty branch), both carrying the *same*
  filter (`_build_filter`: `project_id` always `must`, `SearchFilters` translated into more
  `must` clauses), and `query=FusionQuery(fusion=RRF)`. Added `retrieval/pipeline.py`
  (`search(query, *, project_id, vector_store, embedder, sparse, k=10, filters=None,
  budget_tokens=2000, prefetch_limit=100) -> SearchResult`, exported as `vaultrag.retrieval.search`):
  embeds the stripped query dense (`QueryEmbedder.embed_query`) + sparse
  (`Bm25Embedder.embed_query`) → one `vector_store.query(...)` call → `_to_snippet` builds each
  `Snippet` straight from the hit's payload → `_pack_budget` greedily packs the already-ranked
  hits into `budget_tokens`.
- **Why:** Step 9 of `../Rag/projects/vaultrag/p0-step-plan.md`
  (`../Rag/projects/vaultrag/hybrid-search.md`, `../Rag/projects/vaultrag/retrieval-pipeline.md`,
  arch §9-§11) — the read path every later MCP tool (Step 10) calls into. Filters applied inside
  both prefetch branches (not post-hoc) is a named requirement in `hybrid-search.md`: filtering
  only after an already-capped ANN search would starve a narrow query of candidates the search
  never returned in the first place.
- **Files:** `src/vaultrag/index/base.py` (`SearchFilters`, `ScoredChunk`, `VectorStore.query`
  added to the protocol), `src/vaultrag/index/qdrant.py` (`QdrantVectorStore.query`,
  `_build_filter`), `src/vaultrag/index/__init__.py` (exports), `src/vaultrag/retrieval/pipeline.py`
  (new), `src/vaultrag/retrieval/__init__.py` (now exports `search`/`SearchFilters`/the size
  constants instead of the Step-0 empty stub), `tests/test_index.py` (+11 query tests, new
  `WordHashSparse` fake + `query_store` fixture), `tests/test_retrieval.py` (new, 10 tests),
  `MEMORY.md`. No `pyproject.toml` changes — no new dependencies.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **`SearchFilters` is engine-agnostic and lives in `index/base.py`, not `retrieval/`.**
  It crosses the `VectorStore.query` boundary (a Qdrant-specific engine translates it), so it
  belongs next to `SparseVector`/`ScoredChunk` as a value object the protocol itself defines —
  `retrieval/` imports and re-exports it rather than defining its own copy, keeping one
  definition of "what a caller can narrow by." (2) **The sparse `Prefetch` branch is omitted
  entirely (not sent as an empty vector) when a query encodes to zero BM25 terms** — verified
  empirically (`test_query_with_no_sparse_terms_still_returns_dense_hits`, and a manual probe
  against `QdrantClient(":memory:")` confirming single-branch `FusionQuery(RRF)` degrades
  gracefully to the one branch's own rank order) rather than assumed. A pure-stopword or
  punctuation-only query is a real if rare agent input; this keeps it a normal dense-only search
  instead of a wasted round-trip or a fragile all-zero sparse vector. (3) **Both `Prefetch`
  branches receive the identical filter object** (`project_id` + `SearchFilters`, never applied
  only at the outer `query_points` level) — this is the literal fix for the "no k-starvation on
  filtered queries" requirement in `hybrid-search.md`; an outer-only filter would let the ANN
  prefetch (capped at `prefetch_limit`) return fewer candidates than expected before the filter
  ever ran. (4) **`_pack_budget` always keeps the top-ranked hit even if its `tokens` alone
  exceed `budget_tokens`**, only refusing hits *after* the first. The plan's "simple truncation
  for P0" is under-specified on this edge case; the alternative (returning zero snippets because
  the single best match didn't fit) would make `truncated=True, snippets=[]` look identical to
  "nothing relevant," which is a worse failure mode for an agent than one oversized-but-relevant
  citation. Locked in by `test_budget_packing_always_keeps_the_top_hit_even_if_oversized`.
  Revisit if P1's real compression stage (arch §13: near-dupe drop, MMR, per-doc cap) wants
  different edge-case behavior — this is explicitly the *simple* P0 stand-in for that stage, not
  a permanent policy. (5) **`retrieval.search` builds `Snippet`s straight from the Qdrant
  payload, never re-reading SQLite** — this is what makes the Step 7 `payload-carries-chunk-text`
  divergence load-bearing rather than a nice-to-have (see Open questions): the payload is the
  only place `text` lives at all, since the SQLite `chunks` table is structure-only. (6) Query
  embedding always goes through `query.strip()` first, and an empty/whitespace result
  short-circuits to an empty `SearchResult` **before** calling either embedder — an agent
  sending a blank query is a real (if degenerate) input, and embedding an empty string is pure
  waste, not a meaningful vector.
- **Verify (Step 9 gate, all green):** `uv run pytest` **165 passed** (4 CLI + 7 models + 13
  store + 13 scan + 33 parsing + 15 chunking + 22 embedding + 41 index + 7 indexer-pipeline + 10
  retrieval); `uv run ruff check .` clean; `uv run mypy src` clean (31 files). New `test_index.py`
  coverage runs `QdrantVectorStore.query` against a real `QdrantClient(":memory:")` with a new
  `WordHashSparse` fake (hashes whitespace tokens to stable indices — unlike the existing
  constant-vector `FakeSparse`, this actually distinguishes texts, which is what's needed to
  prove *fusion* rather than just plumbing): tenancy scoping (another project's points never
  returned), `limit` respected, **the plan's named verify criterion** — an exact-identifier query
  (`ERR_CONN_RESET`) ranks its chunk above a purely-irrelevant one via BM25 signal even though its
  dense vector is orthogonal to the query — all four `SearchFilters` dimensions narrowing
  independently (tags/paths/type/modified-range), dims-mismatch and unreachable-Qdrant raising,
  and the empty-sparse-vector dense-only path. `test_retrieval.py` isolates pure orchestration
  against a `FakeVectorStore` double (RRF fusion itself is Step 7's job, already covered above):
  `Snippet` field mapping from payload (including a payload missing optional keys defaulting
  safely), `project_id`/`filters`/`k`/`prefetch_limit` forwarded untouched to `vector_store.query`
  (including the documented defaults when not overridden), the stripped-query embedding calls,
  the empty-query short-circuit making zero embed calls, and all three budget-packing shapes
  (fits with no truncation; truncates a middle/tail hit; keeps an oversized top hit alone).
- **Live-integration: VERIFIED.** Both `docker compose`'s `qdrant` and Ollama's
  `qwen3-embedding:0.6b` were already up from the Step 7/8 sessions. Wrote a throwaway 3-note
  fixture vault (scratchpad only, never committed) — one note about connection pooling, one
  containing the literal string `ERR_CONN_RESET`, one an unrelated changelog entry — and ran the
  real `reindex_vault` (Step 8) into an isolated `chunks_live_verify_step9` collection under
  project `step9-live-verify` (dropped after; the real `chunks` collection was never touched).
  Then called the real `vaultrag.retrieval.search` (real Ollama dense embeddings, real Qdrant
  server, real FastEmbed BM25 — no fakes anywhere in this pass) and confirmed, against actual
  engine behavior rather than local-mode: **exact-identifier query `"ERR_CONN_RESET"` ranks the
  matching note first** (score 1.0 vs 0.33/0.25 for the others); **a paraphrase query** ("how do
  we reuse sockets to avoid repeated handshakes") **ranks the connection-pooling note first via
  dense** with no literal token overlap; a foreign `project_id` sees zero results (tenancy);
  `SearchFilters(tags=(...))` correctly narrows to just the matching note; `budget_tokens=1`
  still returns exactly one snippet with `truncated=True` (decision #4 confirmed live, not just
  in a fake). This closes the same category of gap Step 7's live session flagged — local mode's
  RRF/IDF aren't necessarily the real engine's, so Step 9 wasn't trustworthy until checked
  against a live server, and now has been.
- **Next / handoff:** Start **Step 10 — FastMCP `search` + `open` + `vaultrag mcp` (`mcp/`)** —
  the **P0 finish line**, per **Current state → What's next** above. `retrieval.search(...)` is
  the function the MCP `search` tool wraps directly (its `mode="snippets"` default *is* this
  function's return value; `compact`/`answer` modes and `filters`/`k`/`budget_tokens` param
  parsing from the MCP call are Step 10's job). `open(path, heading?, lines?)` is new work, not
  a wrapper — it reads a note's exact section from the vault (or an already-indexed chunk's
  `line_start`/`line_end`, TBD at Step 10) rather than the Qdrant index. Nothing committed this
  session (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 8: wire the indexing pipeline (`indexer/pipeline.py`, `vaultrag reindex`)
- **Did:** Completed Step 8. Added `indexer/pipeline.py`: `reindex_vault(project_id, vault_path,
  store, vector_store, embedder) -> ReindexStats` — registers the project, `ensure_collection()`,
  scans (Step 3) against `store.document_manifest(project)`, then for each create/update: parse
  (Step 4) → chunk (Step 5) → `embed_chunks` (Step 6) → delete the doc's existing chunks/points →
  reserve the document row (`hash=None`) → upsert fresh points + chunk rows → commit the real
  hash last. Renames are delete-at-`old_path` + ingest-at-`new_path` (the point id embeds the
  path, so a hash match at a new path can't be a metadata-only move here). Deletes remove the doc
  + its points. Every changed path gets one `jobs` row (`enqueue_job`/`set_job_state`); an
  exception during one file's ingest parks its job `dead`, logs, and lets the rest of the run
  continue — one bad note can't abort a whole-vault reindex. Wired `cli.py`'s `reindex` command:
  builds `SQLiteMetaStore`/`QdrantVectorStore`/`OllamaEmbedder` via `.from_settings(...)`, calls
  `reindex_vault`, prints a one-line stats summary, exits 1 on `VectorStoreError`/
  `EmbeddingError`/`OSError` or any per-file failure, closes all three clients in `finally`.
- **Why:** Step 8 of `../Rag/projects/vaultrag/p0-step-plan.md`
  (`../Rag/projects/vaultrag/ingestion-pipeline.md`, arch §4) — the orchestration that turns every
  prior stage (Steps 3-7, each independently built and tested) into the one thing P0 actually
  ships: a working `vaultrag reindex`.
- **Files:** `src/vaultrag/indexer/pipeline.py` (new), `src/vaultrag/indexer/__init__.py`
  (docstring only — see decision 3, `pipeline` is deliberately *not* re-exported), `src/vaultrag/cli.py`
  (`reindex` command wired), `tests/test_indexer_pipeline.py` (new, 7 tests), `tests/test_cli.py`
  (replaced the Step-8 stub test with a hermetic "services down → clean exit 1" test), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **A document's `hash` column commits last, after its chunks and vectors are fully
  upserted; every step before that reserves the row with `hash=None`.** This is what makes a
  crash mid-ingest safe: Step 3's `diff_manifest` already treats "present in manifest with a
  NULL hash" as an update (a comment written at Step 3, before Step 8 existed, that turned out to
  describe exactly this case) — so a half-written doc looks like it still needs reprocessing on
  the next `reindex`, instead of falsely looking done because its old hash (or a prematurely
  written new one) happened to match disk. Locked in by
  `test_partial_failure_reserves_hash_none_and_retries_next_run` (a `FlakyVectorStore` fails the
  first `upsert_chunks`, asserts `hash is None` after, then succeeds and commits on the next run).
  (2) **Every (re)ingest deletes a document's existing chunks/points before writing the new
  ones — for both stores, not just Qdrant.** The Step 7 handoff flagged this trap for the vector
  store (chunk/point ids are `uuid5(project, path, ix)`; upsert only overwrites the ids it
  writes, so a note edited *shorter* strands its old tail forever). The same trap exists in
  SQLite's `chunks` table via the identical id scheme — missed on first read-through, caught by
  writing `test_edit_shrinks_chunks_and_old_tail_is_not_stranded` before checking `store.get_chunk`
  on the stale id, not just `vector_store.count()`. (3) **`vaultrag.indexer.pipeline` is not
  re-exported from `indexer/__init__.py`.** First draft added `from vaultrag.indexer.pipeline
  import reindex_vault` to `__init__.py` for a tidy public surface, which created a real circular
  import: `pipeline` needs `parsing.markdown`, and `parsing.markdown` needs
  `indexer.secret_guard` — fine on its own, but importing any submodule of `indexer` always runs
  `indexer/__init__.py` first, so whichever of `vaultrag.parsing` / `vaultrag.indexer` a test
  happened to import first would fail (`tests/test_chunking.py`'s collection did, reproducibly).
  Fixed by leaving `__init__.py` exporting only `scan`/`secret_guard` as before and having callers
  `from vaultrag.indexer.pipeline import reindex_vault` directly (already how `cli.py` needed it).
  (4) **Renames are re-parsed, not fast-pathed as metadata-only**, even though content is
  unchanged (hash match). The ingestion-pipeline note's "rename = metadata-only, vectors
  untouched" describes a different id scheme than this repo actually built at Step 1/7
  (`uuid5(project, path, ix)` — path is part of the id), so a rename here necessarily gets new
  chunk/point ids; treating it as delete-old + ingest-new is what actually satisfies "re-embeds
  nothing" (the embed cache keys on body-hash, not path) while still being correct for the id
  scheme in hand. Confirms the trap #2 framing from the Step 7 handoff was right, just clarifies
  *why* a true metadata-only path isn't available. (5) **Retry/backoff is not implemented for
  this trigger.** `ingestion-pipeline.md` describes retries/backoff for the *live-watch* job
  runner (trigger #2, watchdog — P1, doesn't exist yet); `vaultrag reindex` is the boot-reconcile/
  manual trigger (#1/#3), which the same note describes only as "walk, diff, enqueue" — so a
  failed file is parked `dead` and picked up again whenever `reindex` next runs (naturally, since
  its hash still won't match disk), rather than building an in-process retry loop with sleep
  timers that nothing here can meaningfully test or that P1's real worker pool will duplicate.
- **Verify (Step 8 gate, all green):** `uv run pytest` **144 passed** (4 CLI + 7 models + 13
  store + 13 scan + 33 parsing + 15 chunking + 22 embedding + 30 index + 7 indexer-pipeline);
  `uv run ruff check .` clean; `uv run mypy src` clean (30 files). New tests run a real
  `SQLiteMetaStore` on a tmp-dir db against `QdrantClient(":memory:")` + a fake sparse encoder
  (Step 7 pattern) + a call-counting fake `Embedder` (Step 6 pattern), covering every plan
  criterion: full reindex creates docs+points; **re-run makes zero embedder calls and zero
  dupes** (nothing in the diff at all, since unchanged paths are skipped outright); delete a note
  → its doc row + points gone, other docs untouched; **edit a note shorter → its stale tail chunk
  row and vector point are both gone**, not just the live one; **rename → points move (count
  unchanged) with zero new embedder calls**, old path's doc/chunks gone, new path indexed; one
  bad file (embedder raises) doesn't stop a good file in the same run, and the bad file's document
  row was never even created; a vector-store failure mid-ingest leaves `hash=None` and is cleanly
  retried (and this time succeeds) on the next `reindex_vault` call.
- **Live-integration: VERIFIED.** Both `docker compose`'s `qdrant` and Ollama's
  `qwen3-embedding:0.6b` were already up from the Step 7 session. Ran `uv run vaultrag reindex`
  against a real throwaway fixture vault (scratchpad, never the design vault) with
  `VAULTRAG_PROJECT_ID=step8-live-verify` and `VAULTRAG_QDRANT_COLLECTION=chunks_live_verify_step8`
  (an isolated collection, dropped afterward — the real `chunks` collection was never touched):
  create (1 note, real Ollama embed, real Qdrant upsert, full 20-key payload incl. `text`
  confirmed via a real `scroll`) → re-run (0 new HTTP calls to either service, confirmed by
  grepping the run's own request log) → **grow to 3 chunks then shrink to 1 → real point count
  goes 3 → 1** (trap #1 confirmed against the real server, not just `:memory:`) → **rename → 0
  `POST /api/embed` calls, point count unchanged** (trap #2 confirmed live) → delete → count 0.
  Every stage of the CLI's actual wiring (`Settings` → real `SQLiteMetaStore`/
  `QdrantVectorStore`/`OllamaEmbedder` → `reindex_vault`) ran exactly as unit-tested, against the
  real services, not fakes.
- **Next / handoff:** Start **Step 9 — hybrid retrieval read path (`retrieval/`)**, per **Current
  state → What's next** above. `search`/`open`'s response shape (`Snippet`/`SearchResult`,
  authored at Step 1) is the target; the payload's `text` field (Step 7's flagged divergence,
  still pending Anik's ratification — `../Rag/agent-inbox/payload-carries-chunk-text.md`) is what
  Step 9 will actually read to build snippet bodies. Nothing new committed this session (still on
  `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — Step 7 live-integration gap closed: real Qdrant server + real Ollama
- **Did:** Anik installed Docker and Ollama on this box (a different machine than the prior
  session — `uv` itself was also missing here; installed via `pipx install uv`, then
  `uv sync` reproduced the exact same 137-pass/ruff-clean/mypy-clean gate). Started
  `docker compose up -d qdrant` (healthy, Qdrant 1.18.2 on `127.0.0.1:6333`) and
  `ollama pull qwen3-embedding:0.6b` (639 MB). Wrote a throwaway script (not committed —
  scratchpad only) that runs the same real-note pipeline the prior session ran against
  qdrant-client's local mode, but this time against the **real server**, into an isolated
  `chunks_live_verify` collection (never touches the real `chunks` collection), then deletes
  it after. Every check the prior session flagged as unprovable **passed**:
  - `HnswConfigDiff(m=16, ef_construct=128)` — read back from `get_collection()`, actually
    applied server-side (local mode silently accepts and ignores this).
  - int8 scalar quantization (`always_ram=True`) and `on_disk=True` originals — both applied,
    not just accepted.
  - Sparse `bm25` vector's IDF `Modifier` — present in the real collection config.
  - **Payload indexes are real, not no-ops** (local mode literally warns "Payload indexes
    have no effect in the local Qdrant" — this was the crux of what was unverified):
    `get_collection().payload_schema` on the real server lists all five
    (`project_id`, `tags`, `path`, `type`, `modified_ts`), and `project_id`'s `data_type`
    reads back as `keyword` (the tenant index is real).
  - **A real hybrid RRF query** (`client.query_points` with dense `Prefetch` + sparse
    `Prefetch` + `FusionQuery(fusion=Fusion.RRF)`, filtered by `project_id`) returns hits, and
    the top hit is the exact chunk queried verbatim — proof the IDF/RRF scoring is the
    engine's own, not something local mode fakes. (This is a hand-rolled query for
    verification only — Step 9 owns the real hybrid-query implementation.)
  - `ensure_collection` → `upsert_chunks` (2 real points, 1024-d Ollama vectors, real FastEmbed
    BM25) → `count()`/`count(project_id)` = 2 → re-upsert idempotent (still 2, no dupes) →
    `delete_by_doc` → 0, all against the real server.
- **Why:** Step 7 was code-complete and unit-tested but had an explicit, named gap: "no real
  Qdrant server has ever run" (see the Step 7 entry below). That gap is what CLAUDE.md §6's
  quality-gate spirit exists to close before trusting Step 9 (hybrid retrieval) on top of it —
  local mode's fusion/IDF scoring is admittedly not the real engine's, so nothing built on top
  of it was trustworthy until checked against the real thing.
- **Files:** `MEMORY.md` only. No `src/` changes — Step 7's code was already correct; this
  session only ran it against infrastructure that didn't exist before. The verification script
  lived in the session scratchpad, not the repo (it's a one-off probe, not a test — the
  project's actual test suite intentionally stays hermetic per the Step 7 entry's own
  reasoning: unit tests must not require a live Qdrant).
- **Left running:** `docker-compose.yml`'s `qdrant` service (`restart: unless-stopped`) is up
  and will persist; `qwen3-embedding:0.6b` is pulled in Ollama. Both are now real
  prerequisites Step 8/Step 9 work can assume are available on this box.
- **Next / handoff:** **Step 7 is now fully closed — no remaining unverified claims.** Start
  **Step 8 — wire the indexing pipeline (`vaultrag reindex`)** per the existing "What's next"
  note above (the two traps: delete-before-re-upsert on update, move-points-on-rename). Nothing
  new committed this session (still on `main`, working tree otherwise clean — commit when
  Anik asks). One environment note for whichever machine runs next: if `uv` is missing,
  `pipx install uv` then `uv sync` reproduces the toolchain from `pyproject.toml`/`uv.lock`
  exactly (confirmed byte-for-byte same test count/lint result on this box as the log's prior
  entries).

### 2026-07-15 — P0 Step 7: Qdrant vector store (`index/`)
- **Did:** Completed Step 7. Added `index/base.py` (`VectorStore` protocol —
  `ensure_collection`/`upsert_chunks`/`delete_by_doc`/`count`/`close`, write-side only, the read
  path is Step 9; `SparseEmbedder` protocol; `SparseVector` frozen dataclass that rejects ragged
  indices/values; `VectorStoreError`), `index/payload.py` (`chunk_payload` builds the per-point
  payload from `reference-metadata-schema.md`; `EMBED_VER`; `select_frontmatter`),
  `index/sparse.py` (`Bm25Embedder` over FastEmbed `Qdrant/bm25`, `embed_documents` (term
  frequencies) + `embed_query` (flat weights, IDF is the collection's job), numpy→native casting
  at the boundary, **lazy** encoder so importing or constructing never triggers the HuggingFace
  artifact fetch), and `index/qdrant.py` (`QdrantVectorStore`: one `chunks` collection, named
  vectors `dense`(cosine, int8 scalar quantization `always_ram`, originals `on_disk`, HNSW
  m=16/ef_construct=128) + sparse `bm25`(IDF modifier); payload indexes `project_id`
  (keyword, `is_tenant=true`), `tags`/`path`/`type` (keyword), `modified_ts` (integer); point id
  = `Chunk.point_id`; batched upsert at 256; delete-by-`doc_id` **filtered by `project_id` too**;
  `count(project_id=None)`; injectable client + sparse encoder; `from_settings`). Added
  `docker-compose.yml` (qdrant only — the api/indexer services land with the image at P0
  polish/P1). Exported the surface from `index/__init__.py`.
- **Why:** Step 7 of `../Rag/projects/vaultrag/p0-step-plan.md`
  (`../Rag/projects/vaultrag/indexing-pipeline.md`, `../Rag/decisions/vector-search-qdrant.md`,
  `../Rag/decisions/sparse-bm25-not-elasticsearch.md`,
  `../Rag/decisions/payload-partitioning-not-collection-per-project.md`, arch §7/§9) — the
  searchable half of the index. Step 8 upserts into it; Step 9 queries it.
- **Files:** `src/vaultrag/index/{base,payload,sparse,qdrant,__init__}.py`, `tests/test_index.py`
  (new, 30 tests), `docker-compose.yml` (new), `pyproject.toml`/`uv.lock` (+`qdrant-client`,
  +`fastembed`), `MEMORY.md`, `../Rag/agent-inbox/payload-carries-chunk-text.md` (new, vault).
- **Decisions (impl-level):**
  (1) **The payload carries `text`, and `embed_ver` = the embedding *recipe* version — a real
  divergence from the vault, written back as
  `../Rag/agent-inbox/payload-carries-chunk-text.md` (proposed).** `reference-metadata-schema.md`
  omits `text`, but the SQLite `chunks` table is structure-only and `Chunk` carries no line span,
  so nothing else *can* hold the body — while Step 9 must build snippets with `text` from the
  hits. The payload is the only place it fits; the omission reads as a gap in an enumeration of
  *metadata* (which a body isn't), not an exclusion. Stores `text` (raw body), not `embed_text`
  — the contextual prefix is an embedding-time construct and would be noise in a citation.
  `embed_ver` is named-but-undefined in the reference; defined here as the recipe version
  (prefix format / truncation / normalization), starting at 1, so `(embed_model, embed_ver)`
  per point does the incremental-migration job the reference wants of it. **Needs Anik's
  ratification** (CLAUDE.md §1: flag the divergence, don't drift).
  (2) **`frontmatter{...select keys}` = the filterable keys** — scalars and flat lists of
  scalars, strings ≤512 chars, nested structures dropped. The payload's job is filtering;
  arbitrary nested YAML bloats every point for something no filter can use.
  (3) **BM25 encodes `embed_text` (prefix + body), not the bare body** — symmetric with dense,
  and the prefix's path/heading tokens are what make known-item lookups land (`search
  "docker-compose.prod.yml"`). Boilerplate prefix terms (the project name) appear in every
  document, so IDF drives them to ~0 on their own — no manual stripping needed.
  (4) **The store owns the sparse encoder; the caller passes only dense vectors.** BM25 is
  cheap, deterministic, and meaningless outside an engine that applies IDF — it belongs to this
  layer, not the embedding pipeline. So Step 8 stays "dense vectors in, points out"; `vectors`
  is keyed `chunk_hash` (exactly `embed_chunks`'s return), so identical bodies share a vector.
  (5) **`delete_by_doc` filters on `project_id` as well as `doc_id`**, though `doc_id` is
  already `uuid5(project, path)` and globally unique. Defense in depth: no code path can delete
  across tenants even if handed a foreign id (CLAUDE.md §6), and it matches the tenant index.
  (6) **A missing/wrong-width vector raises instead of indexing the chunk** — a vector-less
  point would look indexed while being permanently unfindable. Same rule as Step 6: never fake
  success.
  (7) **`ensure_collection` verifies an existing collection's dims** and raises naming the cause
  (changed `VAULTRAG_EMBED_DIMS`/model) rather than letting every upsert fail one by one. The
  index is a derived cache — the fix is recreate + reindex, never reshape vectors.
- **Bug found by probing rather than by tests (fixed):** the first draft caught
  `(UnexpectedResponse, OSError, ValueError)` around every client call, which *looks* right and
  passed every test — but qdrant-client wraps connection-refused in `ResponseHandlingException`,
  which is **not** an `OSError`. So the single most likely real failure (Qdrant not running) blew
  past the handler and leaked a raw client error, burying the "is it running? `docker compose up
  -d qdrant`" message the docstring promised. Both exception types share an `ApiException` base;
  the shared `_CLIENT_ERRORS` tuple now catches that. Regression-locked by
  `test_unreachable_qdrant_says_so_instead_of_leaking_a_client_error` (points at a dead port).
  Worth remembering: the local-mode tests can't surface transport bugs at all.
- **Verify (Step 7 gate, all green):** `uv run pytest` **137 passed** (4 CLI + 7 models + 13
  store + 13 scan + 33 parsing + 15 chunking + 22 embedding + 30 index); `uv run ruff check .`
  clean; `uv run mypy src` clean (29 files). Unit tests run against `QdrantClient(":memory:")` —
  qdrant-client's real local implementation, no Docker — covering every plan criterion (upsert →
  count; delete-by-doc removes them and leaves other docs; re-upsert idempotent: same ids, no
  dupes, edited body overwrites in place), plus dims-mismatch/foreign-collection/unreachable
  errors, tenant-scoped delete + count, missing/wrong-width vector raises, both named vectors
  written, sparse-encodes-`embed_text`, the full payload shape, and doc-fields-never-leak-onto-
  another-doc's-chunk. The four real-BM25 tests skip gracefully when FastEmbed's artifact can't
  be fetched (offline), so the suite stays hermetic.
- **Live-integration: PARTIAL — the real Qdrant *server* has never run (Docker not installed).**
  What *was* verified live, end-to-end through our own code on a real vault note
  (`../Rag/projects/vaultrag/indexing-pipeline.md`): parse → chunk (2 chunks) → **live Ollama**
  embed (1024-d, `qwen3-embedding:0.6b`, via the Step-6 cache + real `SQLiteMetaStore`) → **real
  FastEmbed BM25** (78 terms) → `ensure_collection` → upsert (2 points, both named vectors
  present, dense width 1024) → `count`/`count(project)` = 2 → re-upsert idempotent (still 2) →
  payload complete (all 20 keys incl. `text`, `title`, `links_out`, `frontmatter`, `embed_ver`)
  → `delete_by_doc` → 0 → deleting the already-gone doc is not an error. **What local mode
  cannot prove** (needs `docker compose up -d qdrant`): the HNSW/int8-quantization config is
  stored but never applied, payload indexes are explicit no-ops (local mode warns as much — so
  `is_tenant` placement is untested), and **IDF/fusion scoring is not the engine's**, which is
  why Step 9 can't be trusted without a real server. Anik: installing Docker unblocks that.
- **Next / handoff:** Start **Step 8 — wire the indexing pipeline (`vaultrag reindex`)**. The
  two traps Step 7's shape hands it are written up in **Current state → What's next** (stranded
  tail points on a shortened note; renames must move their points because the id embeds the
  path). Nothing committed yet (still on `main`; branch + `agent(claude-code): …` commit when
  Anik asks).

### 2026-07-15 — P0 Step 6: Embedder interface + Ollama impl + cache (`embedding/`)
- **Did:** Completed Step 6. Added `embedding/base.py` (`Embedder` `Protocol`
  [`model`/`dims`/`embed(texts) -> list[Vector]`, `runtime_checkable`], `Vector = list[float]`,
  `EmbeddingError`, `l2_normalize`, `fit_dims` = Matryoshka truncate-to-`dims` + re-normalize,
  and float32 `serialize_vector`/`deserialize_vector` for the `embed_cache` BLOB),
  `embedding/ollama.py` (`OllamaEmbedder`: **sync** httpx `POST /api/embed` batch endpoint,
  internal batching at 64, per-vector `fit_dims`, validates dim + returned count, typed
  `EmbeddingError` on unreachable Ollama / non-200 / model-not-found with the server's message,
  injectable `httpx.Client` so tests use `httpx.MockTransport`, owns/closes its client only if it
  made it, `from_settings`), and `embedding/cache.py` (`embed_chunks(embedder, store, chunks)` →
  `chunk_hash -> Vector`: de-dupe by hash, read the Step 2 `embed_cache` for
  `(model, dims, hash)`, call the model only for misses, write those back → **0 model calls on an
  unchanged re-embed**; `QueryEmbedder` = `functools.lru_cache` over single-query embeds, returns
  a fresh list each call so a caller can't corrupt the cache). Exported the surface from
  `embedding/__init__.py`.
- **Why:** Step 6 of `../Rag/projects/vaultrag/p0-step-plan.md`
  (`../Rag/projects/vaultrag/embedding-pipeline.md`, `../Rag/decisions/dense-embeddings-qwen3.md`,
  arch §8) — turns each `Chunk.embed_text` (contextual prefix + body, built at Step 5) into the
  dense vector Step 7 upserts to Qdrant. The `Embedder` protocol is the "interfaces over vendors"
  seam (CLAUDE.md §5): ONNX int8 / Voyage are future impls behind it, a config change not a rewrite.
- **Files:** `src/vaultrag/embedding/{base,ollama,cache,__init__}.py`, `tests/test_embedding.py`
  (new, 22 tests), `pyproject.toml`/`uv.lock` (+`httpx`), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **Sync `Embedder`, not async.** The store + embed cache are sync and Step 8's job runner
  isn't async yet; the model call is I/O to a local server, so async callers (the FastAPI query
  path, Step 9) run it in a worker thread — the "keep CPU-bound work off the event loop" pattern
  CLAUDE.md §5 already prescribes. Keeps the protocol trivially testable. (2) **Vectors cached as
  packed float32** (`array('f')`), not float64/JSON: half the BLOB size, the precision Qdrant
  stores anyway, native byte order because the cache is a local derived file always read on the
  machine that wrote it (DR = reindex). (3) **`fit_dims` always L2-normalizes** the (possibly
  truncated) vector, so every stored vector is unit-length regardless of whether the backend
  pre-normalized — a clean invariant for Qdrant cosine and future dot-product; a zero vector is
  passed through (no div-by-zero); a backend returning **fewer** than `dims` components raises
  (misconfig, never pad up). (4) **Cache-through lives in `embed_chunks`, not the indexer.**
  Keyed `(model, dims, chunk_hash)` = the exact Step 2 table key, so an edited note re-embeds only
  changed chunks and a rename re-embeds nothing; de-duping by `chunk_hash` means identical bodies
  embed once and share a point vector (intended). Step 8 maps chunk→vector via `chunk.chunk_hash`.
  (5) **Used `/api/embed` (batch `input` list), not legacy `/api/embeddings` (single `prompt`)** —
  one round-trip per 32–64 chunks per the pipeline note. (6) **Errors surface, never fake success**
  (CLAUDE.md §5): a fabricated/zero vector would silently poison the index, so every failure path
  raises `EmbeddingError`; logs carry only shape/timing, never the texts.
- **Token counter — deliberately NOT bundled here (deferral stands).** The Step-5 handoff floated
  landing the `TokenCounter` seam + a Qwen3 tokenizer at Step 6, but the swap that would change the
  live counter's numbers is a **chunking-param change**, and CLAUDE.md §6 gates those on the eval
  harness — which doesn't exist until P2. Swapping now would be exactly the unmeasurable change the
  gate exists to stop (also the conclusion of `../Rag/agent-inbox/token-counter-interface-defer-to-step6.md`,
  which gates the *default* swap on eval). Kept Step 6 to its plan definition (the embedder) —
  "work in order, don't batch". Revisit with the eval harness (P2), or sooner if Anik approves the
  agent-inbox note and wants the behavior-preserving seam landed on its own.
- **Verify (Step 6 gate, all green):** `uv run pytest` **107 passed** (4 CLI + 7 models + 13 store
  + 13 scan + 33 parsing + 15 chunking + 22 embedding); `uv run ruff check .` clean; `uv run mypy
  src` clean (25 files). The plan's verify criteria are met by tests: sample chunks embed to
  `dims`-wide unit vectors, and **re-embed makes 0 model calls** (`test_embed_chunks_caches_and_
  reembeds_nothing`, counting `FakeEmbedder`). Also covered: float32 codec round-trip, `fit_dims`
  truncate+normalize / too-few-dims raise, Ollama success/order/payload + multi-batch splitting +
  empty-input-no-request via `MockTransport`, dim-mismatch / HTTP-404-model-not-found /
  connection-refused / count-mismatch / missing-`embeddings`-key all raising `EmbeddingError`,
  cache dims-scoping, hash de-dupe, and query-LRU memoization + fresh-list-per-call.
- **Live-integration: VERIFIED (gap closed 2026-07-15).** Anik installed Ollama (v0.32.0) and
  `ollama pull qwen3-embedding:0.6b` (595.78M params, 639 MB) succeeded. Smoke-tested the real
  path through our own code: `OllamaEmbedder.from_settings(Settings())` on the P0 default config
  → native model dims = **1024** (matches `embed_dims`), a 3-text batch returns three **unit-norm**
  1024-d vectors (`fit_dims`/normalize confirmed live), re-embedding the same text is deterministic
  (max abs diff < 1e-5), and `fit_dims(...,512)` truncation stays unit-norm. `embed_chunks` against
  a real `SQLiteMetaStore` + a call-counting wrapper over the live embedder proved the **0-model-
  calls-on-re-embed** property live (run-2 delta = 0). The only run-1-vs-run-2 vector difference is
  float32 storage rounding (cache round-trip diff 3.67e-9 == a pure `serialize→deserialize` diff),
  not a bug — exactly what `test_embed_chunks_caches_and_reembeds_nothing` tolerates with
  `pytest.approx(abs=1e-6)`. Ollama's `/api/embed` contract our impl targets is now confirmed real.
- **Next / handoff:** Start **Step 7 — Qdrant vector store (`index/`)**. Feed it the vectors
  `embed_chunks` returns (map chunk→vector by `chunk.chunk_hash`); point id is `Chunk.point_id`.
  Unit tests must not require a live Qdrant (mirror the Step-6 mock-transport approach); the live
  verify needs `docker compose up -d qdrant`. Nothing committed yet (still on `main`; branch +
  `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — Reviewed external "token counting" proposal; captured decision, deferred code
- **Did:** Vetted an external expert doc ("Production Token Counting Strategy for VaultRAG",
  PDF + `.md` in the impl repo root) that flagged `chunking/tokens.py`'s ~4-chars/token
  heuristic as the one weak spot in an otherwise production-ready chunker. Ran each of its
  proposals against our *actual* code (not the `chars/4`-everywhere strawman it assumes) and
  against the vault. **No code changed** — recorded the decision + deferral in the vault
  instead (`../Rag/agent-inbox/token-counter-interface-defer-to-step6.md`, status: proposed).
- **Why:** Anik's call was "only implement if it's a genuine upgrade with no bad side effects."
  Verdict: the review mostly *confirms* our existing plan (`tokens.py` already documents
  heuristic-now → real-tokenizer-at-Step-6). The one genuinely good idea — a `TokenCounter`
  interface so the Step-6 swap is a config change, with the Qwen3 `tokenizer.json` (via the
  `tokenizers` lib, not `transformers`/torch) as default-when-available and the heuristic as
  fallback — belongs **with Step 6**, gated by the eval harness. Swapping the live counter now
  is an unmeasurable chunking-param change (no embedder, no eval yet) — the exact thing
  `CLAUDE.md` §6's quality gate exists to stop. The 512 cap is a retrieval-quality knob, not a
  model limit (Qwen3 has 32k ctx), so there's no correctness fire to fix now.
- **Rejected from the proposal (reasons in the agent-inbox note):** (a) persisting
  `block.tokens` at parse time — perf premise is false (our chunker already counts each block
  once; merge pass reuses summed `piece.tokens`), and a token count is tokenizer-relative, so
  storing it on `Block` goes stale on any model swap; (b) caching token counts on content hash
  alone — if added (P2), the key must be `(tokenizer_id, block_hash)`, matching how the embed
  cache is keyed `(model, dims, chunk_hash)`.
- **Files:** `../Rag/agent-inbox/token-counter-interface-defer-to-step6.md` (new, vault),
  `MEMORY.md`. No repo code touched; Step 5 stands as-is.
- **Next / handoff:** Unchanged — start **Step 6 (Embedder interface + Ollama impl + cache)**.
  When Step 6 lands, also land the `TokenCounter` seam + Qwen3 counter behind it and
  re-validate the 300/512/120 thresholds against the eval harness (recall@10 ≥ 0.9,
  MRR@5 ≥ 0.75). If Anik approves the agent-inbox note, promote it to `../Rag/decisions/`.

### 2026-07-15 — P0 Step 5: heading-tree chunker (`chunking/`)
- **Did:** Completed Step 5. Added `chunking/tokens.py` (`count_tokens`: ~4-chars/token
  heuristic, 0 for empty/whitespace text) and `chunking/chunker.py` (`chunk_document(document,
  *, project_id) -> list[Chunk]`, three linear passes over `Document.blocks`: (1) `_sections` —
  group contiguous non-heading blocks sharing the same `heading_path` into sections; heading
  blocks are boundary markers only, never chunk body text; a heading with nothing directly
  under it before the next heading contributes no standalone chunk; (2) `_pack_section` —
  greedy packing at block granularity (never splits inside a block) toward `TARGET_TOKENS=300`,
  cutting before a block that would exceed `HARD_MAX_TOKENS=512` unless the current piece is
  still empty, in which case an oversized atomic block becomes its own oversized chunk; (3)
  `_merge_tiny_siblings` — single forward pass folding a piece under `MERGE_MIN_TOKENS=120`
  into its immediate predecessor when they share the same parent heading
  (`heading_path[:-1]`, depth-guarded) and the merge still fits the hard max). Contextual
  prefix and `prev_id`/`next_id` (via deterministic `chunk_point_id`) are built per chunk;
  `parent_id` is left `None` (see decisions). Exported `chunk_document`/`count_tokens`/the
  three size constants from `chunking/__init__.py`.
- **Why:** Step 5 of `../Rag/projects/vaultrag/p0-step-plan.md`
  (`../Rag/projects/vaultrag/chunking-strategy.md`,
  `../Rag/decisions/chunking-heading-tree-not-fixed-size.md`, arch §6) — turns a parsed
  `Document` into the indexed unit every later stage (embed, upsert, retrieve) works with.
- **Files:** `src/vaultrag/chunking/{chunker,tokens,__init__}.py`, `tests/test_chunking.py`
  (new, 15 tests), `MEMORY.md`. No `pyproject.toml` changes.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **Token counting is a ~4-chars/token heuristic, not `tiktoken`.** The vault's
  `chunking-strategy.md` doesn't specify a counter, and P0's embedder (Step 6) is Ollama/Qwen3
  — a BPE vocabulary `tiktoken` doesn't model — so a model-specific tokenizer would be false
  precision against the wrong vocabulary. Matches the class of approximation the arch doc
  already accepts for `budget_tokens` ("simple truncation for P0", §13). Revisit once Step 6's
  real tokenizer exists to validate against, if eval data says the heuristic is off.
  (2) **A heading block's own text never becomes chunk body text.** Only non-heading blocks are
  packed into chunks; a heading's text is already carried in descendants' `heading_path` and
  surfaces via the contextual prefix, so duplicating it into the body would be redundant. A
  side effect: a heading with no direct content before the next heading (e.g. an H1 that's
  purely an umbrella for H2 subsections) yields no chunk of its own — it's ancestor context
  only. (3) **A merged chunk (tiny-sibling case) inherits the *earlier* piece's `heading_path`**
  — the `Chunk` model (fixed at Step 1) has one `heading_path` field, not a list, so a chunk
  spanning two sibling sections (e.g. "TermOne" + "TermTwo" under a "Glossary" H2) must pick
  one. The later section's content is still present in `text`, just not separately labeled;
  this only affects the tiny-stub case the merge rule targets in the first place.
  (4) **Same-parent check for merging is depth-guarded, not just `heading_path[:-1]`
  equality** — root-level content (`heading_path == []`) and a top-level H1 section
  (`heading_path == ["H1"]`) both have an empty `[:-1]`, so a naive comparison would treat
  unrelated root prose as a "sibling" of the first real section and wrongly merge them;
  requiring equal length first closes that gap
  (`test_root_content_does_not_merge_with_first_top_level_heading`). (5) **`parent_id` is left
  `None` in P0.** The plan lists `parent_id` as a `Chunk` field Step 5 emits, but no parent
  *record* exists to point at yet — parent-swap is explicitly deferred to P1 in
  `p0-step-plan.md`. Wiring a pointer to a section that isn't materialized anywhere would dangle
  or invent scope the plan doesn't ask for; revisit when P1 actually builds parent chunks.
  Locked in by `test_parent_id_is_none_in_p0` so it's a deliberate choice, not an oversight, if
  a future session finds it unset. (6) **`chunk_hash` hashes `Chunk.text` (the raw body), not
  `embed_text` (prefix + body)** — the model docstring says "BLAKE3 of text"; keeping the hash
  off the contextual prefix means a chunk's identity/dedupe key doesn't change if the note's
  path or project changes without the body changing (prefix embeds `path`/`project_id`), and
  the Step 2 `embed_cache` key `(model, dims, chunk_hash)` still keys correctly on the actual
  embedded content since `embed_text` is derived from `text` deterministically at embed time.
- **Verify (Step 5 gate, all green):** `uv run pytest` 85 passed (4 CLI + 7 models + 13 store +
  13 scan + 33 parsing + 15 chunking); `uv run ruff check .` clean; `uv run mypy src` clean (22
  files). Token bounds (oversized atomic block exceeds hard max but isn't split; a large section
  splits into multiple chunks all ≤ hard max), contextual-prefix format (with/without a tags
  line), stable `chunk_hash` across reruns (byte-identical `model_dump` twice), correct
  `prev_id`/`next_id` sibling stitching (`None` at both ends, chained point-ids in between), and
  the tiny-sibling-merge + depth-guard edge case are all covered.
- **Next / handoff:** Start **Step 6 — Embedder interface + Ollama impl + cache
  (`embedding/`)**. Feed it `Chunk.embed_text` (already built). Reuse Step 2's `embed_cache`
  table/methods rather than re-deriving the schema. Confirm an Ollama instance with
  Qwen3-Embedding-0.6B pulled is actually reachable before writing the impl — that's an external
  dependency this repo doesn't control. Nothing committed yet (still on `main`; branch +
  `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 4: markdown parsing → `Document` (`parsing/`)
- **Did:** Completed Step 4. Added `parsing/obsidian.py` (wikilink/embed regex covering all
  four forms — `[[Note]]`/`[[Note|Alias]]`/`[[Note#Heading]]`/`[[Note#^block]]` — plus embeds
  `![[...]]`; inline `#tag`/`#a/b` regex with a lookbehind that excludes ATX headings and
  wikilink anchors; `expand_nested_tags` prefix expansion; dataview `key:: value` line regex;
  callout header parsing `[!type]+/- Title`; link-masking so tag/dataview extraction never
  looks inside a `[[...]]` span), `parsing/frontmatter.py` (YAML → typed fields via
  python-frontmatter, `coerce_str_list`/`coerce_epoch` for YAML's list-or-string-or-scalar and
  auto-parsed-date-or-string inconsistency, `custom_frontmatter` strips the promoted keys so
  nothing lives in two places, malformed YAML degrades to no-frontmatter rather than raising),
  and `parsing/markdown.py` (`parse_document`: markdown-it-py `"default"` preset — verified
  empirically to cover GFM tables with no extra plugin — tokenizes the post-frontmatter body;
  a flat-token-stream walk builds source-ordered `Block`s, treating code/table/list/blockquote
  as atomic by slicing raw source lines via `.map` and skipping past nested tokens rather than
  walking them; a `heading_stack` trail produces each block's `heading_path`; line numbers are
  shifted back to file-relative via `frontmatter_line_offset`).
- **Why:** Step 4 of `../Rag/projects/vaultrag/p0-step-plan.md`
  (`../Rag/projects/vaultrag/parsing-pipeline.md`, arch §5) — the format-agnostic normalization
  every later stage (chunking, embedding, indexing) depends on.
- **Files:** `src/vaultrag/parsing/{obsidian,frontmatter,markdown,__init__}.py`,
  `tests/test_parsing.py` (new, 33 tests), `pyproject.toml`/`uv.lock` (+`markdown-it-py`,
  +`python-frontmatter`, dev +`types-pyyaml`), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **Redact at the point of extraction, not as a final pass.** Every block's text is checked
  against `secret_guard.scan_for_secrets` *before* it can reach `heading_stack` or the
  wikilink/tag/dataview regexes — not after building the block. An earlier draft redacted as a
  final pass over the finished block list, which had two real leaks: a secret shaped like
  `api_key:: <value>` would already have been captured into `metadata.frontmatter["api_key"]`
  by the dataview regex before the pass ran, and a secret used as heading text would already be
  baked into every descendant block's `heading_path`. Both are covered by regression tests
  (`test_secret_never_leaks_via_dataview_extraction`, `test_secret_never_leaks_via_heading_path`).
  (2) **Redaction granularity is block-level, not line-level** (the MEMORY.md forward-note from
  Step 3 said "line-level" — refined here with reasoning): a PEM private key's body lines don't
  self-identify to `scan_for_secrets` (only the `-----BEGIN...` header line matches a rule), so
  redacting line-by-line would strip the header and leave the base64 key body searchable.
  Quarantining the whole block is what actually closes that gap
  (`test_multiline_secret_body_is_redacted_not_just_header_line`). In the normal pipeline this
  code path never fires at all — Step 3's whole-file gate already withholds any file containing
  a secret pattern before Step 4 ever sees it; this is a backstop for direct/standalone parser
  use (tests, a future watch-mode). (3) **`Link.anchor` is stored verbatim, not slugified** —
  re-reading its docstring ("heading slug or ^block id, the part after '#'") against how it's
  actually used clarified that anchor *resolution* (matching a link's raw anchor text to an
  actual heading's slug) is a retrieval-time concern, not parsing's; inventing a slugify
  function here would have been unrequested scope. (4) **`python-frontmatter`'s `.content` is
  bypassed when no `---` block is present**, using `raw_text` verbatim instead: `.content` does
  a boundary `.strip()` that silently eats leading indentation on a body's first line, corrupting
  a document that opens directly with a 4-space-indented code block (turned it into an ordinary
  paragraph — caught by a failing test, not by inspection). Fixed narrowly for the no-frontmatter
  case only (zero boundary-detection ambiguity there — the whole input *is* the body); the
  with-frontmatter path still trusts `.content` as-is rather than reimplementing boundary
  detection, which would risk a worse failure mode (frontmatter leaking into the body) for a
  narrower bug. (5) **Lists, blockquotes, and tables are one atomic `Block` each**, sliced
  straight from source lines and never walked into — consistent with the chunker (Step 5) needing
  to treat them as unsplittable; container `_open` tokens' `.map` already spans the whole nested
  structure, and the next token back at the *same* nesting `.level` is always the matching
  `_close`, verified empirically against markdown-it-py's real token output for mixed
  ordered/nested-bullet lists. (6) `MarkdownIt("default", {"typographer": False, "html": False})`
  — typographer (smart quotes/dashes) would mutate indexed text away from what's on disk, which
  fights BM25 exact-match on identifiers/error codes (arch principle #5); raw HTML passthrough
  is unneeded in P0 and off by default anyway.
- **Verify (Step 4 gate, all green):** `uv run pytest` 70 passed (4 CLI + 7 models + 13 store +
  13 scan + 33 parsing); `uv run ruff check .` clean; `uv run mypy src` clean (20 files).
  **Required guardrail gate met:** every planted-secret test (`test_secret_in_block_is_redacted`,
  `test_secret_never_leaks_via_dataview_extraction`, `test_secret_never_leaks_via_heading_path`,
  `test_multiline_secret_body_is_redacted_not_just_header_line`) asserts the secret string is
  absent from the *entire serialized `Document`*, not just the one field it was planted in.
  Plus frontmatter typed-field promotion, all four wikilink forms + embeds, nested tag
  expansion/merge, callout vs. plain-blockquote distinction, dataview merge, code/table/list
  atomicity (including a fake heading marker inside a fence proving it isn't tokenized as real),
  heading_path sibling-reset correctness, and file-relative line-number math across the
  frontmatter boundary.
- **Next / handoff:** Start **Step 5 — heading-tree chunker (`chunking/`)**. It consumes
  `Document.blocks` directly; atomic blocks (`Block.is_atomic`) must never be split. Still
  undecided: token-counting approach (tiktoken vs. a cheap word/char heuristic) — check the
  vault before picking one, it wasn't specified in `chunking-strategy.md` at last read. Nothing
  committed yet (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 3: vault scan + manifest diff + secret guard (`indexer/`)
- **Did:** Completed Step 3. Added `indexer/secret_guard.py` (the non-overridable half of
  the secrets barrier: `is_secret_filename` — `.env*`/`id_rsa*`/`*.pem`/`*credentials*` +
  key-material globs; `scan_for_secrets` — high-precision content scan: PEM private-key
  blocks, AWS/GitHub/Slack/Google/Stripe/JWT tokens, plus a keyword-anchored high-entropy
  assignment rule gated on Shannon entropy so git SHAs/UUIDs/placeholders don't trip it;
  **returns only the rule name, never the secret**). Added `indexer/scan.py`: `walk_vault`
  (os.walk, symlinks off, prunes `.git/.obsidian/.trash/.smart-env/node_modules` + `.ragignore`
  dirs, markdown-only, both secret gates, BLAKE3 per file) → `ScannedFile`; `diff_manifest`
  (pure) → `ScanDiff{creates,updates,deletes,renames,unchanged}` with rename recovery (a create
  whose hash matches a delete becomes a `Rename`, one-to-one/greedy); `scan_vault` wrapper
  Step 8 will call with `store.document_manifest(project)`. Exported the API from
  `indexer/__init__.py`.
- **Why:** Step 3 of `../Rag/projects/vaultrag/p0-step-plan.md`; boot-reconcile front of the
  indexer (`../Rag/projects/vaultrag/ingestion-pipeline.md`, arch §4). Secret guardrail is
  `CLAUDE.md` §6 / `reference-security-model.md` (first-class threat). Full-rebuild property
  (`CLAUDE.md` §6): empty manifest → every file a create, so `reindex` from a bare vault rebuilds.
- **Files:** `src/vaultrag/indexer/{scan,secret_guard,__init__}.py`, `tests/test_scan.py` (new,
  13 tests), `pyproject.toml`/`uv.lock` (+`blake3`, +`pathspec`), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **Scope boundary:** scan *produces* the `ScanDiff`; enqueuing jobs + applying renames as
  metadata-only updates (vectors untouched) is Step 8's job runner — kept out of Step 3 to honor
  "work in order, don't batch". Renames are surfaced first-class in the diff so Step 8 can act.
  (2) `diff_manifest` is a **pure function** (manifest in, diff out) → the classifier is tested
  without a filesystem. A `_MISSING` sentinel distinguishes "path absent" (create) from
  "path present with NULL hash" (a half-recorded doc → treated as update, never a rename source).
  (3) **Content secret-block is whole-file at this boundary** for P0 (a note carrying live-looking
  key material never enters the index); line-level *redaction* is deferred to Step 4 parsing.
  (4) Directory-level `.ragignore` prunes aren't counted in `ScanStats.ignored` (their files are
  never visited — that's the perf win); only file-level ignores increment it. (5) pathspec API:
  used the non-deprecated `gitignore`/`GitIgnoreSpecPattern` (the `gitwildmatch` factory is
  deprecated in pathspec 1.1.1 and emitted warnings).
- **Verify (Step 3 gate, all green):** `uv run pytest` 37 passed (4 CLI + 7 models + 13 store +
  13 scan); `uv run ruff check .` clean; `uv run mypy src` clean (17 files). **Required
  guardrail gate met:** `test_secret_file_never_indexed` (files named `.env`/`id_rsa`/`*.pem`/
  `*credentials*` never surface) and `test_note_with_embedded_secret_never_indexed` (markdown
  notes embedding an AWS key / PEM block / high-entropy assignment are all withheld;
  `secrets_blocked == 3`). Plus content-addressed hashing, ragignore, rename detection, NULL-hash
  update, and the empty-manifest→all-creates full-rebuild property.
- **Next / handoff:** Start **Step 4 — markdown parsing → `Document` (`parsing/`)**. Reuse
  `scan_for_secrets` there for line-level redaction (the deferred half of decision #3). Nothing
  committed yet (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 2: metadata store (SQLite `MetaStore`, `store/`)
- **Did:** Completed Step 2. Added `store/base.py` (the `MetaStore` `Protocol` +
  `runtime_checkable`, row dataclasses `ProjectRow/DocumentRow/ChunkRow/JobRow`, `JobState`/
  `JobKind` enums) and `store/sqlite.py` (`SQLiteMetaStore`, WAL). DDL for **P0 tables only**
  — `projects, documents, chunks, jobs, embed_cache, tags` (deferred `links, api_keys,
  query_log`) — written to carry to Postgres unchanged. Methods: project/document upsert +
  read, `document_manifest` (scan-diff source for Step 3), `upsert_chunks`/`chunk_hashes`/
  `get_chunk`, `set_tags`, jobs enqueue/state, embed-cache get/put (batched IN, chunked at
  900 vars). Added `document_id(project, path)` to `models.py` alongside `chunk_point_id`.
- **Why:** Step 2 of `../Rag/projects/vaultrag/p0-step-plan.md`. Holds structure + ops state
  (vectors live in Qdrant; this is a derived cache). Schema from
  `../Rag/reference/reference-database-schema.md`; SQLite-WAL choice from
  `../Rag/decisions/metadata-store-sqlite-to-postgres.md`.
- **Files:** `src/vaultrag/store/{base,sqlite,__init__}.py`, `src/vaultrag/models.py`
  (+`document_id`), `tests/test_store.py` (new, 13 tests), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault schema — no write-back needed):**
  (1) Identity is content-addressed: `documents.id = uuid5(project, path)`,
  `chunks.id = uuid5(project, path, ix)` → every write is an idempotent upsert, delete-by-doc
  is a filter. (2) Document/chunk upserts use `ON CONFLICT DO UPDATE`, **never `INSERT OR
  REPLACE`** — a REPLACE would cascade-delete child chunks/tags (regression-guarded by
  `test_reupserting_document_preserves_its_chunks`). (3) `PRAGMA foreign_keys=ON` +
  `ON DELETE CASCADE` so `delete_document` cleans chunks+tags. (4) `JobKind.ingest`
  (not `index`) to avoid shadowing `str.index` under StrEnum.
- **Verify (Step 2 gate, all green):** `uv run pytest` 24 passed (4 CLI + 7 models + 13
  store); `uv run ruff check .` clean; `uv run mypy src` clean (15 files). Store tests cover
  round-trips, tag replacement, chunk re-embed-in-place, cascade delete, job idempotency,
  embed-cache dims-scoping, and `isinstance(store, MetaStore)`.
- **Next / handoff:** Start **Step 3 — vault scan + manifest diff + secret guard
  (`indexer/scan.py`)**. Diff against `store.document_manifest(project)`. The planted-fake-
  secret test is a required guardrail gate (`CLAUDE.md` §6). Nothing committed yet (still on
  `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 1: core data models (`models.py`)
- **Did:** Completed Step 1. `models.py` had been drafted in a prior session but was broken
  (imported `StrEnum`, declared the enums as `class X(str, Enum)` with `Enum` never imported →
  `NameError` on import). Fixed the three enums (`DocType`, `BlockType`, `LinkKind`) to subclass
  `StrEnum` (the 3.12 idiom, consistent with the existing import), and replaced two ambiguous
  en-dashes (`–`) in the module docstring with hyphens to satisfy ruff `RUF002`. Models now
  import and the whole shape is exercised by `tests/test_models.py` (round-trip in dict + JSON
  modes, block atomicity, `embed_text` prefix, deterministic scoped `point_id`, `extra=forbid`,
  `SearchResult` shape).
- **Why:** Step 1 of `../Rag/projects/vaultrag/p0-step-plan.md` — the one normalized
  `Document{metadata, blocks, links}` / `Block` / `Chunk` / `Snippet`/`SearchResult` shape every
  later stage depends on (arch §5, ordering constraint #1). Fields sourced from
  `reference-metadata-schema.md` + `reference-database-schema.md`.
- **Files:** `src/vaultrag/models.py`, `tests/test_models.py` (both pre-existing this session;
  edited `models.py` only), `MEMORY.md`.
- **Decisions:** Authored the P0 cited-snippet return contract inline as `Snippet` +
  `SearchResult` (the open question from the step plan — `reference-mcp-tools.md` specifies
  params only). If it hardens through Step 10, promote it to `../Rag/reference/`.
- **Verify (Step 1 gate, all green):** `uv run pytest` 11 passed (4 CLI + 7 models);
  `uv run ruff check .` clean; `uv run mypy src` clean (13 files).
- **Next / handoff:** Start **Step 2 — metadata store (SQLite `MetaStore`, `store/`)**. Nothing
  committed yet (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-14 — P0 Step 0: project scaffold
- **Did:** Scaffolded the Python project. `pyproject.toml` (uv-managed, hatchling, src
  layout, entrypoint `vaultrag → vaultrag.cli:main`); deps `pydantic-settings`, `structlog`,
  `typer` + dev group `ruff/mypy/pytest`. Added `config.py` (env-driven `Settings`,
  `VAULTRAG_` prefix: vault path, project_id, state dir, qdrant/ollama urls, embed model/dims,
  retrieval defaults, log level), `logging.py` (structlog JSON, idempotent `configure_logging`),
  `cli.py` (Typer app with stubbed `reindex`/`mcp` + `--version`). Created the 8 empty module
  packages (`parsing chunking embedding index store indexer retrieval mcp`), each with a
  docstring + step pointer. Shipped `.gitignore` (`state/`, `.venv/`, tool caches), `.ragignore`
  (secret-deny + Obsidian/media globs), `README.md`, and 4 CLI smoke tests.
- **Why:** Step 0 of `../Rag/projects/vaultrag/p0-step-plan.md`. Establishes the role/interface
  skeleton so later steps drop into fixed module homes (arch §4–§11 ordering).
- **Files:** `pyproject.toml`, `uv.lock`, `.gitignore`, `.ragignore`, `README.md`,
  `src/vaultrag/{__init__,config,logging,cli}.py`, `src/vaultrag/{parsing,chunking,embedding,index,store,indexer,retrieval,mcp}/__init__.py`,
  `tests/{__init__,test_cli}.py`.
- **Decisions:** CLI framework = **Typer** (type-first, pairs with FastAPI/pydantic; not named
  in the vault stack — flag for `../Rag/agent-inbox/` if it hardens). uv resolved a **Python
  3.12** interpreter (`requires-python >=3.12`, ruff/mypy `target-version = py312`).
- **Verify (Step 0 gate, all green):** `uv run vaultrag --help` lists `reindex` + `mcp`;
  `uv run ruff check .` clean; `uv run mypy src` clean (12 files); `uv run pytest` 4 passed.
- **Next / handoff:** Start **Step 1 — core data models (`models.py`)**. Nothing committed yet
  (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-14 — P0 broken into steps + plan locked in vault
- **Did:** Gathered P0 design from the vault (arch doc + subsystem + reference + decisions
  notes) and decomposed P0 into **11 implement-one-at-a-time steps** (scaffold → models →
  SQLite store → scan+secret-guard → md parse → heading chunker → Ollama embedder → Qdrant
  store → reindex pipeline → hybrid retrieval → FastMCP `search`/`open`). Locked the durable
  plan as a new vault project-log note and pointed this log at it.
- **Why:** User asked to break P0 into steps (one at a time) and record it in the better home.
  Durable "what to build" plans belong in the vault (`CLAUDE.md` §7); MEMORY.md stays a log.
  Decisions this session: decompose **P0 only** for now (later phases get their own
  `pN-step-plan` sibling when they start); P0 embedder = **Ollama** behind the interface.
- **Files:** `../Rag/projects/vaultrag/p0-step-plan.md` (new), `../Rag/projects/vaultrag/build-roadmap.md`
  (linked + Log line), this `MEMORY.md`. No code yet. Neither git repo committed (awaiting ask).
- **Next / handoff:** Start **Step 0 — Project scaffold** (`uv init`, `pyproject.toml`,
  `src/vaultrag/{cli.py,config.py}`, ruff/mypy, `.gitignore`, `.ragignore`). Per-step verify +
  a MEMORY.md entry before moving on. Full step detail: `../Rag/projects/vaultrag/p0-step-plan.md`.

### 2026-07-14 — Repo scaffolding + agent guide
- **Did:** Created the `vaultrag/` implementation folder as a sibling of the design vault
  (`../Rag`). Added `CLAUDE.md` (agent guide: how to fetch design from the vault, the
  stack, roadmap, conventions, guardrails) and this `MEMORY.md`.
- **Why:** Give any coding agent a stable orientation so it reads the vault instead of
  re-deriving decisions, and a handoff log so sessions don't lose context.
- **Files:** `CLAUDE.md`, `MEMORY.md`.
- **Next / handoff:** Initialize the Python project (`uv init`, `pyproject.toml`, entry
  point `vaultrag`), add `docker-compose.yml` for Qdrant, then begin P0. Confirm exact
  schemas/tool signatures against `../Rag/reference/` before writing code. No git repo
  initialized here yet — `git init` when ready to start committing (`agent(...)` style).
