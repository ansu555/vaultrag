# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** **P1 IN PROGRESS — Stage 1 (reranker) code-complete + eval'd.** The rerank
  stage (retrieval stage 4) is built, wired, green (234 tests, ruff, mypy), and run against
  the live `vaultrag-design` index. `retrieval/rerank.py`: `Reranker` protocol + `BgeReranker`
  (bge-reranker-v2-m3 ONNX-int8, loaded directly via `onnxruntime` from
  `onnx-community/bge-reranker-v2-m3-ONNX`, tokenized with `tokenizers` — no torch/transformers,
  verified working) + `NoOpReranker` graceful fallback + `load_reranker` composition helper.
  Inserted into `retrieval/pipeline.py` after RRF fusion (fetch widens k→top_n=50, rerank,
  threshold, cut to k), config knobs added, wired through `mcp/server.py` + `cli.py mcp`.
  Minimal golden set + eval harness live at `tests/eval/` (decision 3; 20 pairs, recall@10/MRR@5,
  manual-run only). **Key finding (see 2026-07-16 log):** the eval caught that the vault's
  **0.3 rerank threshold regresses retrieval** (recall@10 1.00→0.90) — bge-v2-m3's sigmoid
  scores relevant chunks at ~0.02–0.25, so a fixed floor evicts them. A full threshold sweep
  confirmed 0.3 is the worst point and **0.0 (no floor) the best**; the threshold default is
  now **0.0**, superseding the vault's 0.3 pending ratification of
  `../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md` (Anik's insight:
  score-distribution thresholds must be per-vault/auto-calibrated, not one global number).
  **Rerank is DISABLED by default** (`rerank_enabled=False`, Anik's call): fusion-only beat
  rerank at every threshold on this golden set, so we don't ship a measured regression on by
  default — the stage is code-complete behind the flag (`VAULTRAG_RERANK_ENABLED=true`).
  **Next: Stage 2 (parent-child + parent-swap)** — or, before flipping rerank on, build the
  harder/paraphrase golden set the note calls for (this set is too title-matchy to show
  rerank's value; even at 0 floor it's marginally < fusion on it).
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
  **Ready to start Stage 1 (reranker).** Nothing implemented yet. P0's
  acceptance dogfood is **done** (2026-07-16 session log). The `vaultrag` MCP server is now
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
