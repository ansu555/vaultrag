# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** **P2 COMPLETE — all 7 stages DONE (1+2+3+4+5+6+7), and the index-drift bug is
  FIXED. Next: P3** (multi-project keys + tenancy hardening, compression/budget packer,
  query-log mining, optional `answer` mode, compose profiles) — `CLAUDE.md` §4.
- **Stage 7 (Prometheus `/metrics`) DONE 2026-07-21 — the last P2 stage.** New top-level
  `metrics.py` (own `CollectorRegistry`, metric objects behind `record_*` helpers so no other
  module imports `prometheus_client` — interfaces over vendors) + instrumentation in
  `retrieval/pipeline.py` (per-stage latency), `retrieval/rerank.py`, `embedding/cache.py`,
  `writeback/writer.py`, `indexer/watch.py`, and `--metrics` on `watch`/`mcp`. **Decision 5
  settled as recommended: standalone exporter**, and `prometheus-client` went in the **base**
  install (1 package, **zero transitive deps** — the opposite call from Docling's 101, made the
  same way: measure first). **The design calls that mattered:** (1) **ratios ship as their
  components** — `embed_cache_hit_ratio` → `..._lookups_total{result}`, `empty_result_ratio` →
  `searches_total`+`searches_empty_total`, `rerank_skipped_total` →
  `rerank_decisions_total{decision}` — because an in-process ratio is a **lifetime average** that
  cannot be windowed, so a real regression goes invisible inside it after a week of uptime;
  components are strictly *more* information (PromQL recovers the average). Proposed as a
  supersede in `../Rag/agent-inbox/prometheus-ratios-ship-as-components.md`. (2) **explicit
  decade buckets (0.1 ms → 60 s)**, since stages span five orders of magnitude (graph 0.39 ms,
  reranked query ~25 s) and the library defaults (5 ms–10 s) yield **no p50 or p95 at either
  end** — the two numbers the note asks for; a test pins the range. (3) **no path/query/title/
  project label anywhere** (cardinality *and* privacy — `/metrics` is unauthenticated), enforced
  by an allowlist test so a new high-cardinality label fails by default. (4) **the exporter never
  kills what it observes** — a port clash warns and returns False. (5) recording is always on;
  the flag gates only the HTTP server. **THE BUG THE LIVE RUN FOUND (fixtures could not):
  `ingest_lag_seconds` was permanently zero for every agent write.** Write-back ingests
  **synchronously**, so its fs event arrives after the file already matches the manifest and the
  watcher reconciles `unchanged` → records nothing. The whole memory loop was unmeasured and the
  plan's own criterion ("writing a note moves `ingest_lag_seconds`") was silently unmet while
  every layer looked correct — Stage 4's `Snippet.page` failure mode exactly. Fixed with
  `source="watch"|"writeback"`, which is also the honest shape: **measured live watch 2.21 s vs
  write-back 0.058 s**, a 38x gap that one shared histogram would have averaged into a p95
  describing neither. **Verified live** (real Qdrant + Ollama + real MCP stdio, scraped from
  outside the process): all 9 series present, a search timed **all 8 stages**, `write_ops_total`
  + `write_refused_total{secret}` both moved (CLAUDE.md §6's guardrail is now *observable*, not
  just enforced), an **external** edit moved `ingest_lag{watch}` and was searchable right after,
  queue depth returned to idle. **628 tests** (+30), ruff, mypy (57). Gate unmoved
  1.000/0.892/0.920.
- **Stage 6 (OCR / image text) DONE 2026-07-21, and Stages 1-5 were re-verified first.** New
  `parsing/image.py`: `OcrEngine` protocol with **RapidOCR primary** (it ships inside the Docling
  stack Stage 4 already installs, so the `pdf` extra buys OCR for free — no new dependency) and a
  **Tesseract fallback**; `ocr_enabled` (default on, but that only *permits* OCR — with no engine
  installed images skip cleanly, so the base install is untouched). **`PARSER_VERSION` → 4.**
  Images are `_BINARY_SUFFIXES`, so their secret gate runs on the **converted** text, as Stage 4
  established. **The design calls that mattered:** (1) **an image with no text raises `NoTextFound`
  rather than indexing an empty document** — a zero-block row is unretrievable and shows an agent a
  "result" with no content; it is a *skip*, and on a previously-indexed image an **evict** (text
  edited out must leave the index); (2) **`_parser_for` is format-aware** — Docling and an OCR
  engine install independently, so "is a parser available" had to mean "for *this* format", else
  every image becomes a dead job the moment the `pdf` extra happens to be present; (3)
  recognitions below **0.5 confidence are dropped** (OCR garbage never matches a query but does
  consume a chunk and pollute BM25); (4) lines are emitted in **banded-y-then-x reading order**,
  since detection order follows the model's box ranking, not the page layout; (5) `open` refuses
  images like PDFs. **The VLM caption pass stays off *and unbuilt*** (GPU/money, principle #2) —
  no dead knob. **Verified live end-to-end:** OCR'd image ranked **#1** for both a semantic query
  and the bare identifier `MER-1422` (the BM25 path), blank image left no ghost document, `open`
  refused it, and md/excalidraw/pdf were unaffected. **598 tests** (+21), ruff, mypy (56).
  Gate unmoved 1.000/0.892/0.920.
- **THREE BUGS FOUND AND FIXED while verifying Stages 1-5 (2026-07-21).** The stages were *not*
  clean, though tests, lint, mypy and the gate were all green before I started:
  **(1) SECURITY — the secret guard missed every underscore-prefixed credential name.** A leading
  `\b` in `_ASSIGNMENT_RE` cannot match inside `AWS_SECRET_ACCESS_KEY` / `GITHUB_TOKEN` /
  `DB_PASSWORD` / `MY_API_KEY`, because `_` **is** a word character — so the guard caught the bare
  `password=` form nobody pastes and missed the env-var form everybody does. Pre-existing since
  P0 and **not OCR-specific** (a markdown note leaked the same way); it was masked because planted
  secrets in tests/vaults sat in files whose *filename* rule fired first. Fixed by replacing the
  leading `\b` with `(?<![A-Za-z0-9])`, so `_`/`-` read as boundaries and letters/digits do not.
  **Zero false positives across `../Rag` (58), the eval corpus (27) and `src` (56).**
  **(2) Mermaid: an arrow inside a quoted label was parsed as a connector.** `A["draft --> live"]`
  truncated the node's real name and leaked a garbage edge (`B mapping ]`, unbalanced bracket) —
  exactly the `unresolved_links` noise the placeholder filter exists to prevent. The module
  already masked *pipe* labels before splitting; quoted spans (Mermaid's documented escape for
  grammar characters) were not. Fixed by masking quotes **first**, which also fixed a pipe inside
  a node label.
  **(3) A textless image failed the *reconcile* path while `ingest_path` skipped it** — found only
  by running a real vault, since the unit test used the primitive. One blank screenshot made
  `reindex` report `failed: 1` and park a dead job. `NoTextFound` now joins `SecretBlocked` on the
  existing `_WITHHELD` sentinel (counts as `skipped`). **Pinned by a test that uses `reindex_vault`
  specifically** — the two paths need separate coverage.
- **Stage 5 (Excalidraw + Mermaid) DONE 2026-07-21.** Mermaid fences now feed `diagram` edges into
  Stage 2's `links` table and Excalidraw scenes are parsed + searchable. **The design call that
  mattered: diagram edges are anchored on the *containing note*, not node-to-node.** `A --> B` in
  `architecture.md` writes `architecture.md → A` and `→ B` — forced by the schema (`replace_links`
  replaces by `src_doc`, so an edge sourced at another doc could never be *revoked* when this note's
  diagram is edited) and the right relation anyway. **Only flowchart/sequence produce edges**;
  class/ER/state/gantt/pie/mindmap degrade to searchable-text-only rather than being half-parsed
  into wrong edges. **Unlabelled placeholder ids (`A`, `B2`) are dropped** — Mermaid's own docs use
  them, and emitted as edges they bury the real broken wikilinks in `unresolved_links`; a node
  carrying a *label* is exempt, and a label declared once (`A[Gateway]`) is reused when the id
  recurs bare (needed a two-pass parse). **Grammar subtlety worth keeping:** a labelled link opens
  with **exactly two** dashes, which is what separates it from the three-dash long link — without
  that rule `A --- B --- C` reads as one connector whose "label" is `B` and **the middle node of
  every chained long link vanishes**. **Excalidraw: dropping the `compressed-json` payload is a
  correctness fix, not a feature** — tens of KB of unbroken base64 that the markdown parser would
  embed like prose, spending a whole note's chunk budget on something no query can match.
  Decompression via **`lzstring`** (pure Python, MIT, no transitive deps) **verified byte-compatible
  with the JS `lz-string` the plugin uses, in both directions**; the committed fixture's blob is
  generated by the **JS** library on purpose, since a round-trip through one implementation proves
  only self-consistency. Frames become H2 sections; elements are emitted in **reading order**
  (banded y, then x) because a scene is z-ordered, i.e. drawn-order. **`open` had to change too,
  and only dogfooding showed it:** `search` returned clean text while `open` still handed the agent
  the entire blob — now a bare `.excalidraw` is refused like a PDF and a *wrapper* is served with
  the payload trimmed as a **suffix** cut, so retained lines keep their on-disk numbers
  (18 lines / 83 tokens vs tens of KB), and `open` now reports the same title `search` cites
  (it was saying `Excalidraw Data`, the plugin's scaffolding heading). **576 tests** (+66), ruff,
  mypy (55). Gate unmoved 1.000/0.892/0.920.
- **NEW MECHANISM — `index_state.parser_version` + a stale-index warning (2026-07-21).** Stage 5
  would otherwise have landed as a **silently empty diagram graph on every already-indexed vault**:
  a hash-equal file is `unchanged` to the manifest even when today's parser would read *more* out
  of it, so the reconcile does nothing and the new data never appears. That is **Stage 2's backfill
  trap, one stage later**, so it was fixed *generally* instead of specially — `PARSER_VERSION`
  (in `vaultrag/parsing/__init__.py`, **bump it whenever the parser extracts something new**) is
  recorded per project in a new `index_state` table, and `reindex` warns and names `--force` as the
  repair. Only a clean `--force` run may clear it (a partial run cannot honestly claim the stale
  documents were re-read). **Verified on the real design vault:** the warning fired on the live
  pre-Stage-5 index (57 files `unchanged`), `--force` repaired it in **1.9 s / 183 chunks with no
  re-embedding** (the `(model,dims,chunk_hash)` cache absorbs it), next run a 139 ms silent no-op.
  **Also fixed on the way: `_backfill_graph` parsed everything as markdown**, bypassing the PDF/
  Excalidraw routing — it now goes through `_parse_source`, so the one path that could quietly
  write *wrong* edges no longer can.
- **Stage 4 (PDF via Docling) DONE 2026-07-21. Decision 3 settled: `vaultrag[pdf]`, an optional
  extra.** Measured, not assumed — `docling` resolves to **101 packages incl. torch/torchvision/
  transformers (~2.5 GB)**; requiring that in the base install would contradict principle #2
  outright. (`docling-slim` looked like a lighter path and **is not** — it can't even
  `import DocumentConverter`, missing scipy.) **The pipeline stayed format-agnostic:** Docling
  converts PDF → markdown, that markdown goes through the *existing* markdown parser, so
  chunking/embedding/indexing never learned PDFs exist — one new branch in one function
  (`_parse_source`). **Pages survive by construction:** per-page markdown exports are
  *concatenated* into one document (so the heading tree stays continuous across page breaks —
  parsing each page alone would stop an H1 on page 1 from scoping page 2) with each page's start
  line recorded; blocks already carry `line_start`, so stamping `Block.page` is a lookup, not a
  second parse. `Chunk.page` = the **first** page of the chunk (where an agent should land), since
  a chunk can straddle a break. **The page had to be carried all the way to the agent** — it
  reached the Qdrant payload but `Snippet` had no `page` field, so the plan's acceptance criterion
  was silently unmet until `Snippet.page` was added + populated in both citation paths.
  **The secret guard MOVED rather than disappeared:** the walk content-scans every indexable file,
  but a PDF's bytes aren't text (decoding → mojibake → meaningless scan), so binary sources are
  excluded from the walk's scan and gated on their **converted** text inside the pipeline —
  without that, Stage 4 would have opened a hole in CLAUDE.md §6. **`open` refuses PDFs** with an
  actionable message pointing at `search`'s page-cited chunks (serving decoded PDF bytes is worse
  than a refusal) — a real limitation, since opening a page's text needs re-conversion or stored
  extraction. **A missing extra is a SKIP, not a failure** (new `ReindexStats.skipped`, CLI `?N`):
  otherwise a base install on a mixed vault reports a wall of failures for working as designed.
  **Verified on real PDFs:** a committed 3-page fixture pins the mapping in tests, and a real
  **25-page / 1.2 MB** PDF indexed live → 48 chunks, **every one page-stamped**, 20+ distinct
  pages, notes alongside carrying none; over real MCP stdio `search` returned correct hits with
  page anchors and **pages 5/7/9 were independently confirmed** against Docling's own per-page
  export (the check that catches an off-by-one). **Perf:** first conversion ~220 s but that is
  almost all one-time model download; warm is **~0.6 s/page** (trivial PDF) and **~2.7 s/page**
  (real image-heavy one). `do_ocr=False` measured **no** difference on a born-digital PDF (1.7 vs
  1.8 s, identical output) so no knob was added. **510 tests** (+16), split so Docling-dependent
  ones skip without the extra. Gate unmoved 1.000/0.892/0.920.
- **Stage 3 (`related` + graph-expand) DONE 2026-07-21, and it ships ON.** The plan's
  condition ("on only if a measured non-regression on the Stage 1 gate") was met: gate
  **recall@10 1.000 · MRR@5 0.892 unchanged · nDCG@10 0.919 → 0.920, PASS**, plus a real gain
  where the graph is dense — live design vault, 58 cases, **MRR@5 0.710 → 0.749 (+0.039)**,
  nDCG 0.779 → 0.806. **The boost is a fraction of the result set's score spread, not an
  absolute bonus** — incoming scores are RRF (~0.016), a cross-encoder sigmoid, or a constant
  1.0 from `NoOpReranker`, so any fixed number is a no-op on one scale and decisive on another.
  That scaling buys the safety property the stage rests on: **a chunk can only overtake another
  if it was already within `boost x spread` of it** — authority settles a photo-finish, never a
  decisive win. **Default `graph_boost=0.25` swept on both sets:** corpus 0.10/0.25 +0.000,
  0.50 -0.000, **1.00 -0.151**; design vault 0.10 +0.014, **0.25 +0.033**, 0.50 +0.062, 1.00
  +0.003. Same "two golden sets disagree for an understood reason" shape as rerank — the corpus
  is 27 notes / 57 links with one correct answer per query, so in-result-set in-degree has
  little to work with. 0.50 measured better on the design vault; **0.25 is the conservative
  point non-negative on both**, and it's a per-vault knob. In-degree is counted **only within
  the result set** (query-conditional, not a global PageRank — otherwise every MOC dominates
  every search it grazes). **Neighbours are a separate `SearchResult.related` field, never
  extra snippets**: merging them into `snippets` would make pointers indistinguishable from
  results and **silently inflate every recall number the gate computes**. Latency **p50 0.39 ms
  / p95 0.66 ms** over 58 live queries — 10x under the plan's 5 ms budget, pure SQLite, which is
  why "on by default" is free (none of rerank's latency argument applies). **Found only by
  dogfooding the live vault:** `related` returned **49 entries — 1 link, 2 backlinks, 46 tag
  siblings**, because `tags` is prefix-expanded and one shared `project/…` namespace makes every
  note a weak sibling; new `graph_max_tag_siblings` (default 5) → 3 links + 5 strongest siblings.
  **480 tests** (+51), ruff, mypy (51).
- **`rerank_top_n` 50 → 20, and deeper is WORSE (2026-07-21).** Corpus set, 72 cases:
  fusion **0.892** MRR@5 · `always`@50 **0.928** · `always`@5 **0.939** · **`auto`@5 0.946**.
  So the cheapest config is also the best: candidates past the useful depth hold no winnable
  cases but plenty of distractors, and a distractor scored high enough displaces a correct
  chunk. **`auto` is now measured** (it was the open unknown) and wins on both axes.
  The default is measured, not guessed — new **`vaultrag eval --ceiling`** reports how deep
  the *correct* note already sits in fusion order, with **no model, in seconds**: saturates at
  rank **5** on the corpus fixture, rank **14** on the live design vault. 20 clears the deeper
  with headroom (15 would be fitted to a 58-case sample's exact max). The fixture saturates
  early only because it is 51 chunks — **re-run `--ceiling` per vault; don't trust 20 blindly.**
  Latent bug found and fixed on the way (committed by Anik as `ed0ef31`): `search` set
  `fetch_limit = top_n`, so any `top_n < k` silently returned **fewer results than
  rerank-off** — a shallow rerank would have read as a recall regression that was really
  truncation. Now `max(top_n, k)`, with the unreviewed remainder kept in fusion order below
  the reranked survivors (and exempt from `threshold` — an unscored chunk has no score to
  fail a floor with). **Gate unmoved: 1.000/0.892/0.919** (it pins `rerank_mode=never`).
  **429 tests** green, ruff, mypy (50).
- **✅ Index-drift bug FIXED 2026-07-21 (was: "`reindex` cannot repair a drifted vector
  store").** The CLAUDE.md §6 "DR = reindex" guardrail is true again. Three parts, all measured:
  **(1) `vaultrag reindex --force`** — implemented in the *classifier*
  (`diff_manifest(force=True)` routes hash-equal files to `updates` instead of `unchanged`), so
  deletes and renames are still derived normally; a force that bypassed the diff would re-upsert
  everything **and strand documents deleted from disk**. **(2) `live_stack` refuses to score a
  drifted index** — raises `DriftError` (a `ValueError`, so the eval CLI's existing "can't run"
  handling maps it to **exit 2**), mirroring the `stats.failed` guard `replay_stack` already had.
  **(3) A post-reindex drift warning** — the cheap slice of the `doctor` option: every `reindex`
  ends with the one-query check and says `warning: index drift … — re-run with --force`. That is
  what makes `--force` discoverable *at the moment it's needed*, which was option 1's stated
  weakness. New `consistency.py` (`Drift`/`DriftError`/`check_drift`) + `MetaStore.chunk_count`.
  **Verified live, and the bug reproduced on the way:** with 2 points deleted from Qdrant, a
  plain `reindex` did **real work** (`+1 ~2 =55`, 35 chunks) **and was still drifted 179 vs 177**
  — it will never converge. `eval --live` exit **2**; `--force` → `~58`, 179 chunks, **1.7 s**,
  manifest = Qdrant = 179. **The repair is affordable because force re-upserts but does not
  re-embed** — the `(model,dims,chunk_hash)` cache absorbs it (1.7 s forced vs 8.7 s for the
  partial run before it). **Still exposed:** the **MCP server does not check drift at startup**,
  so a long-running `vaultrag mcp` can still serve a drifted index silently. Resolution appended
  to `../Rag/agent-inbox/reindex-cannot-repair-a-drifted-vector-store.md` (status → resolved).
  **Gotcha retained from the manual repair:** the `sqlite3` CLI defaults `foreign_keys` **off**,
  so deleting `documents` without the pragma orphans `tags` rows and the next ingest fails on FK
  constraints. **494 tests** (+14), ruff, mypy (52). Gate unmoved: 1.000/0.892/0.920.
- **Rerank is a user-chosen mode (2026-07-21, Anik's ruling).** `rerank_enabled: bool` →
  **`rerank_mode: never | auto | always`** (`VAULTRAG_RERANK_MODE`), default **`never`**.
  `auto` decides **per query** from the top-1/top-2 RRF margin — **not** an LLM call, so it stays
  $0/local/deterministic; it skips **37.5%** of corpus queries at the default ratio.
  **The default is `never` for LATENCY, not quality:** bge-v2-m3 measures **~500 ms per passage**
  on this box (int8 502 / quantized 509 — quantization isn't the bottleneck), so `top_n=50` costs
  **~25 s per reranked query** — unusable for an interactive tool, regardless of the real +0.036
  MRR@5. Making the stage *affordable* (drop `rerank_top_n` first — cost is linear in it) now
  ranks ahead of calibrating it; see
  `../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md`. **The eval gate pins
  its mode explicitly** (`vaultrag eval --rerank`, default `never`) rather than inheriting the
  product default — a PR check must never pick up a 600 MB model by accident.
- **Stage 2 (link-graph persistence) DONE 2026-07-21.** The wikilink graph is now *data*:
  links were parsed-then-discarded since P0, and they now live in a **`links` table** written on
  every ingest. New `store/links.py` (pure resolver), `links` + `doc_keys` DDL, 10 `MetaStore`
  methods (`replace_links`/`reresolve_links`/`outgoing_links`/`linked_docs`/`backlinks`/
  `neighbors`/`tag_siblings`/`unresolved_links`/`doc_key_count`), pipeline wiring + a **graph
  backfill**. **Decision 4 settled** — resolution ladder **`path` → `basename` → `title` →
  `alias`** (the plan said title-then-alias; `basename` had to be added *above* title because
  `[[Note]]` addresses the **file**, and a note's `title` is frontmatter-else-heading-else-stem
  and often differs). Ambiguity inside the winning tier → `dst_doc=NULL` + `dst_raw` kept
  (**never guess**); a lower tier never breaks a higher tier's tie. **Re-resolution is broader
  than the plan's "NULL set"**: an already-resolved edge can go stale too (a new `Gateway.md`
  outranks an edge pointing at a note *titled* Gateway) and a **delete** can un-break a tie, so
  every edge naming a changed doc is re-derived, on both add and delete — still bounded to that
  doc's names. **Verified live, not just on fixtures:** the real pre-Stage-2 `state/vaultrag.db`
  (82 docs, **no `links` table at all**) backfilled **54 docs with 0 chunks re-embedded**;
  **147 edges, 90.5% resolved**; the 14 danglers are the vault's own *docs about wikilink syntax*
  (`[[Note]]`, `[[...]]`) + one deliberately-unwritten note — i.e. correct. **0 cross-project
  edges.** Found + fixed a real parsing bug on the way: `[[Note\|alias]]` (Obsidian's escaped
  pipe inside a table) stranded the `\` on the target, so **every link in a markdown table failed
  to resolve**. Eval gate unmoved (1.000/0.892/0.919 — Stage 2 doesn't touch retrieval).
  **Green: 412 tests** (+53), ruff, ruff format, mypy (49 src files).
- **(Stage 1 archive)** **Stage 1 (full eval harness + merge-blocking CI gate) DONE.**
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
  **(1) CI determinism and (2) the fixture corpus were settled + built in Stage 1**, and
  **(4) link-resolution collision / re-resolution was settled + built in Stage 2** (details in the
  Phase bullets above). **Still open, each for its own stage:** (3) Docling as an optional extra
  `vaultrag[pdf]` (Stage 4); (5) standalone Prometheus exporter now, mount on `serve` later
  (Stage 7).
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

### 2026-07-21 — Stages 1-6 verified, then P2 Stage 7: Prometheus `/metrics` — **P2 COMPLETE**
- **Did:** Anik: *"if 1-6 stage are fully ready then go for the next"*. Verified stages 1-6 (598
  tests, ruff, mypy 56, gate 1.000/0.892/0.920, plus a live reindex of `../Rag` — clean, no drift,
  no stale parser), then built and verified Stage 7 of `../Rag/projects/vaultrag/p2-step-plan.md`.
  **That closes P2.**
- **The bug worth remembering, and fixtures could not have found it.** `ingest_lag_seconds` was
  built on the watcher, because the design note frames it as "fs event → searchable". Write-back
  ingests **synchronously**, so its fs event arrives *after* the file already matches the manifest
  and the watcher's reconcile is `unchanged` — which records nothing, correctly. Net effect: every
  agent write left the lag histogram at **zero**, the entire memory loop was unmeasured, and the
  plan's own acceptance criterion ("writing a note moves `ingest_lag_seconds`") was unmet while
  unit tests, lint, mypy and the gate were all green. **This is the same failure mode as Stage 4's
  `Snippet.page`:** the number reached one layer and stopped, and every intermediate layer looked
  right. Fixed with a `source="watch"|"writeback"` label — which turned out to be the honest shape
  anyway: measured live, **watch 2.21 s vs write-back 0.058 s**, so one shared histogram would have
  produced a p95 describing neither route.
- **The naming deviation I'd re-derive: ratios ship as components.** The design note names
  `embed_cache_hit_ratio` / `empty_result_ratio` / `rerank_skipped_total`. A ratio computed
  in-process is a **lifetime average** — it cannot be windowed, so after a week of uptime a real
  regression is invisible inside it, and a bare skip counter cannot express a skip *rate* at all.
  Shipped as counter pairs/labelled counters; PromQL recovers the average, so the components are
  strictly more information. Filed as a supersede in
  `../Rag/agent-inbox/prometheus-ratios-ship-as-components.md` rather than drifting silently.
- **Buckets were a real decision, not boilerplate.** Stages here span five orders of magnitude
  (graph expand 0.39 ms; a reranked query ~25 s). `prometheus_client`'s defaults (5 ms–10 s) put
  every graph observation in the first bucket and every reranked one in `+Inf` — a histogram that
  exists and answers nothing. Explicit decade ladder 0.1 ms → 60 s, with a test pinning the range
  against those measured numbers so a later edit can't quietly narrow it.
- **`prometheus-client` is a base dependency, not an extra** — 1 package, zero transitive deps.
  Deliberately the opposite call from Stage 4's Docling (101 packages → extra), reached the same
  way: resolve it and look, don't assume. Observability that ships by default is there *before*
  the incident.
- **Two false alarms I chased down rather than assumed** (both turned out to be my test artifacts,
  and both were worth confirming): `embed_cache_lookups` read 0/0 after a live write — the note
  had chunked to **0 chunks** because a stale manifest entry from an earlier run made the ingest
  `unchanged`, so there genuinely was no cache lookup; and the watcher reconciling a write-back
  file as `unchanged` is expected, documented behaviour, not a double-ingest bug.
- **Files:** `metrics.py` (new), `config.py` (`metrics_enabled`/`metrics_port`/`metrics_addr`),
  `retrieval/pipeline.py` (per-stage `time_stage` + `record_search`), `retrieval/rerank.py`
  (decision counter), `embedding/cache.py` (hit/miss, counted in *deduplicated* hashes),
  `writeback/writer.py` (`_observed` decorator over the 3 public tools — one funnel for accepted,
  refused *and* write-back lag), `indexer/watch.py` (`_first_dirty` span, queue-depth gauge, dead
  jobs, ingest status), `cli.py` (`--metrics` on `watch`/`mcp`, `_start_metrics`),
  `pyproject.toml`. Tests: `tests/test_metrics.py` (new, 30). **Vault:** `p2-step-plan.md` Log +
  Decision 5 settled; new `agent-inbox/prometheus-ratios-ship-as-components.md`.
- **Green: 628 tests, ruff, ruff format, mypy (57). Gate unmoved: 1.000 / 0.892 / 0.920.**
- **Next / handoff:** **P3** (`CLAUDE.md` §4) — multi-project keys + tenancy hardening,
  compression/budget packer, query-log mining, optional `answer` mode, compose profiles. There is
  **no P3 step plan in the vault yet**; write one the way P0/P1/P2 had, before coding. Things to
  know: (a) **`serve`/FastAPI still does not exist** — when it lands, mount
  `vaultrag.metrics.registry()` at `/metrics`; the standalone exporter stays as the second
  front-end onto the same registry, no rework; (b) a **`project` label on metrics is deliberately
  absent** and becomes meaningful exactly when P3's multi-project tenancy lands — that is the time
  to add it, and the allowlist test in `test_metrics.py` will need it too; (c) `state/vaultrag.db`
  **is still tracked in git** and still needs `git rm --cached state/vaultrag.db` from Anik —
  `.gitignore` is fixed but does not apply to already-tracked files (now 4 sessions running);
  (d) the **MCP server still does not check drift at startup**, unchanged since Stage 4 — with
  `/metrics` now available, a drift *gauge* is the natural way to close it; (e) CI installs no
  extras, so Docling/OCR tests skip there — `uv sync --extra pdf` locally before claiming those
  paths green; (f) `PARSER_VERSION` stays at **4** — Stage 7 observes, it does not parse.

### 2026-07-21 — Stages 1-5 re-verified (3 bugs found + fixed), then P2 Stage 6: OCR / image text
- **Did:** Anik: *"check and test upto stage 5 in p2 if everything is working perfectly with no
  bugs … if any problem or bugs found solve it then go to next stage and test that stage too"*.
  Re-ran the whole stack, found **three real bugs in "done" stages**, fixed them, then built and
  verified Stage 6 of `../Rag/projects/vaultrag/p2-step-plan.md`.
- **The lesson worth keeping: green CI did not mean correct.** Before I touched anything, 576
  tests / ruff / mypy / the gate were all green and the live vault reindexed clean. All three bugs
  were found by *exercising* the features against inputs the tests did not contain — a vault with
  real diagrams and images, not fixtures written alongside the code they test.
- **Bug 1 (security, pre-existing since P0): the secret guard missed `AWS_SECRET_ACCESS_KEY=…`.**
  `\b` cannot match inside `AWS_SECRET_ACCESS_KEY` — `_` is a word character — so the
  keyword-anchored rule caught `password=` and missed every real env-var name (`GITHUB_TOKEN`,
  `DB_PASSWORD`, `MY_API_KEY`). Not OCR-specific: a plain markdown note leaked identically. It hid
  because planted secrets lived in files like `secrets-note.md`, where the **filename** rule fired
  first and masked the content rule's failure. Fix: `(?<![A-Za-z0-9])` instead of the leading `\b`.
  Checked both directions — 8 realistic credential forms now fire, 7 prose/placeholder forms stay
  quiet, and a sweep of `../Rag` + the eval corpus + `src` flagged **nothing** (a false positive
  here silently drops a real note, so that half mattered as much).
- **Bug 2 (Stage 5): a quoted Mermaid label containing an arrow was split as a connector.**
  `A["draft --> live"] --> B` truncated the node name and emitted a junk edge with an unbalanced
  bracket. `parsing/mermaid.py` already masked **pipe** labels before splitting; quoted spans —
  Mermaid's own escape hatch for grammar characters — were not. Masking quotes *first* fixes it
  and, as a bonus, fixes a pipe inside a node label (`A["stdout|stderr"]`).
- **Bug 3 (Stage 6, found live): a blank image failed the reconcile instead of skipping.** My unit
  test used `ingest_path`, which caught `NoTextFound`; `reindex_vault` runs a **different** path
  and did not, so one blank screenshot produced `failed: 1` + a dead job. This is Stage 4's "a
  skip is not a failure" rule recurring, and the fix reuses the `_WITHHELD` sentinel already built
  for `SecretBlocked`. **The regression test uses `reindex_vault` on purpose.**
- **Stage 6 as built.** RapidOCR is **already inside the Docling stack** Stage 4 installs, so OCR
  cost zero new dependencies; Tesseract is the fallback, both behind an `OcrEngine` protocol.
  `PARSER_VERSION` → **4** (the one-line habit from Stage 5 — otherwise every indexed vault
  silently never gains image text). Four judgment calls are in the Phase bullet; the one I would
  re-derive is **`_parser_for` being format-aware**: testing "any parser loaded" would have made
  every image a dead job on exactly the installs that have the `pdf` extra.
- **Verified live, not only on fixtures:** a scratch vault carrying all five source types
  (md + wikilinks, mermaid, `.excalidraw`, PDF, images) through real Qdrant + real MCP stdio. The
  OCR'd image ranked **#1** for a semantic query *and* for the bare identifier `MER-1422`; a
  screenshot of an `AWS_SECRET_ACCESS_KEY` was blocked by the **content** gate (renamed to an
  innocuous filename first, so the filename rule could not mask it again — the same masking that
  hid bug 1); the blank image left no ghost document; `open` refused images; md/excalidraw/pdf
  and `related` were unaffected.
- **Files:** `parsing/image.py` (new), `parsing/__init__.py` (`PARSER_VERSION` 4),
  `parsing/mermaid.py` (quote masking), `indexer/secret_guard.py` (boundary fix),
  `indexer/scan.py` (`IMAGE_SUFFIXES`, `is_image_source`), `indexer/pipeline.py` (`image_parser`,
  `_parser_for`, `SkipReason.no_text`, `NoTextFound` → `_WITHHELD`, per-format `_drop_unparseable`),
  `config.py` (`ocr_enabled`), `cli.py`, `mcp/server.py` (`open` refuses images), `pyproject.toml`
  (mypy overrides). Tests: `tests/test_image.py` (new, 18) + 2 in `test_scan.py` + 2 in
  `test_diagrams.py` + fixtures `diagram-shot.png` / `blank-shot.png`; `test_scan.py`'s fixture
  swapped its "unsupported" `.png` for a `.zip` (since Stage 6 makes `.png` supported).
- **Green: 598 tests, ruff, ruff format, mypy (56). Gate unmoved: 1.000 / 0.892 / 0.920.**
  Live `vaultrag-design` force-reindexed onto parser 4 (58 docs / 187 chunks / **1.7 s**, no
  re-embedding — the `(model,dims,chunk_hash)` cache absorbs it).
- **Next / handoff:** **Stage 7 — Prometheus `/metrics`**, the last P2 stage. Things to know:
  (a) there is still **no `serve`/FastAPI surface**, so it ships **standalone** via
  `prometheus_client.start_http_server`, not mounted on HTTP (the plan's Decision 5, still open);
  (b) **bump `PARSER_VERSION` only when the parser extracts something new** — Stage 7 observes,
  it does not parse, so it should *not* bump; (c) `state/vaultrag.db` **is still tracked in git**
  and still needs `git rm --cached state/vaultrag.db` from Anik — `.gitignore` is fixed but does
  not apply to already-tracked files; (d) the **MCP server still does not check drift at startup**,
  unchanged since Stage 4; (e) CI installs no extras, so the 2 real-OCR tests and the 3 Docling
  tests skip there — run `uv sync --extra pdf` locally before claiming those paths are green;
  (f) the secret-guard fix is worth a vault note — the *filename rule masking a content-rule
  failure* is a general testing trap, not a one-off.

### 2026-07-21 — P2 Stage 5: Excalidraw + Mermaid diagram extraction
- **Did:** Anik: *"implement next stage and completely implement, if any bug or anything comes
  resolve it"* → Stage 5 of `../Rag/projects/vaultrag/p2-step-plan.md`. New `parsing/mermaid.py`
  (fence → `diagram` links) and `parsing/excalidraw.py` (both file shapes), `LinkKind.diagram`,
  `.excalidraw` added to `_TEXT_SUFFIXES`, format routing in `_parse_source`, `open` hardened, and
  a new `index_state.parser_version` staleness marker.
- **Anchoring is the decision to remember.** Diagram edges hang off the *containing note*, never
  node-to-node. `links.src_doc` is what `replace_links` replaces on, so an edge sourced at some
  other document could never be **revoked** when this note's diagram is edited — the graph would
  stop being a pure function of the vault. There is a test that edits a diagram and asserts the
  old edge is gone rather than merely superseded.
- **Two parser rules that are load-bearing, not polish.** (1) A labelled Mermaid link opens with
  **exactly two dashes** — this is Mermaid's own grammar, and without it `A --- B --- C` parses as
  one connector whose "label" is `B`, so **the middle node of every chained long link silently
  disappears**. (2) Unlabelled placeholder ids (`A`, `B2`) are dropped: Mermaid's docs use them and
  so does everyone copying them, and as edges they would flood `unresolved_links` and bury the real
  broken wikilinks. A node with a label is exempt, and a label declared once is reused when the id
  recurs bare — which is why the flowchart parse is two passes.
- **The Excalidraw payload is a correctness problem, not a feature gap.** The plugin's
  `compressed-json` blob is tens of KB of unbroken base64; left to the markdown parser it is an
  atomic code block embedded like prose, spending a whole note's chunk budget on something no query
  can ever match. `lzstring` (pure Python, MIT, no transitive deps) decompresses it — and I
  **verified byte-compatibility against the real JavaScript `lz-string` in both directions** before
  depending on it. The committed fixture's blob is generated by the **JS** library deliberately: a
  round-trip through the Python port would only prove the port agrees with itself.
- **Dogfooding found the gap the fixtures could not.** Over real MCP stdio, `search` returned clean
  scene text (the drawing ranked **#1** for a query about the query path) — but `open` still handed
  the agent the whole base64 blob. Fixed as a **suffix** cut so every retained line keeps its
  on-disk number (`lines=[a,b]` stays true): wrapper now 18 lines / 83 tokens, bare `.excalidraw`
  refused like a PDF. Also caught that `open` reported the title as `Excalidraw Data` (the plugin's
  scaffolding heading) while `search` said `ingest-flow` — one shared `scene_title` now makes them
  agree by construction.
- **The trap I went looking for on purpose, and found.** A hash-equal file is `unchanged` to the
  manifest even when today's parser would read *more* out of it, so Stage 5 would have landed as a
  **silently empty diagram graph on every already-indexed vault** — Stage 2's backfill problem
  exactly, one stage later. Fixed generally: `PARSER_VERSION` + an `index_state` table + a
  post-reindex warning naming `--force`. **Bump `PARSER_VERSION` whenever the parser starts
  extracting something new.** While wiring it I also found `_backfill_graph` parsing *everything*
  as markdown — the one path that could quietly write wrong edges for a PDF or a scene; it now
  routes through `_parse_source`.
- **Two bugs my own tests caught before they shipped:** a fresh index reported itself stale
  (freshness was sampled *after* the ingest that populated it), and the planted-secret test passed
  a string that matches no guard rule — the guard was right, the test was wrong.
- **Files:** `parsing/mermaid.py` + `parsing/excalidraw.py` (new), `parsing/__init__.py`
  (`PARSER_VERSION`), `parsing/markdown.py` (Mermaid fence → links), `models.py`
  (`LinkKind.diagram`), `indexer/scan.py` (`.excalidraw`), `indexer/pipeline.py` (routing,
  `stale_parser`, backfill fix), `store/{base,sqlite}.py` (`index_state`), `mcp/server.py`
  (`open`), `cli.py` (warning), `pyproject.toml` (`lzstring` + mypy override). Tests:
  `tests/test_diagrams.py` (new, 66) + `tests/fixtures/ingest-flow.excalidraw.md` (JS-generated).
  **Vault:** `p2-step-plan.md` Log, `reference-database-schema.md` (`index_state` + diagram
  anchoring), `knowledge-graph-integration.md`.
- **Green: 576 tests, ruff, ruff format, mypy (55). Gate unmoved: 1.000 / 0.892 / 0.920.**
- **Next / handoff:** **Stage 6 — OCR / image text** (RapidOCR/Tesseract). Things to know:
  (a) images are **binary**, so they belong in `_BINARY_SUFFIXES` and their secret gate must run on
  the *converted* text inside the pipeline, the way Stage 4 did for PDFs — `.excalidraw` needed
  neither because it is text; (b) **bump `PARSER_VERSION` to 4** when OCR lands, or every
  already-indexed vault silently never gains image text — that is now a one-line habit rather than
  a rediscovered bug; (c) the plan's optional **VLM PNG caption stays off** (GPU/money, principle
  #2), and the same applies to Excalidraw's optional render-and-caption pass, which was not built;
  (d) `state/vaultrag.db` **is still tracked in git** and still needs `git rm --cached
  state/vaultrag.db` from Anik — `.gitignore` is fixed but does not apply to already-tracked files;
  (e) the **MCP server still does not check drift at startup**, unchanged from the Stage 4 handoff.

### 2026-07-21 — P2 Stage 4: PDF ingestion via Docling (optional extra)
- **Did:** Stage 4 of `../Rag/projects/vaultrag/p2-step-plan.md`, straight after the drift fix in
  the same session. New `parsing/pdf.py` (`PdfParser` protocol + `DoclingPdfParser` +
  `load_pdf_parser` graceful-skip), `.pdf` added to the scanner, format routing in
  `indexer/pipeline._parse_source`, `Block.page` → `Chunk.page` → payload → **`Snippet.page`**,
  `pyproject` extra `pdf`, and `open` refusing PDFs.
- **Decision 3 settled with a measurement, not a preference.** `docling` = **101 packages incl.
  torch/torchvision/transformers**. I checked `docling-slim` first hoping for a light path — it
  is not one (can't `import DocumentConverter`, missing scipy). So: optional extra, base install
  untouched.
- **Three things that would have been silent bugs.** (1) **`Snippet` had no `page`** — the number
  reached Qdrant and stopped there, so the acceptance criterion "the citation lets an agent open
  the right page" was unmet while every intermediate layer looked correct. (2) **The secret guard
  would have had a hole**: the walk content-scans indexable files, but a PDF's bytes decode to
  mojibake, so the scan was meaningless — moved to run on the *converted* text inside the
  pipeline. (3) **A missing extra would have read as failure**: without the filter, every PDF in a
  vault becomes a dead job and a base install reports a wall of errors for behaving as designed.
- **Design call worth remembering: per-page exports are concatenated, not parsed separately.**
  Parsing each page as its own document would break the heading tree (an H1 on page 1 stops
  scoping page 2) and the chunker's entire model is heading-path sections. Concatenating and
  recording each page's start line keeps the tree continuous and makes page-stamping a lookup.
- **Verified on a real 25-page PDF, not just the committed fixture:** 48 chunks all page-stamped;
  over real MCP stdio, `search` returned semantically correct hits and **pages 5/7/9 were
  independently re-derived** from Docling's own per-page export. My first version of that test
  *failed* — but the code was right and the test was wrong (a dict comprehension collapsing
  multiple blocks per page), which is the good direction for that failure to point.
- **Measured perf (don't re-panic about the first number):** the first conversion took **220 s**,
  which is almost entirely **one-time model download** — warm is **~0.6 s/page** trivial,
  **~2.7 s/page** real. `do_ocr=False` made **no** measurable difference on a born-digital PDF
  (1.7 vs 1.8 s, identical output), so no knob was added for it.
- **Files:** `parsing/pdf.py` (new), `models.py` (`Block.page`, `Snippet.page`),
  `chunking/chunker.py` (`_first_page`), `indexer/scan.py` (`_BINARY_SUFFIXES`,
  `is_binary_source`, no content-scan for binary, `_scanned_file` helper),
  `indexer/pipeline.py` (`_parse_source`, `SecretBlocked`, `SkipReason.no_parser`,
  `ReindexStats.skipped`, `_drop_unparseable`), `retrieval/{pipeline,parents}.py` (carry `page`),
  `mcp/server.py` (`open` refuses PDFs), `cli.py`, `pyproject.toml`. Tests: `tests/test_pdf.py`
  (new, 15) + `tests/fixtures/handbook.pdf` (18 KB, 3 pages) + 1 in `test_mcp.py`.
  **Vault:** `p2-step-plan.md` Log + Decision 3 marked settled.
- **Next / handoff:** **Stage 5 — Excalidraw + Mermaid** (Mermaid `A --> B` → `diagram_edge` in
  Stage 2's `links` table; add `diagram` to `LinkKind`). Things to know: (a) **CI does not install
  the `pdf` extra**, so the 3 Docling tests skip there — run `uv sync --extra pdf` locally before
  claiming the PDF path is green; (b) `open` refusing PDFs is a **known limitation**, and the fix
  (storing extracted text, or a `page=` arg that re-converts) is a real design question, not a
  bug to patch casually; (c) Stage 5 adds `.excalidraw`, which is **text** (JSON) — so it belongs
  in `_TEXT_SUFFIXES`, not `_BINARY_SUFFIXES`, and needs no new secret-guard placement.

### 2026-07-21 — index-drift bug fixed: `reindex --force` + a live-eval refusal + a warning
- **Did:** Anik asked whether Stage 4 was safe to start or whether anything needed resolving
  first. Verdict: Stage 4 was unblocked, but the *filed-not-fixed* drift bug was worth an hour
  **before** it, because every Stage 4 acceptance criterion is live-index dogfooding ("a real
  multi-page PDF indexes end-to-end; chunks carry the correct `page`") and Stage 4 is the first
  stage to add a **new file type** — on a drifted index, "this PDF never reached Qdrant" and
  "the Docling adapter skipped it" are indistinguishable, so you'd debug the wrong layer.
  Anik: *"do it"*. Built options **1 + 3** from the agent-inbox note, plus the cheapest useful
  slice of option 2.
- **`--force` lives in the classifier, not in a second code path.** `diff_manifest(force=True)`
  sends hash-equal files to `updates` instead of `unchanged`; everything downstream is the
  ordinary reconcile. This is the part worth remembering: a force implemented as "skip the diff
  and re-upsert everything" would **strand documents deleted from disk**, turning a repair into
  a different kind of corruption. Deletes and renames still derive normally (pinned by a test).
- **The eval harness now refuses live drift.** `live_stack` raises `DriftError` naming both
  counts + the repair command. It subclasses `ValueError` on purpose, so the eval CLI's existing
  "can't run" handler maps it to **exit 2** with no CLI change — a refusal to measure, never a
  quality number. Exactly the treatment `replay_stack` already gave the hermetic version of this
  failure (`stats.failed`); live had the same hole and no guard.
- **Added the detection half too (option 2, cheap form).** Not a `doctor` command — that still
  wants a `serve` role — but every `reindex` ends with the one-query check and prints
  `warning: index drift … — re-run with --force`. Option 1's stated weakness was that it only
  helps someone who *already suspects* a problem; this closes that.
- **Verified live, and reproduced the bug on the way — better than planned.** Deleted one note's
  points from the running Qdrant, then: `eval --live` **exit 2** (refused, named 173 vs 171);
  plain `reindex` did **real work** (`+1 ~2 =55`, 35 chunks — the vault had genuinely changed)
  **and remained drifted at 179 vs 177**, i.e. it will never converge no matter how often you
  run it; the new warning fired; `--force` → `~58`, 179 chunks, **1.7 s**, manifest = Qdrant =
  179, warning gone. **Force is cheap because it re-upserts without re-embedding** — the
  `(model,dims,chunk_hash)` cache absorbs every unchanged chunk, so the *full* forced rebuild
  (1.7 s) beat the *partial* plain run that preceded it (8.7 s). There is a test for that too,
  because it is the property that makes the DR path usable on a big vault.
- **Files:** `src/vaultrag/consistency.py` (new — `Drift`/`DriftError`/`check_drift`),
  `store/base.py` + `store/sqlite.py` (`chunk_count`), `indexer/scan.py` (`force` on
  `diff_manifest`/`scan_vault`), `indexer/pipeline.py` (`force` on `reindex_vault`), `cli.py`
  (`--force` + the warning), `eval/harness.py` (`live_stack` guard). Tests:
  `tests/test_consistency.py` (new, 13) + 1 in `test_cli.py`. **Vault:** resolution appended to
  `agent-inbox/reindex-cannot-repair-a-drifted-vector-store.md`, status draft → resolved.
  **494 tests** (+14), ruff, ruff format, mypy (52). **Hermetic gate unmoved: 1.000 / 0.892 /
  0.920** (this touches no retrieval stage).
- **Next / handoff:** **Stage 4 — PDF via Docling** (decision 3, the `vaultrag[pdf]` optional
  extra, still open). Things to know: (a) **the MCP server still does not check drift at
  startup** — a long-running `vaultrag mcp` can serve a drifted index silently; that is the
  remaining exposure and the argument for option 2's full form once `serve` exists; (b) the
  **design-vault dogfood number moved 0.749 → 0.739 for a non-code reason** — `../Rag` gained a
  note and changed two during this session (173 → 179 chunks), so the 58-case set is scoring a
  different corpus. This is precisely the drift Stage 1 predicted when it chose a frozen fixture
  for the gate; **don't read it as a regression, and don't compare design-vault numbers across
  sessions**; (c) `state/vaultrag.db` **is still tracked in git** and still needs
  `git rm --cached state/vaultrag.db` from Anik — `.gitignore` is fixed but does not apply to
  already-tracked files, and it keeps growing every session.

### 2026-07-21 — P2 Stage 3: `related` tool + graph-expand (retrieval stage 6)
- **Did:** Anik: *"impliment the stage 3 and test it properly … push it"* → Stage 3 of
  `../Rag/projects/vaultrag/p2-step-plan.md`. Stage 2 made the wikilink graph *data*; Stage 3 is
  its first consumer. New `retrieval/graph.py` (narrow `GraphStore` protocol + `authority_boost`
  + `neighbor_notes` + `related_notes`), retrieval **stage 6** wired between parent-swap and
  cite, new **`related(path, hops?, limit?)`** MCP tool, `RelatedNote`/`RelatedResult` models,
  `SearchResult.related`, 4 config knobs, CLI `--graph/--no-graph` for the A/B, and the eval
  harness now carries a graph store so the stage lands *through* the gate rather than around it.
- **The design decision that mattered: scale-free boosting.** Scores arriving at stage 6 are RRF
  (~0.016 and down), a cross-encoder sigmoid in `[0,1]`, or a constant 1.0 — an absolute bonus
  would be a no-op on one and a wrecking ball on another. Expressing the boost as a fraction of
  the result set's own `max - min` gives one meaning **and** the property the stage is safe by:
  a chunk can only overtake another it was already within `boost x spread` of.
- **Shipped ON, measured not assumed.** Gate 1.000 / 0.892 / **0.920** PASS (nDCG +0.001); live
  design vault **MRR@5 0.710 → 0.749**. Boost sweep on both sets is in the Phase bullet — the
  headline is that **1.00 costs -0.151 MRR@5 on the corpus**, which is what the 0.25 default buys
  distance from, and that the corpus is neutral while the real vault gains (same understood
  disagreement as rerank: the corpus is 27 notes / 57 links, one correct answer per query).
- **Two things I deliberately did NOT do.** (1) Pointers are **not** appended to `snippets` —
  they'd be indistinguishable from retrieved results and would silently inflate recall@10, the
  exact silent-green shape this repo has been bitten by twice. They live in `SearchResult.related`,
  outside the budget packer. (2) The graph store is a **reader**: it attaches to an existing state
  DB and never creates one, so a read-only server with no index still comes up clean and simply
  doesn't advertise `related` (two CLI tests pin both directions).
- **The bug only the live vault could show.** On fixtures `related` looked great. Against
  `../Rag` it returned **49 entries: 1 link, 2 backlinks, 46 tag siblings** — `tags` is
  prefix-expanded, so one shared `project/…` namespace makes every note a weak sibling of every
  other (the caveat the Stage 2 handoff flagged, now with a number). The relation the tool is
  *named for* was buried under the weakest one. Added `graph_max_tag_siblings` (default 5, store
  already returns most-shared-first, counted on what is *added* so dedupe can't shrink the cap):
  the same call now answers with 3 links + the 5 strongest siblings.
- **Verified over real stdio, not just in-memory:** subprocess `vaultrag mcp --no-write` against
  the live `vaultrag-design` index — `related` registered, returned the neighbourhood, and
  **every returned path re-opened successfully through `open`** (a pointer that 404s is worse
  than no pointer); `hops=2` correctly promotes 14 notes from `tag_sibling` to the more specific
  `neighbor`; path traversal refused; a 5-hit search carried 9 pointers at `total_tokens` = the
  snippets alone. Index consistency checked first per the standing caveat (SQLite 224 = Qdrant
  224 — no drift, so the numbers mean something).
- **Files:** `retrieval/graph.py` (new), `retrieval/{pipeline,__init__}.py`, `models.py`
  (`RelatedNote`/`RelatedResult`/`SearchResult.related`), `config.py` (4 knobs), `mcp/server.py`
  (`related` + `_vault_relative`, which `_read_note` now shares so the two can't drift),
  `cli.py` (store opened for the graph, `--graph/--no-graph`), `eval/{harness,report}.py`.
  Tests: `tests/test_graph.py` (new, 38), +14 in `test_mcp.py`, +2 in `test_cli.py`.
  **Vault:** `p2-step-plan.md` Log. Green: **480 tests** (+51), ruff, ruff format, mypy (51).
- **Next / handoff:** **Stage 4 — PDF via Docling** (decision 3, the `vaultrag[pdf]` optional
  extra, still open). Things to know: (a) stage 6 reorders *within* the `k` earlier stages kept —
  it cannot resurrect a chunk rerank dropped, which is the honest scope of a post-hoc signal;
  (b) `graph_boost` **0.50 measured better on the design vault** (+0.062 vs +0.033) and is the
  obvious thing to raise if a future harder golden set agrees — the sweep is in `graph.py`'s
  docstring; (c) stage 6 touches no Qdrant filtering, so the in-memory-payload-index caveat from
  Stage 1 does not apply to it; (d) the `reindex`-cannot-repair-drift bug is **still filed, not
  fixed** — `related` reads SQLite while `search` reads Qdrant, so if the two ever disagree
  check manifest vs Qdrant counts before debugging either.

### 2026-07-21 — rerank made affordable: `top_n` 50→20 via a new `--ceiling`, + a silent index-drift bug
- **Did:** answered ask (a) from the previous handoff — make the rerank stage *affordable* before
  tuning its quality — and hit two bugs on the way, one of them serious.
- **The method matters more than the number.** Instead of sweeping `top_n` by running the model
  per config (~1 h for 3 points), note that **a cross-encoder can only promote a chunk it is
  shown**. So the useful depth is bounded by how deep the correct note already sits in *fusion*
  order — computable with **no model, in seconds**. Shipped as **`vaultrag eval --ceiling`**
  (`eval/ceiling.py`). Coverage saturates at fusion rank **5** (corpus fixture) and **14** (live
  design vault). Everything above that is pure cost. This killed the sweep I had running.
- **Deeper reranking is actively worse, not just slower.** `always`@50 MRR@5 **0.928** vs
  `always`@5 **0.939** vs **`auto`@5 0.946** (fusion 0.892). Past the useful depth there are no
  winnable cases left but plenty of distractors, and a distractor scored high enough displaces a
  correct chunk. **`auto` is now measured** — it was the open unknown, and it wins on both axes
  because it skips the known-item queries a cross-encoder can only damage. Default → **20**
  (clears the deeper vault with headroom; 15 would be fitted to a 58-case sample's exact max).
- **Bug 1 (fixed, committed by Anik as `ed0ef31`): `top_n` was not a pure cost knob.** `search`
  set `fetch_limit = top_n`, so `top_n < k` returned **fewer results than rerank-off**. The whole
  sweep would have measured truncation and blamed the cross-encoder. Now `max(top_n, k)` + an
  unreviewed tail in fusion order below the survivors, exempt from `threshold`.
- **Bug 2 (filed, NOT fixed) — the serious one: `reindex` cannot repair a drifted vector store.**
  `vaultrag-design` held **173 chunks in SQLite, 50 in Qdrant**; `reindex` said `=53 unchanged`
  because it checks the manifest, never the vector store. Retrieval kept working from a third of
  the vault with no warning — so **any quality number measured then was meaningless and nothing
  said so**. Directly contradicts CLAUDE.md §6 ("DR = reindex"). Repaired by hand → 173 = 173.
  See `../Rag/agent-inbox/reindex-cannot-repair-a-drifted-vector-store.md`.
- **Self-inflicted, worth remembering:** the `sqlite3` CLI defaults `foreign_keys` **off**. My
  first repair deleted `documents` without the pragma, orphaning 78 `tags` rows, and the next
  reindex failed **29 of 57 notes** on FK constraints. Redone cleanly.
- **Files:** new `eval/ceiling.py` + `tests/test_ceiling.py`; `eval/__init__.py`, `cli.py`
  (`--ceiling`), `config.py` + `retrieval/rerank.py` (`rerank_top_n` 50→20, with the measurement
  in the comment). Vault: new agent-inbox note + ask-1 resolution appended to
  `rerank-threshold-should-be-per-vault-not-global.md`.
- **Green:** 429 tests, ruff, ruff format, mypy (50 src). **Gate unmoved: 1.000/0.892/0.919.**
- **Next / handoff:** **Stage 3 (`related` + graph-expand)** is the next build step and is
  unblocked. Two carried-forward caveats: (1) the drift bug is *filed, not fixed* — Stage 3 reads
  the graph store, so if `related` ever looks inexplicably sparse, **check manifest vs Qdrant
  counts before debugging the query**; (2) **latency numbers from this session are contaminated**
  (the runs overlapped a reindex, the ceiling script and a pytest pass) — `always`@5 427 s /
  `auto`@5 245 s over 72 cases are *relative* signals only; re-measure on an idle box before
  quoting a speedup. Asks 2-4 (fp32-vs-int8 on ARM, smaller cross-encoder, batch/thread tuning)
  stay open but are less urgent now.

### 2026-07-21 — Anik's 3 rulings: `doc_keys` kept, no tag edges, rerank becomes a mode
- **Did:** Anik ruled on the two Stage-2 open questions **and** the long-open rerank default.
  **(1) `doc_keys` — KEEP + document** (*"It's derived data and rebuilt automatically, so it isn't
  introducing another authoritative source of truth"*). Written into
  `../Rag/reference/reference-database-schema.md` with the FK semantics + the resolution ladder,
  and into `knowledge-graph-integration.md`. **(2) Tag edges — don't duplicate**; ratified as
  built, and the reference schema's `kind` is now `wiki|embed|diagram` (was
  `wiki|embed|tag|diagram`). **(3) Rerank** — *"don't make it default yes. but give user to chose
  yes or not. while set the value AI will desice when the best result come with or without
  reranker."*
- **Rerank as built:** `rerank_enabled: bool` → **`rerank_mode: never | auto | always`**
  (`VAULTRAG_RERANK_MODE`). `auto` is the "AI decides" case: a **per-query** decision on the
  **top-1/top-2 RRF margin**, deliberately **not** an LLM call — it's arithmetic on scores fusion
  already computed, so it's $0, local, and deterministic enough for the gate to reproduce exactly.
  Mechanically it promotes the existing `dominant_ratio` fast path from an internal optimization
  to the user-facing policy, so mode and gating are **one** mechanism that can't disagree
  (`always` = ratio 0). Values are `never`/`always` **not** `on`/`off` because A/B overlays are
  YAML and **YAML 1.1 parses bare `on`/`off` as booleans** — `rerank_mode: on` would arrive as
  `True`. The retired `VAULTRAG_RERANK_ENABLED` now **raises** instead of being silently dropped
  (pydantic-settings ignores unknown vars; a silent no-op reads as "the stage is broken").
- **THE finding — rerank is latency-bound, not quality-bound.** Choosing the default forced the
  first real timing of stage 4. Measured on this box (Apple Silicon, onnxruntime CPU):
  **~500 ms per passage** — `model_int8` **502**, `model_quantized` **509**, i.e. **quantization
  is not the bottleneck** (fp16 failed to open; fp32 needs a 2.3 GB external-data sidecar, untested).
  At the shipped `rerank_top_n=50` that is **~25 s for one reranked query**. `search` is an
  interactive MCP tool an agent blocks on, so **default = `never`** — not because the +0.036 MRR@5
  isn't real, but because nobody waits 25 s for it. Also measured the `auto` gate's firing rate on
  the 72-case corpus (fusion scores only, no model needed — cheap to re-run per vault):
  top1/top2 ratio **min 1.000 / median 1.291 / max 2.804**; at the shipped ratio **1.5 it skips
  37.5%** of queries (1.1 → 70.8%, 2.0 → 25.0%). So `auto` is a real middle ground, not `always`
  with extra steps — but 25 s on the 62.5% it *does* rerank is still 25 s.
- **Corrected a mistake I made mid-session:** I first defaulted the product to `auto`, which made
  the **test suite** start downloading and running a cross-encoder (the run hung >40 min). Two
  fixes: default → `never`, and — the durable one — **anything that measures retrieval now pins
  its rerank mode explicitly instead of inheriting it.** `vaultrag eval` gained `--rerank`
  (default `never`; precedence `--rerank` > `--config` overlay > pinned-off) and the eval tests
  use a `gate_settings()` helper. A gate must state what it measures; a PR check must never
  acquire a 600 MB model and a 25 s/query stage by accident.
- **Files:** `config.py` (`RerankMode` enum lives here — `retrieval` can't be imported by the
  dependency-free leaf without a cycle; `rerank_mode` field; legacy-env validator),
  `retrieval/rerank.py` (`RerankParams.for_mode`, **`should_rerank`**, `rerank_params_for`,
  `load_reranker(mode=…)`), `retrieval/__init__.py`, `cli.py` (`--rerank`), `eval/harness.py`
  (`EvalReport.rerank_mode`, reconstructed from what actually ran, not read back from Settings),
  `eval/report.py`, `eval/overlay.py`. Tests: +8 in `test_rerank.py`, `gate_settings()` in
  `test_eval.py`. **Vault:** `reference-database-schema.md`, `knowledge-graph-integration.md`,
  `p2-step-plan.md`, `agent-inbox/rerank-threshold-should-be-per-vault-not-global.md` (the
  latency table + a reprioritized open-questions list). **420 tests** green, ruff, mypy (49),
  gate unchanged at 1.000 / 0.892 / 0.919.
- **Next / handoff:** **Stage 3 (`related` + graph-expand)** is unblocked — the rerank question
  that gated it is now settled. Before spending anything more on rerank *quality*, the open work
  is making the stage **affordable**, in this order (now in the agent-inbox note): (a) drop
  `rerank_top_n` 50 → 5-10, since cost is **linear** in it and this is untested; (b) test fp32
  `onnx/model.onnx` + sidecar — int8 often lacks optimized ARM kernels, so it may not be the
  fast one; (c) a smaller cross-encoder (bge-reranker-base / MiniLM) — the `Reranker` protocol
  already makes it a config change, but the model is pinned in
  `reference-technology-stack.md` so it needs a decision note; (d) profile batch/thread settings.
  Also still unmeasured: **`auto`'s actual quality** on either golden set — I have its firing
  rate and `always`'s quality (+0.036 MRR@5, previous session) but the `auto` eval run was killed
  at 40 min. Run it with a reduced `rerank_top_n` once (a) lands.

### 2026-07-21 — P2 Stage 2: link-graph persistence (`links` table + resolver)
- **Did:** Anik: *"impliment the next stage"* → Stage 2 of
  `../Rag/projects/vaultrag/p2-step-plan.md`. Wikilinks have been parsed since P0 and thrown
  away ever since; they now persist. **(1) Schema:** `links(id, project_id, src_doc, dst_doc
  NULL, dst_raw, dst_key, kind, anchor)` exactly as `reference-database-schema.md` specifies,
  **plus a `dst_key` column** (the normalized target — what re-resolution joins on) and **plus a
  new `doc_keys(project_id, doc_id, kind, key)` table**, a derived reverse index of the four
  names a doc answers to so resolving a target is one indexed lookup instead of four. FK actions
  carry the semantics: `src_doc` **CASCADE** (deleting a note removes the links it wrote),
  `dst_doc` **SET NULL** (links *pointing at* it dangle, as they do in Obsidian). **(2) Resolver**
  — new pure module `store/links.py`: `normalize_key` (casefold / whitespace-collapse / `.md`-strip
  / separator-normalize), `doc_keys_for`, `link_edges`, `resolve_key`. **(3) Store API:** 10
  methods on `MetaStore` incl. `backlinks`, `neighbors` (recursive CTE, both directions, `UNION`
  to terminate on cycles, `MIN(hops)` for shortest), `tag_siblings`, `unresolved_links`.
  **(4) Pipeline:** `_write_graph` after the document commits, + a **graph backfill** in
  `reindex_vault`. **(5)** `ReindexStats.graph_backfilled` + CLI surfacing.
- **Decision 4 settled — but the recommended ladder was wrong and got a tier.** The plan said
  resolve "by normalized title, then alias". That is **not Obsidian's semantics**: `[[Note]]`
  addresses the **file**, whereas `title` is frontmatter-else-first-heading-else-stem and often
  differs from the filename. A vault where `services/gateway-service.md` is titled "Gateway
  Service" must resolve `[[gateway-service]]`, and title-first misses it. Shipped ladder:
  **`path` → `basename` → `title` → `alias`**. Everything else in the recommendation held:
  ambiguity inside the winning tier → `dst_doc=NULL` + `dst_raw` kept (**never guess**), and a
  lower tier never breaks a higher tier's tie (an alias does not settle a title collision — both
  notes really are called that).
- **Re-resolution is broader than the plan's "re-resolve the NULL set", on purpose.** Two cases
  the NULL-only rule misses: **(a)** an *already-resolved* edge goes stale when a new note
  outranks the match it settled for (a real `Gateway.md` appearing must steal `[[Gateway]]` from
  a note merely *titled* Gateway); **(b)** a **delete** can *un*-break a tie — two notes titled
  "Foo" blocked every `[[Foo]]`, delete one and the survivor should win. So every edge whose
  target names the changed doc is re-derived, on both add and delete. Still bounded to that doc's
  names, never a full-graph sweep. Both cases have tests.
- **The backfill is the part that would have been silently forgotten.** Edges are written on
  ingest — but an existing vault's files still hash-match, so the diff calls them `unchanged` and
  **no ingest ever runs**. Stage 2 would have landed as an **empty graph on every already-indexed
  vault**, looking perfectly healthy while `related` found nothing — the same silent-green shape
  as the Stage 1 fixture bug. `reindex_vault` now re-parses docs with **zero `doc_keys` rows**
  (the reliable pre-Stage-2 marker — every ingested doc has at least a `path` key) and writes
  metadata + edges only: no chunking, no embedding, no Qdrant call.
- **Verify (measured on the real vault, not only fixtures):** the live `state/vaultrag.db` was
  genuinely pre-Stage-2 (82 docs, **`links` table did not exist**) → reindex backfilled **54 docs,
  `chunks_written` for those = 0** (nothing re-embedded); **147 edges, 133 resolved = 90.5%**;
  the **14 danglers are all correct** — the vault's own prose *documenting* wikilink syntax
  (`[[Note]]` ×6, `[[...]]`, `[[wikilinks]]`) plus one deliberately-unwritten `[[Future Note]]`;
  **0 cross-project edges** (SQL-verified over the whole table); second reindex = 95 ms no-op,
  `graph_backfilled` 0. Eval gate **unmoved** at recall@10 1.000 / MRR@5 0.892 / nDCG@10 0.919
  (Stage 2 is a data layer — it does not touch retrieval). Hub/orphan analytics come out for free
  and look sane (`evaluation-framework.md` = 8 backlinks, 8 neighbours at 1 hop, 41 at 2).
- **Found + fixed a real parsing bug:** `[[Note\|alias]]` — how Obsidian escapes a pipe **inside a
  markdown table** — left the `\` stranded on the target (`Note\`), so **every wikilink in a table
  failed to resolve**. Normalization now strips it. Pre-existing since P0, only visible once
  something actually consumed the links.
- **Files:** `src/vaultrag/store/links.py` (new), `store/base.py` (+`LinkRow`/`LinkedDoc`/
  `NeighborDoc`/`SiblingDoc` + 10 protocol methods), `store/sqlite.py` (+`links`/`doc_keys` DDL +
  impls + delete-time re-resolution), `store/__init__.py` (exports), `indexer/pipeline.py`
  (`_write_graph`, `_backfill_graph`, `ReindexStats.graph_backfilled`), `cli.py` (surface the
  backfill count). Tests: `tests/test_links.py` (new, 44) + 8 end-to-end in
  `test_indexer_pipeline.py`. **Vault:** `p2-step-plan.md` Log (Stage 2 done + decision 4).
  Green: **412 tests** (+53), ruff, ruff format, mypy (49).
- **Both open questions RULED by Anik, same session (2026-07-21):**
  **(a) `doc_keys` — KEEP + document.** *"It's derived data and rebuilt automatically, so it isn't
  introducing another authoritative source of truth."* Written into
  `../Rag/reference/reference-database-schema.md` (with the FK semantics and the resolution
  ladder) and into `knowledge-graph-integration.md`.
  **(b) Tag edges — do NOT duplicate into `links`.** Ratified as built. The reference schema's
  `kind` now reads `wiki|embed|diagram` (was `wiki|embed|tag|diagram`); siblings stay served from
  `tags`.
- **Next / handoff:** **Stage 3 — `related` MCP tool + graph-expand (retrieval stage 6)**, which
  is the first consumer of all of this; `backlinks`/`neighbors`/`tag_siblings`/`linked_docs` are
  already shaped for it (they return path + title, which is what `related` returns). Things to
  know: (a) the rerank-default question from the previous session is **still unruled and now
  matters more** — Stage 3 reorders results, so settle it before tuning a stage-6 boost;
  (b) `neighbors` walks **resolved edges only**, so a vault with many dangling links has a
  sparser graph than its author thinks — `unresolved_links` is the hygiene signal to surface via
  `overview`; (c) `tag_siblings` counts **prefix-expanded** tags (`a/b/c` → `a`, `a/b`, `a/b/c`),
  so a shared top-level namespace inflates the count — it is a ranking weight, not a literal
  shared-tag count; (d) the hermetic eval gate still **cannot** exercise a payload index (the
  in-memory Qdrant caveat from last session stands) — irrelevant to Stage 2, which is pure
  SQLite, but Stage 3's boost should get a server-Qdrant check if it touches filtering.
  Working tree: new `store/links.py` + `tests/test_links.py`, edits to `store/*`,
  `indexer/pipeline.py`, `cli.py`, `MEMORY.md`; vault plan note edited (separate repo). Commit
  when Anik asks.

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
