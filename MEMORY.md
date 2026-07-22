# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** **P3 IN PROGRESS — Stages 1, 2 and 3 all DONE (2026-07-22); Stage 4 (`serve`)
NOT started.** P2 is COMPLETE (all 7 stages + the 4-bug audit). **Stage 2 is merged to `main`
(PR #15, `03bce8f`) — Anik merged it mid-session.** Stage 3 sits on branch
**`feat/p3-stage3-query-log`**. Plan at `../Rag/projects/vaultrag/p3-step-plan.md`.
- **P3 STAGE 3 (`query_log` + the mining loop) COMPLETE 2026-07-22.** **Green: 789 tests
(+48), ruff, mypy (60), gate unmoved 1.000/0.892/0.920.** **Decision 2 was settled by Anik:
option (a)** — nullable `query_text` column, `VAULTRAG_QUERY_LOG_TEXT` default **on** for
local/stdio, off for shared HTTP; written up as
`../Rag/agent-inbox/query-log-stores-text-opt-in.md` (supersedes `reference-database-schema.md`).
**`rate_result`: not built** (Anik's call — no consumer; the P2-Stage-6 "left off *and unbuilt*"
precedent).
  - **Recording lives in `retrieval/pipeline.search`, not the MCP layer**, so Stage 4's REST
  surface inherits the identical row instead of growing a divergent copy —
  `test_the_mcp_tool_and_the_bare_pipeline_write_the_same_row` is what keeps that true. New
  `retrieval/querylog.py` (`q_hash`, `QueryLogSink` protocol, `QueryLogParams`,
  `record_query`, `query_log_params_for`). Recording is outside the `total` stage timer (the
  log must not be charged to the thing it measures) and **can never fail a search**.
  - **`vaultrag queries`**: `summary` / `frequent` / `slowest` / `recent` / `worst` /
  `zero-hit` reports, `--export` to a golden-set-shaped candidate file, `--prune`,
  `--since-days`, `--max-score`. Retention (90 days / 200k rows) prunes **as the log is
  written**, amortized every 100 writes + always on a process's first write.
  - **THE FINDING THAT CHANGED THE STAGE — the plan's zero-hit report is inert, and
  dogfooding is what proved it.** On the live design vault, *"how do I rotate the stripe
  webhook signing secret"* returns **8 hits** from a vault that has never heard of Stripe —
  identical to a well-answered query. A hybrid retriever ranks *something* first for
  everything; **`n_results` carries no signal at all**, so the mining loop this stage exists
  for mined nothing. Fixed by adding **`top_score REAL`** to the row (measured separation:
  answerable 0.57–1.21, unanswerable 0.50–0.61, the latter clustered just above the RRF
  floor) plus a **threshold-free `--report worst`** that ranks rather than cuts — the bands
  overlap, and the score scale depends on which stages ran (RRF / cross-encoder sigmoid /
  constant), the same scale-free lesson as `graph_boost` and rank-based MMR. **This is a
  SECOND supersession of `reference-database-schema.md`, beyond what Anik ratified, and is
  flagged as such in the agent-inbox note rather than folded in silently.**
  - **The loop closed on its first real use and found a live retrieval bug.** `--report
  worst` ranked *"write-back safety guardrails"* third-worst (0.52) — an answerable
  question — and it is a genuine miss: `write-back-design.md` comes back **4th**, behind
  `p1-step-plan.md`, `knowledge-graph-integration.md`, `reference-security-model.md`. That is
  exactly the golden-set case `evaluation-framework.md` wants and nobody had written.
  - **`query_log` is the only table that needed a real migration path.** Every other table is
  a derived cache where drift is repaired by deleting the state dir ("DR = reindex",
  CLAUDE.md §6); nothing can rebuild a record of what people asked, so a missing column here
  is *data loss* — and `CREATE TABLE IF NOT EXISTS` is silent about columns.
  `init_schema` now adds absent `query_log` columns explicitly (additive, idempotent).
  - **The Stage-2 cascade lesson, applied one table over.** `query_log` has **no FK** to
  `projects` on purpose (a search can run against an unregistered id — the P0
  `--project X --vault Y` shorthand — and a FK would make logging fail exactly where it is
  most interesting), so `delete_project` reaches it only because it *names* it. With the text
  column on, rows left behind are the departing tenant's raw queries, handed to whoever
  re-registers the id. Own test.
  - **The privacy boundary is test-pinned, not just documented.** CLAUDE.md §5 governs the
  **log stream**; Decision 2 makes the DB the one exception. Tests assert — **parameterized
  over both settings of the knob** — that the literal query text appears in neither the
  captured structlog events nor the rendered metrics registry. `mcp/server.py` now uses the
  same `q_hash()` the rows store, so a log line and a row are joinable.
  - **`query_log_retention_days`/`query_log_max_rows` are RESERVED per-project**
  (`projects._RESERVED_SETTINGS`) — one shared table pruned by one shared store opened from
  *base* settings, so a per-project value would be stored and never consulted.
  `query_log_enabled`/`query_log_text` stay overridable (genuinely per-tenant).
- **P3 STAGE 2 (multi-project registry) COMPLETE 2026-07-22 — one process now serves N
projects.** The foundation (registry CRUD + settings resolution + `vaultrag project add|list|
rm|show`) landed first and is **merged to main** (PR #14, `5c8b4f2`); this session finished the
second, higher-risk half. **Green: 734 tests (+52), ruff, mypy (59), gate unmoved
1.000/0.892/0.920.**
  - **`build_mcp_server` now takes `projects: Sequence[ServedProject]`** instead of a
  process-wide `project_id`/`vault_path`/`writer`. `ServedProject` bundles what is genuinely
  per-tenant — `vault_root` (resolved in `__post_init__`; containment compares against it),
  `writer`, `embedder`, `vault_name`, and every retrieval knob — while the vector store, sparse
  embedder, reranker and metadata store stay **shared**, because those are already
  `project_id`-scoped on every call. **One `_select(project)` closure is the single enforcement
  point** and every tool routes through it; the first draft had `overview` doing its own lookup
  and that was rewritten, because a second enforcement point *is* the tenancy hole.
  - **`project` is now a per-call argument on `search`/`open`/`related`/`overview` and all three
  write tools.** With one served project it stays optional (P0 behaviour byte-for-byte); with
  several it is **required** — no implicit default, because a forgotten argument silently
  querying the wrong vault is the one failure multi-tenancy must not have.
  - **New tools: `list_projects()`** (always registered — it is how an agent learns the `project`
  string) and **`overview(project?)`** (registered only with a metadata store, same rule as
  `related`).
  - **CLI: `vaultrag mcp` with no `--project` now serves every registered project**;
  `--project` repeats to serve a subset; `--project X --vault Y` keeps the P0 shorthand.
  `--vault` with several projects is refused rather than guessed.
  - **`project` metrics label** on `searches_total`, `searches_empty_total`, `ingests_total`,
  `write_ops_total`, `ingest_lag_seconds` — plus `init_project_labels()`, the deferred half of
  `_build`'s pre-initialisation (label values are unknown until a process is told what to serve,
  so composition roots announce each project at startup and an idle tenant still reads 0, not
  absent). `test_metrics`'s cardinality allowlist was updated **with** it, per the plan.
  **Deliberately NOT on `search_latency`** (8 stages × 18 buckets × N tenants buys the least —
  stage latency is dominated by shared components — and costs the most); pinned by a test, since
  "we chose not to" is invisible in code.
  - **Verification is the point of this stage, so it is adversarial:** new
  `tests/test_multiproject.py` (27 tests) runs **one** server over **two registered projects**
  sharing one SQLite db and one Qdrant collection, made **maximally confusable** — same relative
  paths, same titles, same chunk text, same dense vector. Two deliberate mutations were run to
  prove it isn't vacuous: making `_select` ignore its argument killed **16 of 25**, and the
  subtler one the plan names (`open` resolving against a construction-time root) killed exactly
  the 2 tests written for it.
  - **A second, worse bug — found by DOGFOODING, not by any test: `project rm` cascaded
  SQLite and left every vector behind.** Registering this repo as a throwaway second project
  and removing it left **243 orphaned Qdrant points** with no `documents` row. Not just wasted
  space: **re-registering the same id makes stale chunks immediately searchable**, which is the
  precise drift `consistency.check_drift` exists to detect after the fact. Neither cache is
  wrong on its own — `MetaStore.delete_project` cannot reach Qdrant (wrong layer), and nothing
  told the **composition root** it had to clear both. Fixed: new
  `VectorStore.delete_by_project()` (tenant-scoped filter, like every other operation) +
  `project rm` now clears both, **vectors first** so a failure leaves the project registered and
  the command re-runnable (the other order strands live vectors with nothing left in the
  registry to clean them up), plus `--keep-vectors` as an explicit opt-out for a broken vector
  store. Verified on the real 243: → 0, neighbour's 213 untouched. **The lesson worth keeping:
  the plan's verify said "registry deletion cascades one project's rows" and the foundation's
  tests asserted exactly that — they were green while half the deletion was missing, because
  "rows" silently meant SQLite. A cascade test that only checks the store it was written
  against is not a cascade test.**
  - **One real bug found while writing it, in `projects.py`, not in the new code:
  `embed_dims` was per-project-overridable and must not be.** The shared collection is created
  with one dense vector width, so a project overriding it would upsert vectors the collection
  cannot hold — failing at index time, far from the setting that caused it. Now reserved.
  `embed_model` **stays** overridable (per-project embedding models are
  `multi-tenant-architecture.md`'s explicit design) because a different model at the *same* width
  is safe: each project's vectors are only ever compared against its own. A model at a different
  width needs that note's documented scale path — promotion to its own collection — not a
  settings key.
  - **The design call the plan reserved for the vault is written up:**
  `../Rag/agent-inbox/overview-tool-shape.md` (status `proposed`). `overview` returns a
  **depth-2 counted folder map + top-20 tags + note count — a map, never a file listing**, both
  lists bounded by a constant so response cost is flat in vault size, and **no `budget_tokens`
  argument** (a budget param would imply the response could need one). Depth 2 was *measured*,
  not guessed: this design vault's 63 notes fold to **7 folder entries** at depth 2 — the whole
  taxonomy — while depth 1 would collapse `projects/vaultrag` into `projects` and lose the only
  distinction that matters in a multi-project vault. Arch §14's "hygiene analytics via overview"
  (orphans/hubs/unresolved) was deliberately **not** built: that is a report for a vault author,
  not orientation for a fresh session.
- **P3 STAGE 1 (deterministic stage-7 compressor) DONE 2026-07-22.** New
`retrieval/compress.py` replaces P0's greedy `pack_budget` in the pipeline with the four
`context-compression.md` steps: **near-dupe drop → MMR (λ=0.7) → per-doc-capped greedy pack
→ trim**. Wired into `retrieval/pipeline.py` (stage `pack` → `compress`; `metrics.SEARCH_STAGES`
  - `test_metrics` updated), threaded through `mcp/server.py`, `cli.py`, and the **eval harness**
  (so the gate scores the shipped config), with per-vault knobs `compress_dupe_threshold` (0.9),
  `compress_mmr_lambda` (0.7), `compress_per_doc_cap` (3). **Gate unmoved: recall@10 1.000 · MRR@5
  0.892 · nDCG@10 0.920.** **669 tests** (+24, all of `test_compress.py`), ruff, mypy (58).
  **Decision 3 RESOLVED — lexical (token-set Jaccard), not Qdrant vectors, and for a reason the
  plan didn't anticipate.** The plan expected int8 imprecision to decide it; it doesn't —
  *measured* on the hermetic fixture, int8-quantized cosine tracks full precision to **max Δ
  0.0013** and flips the 0.92 decision on **0/3968** pairs. The real blocker is **parent-swap**
  (stage 5, ON by default): it emits snippets reconstructed live from the vault with **no stored
  vector** (`parents.py`: `chunk_id=None`), and MMR needs one consistent similarity across *all*
  candidates — so the vector "fallback" would be the *common* path, mixing two incommensurable
  scales. Lexical applies uniformly to child + parent snippets, needs no extra Qdrant round-trip,
  and keeps "free, ~0 ms". `**index/qdrant.py` was NOT touched** (the plan listed it conditional on
  the vector path). Written up in `../Rag/agent-inbox/compression-similarity-is-lexical-not-vector.md`
  (status `proposed`; supersedes the plan's Decision 3 + `context-compression.md`'s "cos > 0.92" —
  should graduate to `decisions/` if Anik accepts). MMR relevance is **rank-based, not raw score**
  (same scale-free lesson as `graph_boost` — RRF/sigmoid/constant scores can't be mixed with a
  [0,1] diversity term), which also makes the **top hit un-demotable**. **Dogfood (58 design-golden
  queries, budget 2000, parents+graph on): near-neutral** — distinct-docs 5.26 → 5.34, tokens
  ~1724 → ~1729, compress() p50 1.5 ms / p95 2.7 ms. Correct: parent-swap already removes
  same-section redundancy and the vault has zero near-dupe pairs, so there's little to compress —
  the four steps are cheap insurance for messier inputs (proven by unit tests) and the gate
  confirms zero harm.
- **P3 IS NOW DECOMPOSED INTO 7 STAGES — `../Rag/projects/vaultrag/p3-step-plan.md`** (written
2026-07-22, the sibling of `p0-`/`p1-`/`p2-step-plan.md`; the P2 handoff's "there is no P3 step
plan in the vault yet" is resolved). Order: **1** real stage-7 compression (near-dupe → MMR →
per-doc cap → trim) → **2** multi-project registry (+ `list_projects`/`overview`, `project`
metrics label) → **3** `query_log` + mining CLI → **4** `**serve`** (FastAPI `/v1` + `ROLE`
switch + `/metrics` mount + the outstanding startup drift check) → **5** api_keys/scopes/rate
limits → **6** Dockerfile + compose profiles (Grafana/Prometheus/Langfuse/alerts) → **7**
optional `answer` mode. **The structural finding: `serve` is a P3 stage in its own right** —
`CLAUDE.md` §4's P3 bullet never names it, but keys, rate limits and the compose profiles all
presuppose an HTTP transport, and `reference-rest-api.md` + `reference-docker-compose.md` are
both written as if it exists. It does not. Core (1-3) before the transport (4), transport before
what it carries (5-6). **5 decisions raised, none settled — Anik rules:** (1) `answer` mode
probably **should not be built** (every consumer is already an LLM; the precedent is P2 Stage 6's
VLM pass, left off *and unbuilt*); (2) `**query_log`'s schema contradicts itself** — it stores
`q_hash` while the same reference note calls it the golden-set mining source, and a hash cannot
become a query, so Stage 3 needs a superseding decision on storing query text; (3) MMR's vector
source needs a **measurement** (int8-quantized vectors vs the 0.92 cosine threshold) before it's
chosen; (4) `serve` ships with a **bootstrap key** so the security model's "HTTP always requires
a key" holds from the first commit without merging two stages; (5) the Docker image splits into
`latest` (base) + `formats` (pdf extra) tags.
- **P2 IS COMPLETE — all 7 stages shipped + the closing 4-bug audit CLOSED.** Full per-stage
detail (eval harness + merge-blocking CI gate → link-graph persistence → `related`/graph-expand
→ PDF via Docling → Excalidraw+Mermaid → OCR/image text → Prometheus `/metrics`), plus the
mid-stage 3-bug fix, the rerank-mode + index-drift work, and the 4-bug audit (all fixed/merged
and re-verified 2026-07-21), is **archived in `MEMORY-p2.md`** — not repeated here (log, not
design doc; CLAUDE.md §7). What P3 builds on: merge-blocking eval gate **recall@10 1.000 · MRR@5
0.892 · nDCG@10 0.920**, the wikilink graph as data (`links` table), PDF/diagram/OCR ingestion,
a standalone Prometheus exporter, and `main` branch-protected (4 required checks). 645 tests at
P2 close; 669 after P3 Stage 1.
- **STILL OPEN from P2 (needs an Anik/vault decision; does NOT block P3 Stage 2) — audit item 5:
zero-chunk "ghost" docs are graph nodes, not ghosts.** A frontmatter-only stub note creates a
0-chunk `documents` row that is unretrievable by search but is a **live graph node** (resolves
`[[Stub]]` + aliases, carries its tags); the "obvious" fix (evict it like a textless image) is a
*measured* **regression** — it dangles the wikilinks and drops the tag. Written up as
`../Rag/agent-inbox/zero-chunk-docs-are-graph-nodes-not-ghosts.md` (status `proposed`); full
reasoning in `MEMORY-p2.md`.
- **(P1 archive)** **P1 CODE-COMPLETE — all 5 stages done + LIVE-verified; the memory loop is
closed.** Stage 1 rerank (bge-v2-m3 ONNX-int8, behind a flag, **OFF** by default) · Stage 2
parent-child + parent-swap (**ON**, superset-guarded / non-lossy) · Stage 3 `obsidian://`
citations + line ranges · Stage 4 single-file `ingest_path` primitive + watchdog live-watch
(1.5 s debounce, worker pool, retry→dead) · Stage 5 write-back
(`write_note`/`append_section`/`patch_frontmatter` + one guardrail gauntlet + best-effort git
auto-commit + synchronous reindex). New packages: `retrieval/{rerank,parents,cite}.py`,
`indexer/watch.py`, `writeback/`. **Green: 315 tests**, ruff, ruff format, mypy (41 src files).
**Full per-stage detail (the 5 verbose stage bullets + the 7 dated session-log entries) is
archived in `MEMORY-p1.md**` — not repeated here (log, not design doc; CLAUDE.md §7).
- **(P0 archive)** **P0 was DONE — code-complete AND acceptance-complete.** Steps 0 (scaffold) + 1
(models) + 2 (store) + 3 (scan) + 4 (parsing) + 5 (chunking) + 6 (embedding) + 7 (Qdrant
vector store) + 8 (reindex pipeline) + 9 (hybrid retrieval read path) + **10 (FastMCP
`search`/`open` + `vaultrag mcp` — the P0 finish line) DONE**, and on **2026-07-16 the last
unrun acceptance step finally ran (Docker unblocked): the `claude mcp add` dogfood.** The
design vault (`../Rag`, 51 md files) was reindexed as project `vaultrag-design` (122
chunks), the server registered (`claude mcp get vaultrag` → **✔ Connected**), and `search`
driven over **real stdio** returned precise, correctly-cited snippets (see session log).
**"Usable from Claude Code" (CLAUDE.md §4) is proven.** Next up: **P1** (watchdog +
manifest/renames, reranker, parent-child, citations w/ `obsidian://`, `write_note` + git
auto-commit).
- **What works (P1, stable — daily-driver quality):** `uv sync` installs; `uv run vaultrag reindex --project X --vault Y` builds the index; `uv run vaultrag mcp --project X --vault Y`
serves `search`/`open` (+ the 3 write tools, on by default) over real stdio; `uv run vaultrag watch` and `mcp --watch` keep the index live. Pipeline: vault scan (BLAKE3 + `.ragignore` +
secret guard) → md parse → heading-tree chunk (`parent_id` = pointer to the enclosing H1/H2)
→ Qwen3 embed via Ollama (cache keyed `(model,dims,chunk_hash)`) → Qdrant one `chunks`
collection (dense int8 + sparse `bm25`, RRF fusion, payload-partitioned tenancy) → retrieval
stages 0–5+8 (fuse → rerank[flagged] → parent-swap → cite w/ `obsidian://` URIs) → FastMCP.
Write path: gated `NoteWriter` → git commit → synchronous `ingest_path`. Swappable behind
`Embedder`/`Reranker`/`VectorStore`/`MetaStore`/`SparseEmbedder`/`ParentResolver` protocols.
**Green: `ruff`, `ruff format`, `mypy src` (41 files), `pytest` (315 tests).** Per-module detail
lives in the code, `MEMORY-p1.md` (P1), and `MEMORY-p0.md` (P0).
- **What's next: P3 STAGE 4 (`serve` — FastAPI `/v1`, the `ROLE` switch, `/metrics` mounted)**
at `../Rag/projects/vaultrag/p3-step-plan.md`. Scope: the HTTP transport
`reference-rest-api.md` specifies, as a **thin wrapper over the same core the MCP tools call**
— the parity test (one query, MCP `search` and `POST /v1/{project}/search` return the same
payload) is the stage's real acceptance criterion. Also closes the P2 carry-over: a startup
**drift check** + drift gauge, so neither `serve` nor `mcp` can silently serve an index that
disagrees with the manifest. Stage 3 already put the query log in the core pipeline, so the REST
surface inherits per-query rows for free — that was the point of the placement.
  **Decisions still open for Anik:** (1) `answer` mode build-at-all (Stage 7), (4) `serve`
  bootstrap key (**Stage 4** — a ratification of the recommended default, not a blocker),
  (5) Docker base/formats split (Stage 6). Decisions 2 and 3 are **settled**. Three
  `agent-inbox/` notes await a ruling: `compression-similarity-is-lexical-not-vector.md`
  (Stage 1), `overview-tool-shape.md` (Stage 2), and
  `query-log-stores-text-opt-in.md` (Stage 3 — note its **`top_score` section flags a second
  supersession that went beyond what was ratified**, and is the one part of Stage 3 that most
  wants a look). **Dogfooding:** the `vaultrag` MCP server has
  been run
  against the live `vaultrag-design` index in prior sessions — use `search`/`open` against
  `../Rag` while building and treat bad retrieval as a real bug report (repo `CLAUDE.md` §1). To
  (re)build the index: ensure `ollama serve` + `docker compose up -d qdrant` are up, then
  `uv run vaultrag reindex --project vaultrag-design`. Paths are machine-specific
  — this box is `/Users/anik/Desktop/{vaultrag,Rag}` (macOS). **As of Stage 2 the registry
  supplies the vault path and one server covers every project**, so registration is now:
  `vaultrag project add vaultrag-design --vault ../Rag`, then
  `claude mcp add vaultrag -- uv run --directory <repo> vaultrag mcp`
  (no `--project`/`--vault`; remove with `claude mcp remove vaultrag -s local`). Both projects on
  this box are registered: `vaultrag-design` → `../Rag` and `vaultrag-code` → this repo.
- **Design source of truth:** `../Rag` (Obsidian vault). Grep it before designing; see
`CLAUDE.md` §1.
- **Open questions / for Anik:**
  - Promote the now-hardened MCP return shapes (`SearchResult`/`Snippet`/`OpenResult`,
  `models.py`) into `../Rag/reference/reference-mcp-tools.md` (still params-only).
  - Rule on two proposed vault notes in `../Rag/agent-inbox/`: `payload-carries-chunk-text.md`
  (payload stores `text`, which Step 9 depends on) and `fastmcp-3x-not-2x.md` (shipped on 3.4.4,
  stack note says 2.x).
  - (P0 Docker blocker — RESOLVED 2026-07-16; details in `MEMORY-p0.md`.)

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

### 2026-07-22 (latest) — P3 Stage 3: the query log, and the measurement that rebuilt it

- **Did:** Anik: *"if stage 2 is fully implemented all the bugs are resolved then move to stage
3."* Verified Stage 2 first (783→ suite green, gate PASS) and re-checked git rather than trusting
this log — **Anik had already pushed and merged Stage 2 as PR #15 (`03bce8f`)**, so the handoff's
"unpushed, no PR" was stale within the hour. Branched `feat/p3-stage3-query-log` off `main`.
- **Put the blocking decision before writing any DDL, since Stage 3 hits it on line one.**
Anik chose **(a)**: nullable `query_text`, default on for local/stdio, `query_log_text=false` for
shared HTTP. Also ruled **`rate_result`: skip**. Written up as
`../Rag/agent-inbox/query-log-stores-text-opt-in.md`.
- **The stage's real content turned out not to be the table.** The plan's mining loop is built on
a **zero-hit report**, and dogfooding showed that report is **inert**: on the live design vault,
*"how do I rotate the stripe webhook signing secret"* returns **8 hits** from a vault that has
never heard of Stripe — indistinguishable from a well-answered query. A hybrid retriever ranks
*something* first for everything, so `n_results` carries **no signal**. Shipping the stage as
specced would have shipped a dead feature that looked green: 36 passing tests, and a report that
never fires. **The tests could not have caught this** — they use fakes, where a zero-hit result is
whatever the fixture says. Only running it against a real index did.
- **Fixed by measurement, not by guessing.** Probed 6 answerable vs 5 unanswerable queries at the
shipped default (`rerank_mode=never`, so RRF scores): answerable **0.57–1.21**, unanswerable
**0.50–0.61**, the latter clustered just above the RRF floor. So `top_score REAL` went into the
row — but **the bands overlap, so no global threshold ships**: `--report worst` *ranks* by score
and applies no cutoff ("the 20 worst" needs none), and `--export --max-score X` takes the cutoff
from the operator. Same scale-free lesson as `graph_boost` and Stage 1's rank-based MMR.
- **This is a second supersession of a `reference/` note beyond what Anik ratified, and it is
flagged rather than folded in.** Decision 2 was asked and answered about `query_text`
specifically. `top_score` is its own change, argued in its own section of the agent-inbox note,
with the measurement that forced it — because quietly widening a superseding schema change is how
a reference note stops describing the system.
- **The loop closed on its first real use and found a live retrieval bug.** `--report worst`
ranked *"write-back safety guardrails"* third-worst (0.52) — an answerable question — which
looked like a false positive from the band overlap and is not: `write-back-design.md` comes back
**4th**, behind `p1-step-plan.md`, `knowledge-graph-integration.md`, `reference-security-model.md`.
A genuine golden-set case, discovered rather than invented. Left for Anik to accept into a golden
set (the export deliberately never merges into the gate's frozen set).
- **Two hazards found while building, both about tables nothing points at.** (1) `query_log` is
**the only table `reindex` cannot rebuild**, so "delete the state dir" — the standard fix
everywhere else — is *data loss* here, and `CREATE TABLE IF NOT EXISTS` is silent about a missing
column. `init_schema` now adds absent `query_log` columns explicitly. (2) `query_log` has **no FK**
to `projects` (the P0 shorthand serves unregistered ids, and a FK would make logging fail exactly
where it is most interesting), so `delete_project` reaches it only by naming it — the Stage-2
orphaned-vector bug's exact shape, one table over, caught this time before shipping.
- **A design bug the tests caught, in my own first draft.** `record_query` treated `params=None`
as "log with defaults", while `ServedProject.query_log=None` means "this tenant opted out" — so a
tenant that opted out was logged anyway. Fixed by making the **sink alone** decide (the transport
withholds it), exactly how `graph_enabled` withholds the graph store. Two arguments that could
each mean "off" is how one of them gets forgotten.
- **Verified:** 789 tests (+48: 42 in the new `tests/test_querylog.py`, 6 CLI), ruff, mypy (60),
**gate PASS 1.000 / 0.892 / 0.920 (unmoved)**. Live dogfood on `vaultrag-design` end to end:
10 real queries → rows → `--report worst` → `--export` → `load_candidates`. **The box is back to
its starting state:** the synthetic dogfood rows were deleted (they were my probes, not Anik's
usage), only `vaultrag-design` registered, 61 docs.
- **Files:** `retrieval/querylog.py` (new), `retrieval/{pipeline,__init__}.py`,
`store/{base,sqlite}.py` (DDL + `log_query`/`query_stats`/`prune_query_log`/`query_log_count` +
the additive migration + `delete_project` cascade), `config.py` (4 knobs), `projects.py`
(2 reserved keys), `mcp/server.py` (`ServedProject.query_log`, `query_log_sink`, shared `q_hash`),
`cli.py` (`queries` command + `mcp` wiring), `eval/{golden,__init__}.py`
(`write_candidates`/`load_candidates`, `load_golden(require_expected=…)`); tests:
`test_querylog.py` (new), `test_cli.py`; vault: `agent-inbox/query-log-stores-text-opt-in.md`
(new), `projects/vaultrag/p3-step-plan.md` (Stage 3 log).
- **Next / handoff:** **Stage 4 (`serve`)**. Nothing blocks it — Decision 4 (bootstrap key) is a
ratification of a recommended default, not an open question. Stage 3 is committed on
`feat/p3-stage3-query-log`, **unpushed, no PR** (CLAUDE.md: push only when asked); Anik merged
Stage 2 himself mid-session, so check `main` before assuming this is still true. The one thing
worth Anik's eyes before Stage 4: **the `top_score` section of
`agent-inbox/query-log-stores-text-opt-in.md`** — it is the part of Stage 3 that went beyond what
was ratified, and if he'd rather it were reverted, that is cheaper now than after Stage 4's REST
surface starts writing the same rows.

### 2026-07-22 — P3 Stage 2 finished: one process serves N projects

- **Did:** Anik: *"continue the rest of Stage 2; if 1 and 2 are complete move to the next
stage."* First re-established ground truth rather than trusting this log — and the log was
**stale**: it said the Stage-2 foundation was "uncommitted on branch `feat/p3-stage2-registry`",
but Anik had already merged it (PR #14, `5c8b4f2`) and the tree was clean. Then built the second
half. Full detail in the Stage-2 bullet above; the parts worth reading here are the judgement
calls.
- **`overview`'s shape went to the vault first, as the plan required.** Its one-line spec ("folder
tree + top tags + note count") is genuinely under-determined in the three ways the plan predicted,
and each has a wrong answer that defeats the tool: a full file listing is the context dump
`search` exists to replace. Settled as a **map, not an index** — counts never paths, every list
capped by a constant so cost is flat in vault size, no `budget_tokens` (its presence would imply
the response could need one). **Depth 2 is measured**: this vault's 63 notes fold to 7 entries,
which is its entire taxonomy. `../Rag/agent-inbox/overview-tool-shape.md`, status `proposed`.
- **The refactor's real content is where state lives, not the tool signatures.** `ServedProject`
splits per-tenant (vault root, writer, embedder, retrieval knobs) from shared (vector store,
sparse, reranker, metadata store) — the shared half being exactly the things already
`project_id`-scoped per call. The bug this prevents is not a missing Qdrant filter (unconditional
`must` since P0) but a **vault root captured once at construction** while `project` varies per
call, which would make `open(path, project="alpha")` read beta's file.
- **A second enforcement point is the hole.** The first draft had `overview` resolving `project`
itself instead of going through `_select`. Rewritten to take the closure like every other tool —
there is now exactly one place that decides which projects a caller may reach.
- **Verification is adversarial and was mutation-checked, because a tenancy test that passes for
the wrong reason is worse than none.** `tests/test_multiproject.py`: one server, two *registered*
projects (extending P2's hand-inserted decoy as the plan asks), same paths/titles/text/vectors so
only the filter can separate them. Mutation 1 (`_select` ignores its argument) killed **16/25**;
mutation 2 (`open` uses a construction-time root) killed exactly its 2 tests. Both restored.
- **Found and fixed a real defect in the *foundation*, not the new code: `embed_dims` was
per-project-overridable.** One shared collection has one vector width, so that override fails at
index time far from its cause. Reserved. `embed_model` stays overridable on purpose — same width,
different model is safe because a project's vectors are only compared against its own.
- **Dogfooding earned its keep — it found the one bug the tests could not.** Registering this
repo as a throwaway second project, running the real two-project server against live
Qdrant+Ollama, and then removing it exposed the `project rm` vector orphan (243 points; see the
Stage-2 bullet). The live run also confirmed the thing that matters most: `open("CLAUDE.md")`
returns **genuinely different files** under `vaultrag-design` vs `vaultrag-code`, and omitting
`project` refuses with both ids named.
- **A second dogfood finding, deliberately NOT fixed — flagged instead (CLAUDE.md §1: flag
divergence, don't silently diverge).** `overview(vaultrag-code)` came back led by
`.venv/lib` (41 notes) and `.pytest_cache`, because `_ALWAYS_PRUNE_DIRS` is
`{.git, .obsidian, .trash, .smart-env, node_modules}` — deliberately *Obsidian-vault* shaped,
and a code repo is not a vault. Whether `.venv`/`__pycache__`/`.pytest_cache` should join that
set, or belong in the shipped `.ragignore` example, or whether "don't point it at a repo" is
the right answer, is a **design call for the vault**, not a quiet edit mid-stage. Worth noting
the positive read too: `overview` made a scan-scope mistake obvious in one call, which is
evidence it does the job the design note claims.
- **Verified:** 741 tests (+59 this session: 27 multiproject, 12 mcp overview/list_projects, 7
store `folder_counts`, 12 CLI resolution + `project rm` cascade, 3 `delete_by_project`, plus
metrics), ruff, mypy (59), **gate PASS 1.000 / 0.892 / 0.920 (unmoved)**. The box is back to its
starting state: only `vaultrag-design` registered, 61 docs / 213 points.
- **Files:** `mcp/server.py` (the refactor + 2 new tools), `cli.py` (`_registry`,
`_resolve_served_settings`, N-project `mcp`, `project rm` two-cache cascade), `metrics.py`
(+`init_project_labels`), `models.py` (`ProjectSummary`/`OverviewResult`/`FolderCount`/
`TagCount`), `projects.py` (`embed_dims` reserved), `index/{base,qdrant}.py`
(`delete_by_project`), `store/{base,sqlite}.py` (`folder_counts`), `retrieval/pipeline.py`,
`indexer/watch.py`, `writeback/writer.py` (metric call sites); tests:
`test_multiproject.py` (new), `test_mcp.py`, `test_metrics.py`, `test_store.py`, `test_index.py`,
`test_cli.py`; vault: `agent-inbox/overview-tool-shape.md` (new).
- **Next / handoff:** **Stage 3 (`query_log`) is NOT started — Anik ended the session at Stage 2.**
Stage 2 is committed on branch `feat/p3-stage2-multiproject` (`fed33cd`), **not pushed and no PR
opened** (CLAUDE.md: push only when asked) — that is the first thing to do next session if Anik
wants it on `main` like Stages 1 and the Stage-2 foundation.
**Decision 2 was put to Anik this session and deliberately left unanswered — do not treat that as
"no", and do not start Stage 3 without it.** It **blocks the DDL**: `reference-database-schema.md`
stores `q_hash` and simultaneously calls the table the golden-set mining source, and a hash cannot
become a query. It matters more than the usual open decision because **`query_log` is the only
table in the system that is *not* rebuildable from the vault** — every other row is a derived
cache where "DR = reindex" applies, so a wrong schema here is the one that is not cheap to undo.
The three options as put: (a) opt-in `query_text` column, default on for local/stdio and off for
multi-tenant HTTP — the plan's recommendation, and a superseding `agent-inbox/` note since it
overrides a *reference*; (b) always store the text, no knob (local-first tool, no branch); (c)
`q_hash` only and **correct the reference note's mining claim instead**, which shrinks Stage 3 to
telemetry Prometheus largely already provides. `rate_result` (build vs skip, on the P2 Stage 6
"left off *and unbuilt*" precedent) was raised alongside and is likewise unanswered.
Two Stage-1/2 vault notes also still await a ruling
(`compression-similarity-is-lexical-not-vector.md`, `overview-tool-shape.md`); neither blocks
anything.

### 2026-07-22 (later) — P3 Stage 1: the real stage-7 compressor

- **Did:** Anik: *"if everything is ready then implement the phase 3 stage 1"*. Verified readiness
(P2 complete, plan exists, only Decision 3 touches Stage 1 and it is a *measurement* I run, not
an Anik call), then built the deterministic stage-7 compressor. New `retrieval/compress.py`:
near-dupe drop (token-set Jaccard > 0.9) → MMR (λ=0.7, rank-based relevance) → per-doc-capped
(3) greedy pack → trim (blank-run collapse + trailing-fragment cut, **code fences byte-identical**).
- **Wiring:** `retrieval/pipeline.py` stage `pack` → `compress` (the plain `pack_budget` stays only
for the MCP compact-preview re-pack); `CompressParams` + `compress_params_for(settings)` mirror
the rerank/graph param pattern; threaded through `mcp/server.py`, `cli.py`, and **both eval-harness
stacks** so the gate scores shipped config; `config.py` gains 3 per-vault knobs;
`metrics.SEARCH_STAGES` "pack" → "compress" (+ `test_metrics`).
- **Decision 3 resolved by MEASUREMENT + architecture, not preference (see the Phase bullet above
and `../Rag/agent-inbox/compression-similarity-is-lexical-not-vector.md`).** int8 is *not* the
blocker (measured: tracks full precision, 0/3968 threshold flips); **parent-swap** is (default-on,
emits vectorless snippets; MMR needs one uniform metric; re-embedding rejected). → lexical Jaccard,
uniform over child + parent snippets, no `qdrant.py` change.
- **The test churn was real, not incidental:** near-dupe + per-doc-cap genuinely change behaviour for
~8 pre-existing tests that fed **artificially-identical** `make_hit` fakes (same path/doc_id/text)
through `search()` — their intent was budget/preview/fetch, so each got distinct docs+text to test
its actual concern. New `test_compress.py` (+24) pins the four steps in isolation, incl. the
planted near-dupe collapse and byte-identical code survival the plan's Verify asks for.
- **Verified:** gate PASS **1.000/0.892/0.920** (unmoved — corpus has no near-dupes, λ=0.7 keeps
relevance dominant); 669 tests, ruff, mypy (58); dogfood on the live design vault (reindexed
61 notes/213 chunks) near-neutral (distinct-docs +0.08, ~0 token change, compress() p50 1.5 ms).
- **Files:** `src/vaultrag/retrieval/compress.py` (new), `retrieval/pipeline.py`, `retrieval/__init__.py`,
`config.py`, `mcp/server.py`, `cli.py`, `metrics.py`, `eval/harness.py`, `tests/test_compress.py`
(new), `tests/test_retrieval.py`, `tests/test_mcp.py`; vault: `agent-inbox/…lexical-not-vector.md`
(new), `projects/vaultrag/p3-step-plan.md` (log).
- **Next / handoff:** **P3 Stage 2 (multi-project registry).** Anik still to rule on decisions 1, 2,
4, 5 (none block Stage 2) and to ratify the lexical-compression agent-inbox note. The compressor is
intentionally near-neutral on the clean design vault — don't read that as "it does nothing"; it is
insurance whose value appears on redundant/large-k/messy inputs, which the design vault isn't.

### 2026-07-22 — P3 decomposed into 7 stages (plan only; no code)

- **Did:** Anik: *"plan next phase in stages"*. Wrote `../Rag/projects/vaultrag/p3-step-plan.md`,
the sibling of the P0/P1/P2 plans, closing the P2 handoff's "there is **no** P3 step plan in the
vault yet; write one the way P0/P1/P2 had, before coding". **No code was written.**
- **Grounded in a fresh code check, not in the roadmap bullet** (the P2 plan's "starting point"
section is the format worth copying): compression is still P0's greedy `pack_budget` — no
near-dupe drop, no MMR, no per-doc cap, no trim; `projects` exists as a **table nothing really
uses** (`project_id` arrives as a CLI flag, one project per process) while tenancy *enforcement*
is already correct where it exists (`_build_filter` makes `project_id` an unconditional `must`);
`api_keys` and `query_log` are absent; there is **no `api/` package, no `serve`, no Dockerfile**,
and `docker-compose.yml` still runs Qdrant alone; `list_projects` and `overview` are in
`reference-mcp-tools.md` and **not in the code**.
- **The structural finding, and the reason the stage list isn't just the roadmap bullet re-typed:
`serve` is a P3 stage in its own right.** `CLAUDE.md` §4 lists "multi-project keys + tenancy
hardening, compression/budget packer, query-log mining, `answer` mode, compose profiles" — four
of those five are **meaningless without an HTTP transport**, and both `reference-rest-api.md` and
`reference-docker-compose.md` are written as though one exists. It does not. Left implicit, four
separate stages would each smuggle in a third of a web server. Hence the spine: core (1 compress,
2 registry, 3 query log) → transport (4 serve) → what it carries (5 keys, 6 compose) → optional
(7 answer).
- **Two orderings that are load-bearing, not taste.** (a) **Query log lands before `serve`, and
records in the *core* pipeline** — put it in the MCP layer and the REST surface grows a second,
divergent copy; put it in the core and Stage 4 inherits it for free. (b) **Compression goes
first** because it is the only P3 stage the eval gate judges; everything after it is plumbing the
gate cannot see, so P3 opens against a number.
- **5 decisions raised, all with recommendations, none settled.** The two worth reading:
**(2) `query_log` contradicts itself in the vault** — `reference-database-schema.md` defines the
row as `q_hash` (no text) and, eleven lines later, calls the table the source that turns zero-hit
queries into golden-set candidates. **A hash cannot become a golden-set query**, so Stage 3 hits
this on day one; recommended an opt-in `query_text` column (default on for local/stdio, kill
switch for multi-tenant HTTP) on the grounds that `CLAUDE.md` §5's rule governs the **log
stream**, which stays absolute — and flagged that this **supersedes a reference schema** and so
belongs in `agent-inbox/` rather than drifting. **(1) `answer` mode probably should not be
built at all:** its own decision note's "switch when" is *a consumer without its own LLM*, and
every consumer today is Claude Code. Recommended cancelling the stage if no consumer appears —
the precedent is P2 Stage 6's VLM caption pass, left off **and unbuilt** rather than shipped
dark. Also: (3) MMR's vector source needs a **measurement** (Qdrant `with_vectors` is int8 —
confirm cosine on quantized vectors tracks full precision before trusting a 0.92 threshold, else
fall back to lexical near-dupe); (4) `serve` ships with a **bootstrap key** so
`reference-security-model.md`'s "HTTP always requires a key, even on localhost" holds from the
first commit without merging stages 4+5 into one unreviewable PR; (5) the Docker image splits
into `latest` (base) + `formats` (pdf extra) tags — same measure-the-weight reasoning as Docling
(→ extra) and prometheus-client (→ base), which landed on opposite answers.
- **Two P2 carry-overs are now assigned rather than floating:** the **MCP startup drift check**
(outstanding since P2 Stage 4 — a long-running `vaultrag mcp` can still serve a drifted index
silently) lands in Stage 4 alongside a drift gauge; the `**project` metrics label**, deliberately
absent in P2, lands in Stage 2 when it becomes bounded and meaningful — and
`test_metrics.py`'s cardinality allowlist must be updated *with* it, not after.
- **Stage 6 finally makes two long-asserted claims measurable:** the `CLAUDE.md` §6 "DR = reindex"
guardrail gets run as an actual drill (fresh volumes → clone vault → compose up → reindex →
search), and the 3-4 GB single-box RAM figure gets measured for the first time instead of quoted.
- **Files:** `../Rag/projects/vaultrag/p3-step-plan.md` (new), this file. No `src/` changes.
- **Next / handoff:** **Anik rules on the 5 decisions** (1 and 2 change what gets built; 3 changes
how Stage 1 is implemented; 4 and 5 are ratifications of a recommended default). Then start
**Stage 1**. Note the two open questions in the plan: multi-project write-back is N independent
git repos and should be sanity-checked *before* Stage 2 rather than during Stage 5, and
`overview()` has a one-line spec and no design note — if its shape turns out non-obvious, that is
a vault note, not an improvisation in `mcp/server.py`.

### P2 (Stages 1–7 + the closing 4-bug audit, 2026-07-20 → 2026-07-21) — archived in `MEMORY-p2.md`

The full P2 build log — the 7 stages (eval harness + CI gate → link-graph persistence →
`related`/graph-expand → PDF via Docling → Excalidraw+Mermaid → OCR/image text → Prometheus
`/metrics`) plus the plan-decompose, decisions-settled, rerank-mode, index-drift, 3-bug mid-stage
fix, and 4-bug audit entries — was moved to `MEMORY-p2.md` on 2026-07-22 to keep this log
P3-focused. Nothing deleted; read it there (or `git log`) for how P2 was built.

### P1 (Stages 1–5, 2026-07-16 → 2026-07-19) — archived in `MEMORY-p1.md`

The full P1 build log — the 5 stages (rerank → parent-child → citations → watchdog →
write-back) plus the plan-decompose + decisions-settled entries — was moved to `MEMORY-p1.md`
on 2026-07-20 to keep this log P2-focused. Nothing deleted; read it there (or `git log`) for how
P1 was built.

### Older entries (P0, Steps 0–10) — archived in `MEMORY-p0.md`

The full P0 build log — acceptance dogfood, Step 10 + both code audits, Steps 0–9, scaffold —
was moved to `MEMORY-p0.md` on 2026-07-16 to keep this log P1-focused. Nothing deleted; read it
there (or `git log`) for how P0 was built.