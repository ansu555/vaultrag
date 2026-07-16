# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** **P1 IN PROGRESS — Stage 2 (parent-child + parent-swap) code-complete + live-verified.**
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
- **What works (P0, stable):** `uv sync` installs; `uv run vaultrag reindex --project X --vault Y`
  builds the index end-to-end; `uv run vaultrag mcp --project X --vault Y` serves `search`+`open`
  over real stdio (subprocess round-trip verified). Green: `ruff check`, `ruff format --check`,
  `mypy src` (33 files), `pytest` (216 tests). Pipeline: vault scan (BLAKE3 + `.ragignore` +
  secret guard) → md parse → heading-tree chunk (`parent_id` currently `None`; prev/next stitched)
  → Qwen3 embed via Ollama (cache keyed `(model,dims,chunk_hash)`) → Qdrant one `chunks` collection
  (named vectors `dense` int8 + sparse `bm25`, RRF fusion, payload-partitioned tenancy) → FastMCP
  `search`/`open`. Swappable behind `Embedder`/`VectorStore`/`MetaStore`/`SparseEmbedder`
  protocols. **Per-module detail lives in the code and `MEMORY-p0.md`** — not repeated here (log,
  not design doc; CLAUDE.md §7).
- **What's next:** **P1** (`CLAUDE.md` §4): watchdog + manifest/renames, reranker,
  parent-child, citations w/ `obsidian://` links, `write_note` + git auto-commit. **P1 is now
  decomposed into 5 implement-one-at-a-time stages in the vault:
  `../Rag/projects/vaultrag/p1-step-plan.md`** (sibling of `p0-step-plan.md`) — Stage 1 rerank
  → 2 parent-child → 3 citations → 4 watchdog → 5 write-back; quality trio first, the
  `ingest_path` primitive (Stage 4) before write-back (Stage 5). **The plan's open decisions are
  now settled (2026-07-16 review entry below):** reranker = keep bge-v2-m3 but load ONNX-int8
  directly via `onnxruntime` from `onnx-community/bge-reranker-v2-m3-ONNX` (canonical BAAI repo
  has no ONNX; `tokenizers` from fastembed tokenizes, no torch/transformers); parent-rep =
  reconstruct from vault (`parent_id` = pointer); eval = pull a minimal golden set forward for
  Stages 1–2 (full harness/CI stays P2); watcher = `serve`/indexer owns it, `mcp --watch` opt-in.
  **Stages 1 (rerank) + 2 (parent-swap) are DONE.** **Next: Stage 3 (`obsidian://` citations,
  retrieval stage 8) → 4 (watchdog + single-file ingest) → 5 (write-back).** The `vaultrag` MCP server is now
  registered in this project's local Claude Code config and pointed at the live
  `vaultrag-design` index, so **dogfooding is available right now**: as you build P1, use
  `search`/`open` against `../Rag` and treat any bad retrieval as a real bug report (repo
  `CLAUDE.md` §1). To re-run the dogfood from scratch on this **Linux** box (paths differ
  from the macOS commands that used to live here): ensure `ollama serve` + `docker compose
  up -d qdrant` are up, then `uv run vaultrag reindex --project vaultrag-design --vault
  ../Rag`; the server is already added via `claude mcp add vaultrag -- uv run --directory
  /home/anik2003/Documents/vaultrag vaultrag mcp --project vaultrag-design --vault
  /home/anik2003/Documents/Rag` (remove with `claude mcp remove vaultrag -s local`).
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

### Older entries (P0, Steps 0–10) — archived in `MEMORY-p0.md`
The full P0 build log — acceptance dogfood, Step 10 + both code audits, Steps 0–9, scaffold —
was moved to `MEMORY-p0.md` on 2026-07-16 to keep this log P1-focused. Nothing deleted; read it
there (or `git log`) for how P0 was built.
