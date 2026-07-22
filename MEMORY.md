# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** **P3 IN PROGRESS — Stage 1 DONE; Stage 2 (multi-project registry) FOUNDATION
DONE, not yet complete (2026-07-22).** P2 is COMPLETE (all 7 stages + the 4-bug audit).
**On branch `feat/p3-stage2-registry`, uncommitted.** See the Stage-2 bullet directly below
for what landed and what remains; plan at `../Rag/projects/vaultrag/p3-step-plan.md`.
- **P3 STAGE 2 FOUNDATION (the `projects` registry becomes real) DONE 2026-07-22 — the rest of
Stage 2 is still open.** Until now a project was a *string* (`--project X --vault Y`, one tenant
per process); the `projects` table existed but nothing resolved through it. **Landed + green
(682 tests, +13; ruff; mypy 59; gate unmoved 1.000/0.892/0.920):**
(1) **store** — `list_projects()`, `delete_project()` (cascades one tenant's docs→chunks/tags/
doc_keys/links + index_state via FK, deletes its `jobs` explicitly since they carry a bare
`project_id` with no FK, leaves the shared `embed_cache` and every other tenant intact),
`document_count()`, `top_tags()` (the two `overview` aggregates), all added to the `MetaStore`
protocol too. (2) `**projects.py**` (new) — `ProjectRegistry` (add/list/get/remove/**resolve**)
  - `UnknownProjectError`. `resolve(project)` overlays the row's `vault_path`/`project_id` and its
  `settings` JSON onto the process base `Settings` via `Settings(**{**base.model_dump(), …})`
  (re-validates/coerces types); **shared infra is reserved** (`state_dir`, `qdrant_url/collection`
  can't be per-project — that's what keeps N tenants in one db + one payload-partitioned
  collection), and an unknown/reserved override key **fails loudly** rather than being stored-but-
  ignored. (3) **CLI** — `vaultrag project add|list|rm|show` subcommand group (`--set key=value`,
  JSON-parsed), and `reindex`/`watch`/`mcp` now resolve `--project` through the registry via a new
  `_resolve_settings()` so `**--vault` is optional for a registered project** (explicit `--vault`
  still wins; an unregistered id keeps the P0 shorthand). **The bug the read-only tests caught:**
  `_resolve_settings` must **not** open/create the SQLite db just to check the registry — a
  read-only `mcp --project X --vault Y` must never conjure an empty state db (same invariant the
  graph reader keeps), so it consults the registry **only when `db_path.is_file()`** (no db ⇒ no
  registered projects anyway). **Pre-Stage-2 sanity check RESOLVED** (the plan asked to confirm
  before Stage 2): a multi-project write is **N independent git commits, not a new transactional
  problem** — one project = one vault dir = one git repo (`multi-tenant-architecture.md`), and the
  shared SQLite/Qdrant state is already per-row/per-point and `project_id`-scoped.
  **STILL OPEN in Stage 2 (the larger, higher-risk half — next session):** the **`list_projects`
  - `overview` MCP tools** (`overview`'s shape — tree depth / tag count / token budget — is the one
  genuine design call, and per the plan it's a **vault note**, not an improvisation); the
  **one-process-serves-N** refactor (per-call `project` arg on search/open/related/write switching
  vault_root/vault_name/writer — the vector/graph stores are already `project_id`-scoped per call,
  so the per-project pieces are vault_root + writer) **with the adversarial cross-tenant test the
  plan mandates** (extend P2's decoy-tenant test so the decoy is a *registered* project, and prove
  `open` resolves against the **right** vault root); the `**project` metrics label** (+ its
  `test_metrics` allowlist update, now that it's bounded).
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
- **What's next: FINISH P3 Stage 2** at `../Rag/projects/vaultrag/p3-step-plan.md` — the registry
**foundation is DONE** (see the Stage-2 bullet above), the **remaining half** is: `list_projects`
  - `overview` MCP tools (write `overview`'s shape as a vault note first — the plan says its
  tree-depth/tag-count/token-budget is a design decision, not an improvisation); the
  one-process-serves-N MCP refactor (per-call `project` on search/open/related/write) **with the
  adversarial cross-tenant test** (extend P2's decoy test so the decoy is a *registered* project;
  prove `open` reads the right vault root); and the `project` metrics label + its `test_metrics`
  allowlist update. The vector/graph stores are already `project_id`-scoped per call, so the
  per-project pieces to thread are `vault_root` + `NoteWriter`. **Work is on branch
  `feat/p3-stage2-registry` (uncommitted) — commit when Anik asks.**
  **Decisions still open for Anik:** (1) `answer` mode build-at-all, (2) `query_log` text column
  (supersedes a reference schema), (4) `serve` bootstrap key, (5) Docker base/formats split — none
  block Stage 2. **Dogfooding:** the `vaultrag` MCP server has been run
  against the live `vaultrag-design` index in prior sessions — use `search`/`open` against
  `../Rag` while building and treat bad retrieval as a real bug report (repo `CLAUDE.md` §1). To
  (re)build the index: ensure `ollama serve` + `docker compose up -d qdrant` are up, then
  `uv run vaultrag reindex --project vaultrag-design --vault ../Rag`. Paths are machine-specific
  — this box is `/Users/anik/Desktop/{vaultrag,Rag}` (macOS); (re)add the server with
  `claude mcp add vaultrag -- uv run --directory <repo> vaultrag mcp --project vaultrag-design --vault <vault>` (remove with `claude mcp remove vaultrag -s local`).
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