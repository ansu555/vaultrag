# Benchmarks

Every number on this page comes from a run that was actually executed. Where a figure is
an estimate or an upper bound rather than a measurement, it says so.

**Read the methodology before the numbers.** A retrieval benchmark without its corpus and
its determinism story is a vibe with decimal places.

---

## Reference hardware

| | |
|---|---|
| Machine | Apple M1, 8 GB unified memory, macOS 26.2 |
| Embeddings | Qwen3-Embedding-0.6B via host Ollama, **CPU** |
| Reranker | bge-reranker-v2-m3 ONNX int8 via onnxruntime, **CPU** |
| Vector store | Qdrant in Docker, int8 quantized, HNSW m=16 / ef_construct=128 |
| Metadata | SQLite WAL |

This is a deliberately modest box. A GPU-backed Ollama changes the ingest and rerank
numbers substantially; nothing else on this page depends on the hardware much.

---

## Methodology

### The two golden sets

| Set | Size | Corpus | Purpose |
|---|---|---|---|
| **`corpus`** (Meridian) | **72** query → note pairs | 27 synthetic notes about a fictional platform, **frozen** | The merge gate. |
| **`design`** | **56–58** pairs | The live VaultRAG design vault, **drifts on purpose** | Dogfooding. |

**Why a frozen fixture and not the real vault.** The design vault is edited constantly, so
a number computed from it today is not comparable to one computed next month — the bar
would move underneath us. The fixture is frozen, so a change in the number means a change
in **our code**.

The fixture is also written to be *harder* than the earlier design set: most queries are
paraphrases sharing no distinctive token with the expected note's title, several notes
cover neighbouring topics so ranking has to discriminate, and a few are bare identifiers
(`MER-1422`, `MERIDIAN_DEDUPE_TTL_SECONDS`) that exercise the BM25 half.

### Why the gate is hermetic — and what that costs

`vaultrag eval` reindexes the fixture into an **in-process Qdrant** using **frozen Qwen3
vectors** recorded once from real Ollama. No Docker, no Ollama, no network, deterministic
across machines.

- A gate that swapped in a fake embedder would test the plumbing while telling us nothing
  about whether **Qwen3** finds the right note.
- `ReplayEmbedder` **raises** on a cache miss rather than falling back to a live call or
  inventing a vector. A fabricated embedding would make CI green while measuring nothing.
- Everything *except* the dense vectors is real: today's scanner, parser, chunker and
  payload code run on every invocation. A chunking change that hurts retrieval shows up.

**The honest caveat:** because it is in-memory, the gate emits
`UserWarning: Payload indexes have no effect in the local Qdrant`. So the hermetic run
scores an index **without payload indexes**. Verified separately against a real server —
see [live-server verification](#live-server-verification) — where they materialize as
designed.

### The live path is not deterministic, and that is the point

Three consecutive live evals against an **unchanged** index:

| Run | MRR@5 |
|---|---|
| 1 | 0.885 |
| 2 | 0.892 |
| 3 | 0.871 |

**Spread 0.021**, from approximate HNSW. A gate wired to live Qdrant would flap by ±0.02
run to run. It would not flip today's gate (0.892 against a 0.75 threshold), but **live
numbers are not comparable run-to-run and must not be quoted as though they were.**

---

## Retrieval quality

### The gate — shipped configuration

Frozen corpus, 72 cases, rerank off (the shipped default), graph expand on.

| Metric | Threshold | **Measured** |
|---|---|---|
| recall@10 | ≥ 0.90 | **1.000** |
| MRR@5 | ≥ 0.75 | **0.892** |
| nDCG@10 | *reported, not gated* | **0.920** |

Exit code 0. This is what `.github/workflows/eval.yml` enforces on every PR.

**nDCG@10 is reported rather than gated** because there was no baseline to justify a
threshold when it was added, and a gate whose bar was guessed is a gate that will be
argued with rather than fixed.

**The gate was verified to bite.** An overlay setting `default_top_k: 1` drops recall@10 to
0.806 and exits **1** with the miss list. A gate nobody has watched fail is not known to
work.

### Rerank A/B

72-case corpus. Fusion baseline vs cross-encoder at varying depth.

| Config | MRR@5 | Δ vs fusion | recall@10 |
|---|---|---|---|
| fusion only *(shipped default)* | 0.892 | — | 1.000 |
| `always` @ `top_n=50` | 0.928 | +0.036 | 1.000 |
| `always` @ `top_n=5` | 0.939 | +0.047 | 1.000 |
| **`auto` @ `top_n=5`** | **0.946** | **+0.054** | 1.000 |

nDCG is unchanged by construction — it is computed pre-rerank.

**The cheapest config is also the best.** Past the useful depth there are no winnable cases
left but plenty of distractors, and a distractor scored high enough displaces a correct
chunk.

**`auto` skips 37.5 %** of corpus queries at the default `dominant_ratio=1.5`.

### The rerank ceiling — the model-free method

A cross-encoder can only promote a chunk it is **shown**, so the useful `top_n` is bounded
by how deep the correct note already sits in *fusion* order. `vaultrag eval --ceiling`
computes that with **no model, in seconds**.

| Golden set | Coverage saturates at fusion rank |
|---|---|
| Meridian corpus (72 cases) | **5** |
| Live design vault (58 cases) | **14** |

Default `rerank_top_n = 20` clears the deeper vault with headroom, without being fitted to
one 58-case sample's exact maximum. This analysis replaced a `top_n` sweep that would have
taken ~1 hour for three data points.

### Graph expand A/B — the `graph_boost` sweep

| `graph_boost` | Meridian corpus (72) | Live design vault (58) |
|---|---|---|
| 0.10 | MRR@5 0.892 (+0.000) | MRR@5 0.730 (+0.014) |
| **0.25 — default** | MRR@5 0.892 (+0.000) | MRR@5 **0.749 (+0.033)** |
| 0.50 | MRR@5 0.891 (−0.000) | MRR@5 0.778 (+0.062) |
| 1.00 | MRR@5 **0.741 (−0.151)** | MRR@5 0.719 (+0.003) |

Stage-6 on/off on the gate: recall@10 1.000 unchanged, MRR@5 0.892 unchanged, nDCG@10
0.919 → **0.920**. On the live design vault: MRR@5 0.710 → **0.749**, nDCG 0.779 → 0.806.
Expected note moves up in 8 cases, down in 3.

**Why the two sets disagree.** The corpus is 27 notes and 57 links, written
one-correct-answer-per-query, so in-result-set in-degree has little to work with. The
design vault has real hub structure. Same pattern as rerank, for the same kind of reason.

**0.50 measured better on the design vault**, but 0.25 is the conservative point that is
non-negative on *both* — and it is a per-vault knob with this table as the evidence for
raising it.

### Compression A/B

58 live design-vault queries, `budget_tokens=2000`, parents + graph on.

| | Before | After |
|---|---|---|
| Distinct documents per response | 5.26 | 5.34 |
| Response tokens | ~1 724 | ~1 729 |
| Gate | 1.000 / 0.892 / 0.920 | **unmoved** |

**Near-neutral, and that is the correct result.** Parent-swap already removes same-section
redundancy, and the design vault has zero near-duplicate pairs, so there is little to
compress. Read it as cheap insurance whose value appears on redundant, large-`k` or messy
inputs — proven by unit tests, with the gate confirming zero harm.

**int8 vs full precision**, measured on the frozen fixture while deciding the similarity
metric:

| | Value |
|---|---|
| Max Δ cosine, int8 vs float32 | **0.0013** |
| p95 Δ | 0.0007 |
| Threshold decisions flipped at 0.92 | **0 of 3 968 pairs** |

Quantization was not the blocker it was assumed to be.

---

## Latency

### Per-stage

| Stage | p50 | p95 | Notes |
|---|---|---|---|
| `graph` | **0.39 ms** | **0.66 ms** | 58 live queries. Pure SQLite — ~10× under the 5 ms design budget. |
| `compress` | **1.5 ms** | **2.7 ms** | 58 live queries. Lexical Jaccard, no network. |
| `rerank` | **0 ms** (default) | — | Off by default. |
| `rerank` when on | **~500 ms per passage** | — | `model_int8` 502 ms, `model_quantized` 509 ms. |

**Quantization is not the reranker's bottleneck** — int8 and the generic quantized export
are within 1.5 % of each other. The passage count is the whole ballgame, which is why
`rerank_top_n` dropped from 50 to 20.

At `top_n=20`: **~10 s** of blocking time per reranked query. At `top_n=50`: ~25 s.

### Histogram buckets

Stage latencies span five orders of magnitude — graph expand at 0.39 ms, a fully reranked
query at ~25 s. `prometheus_client`'s default buckets (5 ms – 10 s) yield **no p50 and no
p95 at either end**: a panel that exists and answers nothing.

So the buckets are an explicit decade ladder, **0.1 ms → 60 s**, with a test pinning the
range.

---

## Ingest throughput

| Scenario | Result |
|---|---|
| **Cold index**, CPU-only, host Ollama | 67 documents → 261 chunks in **326 s** (≈4.9 s/doc) |
| **Warm reindex**, no changes | **95–139 ms** (full manifest scan, zero ingests) |
| Live design vault reindex | 61 notes → 213 chunks |

The cold number is dominated entirely by CPU embedding. The warm number is the embed cache
and the manifest doing their jobs — an unchanged file is never parsed, chunked, embedded or
upserted.

---

## Memory footprint

Measured at **100 260 Qdrant points** in one collection.

| Component | RSS |
|---|---|
| `vaultrag-api` container | **144 MiB** |
| `vaultrag-indexer` container | **130 MiB** |
| `vaultrag-qdrant` container | **453 MiB** |
| **Containers total** | **727 MiB** |
| Host Ollama (embedding model resident) | **+1 070 MiB** |
| **System total, shipped default** | **≈1.8 GiB** |
| *Reranker, if enabled* | **+1 068 MiB** |

Three things worth taking away:

1. **The reranker is the largest single component** — larger than every container
   combined — and it is off by default. `rerank_mode` is a *memory* decision as much as a
   latency one.
2. **Qdrant's RSS falls ~280 MiB once indexing settles** (dense vectors are `on_disk` with
   int8 `always_ram`). **Size the box against the ingest peak, not idle.**
3. **The design vault's "3–4 GB at 100k chunks" figure holds as an upper bound and
   overstates the shipped default by ~2×.**

**Not measured, stated plainly:** the 100k points were synthetic, and SQLite was never
exercised past 67 real documents. Treat the metadata store's behaviour at scale as
unverified.

**Image size:** `vaultrag:latest` is **757 MB**. The `formats` tag adds ≈2.5 GB (Docling +
torch + OCR).

---

## Live-server verification

The hermetic gate runs against an in-memory Qdrant. This run re-scored it against the real
server, because the two differ in ways that matter.

**(1) Payload indexes are real on a server.** In-memory Qdrant emits
`UserWarning: Payload indexes have no effect in the local Qdrant` — so *every gate run to
date has scored an index with no payload indexes at all*. On the server they materialize as
designed: `project_id` as a keyword index with **`is_tenant: true`**, plus `path`, `tags`,
`type`, `modified_ts`; dense int8 scalar quantization; sparse `bm25` with `idf`.

**(2) Live numbers, same corpus:**

| Metric | Hermetic | Live server |
|---|---|---|
| recall@10 | 1.000 | **1.000** |
| MRR@5 | 0.892 | **0.878 – 0.892** |
| nDCG@10 | 0.919 | **0.909 – 0.919** |

A hair under hermetic, which is the expected cost of int8 quantization plus approximate
HNSW versus local exact search. Gate passes either way.

**(3) Tenancy, verified adversarially.** A decoy project was indexed into the **same
collection** holding byte-identical note content under `DECOY-` prefixed paths — so every
decoy chunk is a perfect-duplicate competitor for its corpus twin (102 points, identical
vectors).

- Re-running the 72-case eval: **metrics identical to the digit**, zero decoy paths in any
  ranked list.
- A direct 12-query probe: 98 results per tenant, **zero cross-contamination in both
  directions**, and neither tenant over-filtered to empty.
- Repeated at **100 000** foreign-tenant points: a `dr-vault` search returned only
  `dr-vault` documents.

---

## Disaster-recovery drill

Run for real, not asserted: fresh volumes → `git clone` the vault → `docker compose up` →
`reindex` → search.

| Step | Result |
|---|---|
| Cold rebuild from bare Markdown | 67 docs → 261 chunks, **326 s** |
| Search after rebuild | Correct results, **cited** with `obsidian://` URIs and line ranges |
| Write-back through the container | Worked: rw mount, `git` in the image, auto-commit `committed: true`, synchronous reindex |

**"DR = reindex" is proven end to end**, not merely designed for.

---

## Observability verification

A dashboard panel querying a renamed metric renders an empty graph forever and looks
exactly like a healthy system. "Open Grafana and look" was never a defence, so the check is
mechanical:

- `tests/test_deploy.py` (21 tests) resolves **every `vaultrag_*` name and every label** in
  the dashboard JSON and the alert rules against the **live metric registry**, and the
  scrape config against the compose services and `Settings.metrics_port`.
- A live script then ran **every panel expression against the running Prometheus**:
  **16/16 returned data; 5/5 rules loaded and evaluated.**
- **One alert was fired deliberately.** Stopping the indexer trips `VaultRAGRoleDown` →
  Prometheus `firing` → Alertmanager `active` → routed to the webhook receiver; it cleared
  when the indexer came back.

That last one corrected a design assumption: the plan expected *ingest lag* to detect a
dead indexer. It cannot — an indexer that is **gone** stops producing lag observations
entirely. Scrape liveness is the right detector.

---

## Test and gate posture

| | |
|---|---|
| Tests | **923** |
| Type checking | `mypy --strict` over 68 source files |
| Lint + format | `ruff check` **and** `ruff format --check` |
| CI jobs | `ruff + mypy` · `pytest` · `pytest (pdf + ocr extras)` · `docker build` · `retrieval quality gate` |

**Mutation-tested, not merely written.** Representative results from recent stages —
8 mutations run in one stage, 8 killed, 0 survivors; 6 in another, 6 killed, 0 survivors:

| Mutation | Tests killed |
|---|---|
| Typo a metric name in the dashboard | 2 |
| Typo a legend label | 1 |
| Move the ingest-lag threshold off a bucket edge | 2 |
| Publish a port on all interfaces | 1 |
| Drop the unconfigured-`serve` refusal | 1 |
| Drop the metrics-exporter start | 1 |
| Accept any key secret (neuter `verify_secret`) | 2 |
| Neuter the rate-limit branch | 3 |
| Drop `filters` from the REST search call | 1 |
| Drop `heading` from the REST open call | 2 |
| Remove auth from the router | 12 |

---

## Known gaps

Stated because a benchmark page that only lists strengths is marketing.

| Gap | Status |
|---|---|
| SQLite at scale | Never exercised past 67 real documents. The 100k-point measurement used synthetic Qdrant points only. |
| Concurrency under load | No sustained multi-client load test has been run. |
| GPU numbers | Everything here is CPU. GPU ingest and rerank figures are unmeasured. |
| Postgres path | The DDL is written to migrate; the migration has not been executed. |
| **Long cross-cutting documents outrank focused notes** | Dogfooded across 8 subsystem queries on the live design vault: a `*-step-plan.md` ranked #1 on **3 of 8**, while the subsystem's own design note ranked #1 on only **1 of 8**. **The Meridian gate corpus has no long cross-cutting documents, so this failure class is invisible to the gate by construction.** A `--no-graph` A/B is the cheapest next probe. |

That last row is the most important line on this page. The gate is green and there is a
known retrieval failure mode it cannot see. Golden sets measure what they contain.

---

## Reproducing these

```bash
uv run vaultrag eval                                  # the gate — hermetic, ~10 s
uv run vaultrag eval --show-cases                     # + per-case ranks
uv run vaultrag eval --rerank always                  # the rerank A/B
uv run vaultrag eval --no-graph                       # the stage-6 A/B
uv run vaultrag eval --ceiling                        # the model-free depth analysis
uv run vaultrag eval --golden design                  # live dogfooding (non-deterministic)
uv run vaultrag eval --config a.yml --config b.yml    # arbitrary A/B, deltas vs the first
```

Overlay files are YAML or JSON applied on top of the environment. To sweep one knob:

```yaml
# boost-050.yml
graph_boost: 0.50
```

```bash
uv run vaultrag eval --config baseline.yml --config boost-050.yml
```

> Values in overlays are YAML. `rerank_mode` uses `never`/`auto`/`always` rather than
> `on`/`off` deliberately — YAML 1.1 parses bare `on`/`off` as **booleans**, so
> `rerank_mode: on` would arrive as `True` and fail validation in the one workflow the mode
> exists to support.

---

## Related reading

- [Retrieval pipeline](retrieval.md) — what each measured stage does
- [Design decisions](design-decisions.md#decisions-that-changed-under-measurement) — the plans these numbers overturned
- [Development](development.md#the-eval-gate) — how to add golden cases and re-record vectors
