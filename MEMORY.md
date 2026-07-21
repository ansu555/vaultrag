# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** **P2 IN PROGRESS — Stage 1 (full eval harness + merge-blocking CI gate) DONE.**
  Retrieval quality is now a **number in CI**, not a vibe. New **`src/vaultrag/eval/`** package
  (`metrics`/`golden`/`replay`/`harness`/`overlay`/`report`) + **`vaultrag eval`** CLI. **Decisions
  1 + 2 of the P2 plan settled as recommended** (3-5 still open, each for its own stage).
  **(2) Fixture corpus:** `tests/eval/corpus/` = a **27-note synthetic vault** ("Meridian", a
  fictional event-ingest platform) so the gate is frozen instead of drifting with `../Rag`;
  **72 pairs** in `corpus_golden.json`, written *harder* than the P1 set (paraphrases sharing no
  title token, neighbouring topics that force discrimination, bare identifiers like `MER-1422` to
  exercise BM25). The design-vault set survives as **dogfooding** and grew **20 → 56**.
  **(1) CI determinism:** real Qwen3 vectors recorded once into a committed
  **`tests/eval/vectors.db`** (~580 KB) + Qdrant `:memory:`; `ReplayEmbedder` **raises** on a miss
  (never fakes a vector). *Refinement on the plan:* the production `embed_cache` is keyed by
  **chunk** hash so it can't hold **query** vectors — the fixture is its own table keyed
  `(model, dims, blake3(text))` covering both. **Only the dense vectors are replayed**: every run
  reindexes the corpus through today's scanner/parser/chunker/payload, so a chunking regression is
  visible — and, by the same token, **a chunking or embed-model change invalidates the fixture** and
  the run fails loudly (`vaultrag eval --record` to refresh; a PR touching chunking carries an
  updated `vectors.db`). **That "fails loudly" was ASPIRATIONAL until 2026-07-21 — it silently
  passed instead; found and fixed while testing (see the session log entry).** Added **nDCG@10** (pre-rerank; **reported, not gated** — no baseline yet to
  justify a threshold) beside recall@10 / MRR@5. **Measured (shipped config, rerank off):
  recall@10 1.000 · MRR@5 0.892 · nDCG@10 0.919 over 72 cases** — both targets clear. **The gate
  was verified to bite:** overlay `default_top_k: 1` → recall@10 0.806, exit **1**. Hermeticity
  verified by pointing `ollama_url` at a dead port. **CI:** `.github/workflows/eval.yml` on every PR
  (deliberately *not* path-filtered — a path-filtered required check that never runs blocks merges
  instead of guarding them); only network need is FastEmbed's small BM25 artifact, cached.
  **Green: 359 tests** (+43 `test_eval.py`, +1 stale-fixture regression), ruff, ruff format,
  mypy (48 src files). **Stage 1 was re-tested against live Qwen3 on 2026-07-21** — replay and
  live embeddings agree exactly (0/72 ranks moved, query-vector cosine 1.000000), and a
  silent-pass bug in `replay_stack` was found and fixed (session log). **Also re-run against the
  real Qdrant server** (not just `:memory:`): payload indexes + `is_tenant` tenancy verified
  bidirectionally against a duplicate-vector decoy tenant (zero leakage); live MRR@5 is
  **non-deterministic** (0.871–0.892 on an unchanged index), which empirically justifies the
  hermetic gate; and **rerank-on beats fusion on this corpus (+0.036 MRR@5)**, inverting the P1
  finding the shipped default rests on — **open for Anik** (session log).
  **Next: Stage 2 (link-graph persistence — the `links` table + resolver).**
- **(P2 plan)** P2 is decomposed into
  **7 implement-one-at-a-time stages** in the vault: `../Rag/projects/vaultrag/p2-step-plan.md`
  (sibling of `p0-`/`p1-step-plan.md`). Order (dependency-driven): **1** full eval harness +
  merge-blocking CI gate → **2** link-graph persistence (`links` table + resolver, populate on
  ingest) → **3** `related` tool + graph-expand (retrieval **stage 6**) → **4** PDF via Docling
  (optional extra) → **5** Excalidraw + Mermaid diagram extraction (`diagram_edge` → links table)
  → **6** OCR/image text (RapidOCR/Tesseract) → **7** Prometheus `/metrics` (standalone exporter).
  **Rationale:** eval gate first (every index-touching stage must land through it — the
  "measurable quality" thesis); graph *data* (2) before its consumers (3, and Mermaid edges in 5);
  Prometheus (7) last since it observes the rest. **Grounded in a fresh code check (2026-07-20):**
  links are **parsed but never persisted** (`models.Link`/`extract_links` populate `Document.links`
  but there is **no `links` table** — schema is still P0-only, and ingest discards edges → Stage 2
  builds the data layer); retrieval has stages 0–5+8 but **no stage 6**; **no** PDF/Excalidraw/
  Mermaid-edge/OCR parser; **no** FastAPI `serve`/`api/` (so Prometheus ships standalone via
  `prometheus_client.start_http_server`, not mounted on HTTP). (The "eval is a minimal 20-pair set,
  no CI gate" line of that check is now **superseded — Stage 1 built it**.) Of the 5 open decisions,
  **(1) CI determinism and (2) the fixture corpus were settled + built in Stage 1** (details in the
  Phase bullet above). **Still open, each for its own stage:** (3) Docling as an optional extra
  `vaultrag[pdf]` (Stage 4); (4) link-resolution collision / re-resolution rules (Stage 2);
  (5) standalone Prometheus exporter now, mount on `serve` later (Stage 7).
- **(P1 archive)** **P1 CODE-COMPLETE — all 5 stages done + LIVE-verified; the memory loop is
  closed.** Stage 1 rerank (bge-v2-m3 ONNX-int8, behind a flag, **OFF** by default) · Stage 2
  parent-child + parent-swap (**ON**, superset-guarded / non-lossy) · Stage 3 `obsidian://`
  citations + line ranges · Stage 4 single-file `ingest_path` primitive + watchdog live-watch
  (1.5 s debounce, worker pool, retry→dead) · Stage 5 write-back
  (`write_note`/`append_section`/`patch_frontmatter` + one guardrail gauntlet + best-effort git
  auto-commit + synchronous reindex). New packages: `retrieval/{rerank,parents,cite}.py`,
  `indexer/watch.py`, `writeback/`. **Green: 315 tests**, ruff, ruff format, mypy (41 src files).
  **Full per-stage detail (the 5 verbose stage bullets + the 7 dated session-log entries) is
  archived in `MEMORY-p1.md`** — not repeated here (log, not design doc; CLAUDE.md §7).
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
- **What works (P1, stable — daily-driver quality):** `uv sync` installs; `uv run vaultrag
  reindex --project X --vault Y` builds the index; `uv run vaultrag mcp --project X --vault Y`
  serves `search`/`open` (+ the 3 write tools, on by default) over real stdio; `uv run vaultrag
  watch` and `mcp --watch` keep the index live. Pipeline: vault scan (BLAKE3 + `.ragignore` +
  secret guard) → md parse → heading-tree chunk (`parent_id` = pointer to the enclosing H1/H2)
  → Qwen3 embed via Ollama (cache keyed `(model,dims,chunk_hash)`) → Qdrant one `chunks`
  collection (dense int8 + sparse `bm25`, RRF fusion, payload-partitioned tenancy) → retrieval
  stages 0–5+8 (fuse → rerank[flagged] → parent-swap → cite w/ `obsidian://` URIs) → FastMCP.
  Write path: gated `NoteWriter` → git commit → synchronous `ingest_path`. Swappable behind
  `Embedder`/`Reranker`/`VectorStore`/`MetaStore`/`SparseEmbedder`/`ParentResolver` protocols.
  **Green: `ruff`, `ruff format`, `mypy src` (41 files), `pytest` (315 tests).** Per-module detail
  lives in the code, `MEMORY-p1.md` (P1), and `MEMORY-p0.md` (P0).
- **What's next: P2** (`CLAUDE.md` §4) — decomposed + locked into **7 stages** at
  `../Rag/projects/vaultrag/p2-step-plan.md` (ordering + the 5 open decisions are in the Phase
  bullet above). Start with **Stage 1 (full eval harness + merge-blocking CI gate)** — it gates
  every index-touching stage after it. **Dogfooding:** the `vaultrag` MCP server has been run
  against the live `vaultrag-design` index in prior sessions — use `search`/`open` against
  `../Rag` while building and treat bad retrieval as a real bug report (repo `CLAUDE.md` §1). To
  (re)build the index: ensure `ollama serve` + `docker compose up -d qdrant` are up, then
  `uv run vaultrag reindex --project vaultrag-design --vault ../Rag`. Paths are machine-specific
  — this box is `/Users/anik/Desktop/{vaultrag,Rag}` (macOS); (re)add the server with
  `claude mcp add vaultrag -- uv run --directory <repo> vaultrag mcp --project vaultrag-design
  --vault <vault>` (remove with `claude mcp remove vaultrag -s local`).
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

### 2026-07-21 — Stage 1 re-tested against the REAL Qdrant server (tenancy + rerank findings)
- **Did:** Anik: *"run the qdrant and test it using that"*. Brought up `docker compose up -d qdrant`
  and re-ran Stage 1 against the **real server** instead of `:memory:`, plus an adversarial tenancy
  probe. **(1) Payload indexes are real here.** The in-memory gate emits `UserWarning: Payload
  indexes have no effect in the local Qdrant`, so **every gate run to date has scored an index with
  no payload indexes at all**. On the server they materialize as designed — `project_id` as
  `keyword` with **`is_tenant: true`** (payload-partitioned tenancy), plus `path`/`tags`/`type`/
  `modified_ts`; dense `int8` scalar quantization, sparse `bm25` with `idf`, 51 points. **(2) Live
  server numbers:** recall@10 **1.000**, MRR@5 **0.878–0.892**, nDCG@10 **0.909–0.919**, gate PASS —
  a hair under the hermetic 0.892/0.919, which is the expected cost of int8 + approximate HNSW vs
  local exact search. **(3) Tenancy verified bidirectionally, adversarially.** Indexed a second
  project `tenancy-decoy` into the *same collection* holding **byte-identical note content** under
  `DECOY-` prefixed paths — so every decoy chunk is a perfect-duplicate competitor for its corpus
  twin (102 points, identical vectors). Re-running the 72-case eval gave **metrics identical to the
  digit** and **zero** decoy paths in any ranked list; a direct probe over 12 queries returned 98
  results per tenant with **0 cross-contamination in both directions** (and neither tenant
  over-filtered to empty). Cleaned up after (decoy points deleted by filter, orphan chunks + project
  row dropped from `state/vaultrag.db`, decoy vault removed; 51 points / 2 projects remain).
- **Findings worth acting on (none acted on unilaterally):**
  **(a) The live path is NOT deterministic.** Three consecutive live evals on an *unchanged* index:
  MRR@5 **0.885 / 0.892 / 0.871** (spread 0.021), from approximate HNSW. This **empirically
  validates Decisions 1+2** — a gate wired to live Qdrant would flap by ±0.02 run-to-run. It
  wouldn't flip today's gate (headroom 0.892 vs 0.75), but live numbers are **not comparable
  run-to-run** and shouldn't be quoted as if they were.
  **(b) Rerank HELPS on the P2 corpus — the opposite of the P1 finding.** A/B over 72 cases:
  fusion-only MRR@5 0.892 → **rerank-on 0.928 (+0.036)**, recall@10 stays 1.000, nDCG unchanged
  (it is pre-rerank by definition). `config.py` disables rerank by default citing the P1 set
  (fusion 1.00/0.775 vs rerank 0.95/0.743) — but that comment already predicted this, calling the
  result stable only "on this **title-matchy** set". The Meridian corpus was deliberately written
  with paraphrases sharing no title token, which is exactly where a cross-encoder should win. So
  the two results are **coherent, not contradictory** — and they mean **the shipped default was
  calibrated on a set now known to be unrepresentatively easy**. *Not* flipping the default here:
  that is a vault decision, it is confounded (synthetic corpus; the real vault may be title-matchy
  in practice), and rerank costs a lot of CPU. **For Anik to rule on** —
  candidate note for `../Rag/agent-inbox/`.
- **Also learned (environment):** the first `qdrant/qdrant:latest` pull was **corrupt** — container
  crash-looped on `exec ./entrypoint.sh: exec format error` while `docker image inspect` reported a
  correct `linux/arm64` on an arm64 host. `docker rmi` + re-pull fixed it; suspect a bad layer, not
  an arch mismatch. Worth remembering before debugging compose for real. Also incidentally
  confirmed the **embed cache works**: the decoy vault (identical content, different paths) indexed
  in **2.2 s vs 60.7 s** for the corpus — all 51 chunks hit the `(model, dims, chunk_hash)` cache.
- **Side finding — `.gitignore` bug, `state/` was never ignored (pre-existing, since P0):** line 2
  read ``state/            # sqlite db + embed cache …``. **`.gitignore` has no trailing-inline-
  comment syntax** — the whole line is one literal pattern, so it matched nothing and
  **`state/vaultrag.db` has been tracked and committed all along** (925 KB → 1.29 MB after this
  session's reindexes). That contradicts `CLAUDE.md` §3 ("state/ … (gitignored)") and §6 ("the index
  is derived") — a rebuildable cache is in git history. Reproduced the bug in a throwaway repo to be
  sure. **Fixed** `.gitignore` (comment moved to its own line; pattern now matches). **Not done —
  needs Anik:** the file is already tracked, and gitignore does not apply to tracked files, so it
  also needs `git rm --cached state/vaultrag.db` to actually untrack (leaves the local file alone).
  Left unstaged since commits happen only when asked.
- **Files:** `.gitignore` (the fix above) + `MEMORY.md` (this entry). No `src/` changes — the Qdrant
  work was verification only (probe scripts stayed in the scratchpad). Green after it all:
  **359 tests**, ruff, mypy (48), hermetic gate unchanged at 1.000/0.892/0.919.
- **Next / handoff:** Stage 2. Two things for the next session: (a) the hermetic gate has **never**
  exercised a payload index — if a later stage adds payload filtering (tags, path prefix, date), the
  in-memory gate **cannot** catch a broken filter, so that stage needs a server-Qdrant test like the
  decoy probe above; (b) rule on the rerank default (finding **b**) before Stage 3, since
  `related`/graph-expand reorders results and will interact with it.

### 2026-07-21 — P2 Stage 1 verified against live embedding models; found + fixed a silent-pass bug
- **Did:** Tested Stage 1 end-to-end at Anik's request (*"test phase 2 stage 1 properly with
  embedding models also"*), including against **live Qwen3**, not just the frozen fixture.
  **(1) Fixture fidelity — the headline result.** Ran the *identical* eval twice through the same
  `replay_stack` code path, changing only where the floats come from: `ReplayEmbedder` (frozen
  `vectors.db`) vs a live `OllamaEmbedder` on `qwen3-embedding:0.6b`. **Identical metrics
  (recall@10 1.000 · MRR@5 0.892 · nDCG@10 0.919), 0/72 cases moved rank**, and re-embedding all
  72 golden queries live gave **min/mean/max cosine 1.000000** vs the recorded vectors. The gate is
  measuring real Qwen3, and the fixture has not drifted. **(2) Found a real bug (now fixed):
  a stale fixture silently PASSED the gate.** `replay_stack` called `reindex_vault` and **discarded
  its `ReindexStats`**. `reindex_vault` is deliberately resilient (one bad note parks its job, the
  rest of the vault indexes) — right for production, wrong for the gate: it downgraded
  `ReplayEmbedder`'s refusal-to-fabricate into a *quietly smaller index*, which was then scored and
  reported PASS. Demonstrated concretely: editing `services/gateway-service.md` (targeted by **3**
  golden queries) → that note dropped out of the index entirely → **recall 1.000 → 0.958, MRR
  0.892 → 0.852, `gate: PASS`, exit 0**. That reads to a developer as "retrieval got slightly
  worse", when in fact 1 of 27 notes was never indexed — the exact silent-green failure
  `ReplayEmbedder` exists to prevent (its own docstring: *"a silently-fabricated embedding would
  make the gate green while measuring nothing"*). **Fix:** `replay_stack` now checks
  `stats.failed` and raises `EmbeddingError` naming the count + the `--record` hint → CLI exit **2**
  (can't-run), not a bogus quality number. Post-fix both scenarios (new unrecorded note; edited
  targeted note) exit **2** with an actionable message; baseline unchanged.
- **Why:** The gate's whole value is that it cannot be green for the wrong reason. A fixture that
  silently stops covering part of the corpus is precisely "green for the wrong reason", and it was
  reachable by the single most likely P2 event — a chunking change (Stage 5 touches parsing; any
  chunker tweak re-hashes chunk text and misses the fixture wholesale).
- **Also verified (measured, not asserted):** gate PASS exit **0** at 1.000/0.892/0.919 · gate
  **bites**: `default_top_k: 1` → recall 0.806, exit **1** with miss list · **hermetic**:
  `VAULTRAG_OLLAMA_URL=http://localhost:1` → identical numbers, exit 0 · **deterministic**: two
  consecutive runs identical · corpus restored to 27 notes, no probe files left behind.
- **Files:** `src/vaultrag/eval/harness.py` (`replay_stack` now raises on `stats.failed`; imports
  `EmbeddingError`), `tests/test_eval.py` (+1 regression test
  `test_a_stale_fixture_aborts_instead_of_scoring_a_partial_index` — copies the corpus to `tmp_path`,
  adds an unrecorded note, asserts the raise; the committed corpus is never mutated), `MEMORY.md`
  (corrected the "fails loudly" claim, which was aspirational as written). **No production
  (`indexer/pipeline.py`) change** — its per-file resilience is correct for a live vault; only the
  harness's assumption about it was wrong. Green: **359 tests** (+1), ruff, ruff format, mypy (48).
- **Next / handoff:** Stage 1 is now genuinely trustworthy — proceed to **Stage 2 (link-graph
  persistence)**. Things to know: (a) the `--record` path shares the same new guard, so a recording
  run that hits a live-Ollama failure also aborts instead of writing a partial fixture; (b) the
  **rerank A/B is impractically slow on CPU** — `--config rr-off --config rr-on` over 72 cases runs
  the cross-encoder ~144 times and blew a 10-min timeout on first run (the bge-v2-m3 ONNX download
  is now cached); if rerank ever needs gating, cut the case count or run it as a separate
  non-blocking job, don't put it in the PR gate; (c) MEMORY's earlier claim that a fixture
  invalidation "fails loudly" was **not true when written** — worth re-testing other
  "verified" claims the same way (assert the failure mode, not just the happy path).
- **Did:** Built Stage 1 end-to-end — retrieval quality is now a CI number instead of a vibe.
  (1) New **`src/vaultrag/eval/`** package (lives in `src/` because the CLI can't import from the
  test tree; the *data* stays in `tests/eval/`): `metrics.py` (recall@k, MRR@k, **nDCG@k**,
  `Metrics.meets_targets/failures`), `golden.py` (`Case`/`GoldenSet` + strict loader — a malformed
  case **raises**, never silently shrinks the scored set — plus `verify_paths`), `replay.py`
  (`VectorFixture` + **`ReplayEmbedder`** + `RecordingEmbedder`), `harness.py` (`replay_stack`
  hermetic / `live_stack` dogfooding → one `SearchStack`; `run_eval`), `overlay.py` (YAML/JSON
  `Settings` overlays for A/B, **unknown keys rejected**), `report.py` (summary / miss list / A/B
  table). (2) **`tests/eval/corpus/`** — a 27-note synthetic vault ("Meridian"). (3)
  **`corpus_golden.json`** 72 pairs (the gate); **`golden.json`** grown **20 → 56** (dogfooding).
  (4) **`tests/eval/vectors.db`** — 123 real Qwen3 vectors (51 corpus chunks + 72 queries), ~580 KB.
  (5) **`vaultrag eval`** CLI: `--golden corpus|design|<path>`, `--live`, repeatable `--config`
  (A/B), `--record`, `--show-cases`, `--gate/--no-gate`; exit 0 pass / 1 gate-fail / 2 can't-run.
  (6) **`.github/workflows/eval.yml`** (the repo's first workflow). (7) `tests/eval/README.md`
  documenting the re-record workflow; `tests/eval/run_eval.py` → shim. Added `pyyaml` dep.
  Green: **358 tests** (+43), ruff, ruff format, mypy (48 src files).
- **Why:** Anik: *"impliment stage 1"* — Stage 1 of `../Rag/projects/vaultrag/p2-step-plan.md`
  (`evaluation-framework.md`, arch §23; repo `CLAUDE.md` §6). It ships first because every later P2
  stage that touches the index has to land through it.
- **Decisions settled (1 + 2, as recommended in the plan; 3-5 stay open for their stages):**
  **(2)** a committed fixture corpus, not the drifting design vault — a number from `../Rag` today
  isn't comparable to one from next month. **(1)** frozen real Qwen3 vectors replayed + Qdrant
  `:memory:`. **One refinement on the plan's wording:** it said "frozen `embed_cache`", but that
  table is keyed by **chunk** hash and so cannot hold **query** vectors, which the gate needs too —
  so the fixture is its own table keyed `(model, dims, blake3(text))` covering both.
- **Verify (measured, not asserted):** shipped config, rerank off → **recall@10 1.000 · MRR@5 0.892
  · nDCG@10 0.919** over 72 cases; both targets clear. **The gate bites:** overlay
  `default_top_k: 1` → recall@10 0.806 → exit **1** with the miss list. **Hermetic:** re-ran with
  `ollama_url` pointed at a dead port — identical numbers, exit 0. **Deterministic:** two
  consecutive runs, identical to 3 dp. Recording ran against **live Ollama** (`qwen3-embedding:0.6b`).
- **Files:** `src/vaultrag/eval/{__init__,metrics,golden,replay,harness,overlay,report}.py` (new),
  `cli.py` (+`eval` command, `_resolve_golden`, `_record_fixture`), `pyproject.toml`/`uv.lock`
  (pyyaml). `tests/`: `test_eval.py` (new, 43), `eval/corpus/**` (27 notes, new),
  `eval/corpus_golden.json` (new), `eval/vectors.db` (new, binary), `eval/golden.json` (grown),
  `eval/README.md` (new), `eval/run_eval.py` (shim). `.github/workflows/eval.yml` (new).
  **Vault:** `p2-step-plan.md` Log (Stage 1 done + decisions 1/2 settled).
- **Next / handoff:** **Stage 2 — link-graph persistence** (`links` table + title/alias resolver,
  persist `Document.links` on ingest, backlinks/neighbors/siblings CTEs). Settle **decision 4**
  (link-resolution collisions) while coding it. Things the next session should know:
  (a) **a chunking or embed-model change invalidates `vectors.db`** — the gate fails loudly with a
  `--record` hint rather than scoring wrongly, so such a PR must carry a re-recorded fixture (this
  is by design, and documented in `tests/eval/README.md`); (b) nDCG@10 is **reported, not gated** —
  no baseline yet justifies a threshold; (c) the gate's headroom is real but not huge (MRR 0.892 vs
  0.75) — if a later stage needs a harder set, add near-duplicate-topic notes to the corpus rather
  than more queries; (d) the repo still has **no general lint/type/test CI** — `eval.yml` is the
  only workflow, and adding a `ci.yml` running `ruff`/`mypy`/`pytest` is a cheap follow-up;
  (e) the harness deliberately passes no `parent_resolver`/`vault_name` (neither can move a
  path-based metric) and uses a large token budget so recall@10 measures retrieval, not packing.
  Working tree: new `src/vaultrag/eval/`, `tests/test_eval.py`, `tests/eval/**`,
  `.github/workflows/eval.yml`, edits to `cli.py`/`pyproject.toml`/`MEMORY.md`; vault plan note
  edited (separate repo). Commit when Anik asks.

### 2026-07-20 — P2 decomposed into 7 stages + locked to the vault (plan only, no code)
- **Did:** At Anik's request (*"plan phase 2 break it into steps and lock it"*), broke P2 into
  **7 implement-one-at-a-time stages** and locked the durable plan to the vault as
  `../Rag/projects/vaultrag/p2-step-plan.md` (sibling of `p0-`/`p1-step-plan.md`, same format).
  Stages: **1** full eval harness (golden 20→50–150, +nDCG@10, `vaultrag eval` +A/B) + merge-blocking
  **CI gate** → **2** link-graph persistence (build the `links` table + title/alias resolver, persist
  edges on ingest, backlinks/neighbors/siblings CTEs) → **3** `related` MCP tool + graph-expand
  (retrieval **stage 6** authority boost) → **4** PDF via **Docling** (optional extra, page anchors)
  → **5** **Excalidraw + Mermaid** extraction (Mermaid `diagram_edge` → links table) → **6** OCR/image
  text (RapidOCR/Tesseract) → **7** **Prometheus** `/metrics` (standalone `prometheus_client` exporter).
  Ordering is dependency-driven: eval gate first (every index-touching stage lands through it), graph
  *data* (2) before consumers (3 + Mermaid edges in 5), Prometheus (7) last.
- **Why:** P1 is code-complete; P2 = `CLAUDE.md` §4 / [[build-roadmap]] ("full-format coverage,
  measurable quality"). Locking the stage plan in the vault (not just MEMORY) matches the P0/P1
  precedent — the vault is the source of truth for *what to build* (repo `CLAUDE.md` §1); MEMORY is
  the live progress log.
- **Grounded the plan in a fresh code check (verified, not assumed):** links are **parsed but never
  persisted** — `models.Link` + `parsing/obsidian.extract_links` fill `Document.links`, but the SQLite
  schema is still P0-only (`projects/documents/chunks/tags/jobs/embed_cache`), **no `links` table**, no
  store methods, and `indexer/pipeline` writes no edges (Stage 2 builds this). `retrieval/pipeline`
  implements stages 0–5+8, **no stage 6**. **No** PDF/Excalidraw/Mermaid-edge/OCR parser. Eval =
  minimal **20-pair** golden set + `run_eval.py` (recall@10/MRR@5 only). **No** `serve`/FastAPI/`api/`
  → Prometheus must be standalone. Tech pins confirmed against `reference-technology-stack.md`: Docling
  (MIT), RapidOCR/Tesseract, Prometheus `/metrics`; graph stays zero-LLM ([[no-full-graphrag]]).
- **Files:** `../Rag/projects/vaultrag/p2-step-plan.md` (new, vault — separate git repo). `MEMORY.md`
  (this entry + Current-state block). **No `src/` changes.**
- **Next / handoff:** **Settle the 5 open decisions** flagged in the plan (each has a recommendation):
  (1) CI determinism → frozen `embed_cache` replay + Qdrant `:memory:`, no Ollama in CI; (2) a committed
  `tests/eval/corpus/` fixture vault vs coupling the gate to the drifting design vault; (3) Docling as an
  optional `vaultrag[pdf]` extra; (4) link-resolution collision/re-resolution semantics; (5) standalone
  Prometheus exporter now, mount on `serve` later. Then implement **Stage 1 (eval harness + CI gate)** —
  it's independent and every later P2 stage must land through it. Working tree: `MEMORY.md` dirty; the
  vault got a new tracked file (`p2-step-plan.md`, separate repo). Commit when Anik asks.

### P1 (Stages 1–5, 2026-07-16 → 2026-07-19) — archived in `MEMORY-p1.md`
The full P1 build log — the 5 stages (rerank → parent-child → citations → watchdog →
write-back) plus the plan-decompose + decisions-settled entries — was moved to `MEMORY-p1.md`
on 2026-07-20 to keep this log P2-focused. Nothing deleted; read it there (or `git log`) for how
P1 was built.

### Older entries (P0, Steps 0–10) — archived in `MEMORY-p0.md`
The full P0 build log — acceptance dogfood, Step 10 + both code audits, Steps 0–9, scaffold —
was moved to `MEMORY-p0.md` on 2026-07-16 to keep this log P1-focused. Nothing deleted; read it
there (or `git log`) for how P0 was built.
