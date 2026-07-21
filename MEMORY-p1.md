# VaultRAG — P1 session-log archive

Frozen P1 build history — the 5 stages (rerank → parent-child → citations → watchdog
live-watch → write-back), plus the plan-decompose and decisions-settled entries — moved out
of `MEMORY.md` on 2026-07-20 to keep the active log P2-focused. The **live** log is `MEMORY.md`;
this file is the how-P1-was-built record, unedited on archival. P1 shipped **daily-driver quality
with the memory loop closed**: reranker (flagged), parent-swap, `obsidian://` citations, watchdog
live-watch, and write-back with git auto-commit — 315 tests green. Durable *design* lives in the
vault (`../Rag/projects/vaultrag/p1-step-plan.md`), not here.

---

## P1 completion snapshot (Current-state bullets, archived from MEMORY.md 2026-07-20)

- **(P1 archive)** **P1 CODE-COMPLETE — Stage 5 (write-back + MCP write tools + git auto-commit) done +
  LIVE-verified (real Ollama + Qdrant + real vault). All 5 P1 stages are done; the memory loop is
  closed.** New **`writeback/`** package. **`NoteWriter`** (`writeback/writer.py`) backs the three
  MCP write tools and runs every accepted write through one gauntlet (repo `CLAUDE.md` §6 /
  `write-back-design.md`): **(1) containment** — target realpath (`resolve()` first, so symlinks
  can't tunnel out) must stay under the vault root; **(2) write allowlist** — must sit under one of
  `writeback_allow_roots` (default just `agent-inbox/`; per-vault config opens e.g. `decisions/` — the
  configurable half of "default write root agent-inbox/", *not* a hardcoded folder, per Anik's
  no-magic-constant steer); **(3) index gates** — the *same* `exclusion_reason` (secret filename /
  `.ragignore` / unsupported / pruned) **and** `scan_for_secrets(content)`, so a note whose body is
  key material is **unwritable AND (ingest_path re-gates) never indexed** — the write-side twin of P0
  Step 3's planted-secret test; **(4) size + rate caps** (`writeback_max_bytes` 1 MB;
  `writeback_max_writes_per_minute` 30, sliding window); **(5) optimistic lock** — `patch_frontmatter`
  requires the `base_hash` the agent last saw → mismatch raises `WriteConflict` carrying the *current*
  hash + content (the "409 + current content"); `append_section` is additive (inserts at the section's
  end via `heading_section`) so it takes no lock, matching the `reference-mcp-tools.md` signatures.
  Then it **writes → git-commits (best-effort) → synchronous `ingest_path`** (Stage 4's primitive) so
  the note is retrievable the same session. **git** (`writeback/git.py`): auto-commits each write to the
  *vault's* repo as `agent(<author>): <what>`, attributed to the agent identity via `-c user.name/email`
  (no repo-config mutation); **best-effort** — a non-git vault / missing git still gets the write +
  index (`committed=False`), the vault is the source of truth. **Index-failure is non-fatal**: if Qdrant/
  Ollama are down at write time the note is still written + committed and `WriteResult{indexed=False,
  warning=…}` says so (a later reindex/watch recovers it). New `WriteResult` model (`models.py`); 3 MCP
  tools registered in `build_mcp_server` only when a `writer` is passed (`mcp/server.py`), with
  model-facing descriptions + `WriteRefused`/`WriteConflict`→`ToolError`. CLI: `vaultrag mcp` opens the
  SQLite store + builds a `NoteWriter` **by default** (write-back on — the memory loop is the point;
  `--no-write` or `VAULTRAG_WRITEBACK_ENABLED=false` serves read-only), sharing the store with `--watch`.
  6 new config knobs. **Green: 315 tests** (+27: `test_writeback.py` [20], `test_mcp.py` [+5 write-tool],
  `test_cli.py` [+2]); ruff, ruff format, mypy (41 src files). **Verify:** unit tests drive the real
  writer against real SQLite + in-memory Qdrant + fake embedder (create/append/patch, conflict, all
  guardrails incl. **planted-secret-content unwritable+not-indexed**, real-git commit in a tmp repo,
  index-failure-nonfatal); **a full Ollama-backed dogfood against the live `vaultrag-design` vault PASSED**
  (`scratchpad/writeback_dogfood.py`, git disabled so the user's Rag history is untouched): reconciled
  `../Rag` (count 145), `write_note` → real embed, `indexed chunks=1` (145→146), **a real hybrid `search`
  for its token ranked the just-written note #1** with a working `obsidian://` URI, `append_section` +
  `patch_frontmatter` (stale `base_hash`→409, correct→patched), then deleted + evicted — vault + index
  left clean (Rag git status shows only the pre-existing p1-step-plan.md edit).
  **Next: P2** (Docling PDFs, Excalidraw/Mermaid, OCR, `related`/graph-expand, full eval harness + CI
  gate, Prometheus). See `CLAUDE.md` §4.
- **(Stage 4 archive)** **Stage 4 (single-file ingest + watchdog live-watch) code-complete +
  LIVE-verified (real Ollama + Qdrant + real vault).** Two pieces. (1) **Extracted the single-file primitive** from `reindex_vault`
  (`indexer/pipeline.py`): a new `IngestContext` bundles `(project, vault, store, vector_store, embedder)`, and
  public **`ingest_path(ctx, path, *, ignore=None)` / `remove_path(ctx, path)`** reconcile ONE vault-relative
  path against disk → `IngestOutcome{status: indexed|unchanged|removed|skipped, chunks, reason}`. This is the
  unit live-watch and (Stage 5) write-back both drive. `ingest_path` **re-applies the boot-scan gates** —
  path-level `exclusion_reason` (secret filename / `.ragignore` / unsupported / pruned) **and** content-level
  `scan_for_secrets` — so a single-file event can never index what a full `reindex` would withhold (CLAUDE.md
  §6); a note that *becomes* a secret / gets `.ragignore`d / vanishes is **evicted**. It hashes the **raw
  bytes** (matches scan's `_hash_file`) then decodes for parse/secret-scan, so live-watch manifest hashes stay
  consistent with a later `reindex`, and skips re-embed when the hash is unchanged. `reindex_vault` keeps its
  exact signature + `ReindexStats` and now shares the inner `_ingest_file`/`_delete_file` work (all 269 prior
  tests still green). (2) **`indexer/watch.py` — `VaultWatcher`**: `watchdog` `Observer` → per-path **1.5 s
  debounce** (collapses Obsidian's temp-file save storm into one ingest) → **`ThreadPoolExecutor` (default 4)**
  → `ingest_path`, with a `jobs` row per path and **retry 3× exponential backoff → park `dead`** (one bad note
  can't kill the watcher). Thread-based (the pipeline is sync, watchdog already threads): the observer only
  marks paths dirty; a dispatcher thread moves quiet paths to the pool; an in-flight path isn't re-dispatched
  until it finishes → per-path serial, last-write-wins. Directory delete/move fans out by manifest prefix + a
  dest-subtree walk. **Process model (per plan Decisions):** `vaultrag watch` = boot-reconcile then watch
  (standalone, indexer role); `vaultrag mcp --watch` runs one watcher in a **daemon thread** that dies with the
  stdio process; plain `mcp` stays watcher-free. New config knobs `watch_debounce_seconds` (1.5) +
  `watch_workers` (4). Added `watchdog>=4.0` dep. **Green: 288 tests** (+19: `test_ingest_primitive.py` [10:
  index/unchanged/evict + secret-content/secret-name/ragignore/unsupported gates], `test_watch.py` [6: debounce
  coalescing, retry→dead, flush, **real-`watchdog`-Observer e2e** write→indexed / delete→evicted],
  `test_cli.py` [3: watch help/failure + `mcp --watch` start/stop wiring]); ruff, ruff format, mypy (39 src
  files). **Verification:** (a) the real-`watchdog`-Observer e2e test drives the *actual* observer → debounce →
  worker pool → `ingest_path` against real SQLite + real Qdrant(`:memory:`); (b) **a full Ollama-backed dogfood
  against the live `vaultrag-design` vault PASSED** (Ollama + Qdrant both up later in the session; `qwen3-
  embedding:0.6b` present). The dogfood (`scratchpad/watch_dogfood.py`): boot-reconciled `../Rag`, started a
  `VaultWatcher`, **created a throwaway `agent-inbox/` note → watcher logged `status=indexed chunks=1`, Qdrant
  count 145→146**; a **real hybrid `search` for its unique token returned that note as the #1 hit** with a
  working `obsidian://open?vault=Rag&file=agent-inbox%2F…` URI (Stage 3 wired too); then **deleted it → watcher
  logged `status=removed`, count back to 145** — vault + index left clean. Bonus: the observer fired on
  `.obsidian/workspace.json` and correctly `status=skipped` it (live gate works); a follow-up `status=unchanged`
  re-fire confirmed idempotency held under the save storm.
  **Next: Stage 5 (write-back + MCP write tools + git auto-commit) — reuses this `ingest_path` primitive for
  the synchronous same-session reindex.**
- **(Stage 3 archive)** **Stage 3 (`obsidian://` citations, retrieval stage 8) code-complete +
  live-verified.** New `retrieval/cite.py`: `obsidian_uri(vault_name, path)` builds
  `obsidian://open?vault=…&file=…` (both parts percent-encoded, `safe=""` so subfolder `/`→`%2F` and
  round-trips) + a `cite(snippets, vault_name)` stage that stamps each `Snippet.uri` (no-op without a
  name → `uri` stays `None`). **Line range** now flows end-to-end: chunker records each chunk's
  `line_start`/`line_end` (`chunking/chunker._line_span`, min/max over its blocks) → Qdrant payload
  (`index/payload.py`, impl-level like `text`) → `_to_snippet` reads them back; a **parent-swapped**
  snippet instead cites its reconstructed *section's* span (`ParentSection` gained `line_start`/`line_end`
  from `heading_section`). Added `Snippet.uri`/`line_start`/`line_end` and `Chunk.line_start`/`line_end`
  (`models.py`). New `Settings.vault_name` (Obsidian display name — **not** derivable from the path);
  `cli.py mcp` falls back to `vault_path.resolve().name`; threaded `vault_name` through
  `build_mcp_server` → `retrieval.search`. Cite runs before budget-pack (URI isn't text, costs no budget).
  **Correction:** the plan said `Snippet` "already reserves the URI field as P1" — it did **not**; added now.
  Green: **269 tests** (+17: `test_cite.py` new, plus cite/line cases in retrieval/chunking/parents/mcp/index),
  ruff, ruff format, mypy (37 src files incl. `cite.py`). **Live-verified**: wiped `state/vaultrag.db`
  (chunk/doc hashes unchanged by these edits → a plain reindex would skip everything, so a manifest wipe was
  needed to backfill the new payload fields) and full-reindexed `vaultrag-design` (55 docs, 143 chunks); a
  direct in-process `search(..., vault_name="Rag")` returned working URIs, parent-swapped hits citing the exact
  section span (Stage 3 section → lines 82–91), and a plain child hit carrying its payload line range.
  **NOTE:** the *registered* `vaultrag` MCP connection still returned the pre-Stage-3 shape (no `uri`/line
  keys) — Claude Code bound that stdio subprocess before these edits; it serves new code on the next fresh
  spawn. The in-process script is the authoritative proof (same code path, live index/vault).
  **Next: Stage 4 (single-file `ingest_path` + watchdog live-watch).**
- **(Stage 2 archive)** **Stage 2 (parent-child + parent-swap) code-complete + live-verified.**
  Retrieval **stage 5** is built, wired, green (252 tests, ruff, mypy), and driven against the
  live `vaultrag-design` index. **Chunker** (`chunking/chunker.py`) now sets each child's
  `parent_id` to a **pointer** `<path>#<anchor>` at the enclosing H1/H2 section (shared helper
  `parsing/sections.parent_anchor`, depth ≤ 2) — no parent text stored; verified in SQLite
  (`chunks.parent_id`). **Retrieval** (`retrieval/parents.py`, new): `ParentResolver` protocol +
  `VaultParentResolver` (reconstructs the section **live from the vault** via `open`'s own gated
  reader → `parse_document` → `heading_section`, so it can never surface a file `open` would
  refuse) + `parent_swap` policy — group reranked hits by parent pointer, swap each to its
  section, **dedupe same-section children into one parent snippet**. Inserted after rerank in
  `pipeline.py`; wired through `mcp/server.py` (fresh resolver per call = per-call cache + freshness;
  snippets mode only, compact skips it) + `cli.py`; config knobs `parent_swap_enabled` (**ON by
  default**) + `parent_max_tokens` (2000). **Key finding (see 2026-07-16 log):** the live dogfood
  caught that a **tiny-sibling-merge child mislabels its section** — its `heading_path` names only
  the *first* merged section, so reconstructing that section is *smaller* than the child and would
  **lose content** (e.g. a 220-tok child → 38-tok "parent"). Fix: a **superset guard** — swap only
  when the reconstructed parent is `≥` the biggest child in the group (and `≤ min(parent_max_tokens,
  budget_tokens)`); otherwise keep the child. Parent-swap is now **strictly non-lossy**.
  **Real number (re-measured 2026-07-16 on the Stage-2-reindexed live index):** shipping
  **fusion-only** baseline is recall@10 **1.000** / MRR@5 **0.775** (both > targets 0.9/0.75).
  Parent-swap is neutral **by construction** — it repackages *within* a note and the superset
  guard keeps each path's first-occurrence rank, so path-recall/MRR@5 can't move; the committed
  `run_eval.py` harness calls `search()` *without* a `parent_resolver`, so it measures fusion-vs-
  rerank and does **not** exercise parent-swap (the earlier "0.767→0.767 with swap on/off" line
  came from an ad-hoc modified harness on the pre-reindex index — superseded). Fusion drifted
  0.825→0.775 vs Stage 1 purely because the index was rebuilt with the Stage 2 chunker (parent-id
  pointers + tiny-sibling merges shifted a few chunk boundaries); still clears both targets, so
  parent-swap ON by default ships no regression (unlike Stage 1 rerank).
  **Next: Stage 3 (`obsidian://` citations, retrieval stage 8).**
- **(Stage 1 archive)** **Rerank (stage 4) code-complete behind a flag, DISABLED by default.**
  `retrieval/rerank.py`: `Reranker` protocol + `BgeReranker` (bge-reranker-v2-m3 ONNX-int8 via
  `onnxruntime` from `onnx-community/bge-reranker-v2-m3-ONNX` + `tokenizers`, no torch/transformers)
  + `NoOpReranker` fallback + `load_reranker`. Threshold default **0.0** (no floor): the vault's 0.3
  was measured to regress recall (bge-v2-m3 sigmoids relevant chunks at ~0.02–0.25), superseded
  pending `../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md`. `rerank_enabled=False`
  (Anik's call): fusion beat rerank at every threshold on this title-matchy golden set; flip on per-vault
  with `VAULTRAG_RERANK_ENABLED=true` once a harder golden set shows it winning.

---

## P1 session log

### 2026-07-19 — P1 Stage 5 (write-back + MCP write tools + git auto-commit) implemented + live-verified
- **Did:** Built the write-back mechanism — the last P1 stage — closing the memory loop. New **`writeback/`**
  package: `errors.py` (`WriteRefused`+`RefusalReason`, `WriteConflict` carrying current hash+content),
  `git.py` (best-effort `commit_paths` to the *vault's* repo, agent identity via `-c`, never mutates git
  config), `writer.py` (**`NoteWriter`** with `write_note`/`append_section`/`patch_frontmatter`). Every
  accepted write runs one gauntlet: containment (realpath under vault root, symlink-safe) → write allowlist
  (`writeback_allow_roots`, default `agent-inbox/`) → the *same* index gates as the scanner
  (`exclusion_reason` + `scan_for_secrets` on content) → size/rate caps → optimistic lock (patch only) →
  **write → git-commit → synchronous `ingest_path`** (reuses Stage 4's primitive). `append_section` is
  additive/unlocked (inserts at the section end via `heading_section`); `patch_frontmatter` requires
  `base_hash` (409 = `WriteConflict` + current content). Index-failure is non-fatal (note still written +
  committed; `WriteResult{indexed=False, warning}`). Added `WriteResult` (`models.py`); wired 3 tools into
  `build_mcp_server(writer=…)` (registered only when a writer is supplied) with model-facing descriptions +
  `WriteRefused`/`WriteConflict`→`ToolError`; `cli.py mcp` opens the store + builds a `NoteWriter` **by
  default** (`--no-write` / `VAULTRAG_WRITEBACK_ENABLED=false` → read-only), shared with `--watch`. 6 config
  knobs (`writeback_enabled`, `writeback_allow_roots`, `writeback_author`, `writeback_max_bytes`,
  `writeback_max_writes_per_minute`, `writeback_git_enabled`). Green: **315 tests** (+27:
  `test_writeback.py` new [20], `test_mcp.py` [+5], `test_cli.py` [+2]), ruff, ruff format, mypy (41 src).
- **Why:** Anik: *"implimnet stage 5"* — Stage 5 of `../Rag/projects/vaultrag/p1-step-plan.md` (arch §17;
  `write-back-design.md`; `reference-security-model.md`). This is the line that turns the search index into
  agent long-term memory (`reference-mcp-tools.md`): after a decision/fix an agent `write_note`s a record
  and a future session `search`es it.
- **Verify:** (1) unit tests drive the real `NoteWriter` against real SQLite + in-memory Qdrant + a fake
  embedder — create/append/patch actually parse→chunk→embed→upsert→retrievable; guardrails incl. the
  first-class **planted-secret-content unwritable AND never indexed** (write-side twin of P0 Step 3),
  path-traversal, allowlist, `base_hash` 409, size/rate caps, a **real git commit in a throwaway repo**
  (asserts author + `agent(claude-code): created …` subject), and index-failure-is-nonfatal (monkeypatched
  Qdrant/Ollama failure → write survives). (2) **A full Ollama-backed dogfood against the live
  `vaultrag-design` vault PASSED** (`scratchpad/writeback_dogfood.py`, `git_enabled=False` so the user's Rag
  git history is never touched — git is already proven in the unit test): reconciled `../Rag` (count 145),
  `write_note` → real embed → `indexed chunks=1` (145→146), **a real hybrid `search` for the token ranked
  the freshly-written note #1** with `obsidian://open?vault=Rag&file=agent-inbox%2F…`; `append_section`
  landed + reindexed; `patch_frontmatter` conflicted on a stale `base_hash` then patched on the live one;
  then the note was deleted + evicted (count back to 145). Confirmed the Rag vault git status shows only the
  pre-existing `p1-step-plan.md` edit — no stray file, no new commit.
- **Files:** `src/vaultrag/`: `writeback/{__init__,errors,git,writer}.py` (new), `models.py` (WriteResult),
  `config.py` (6 knobs), `mcp/server.py` (writer param + 3 tools + `_register_write_tools`/`_conflict_message`),
  `cli.py` (store-for-writeback + `--write/--no-write`). Tests: `tests/test_writeback.py` (new),
  `test_mcp.py`, `test_cli.py`. **Vault:** `p1-step-plan.md` Log (Stage 5 done).
- **Next / handoff:** **P1 is code-complete — the whole roadmap moves to P2** (`CLAUDE.md` §4): Docling PDFs,
  Excalidraw/Mermaid extraction, OCR, `related`/graph-expand (retrieval stage 6), the **full** eval harness
  + merge-blocking CI gate (only a *minimal* golden set exists so far), Prometheus. Open write-back polish
  for later (not blockers): the write allowlist + rate cap are per-*server-session* (no api_keys/tenancy yet
  — that's P3); `write_note` treats `content` as pure body and owns the frontmatter (a body that itself
  starts with `---` would double up — document/guard if it ever bites); `.ragignore` is re-read per write
  (correctness over speed). Consider promoting the hardened `WriteResult` shape into
  `../Rag/reference/reference-mcp-tools.md` (still params-only). Working tree: new `src/vaultrag/writeback/`
  + `tests/test_writeback.py`, edits across models/config/mcp/cli + test_mcp/test_cli; vault plan note edited
  (separate repo). Commit when Anik asks.

### 2026-07-19 — P1 Stage 4 (single-file ingest + watchdog live-watch) implemented + mechanism-verified
- **Did:** Implemented Stage 4 end-to-end. (1) **Primitive extraction** (`indexer/pipeline.py`): added
  `IngestContext` (bundles `project/vault/store/vector_store/embedder`) and public `ingest_path` / `remove_path`
  returning `IngestOutcome{status, chunks, reason}` (`IngestStatus` = indexed/unchanged/removed/skipped;
  `SkipReason` = excluded/secret/gone). `ingest_path` reconciles one path against disk and **re-applies both
  boot-scan gates** (`exclusion_reason` + `scan_for_secrets`) so live-watch/write-back can't index what a
  reindex withholds, evicting a path that became gated/secret/gone; it hashes **raw bytes** (matches scan) and
  skips re-embed on an unchanged hash. Refactored `reindex_vault`'s `_process_*` to take `ctx` and share
  `_ingest_file`/`_delete_file`; public signature + `ReindexStats` unchanged (269 prior tests stayed green).
  (2) **`indexer/watch.py`** (new): `VaultWatcher` = `watchdog` Observer → per-path 1.5 s debounce → dispatcher
  thread → `ThreadPoolExecutor(4)` → `ingest_path`, jobs-table row per path, retry 3× exp-backoff → `dead`.
  Per-path serial (in-flight not re-dispatched), parallel across paths. Dir delete/move → prefix-evict +
  dest-subtree walk. Test hooks: `process_path`, `flush`, `wait_idle`, injectable `clock`/`sleep`. (3) **CLI**
  (`cli.py`): new `watch` command (boot-reconcile then watch, blocks on Ctrl-C) + `mcp --watch` (daemon-thread
  watcher, opens a `SQLiteMetaStore`, stops+closes in the finally). (4) **config**: `watch_debounce_seconds`
  (1.5) + `watch_workers` (4). (5) Added `watchdog>=4.0` to `pyproject.toml` (`watchdog==6.0.0`). Tests:
  `test_ingest_primitive.py` (new, 10), `test_watch.py` (new, 6 incl. real-Observer e2e + save-storm
  coalescing + retry→dead), `test_cli.py` (+3). Green: **288 tests**, ruff, ruff format, mypy (39 src files).
- **Why:** Anik: *"impliemt the the next stage"* — Stage 4 of `../Rag/projects/vaultrag/p1-step-plan.md`
  (arch §4). Closes the freshness half of P1: an edit is searchable within ~2 s with no manual reindex, and the
  same `ingest_path` primitive is what Stage 5 (write-back) will call for its synchronous same-session reindex.
- **Verify:** two levels. (1) The **real-`watchdog`-Observer e2e** test
  (`test_watch.py::test_live_watch_indexes_then_evicts_a_file`) drives the actual observer → debounce → worker
  pool → `ingest_path` against real SQLite + real Qdrant(`:memory:`): writing a note indexes it within the
  debounce; deleting it evicts it. Plus the **planted-secret-content-never-indexed** guardrail (write-side twin
  of P0 Step 3's). (2) **A full Ollama-backed dogfood against the live `vaultrag-design` vault PASSED** — Ollama
  came up mid-session (`qwen3-embedding:0.6b` present), Qdrant up. `scratchpad/watch_dogfood.py`: reconciled
  `../Rag`, started a `VaultWatcher`, wrote a throwaway `agent-inbox/` note → `status=indexed chunks=1` (Qdrant
  145→146) via real Ollama embed; a **real hybrid `search` for its token ranked it #1** with a working
  `obsidian://open?vault=Rag&file=…%2F…` URI; deleting it → `status=removed` (count back to 145); vault + index
  left clean. The observer also fired on `.obsidian/workspace.json` and correctly `status=skipped` it (live gate
  proof), and a `status=unchanged` re-fire showed idempotency under a save storm. So the embed-call piece
  (the only thing the e2e faked) is now proven live too.
- **Files:** `src/vaultrag/`: `indexer/pipeline.py` (primitive + ctx), `indexer/watch.py` (new), `cli.py`
  (watch cmd + `mcp --watch`), `config.py` (2 knobs), `pyproject.toml`/`uv.lock` (watchdog). Tests:
  `tests/test_ingest_primitive.py` (new), `tests/test_watch.py` (new), `tests/test_cli.py`. **Vault:**
  `../Rag/projects/vaultrag/p1-step-plan.md` Log (Stage 4 done).
- **Next / handoff:** **Stage 5 — write-back + MCP write tools + git auto-commit** (`writeback/*` new,
  `mcp/server.py` +3 tools `write_note`/`append_section`/`patch_frontmatter`, `cli.py`). Reuses Stage 4's
  `ingest_path` for the synchronous post-write reindex. Guardrails (CLAUDE.md §6): realpath containment under
  vault root, `agent-inbox/` default write root, `base_hash` optimistic lock (409 + current content), size/rate
  caps, `.ragignore`d paths unwritable, per-file `agent(<name>): <what>` git commit, and a **planted-secret
  path unwritable + never indexed** test (the write-side twin, now also covered on the ingest side). Minor
  Stage-4 notes: (a) `.ragignore` is loaded **once** at watcher start (edit it → restart the watcher);
  (b) `mcp --watch` opens a second SQLite connection on the same file as any running `watch`/indexer — solo
  dogfooding only, per the settled Decisions. Consider a tiny `../Rag/agent-inbox/` note that `ingest_path`
  hashes raw bytes (parity with scan) — impl-level, like the existing payload notes. Working tree: `src/` +
  `tests/` edits, `pyproject.toml`/`uv.lock` changed; vault plan note edited (separate repo). Commit when Anik asks.

### 2026-07-16 — P1 Stage 3 (`obsidian://` citations, retrieval stage 8) implemented + live-verified
- **Did:** Implemented Stage 3 end-to-end. (1) New pure `retrieval/cite.py`: `obsidian_uri(vault_name,
  path)` → `obsidian://open?vault=…&file=…`, both parts `quote(..., safe="")`-encoded so spaces (`%20`)
  and subfolder slashes (`%2F`) round-trip; `cite(snippets, *, vault_name)` returns copies with `Snippet.uri`
  stamped (no name → returns the list unchanged, `uri` stays `None`). (2) **Line range** wired through the
  whole path: `chunking/chunker._line_span` sets each chunk's `line_start`/`line_end` (min/max over its
  blocks); `index/payload.py` carries them; `retrieval/pipeline._to_snippet` reads them (`_as_int` guards
  bool/None); `ParentSection` gained `line_start`/`line_end` (from `heading_section`) so a swapped-in parent
  cites the *section* span, not the child's. (3) Models: `Snippet` gained `uri`/`line_start`/`line_end`,
  `Chunk` gained `line_start`/`line_end`. (4) `Settings.vault_name` (default `None`); `cli.py mcp` resolves
  `settings.vault_name or vault_path.resolve().name` and threads it via `build_mcp_server` →
  `retrieval.search(..., vault_name=…)` → `cite` (run before `pack_budget`; a URI isn't text so it costs no
  budget). Exported `cite`/`obsidian_uri` from `retrieval/__init__`. Tests: new `tests/test_cite.py` (URI
  shape/encoding/round-trip + cite no-op/no-mutate), + line-range/URI cases in `test_retrieval.py`,
  `test_chunking.py` (chunk line span), `test_parents.py` (section span), `test_mcp.py` (served URI wiring),
  `test_index.py` (payload line fields); `fakes.make_parent` + `make_chunk` gained line params. Also reflowed
  one **pre-existing** over-long comment in `config.py` (rerank block) that was failing `ruff check` on HEAD.
  Green: **269 tests**, ruff, ruff format, mypy (37 src files).
- **Why:** Anik: *"impleinmt the next stage"* — Stage 3 of `../Rag/projects/vaultrag/p1-step-plan.md`
  (arch §10-stage8). URIs make every citation one click from Obsidian; the line range + `path#heading` let an
  agent `open(path, lines=[…])` precisely.
- **Live verify:** wiped `state/vaultrag.db` (backup in the session scratchpad) because these edits don't
  change chunk/doc hashes — a plain reindex would skip every doc, so the manifest wipe forces a full re-upsert
  that backfills the new payload fields. Reindexed `vaultrag-design` (55 docs / 143 chunks). A direct
  in-process `search(..., vault_name="Rag")` against live Qdrant+Ollama+vault returned working
  `obsidian://open?vault=Rag&file=…%2F…` URIs on every hit, parent-swapped hits citing the exact section line
  range (Stage 3 section → lines 82–91; verified against the file), and a plain child hit with its payload line
  range. **Caveat:** the registered `vaultrag` MCP tool still returned the old snippet shape (no `uri`/line
  keys) — its stdio subprocess was spawned before these edits this session; a fresh spawn (next session) serves
  the new code. The in-process script is the faithful proof.
- **Files:** `src/vaultrag/`: `retrieval/cite.py` (new), `retrieval/pipeline.py`, `retrieval/parents.py`,
  `retrieval/__init__.py`, `chunking/chunker.py`, `index/payload.py`, `models.py`, `config.py`,
  `mcp/server.py`, `cli.py`. Tests: `tests/test_cite.py` (new), `test_retrieval.py`, `test_chunking.py`,
  `test_parents.py`, `test_mcp.py`, `test_index.py`, `fakes.py`. **Vault:** `p1-step-plan.md` Log (Stage 3
  done). `state/vaultrag.db` rebuilt.
- **Next / handoff:** **Stage 4 — single-file `ingest_path`/`remove_path` primitive + watchdog live-watch**
  (`indexer/watch.py`, extract the primitive from `reindex_vault`; 1.5 s debounce, `jobs` table, worker pool;
  `serve`/indexer owns the watcher, `mcp --watch` opt-in). Stage 4 must land before Stage 5 (write-back reuses
  `ingest_path`). Minor: `Snippet.uri` is base `obsidian://open` only (no heading/line jump — that needs the
  Advanced-URI community plugin); the heading+line range travel as separate snippet fields, matching arch
  §10-stage8. Consider a tiny `../Rag/agent-inbox/` note that the payload now also carries `line_start`/
  `line_end` (impl-level, like the existing `payload-carries-chunk-text.md`). Working tree: many `src/`+`tests/`
  edits; `state/` rebuilt; vault plan note edited (separate repo). Commit when Anik asks.

### 2026-07-16 — P1 Stage 2 (parent-child + parent-swap) implemented + live-verified; superset guard added
- **Did:** Implemented Stage 2 end-to-end (retrieval stage 5). (1) **Chunker parent linkage**:
  `chunking/chunker.py` now sets `parent_id` to a pointer `<path>#<anchor>` at the enclosing H1/H2
  section via a new shared pure helper `parsing/sections.parent_anchor(heading_path)` (depth ≤ 2:
  a deeper child rolls up to its H2). No parent text stored — the pointer is a record, retrieval
  reconstructs. (2) **`retrieval/parents.py` (new)**: `ParentSection`, `ParentResolver` protocol,
  `VaultParentResolver` (read note via injected gated reader → `parse_document` → `heading_section`
  → slice raw lines → `count_tokens`; memoizes parses per instance), and the `parent_swap` policy
  (group hits by `(path, parent-anchor)` derived from each hit's `heading_path`; resolve each parent
  once; swap + dedupe same-section children into one parent snippet inheriting the best child's rank).
  (3) Inserted into `retrieval/pipeline.py` after rerank (`search` gained `parent_resolver` +
  `parent_max_tokens`); (4) wired `mcp/server.py` (builds a **fresh** `VaultParentResolver` per search
  call reusing `open`'s `_read_note` — per-call cache **and** freshness; **snippets mode only**,
  compact skips the vault reads) + `cli.py`; (5) config `parent_swap_enabled` (**default ON**) +
  `parent_max_tokens` (2000). Tests: rewrote `test_chunking` parent_id cases, added parent-swap cases
  to `test_retrieval` (swap/dedupe/cap-fallback/unresolved-fallback/different-sections/root-no-swap/
  **superset guard**), new `test_parents.py` (resolver reconstruct/missing/unknown-heading/parse-once),
  new `test_mcp` cases (reconstruct-from-real-vault, **secret-gated note → falls back to child**,
  compact-skips-swap). Green: **252 tests**, ruff, ruff format, mypy (35 files).
  **Live-verified against the running index** (reindex → SQLite parent_id check → live `search`).
- **Why:** Anik: *"impliment p1 - s2"* — Stage 2 of `../Rag/projects/vaultrag/p1-step-plan.md`.
  Default ON (unlike Stage 1's OFF) because it's a **core** stage and — with the guard below — a
  **measured non-regression**: on the re-run harness (2026-07-16) the shipping fusion baseline is
  recall@10 **1.000** / MRR@5 **0.775** (both > targets), and parent-swap can't move path-recall/
  MRR@5 **by construction** — it repackages *within* a note and the parent inherits the child's
  first-occurrence rank; dedup still fires. (The committed `run_eval.py` doesn't pass a
  `parent_resolver`, so it measures fusion-vs-rerank, not swap on/off — the neutrality is a
  structural argument, not a harness column.)
- **Key finding — superset guard (the live dogfood earned its keep):** the first live run swapped
  *every* hit, and several parents came back **smaller than the child** (e.g. a 220-tok
  `decisions/vector-search-qdrant.md` child → a 38-tok "Decision" parent). Cause: a **tiny-sibling
  merge** (chunker Pass 3) folds several small H2 sections into one chunk but keeps only the *first*
  section's `heading_path`, so reconstructing "that section" is a strict *subset* of the child and
  the swap **loses content**. Fix: swap only when `max(child tokens in group) ≤ parent tokens ≤
  min(parent_max_tokens, budget_tokens)` — i.e. the parent must be a genuine superset. Parent-swap
  is now **strictly non-lossy**: it enriches (bigger coherent section) or keeps the child, never
  shrinks. On this vault most small notes correctly keep their child; big multi-section notes
  (`p0-step-plan`, `p1-step-plan`, architecture §-sections) genuinely swap + dedupe.
- **Files:** `src/vaultrag/`: `chunking/chunker.py`, `parsing/sections.py` (+`parent_anchor`,
  `PARENT_MAX_DEPTH`), `parsing/__init__.py`, `retrieval/parents.py` (new), `retrieval/pipeline.py`,
  `retrieval/__init__.py`, `config.py`, `mcp/server.py`, `cli.py`. Tests: `tests/fakes.py`
  (+`FakeParentResolver`, `make_parent`), `tests/test_chunking.py`, `tests/test_retrieval.py`,
  `tests/test_parents.py` (new), `tests/test_mcp.py`. **Vault plan note** `p1-step-plan.md` Log
  updated (Stage 2 done + superset-guard finding).
- **Next / handoff:** **Stage 3 — `obsidian://` citations (retrieval stage 8)**: per snippet emit a
  working `obsidian://open?vault=<name>&file=<url-encoded path>` URI (+ line range); needs a
  `vault_name` config knob; `Snippet` already reserves the URI field. **Possible vault write-back
  for Anik:** the superset-guard finding is a durable chunking/retrieval nuance (a tiny-sibling-merge
  chunk's `heading_path` under-labels it) — consider a note in `../Rag/agent-inbox/`; a cleaner
  future fix would relabel a merged chunk with the common-ancestor heading, but the guard is the
  correct P1 answer. Note: reindex re-chunked only the 4 *new/changed* notes (idempotency), so the
  51 unchanged docs still have `parent_id=NULL` in SQLite — parent-swap doesn't care (it derives the
  pointer from `heading_path` in the Qdrant payload), but a `reindex` after a chunker change won't
  backfill unchanged docs without a state wipe. Working tree: many `src/`+`tests/` edits, `state/`
  rebuilt. Commit when Anik asks.

### 2026-07-16 — P1 Stage 1 (reranker) implemented + eval'd; threshold finding → default 0.3→0.0
- **Did:** Implemented Stage 1 end-to-end. New `retrieval/rerank.py`: `Reranker` protocol,
  `BgeReranker` (bge-reranker-v2-m3 ONNX-int8 via `onnxruntime`+`tokenizers`+`huggingface_hub`,
  no torch/transformers — **verified reachable and correct**: model inputs `input_ids`+
  `attention_mask`, one logit→sigmoid, relevant passages score high / off-topic ~0), `NoOpReranker`
  fallback, `rerank_candidates` stage (top_n prefetch, threshold, keep=k, dominant-top-1 fast
  path), `load_reranker` (disabled→None, load-fail→NoOp). Wired into `retrieval/pipeline.py`
  (stage 4, fetch widens to top_n only when reranking), `mcp/server.py`, `cli.py mcp`. Added 6
  config knobs (`config.py`). Declared the ML deps directly in `pyproject.toml` (+ mypy
  overrides). Built the **minimal golden set + eval harness** (`tests/eval/golden.json` 20 pairs,
  `run_eval.py`) per decision 3. Added `tests/test_rerank.py` + rerank cases in `test_retrieval.py`
  + `FakeReranker`. **Ran the eval on the live index** and found rerank **regressed** vs fusion
  (recall@10 1.00→0.90, MRR@5 0.825→0.767). Diagnosed: the vault's **0.3 threshold** evicts
  relevant chunks (bge-v2-m3 sigmoid scores relevant design-doc chunks at ~0.02–0.25; a #3-ranked
  correct note scored only 0.2145 and got cut). A **threshold sweep** (rerank once, evaluate all
  thresholds in memory) confirmed 0.3 is the worst point and **0.0 (no floor) the best** (recall
  1.00, MRR 0.796). Set the threshold default to **0.0** (`rerank.py` `DEFAULT_THRESHOLD`,
  `config.py`). Even at 0 floor rerank is marginally < fusion on this *title-matchy* set —
  expected, the set is too easy to show rerank's value (dominant fast-path skips most queries).
  **Per Anik, then set `rerank_enabled=False` (rerank OFF by default):** fusion currently scores
  higher, so we don't ship a measured regression on; the stage is code-complete behind the flag.
- **Why:** Anik: *"lets implement the phase 1 stage 1"*; then, on seeing the regression, confirmed
  a broader insight — **hardcoded score-distribution thresholds can't be optimal per-vault, and
  open-source users won't get good results from my number**; asked to (a) write it to the vault for
  the future and (b) test whether reducing the threshold performs better (it does).
- **Files:** `src/vaultrag/retrieval/rerank.py` (new), `retrieval/pipeline.py`, `retrieval/__init__.py`,
  `mcp/server.py`, `cli.py`, `config.py`, `pyproject.toml`; `tests/test_rerank.py` (new),
  `tests/test_retrieval.py`, `tests/fakes.py`, `tests/eval/{__init__,golden.json,run_eval}.py` (new).
  **Vault:** `../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md` (new — the
  durable design ask: distribution-free default gate + opt-in `vaultrag calibrate` + per-project
  persisted settings; supersedes the "0.3" line in `reranking.md`/`reranker-bge-v2-m3.md`/arch §12).
- **Next / handoff:** **For Anik to rule on:** ratify the per-vault-threshold note (esp. *where
  per-project settings live*). Default posture is **settled: rerank OFF by default** (Anik's call —
  fusion wins on this set); re-enable via `VAULTRAG_RERANK_ENABLED=true` per-vault. **Then:** either
  build the harder/paraphrase golden set to get a real rerank verdict (the thing that would justify
  flipping it on), or proceed to **Stage 2 (parent-child + parent-swap)**. Note: rerank
  on CPU is slow here (~50 long candidates ≈ 20–50 s/query, int8, no GPU) — a real latency
  observation vs arch's 100–400 ms target (GPU/tuning is the arch's answer). Working tree: many
  `src/` + `tests/` edits, untracked `tests/eval/` + `state/`; vault got 1 new agent-inbox note.
  Commit when Anik asks.

### 2026-07-16 — P1 plan reviewed + all 4 open decisions settled (plan-note edits only, no code)
- **Did:** Anik asked to verify `p1-step-plan.md` was written correctly and to settle the open
  decisions. Verified the plan against the repo + vault: all 11 vault citations exist and every
  code-state claim is accurate — **except one real error**: Stage 1 claimed `fastembed`'s
  `TextCrossEncoder` could serve `bge-reranker-v2-m3`. It can't — `fastembed==0.8.0`'s
  cross-encoder list is `ms-marco-MiniLM-L-6/L-12`, `bge-reranker-base`, `jina-reranker-v1/v2`;
  **no v2-m3**. Then settled 4 decisions (Anik deferred each to my recommendation):
  1. **Reranker** → keep bge-v2-m3 (honors `decisions/reranker-bge-v2-m3.md`), load ONNX-int8
     **directly via `onnxruntime`**, not fastembed. Probed HF to de-risk: the canonical
     `BAAI/bge-reranker-v2-m3` ships **no ONNX**, and `torch`/`transformers`/`optimum` are all
     **absent** from the tree — but **`onnx-community/bge-reranker-v2-m3-ONNX` ships
     `onnx/model_int8.onnx` + a standalone `tokenizer.json`**, and `tokenizers 0.23.1` (already
     present via fastembed) loads it with no `transformers`. So the vault model is reachable with
     **zero new deps** — the plan now names the exact source so Stage 1 doesn't hit the no-ONNX
     BAAI repo.
  2. **Parent representation** → reconstruct from vault; `parent_id` is a pointer (path+heading),
     sliced via `parsing/sections.heading_section` at swap time (CLAUDE.md §6 #1).
  3. **Eval gate** → pull a *minimal* golden set forward for Stages 1–2 (≈15 pairs +
     recall@10/MRR@5 script, run manually); full harness + CI gate stays P2.
  4. **Watcher** → `serve`/indexer owns it; `vaultrag mcp` watcher-free unless `--watch`.
- **Why:** Anik: *"check the phase 1 break into stages file writen correctly or not"* + settle the
  3 flagged decisions; then *"do what u think is best"* → also verified the reranker ONNX source
  and updated the handoff docs.
- **Files:** `../Rag/projects/vaultrag/p1-step-plan.md` (Stage 1 reranker corrected + concrete
  ONNX source; Stage 2/4 wording; Deferred note; **Open questions → Decisions**; Log). `MEMORY.md`
  (this entry + What's-next). **No `src/` changes.**
- **Next / handoff:** Decisions are settled — **implement Stage 1 (reranker)** next:
  `retrieval/rerank.py` (`Reranker` protocol + `BgeReranker` loading `model_int8.onnx` from
  `onnx-community/bge-reranker-v2-m3-ONNX` via onnxruntime + `tokenizers`, + `NoOpReranker`
  fallback), insert as retrieval stage 4 after RRF fusion, + config knobs. Build the minimal
  golden set alongside it (decision 3). Working tree: `MEMORY.md` dirty + untracked `state/`; the
  vault plan note is edited (separate git repo). Commit when Anik asks.

### 2026-07-16 — P1 decomposed into 5 stages (plan only, no code)
- **Did:** At Anik's request, broke P1 into **5 implement-one-at-a-time stages** and wrote them
  to the vault as `../Rag/projects/vaultrag/p1-step-plan.md` (sibling of `p0-step-plan.md`, same
  format). Stages: **1** reranker (bge-v2-m3 via fastembed `TextCrossEncoder`, retrieval stage 4)
  → **2** parent-child + parent-swap (stage 5) → **3** `obsidian://` citations (stage 8) → **4**
  single-file `ingest_path` primitive + watchdog live-watch → **5** write-back (`write_note`/
  `append_section`/`patch_frontmatter`) + git auto-commit. Quality trio (1–3) is read-path-only
  and independent; hard ordering is **4 before 5** (write-back reuses Stage 4's `ingest_path`).
  Grounded the plan in a code check: `parent_id` is currently hardcoded `None` in
  `chunking/chunker.py`; `config.py` has no rerank/watch/vault-name knobs; no write tools exist.
- **Why:** Anik: *"lets brake the phase 1 into multiple stages to impliment"* — chose *persist the
  plan only* (no coding this session) and *put it in projects/vaultrag* (not agent-inbox).
- **Files:** `../Rag/projects/vaultrag/p1-step-plan.md` (new, vault), `MEMORY.md` (this entry +
  What's-next pointer). **No `src/` changes.**
- **Next / handoff:** Settle the 3 open decisions in the plan (parent representation:
  reconstruct-from-vault recommended; eval-gate-vs-dogfood since the harness is P2; watcher
  process model), then implement **Stage 1 (reranker)** first. Working tree: `MEMORY.md` dirty +
  the untracked `state/`; the vault got a new tracked file. Commit when Anik asks.
