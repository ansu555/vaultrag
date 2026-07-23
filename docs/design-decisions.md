# Design decisions — techniques and why

Every significant technique choice, the alternative it beat, and the evidence behind it.

The canonical decision records live in the design vault at `../Rag/decisions/`. This page
is the readable summary plus the *measured* outcomes, which the vault notes predate.

---

## At a glance

| Layer | Choice | Beat | One-line reason |
|---|---|---|---|
| Source of truth | **The Obsidian vault (a git repo)** | A database of record | DR is `reindex`. Every index is disposable. |
| Vector + keyword | **Qdrant**, one collection | Weaviate, pgvector, Elasticsearch | Dense + sparse + payload filtering in one engine, one round-trip. |
| Tenancy | **Payload partitioning** | Collection-per-project | Per-tenant collection overhead is what kills you at scale. |
| Sparse retrieval | **BM25 sparse vectors via FastEmbed** | Elasticsearch / OpenSearch | Zero extra infrastructure; fuses inside the same query. |
| Fusion | **RRF** | Weighted score sum | Rank-based, so scale-free and tuning-free. |
| Dense embeddings | **Qwen3-Embedding-0.6B** (Apache-2.0) | OpenAI/Voyage APIs, bge-m3 | Local, free, 1024d, Matryoshka-truncatable. |
| Reranker | **bge-reranker-v2-m3**, ONNX int8 | Cohere Rerank API | Biggest accuracy lever, and it runs on the box. |
| Chunking | **Heading tree** | Fixed-size sliding windows | Documents already have structure; windows destroy it. |
| Parent context | **Reconstructed live from the vault** | Stored parent chunks | Keeps the index a pure derived cache. |
| Graph | **Obsidian's wikilinks as a `links` table** | Full GraphRAG (LLM entity extraction) | The author already drew the graph. Zero LLM calls. |
| Metadata store | **SQLite (WAL) → Postgres** | Postgres from day one | One file, no daemon, and the same DDL migrates. |
| Jobs | **In-process asyncio + SQLite `jobs`** | Redis + Celery | A broker is a second system to operate for no durability gain. |
| Compression | **Deterministic** (dedupe → MMR → pack → trim) | LLM summarization | Free, ~0 ms, no information invented or lost. |
| Response | **Cited snippets** | Synthesized answers | The caller is already an LLM. |
| Primary interface | **MCP** | REST-first | The consumers are agents. |
| Framework | **None** | LangChain / LlamaIndex | The abstractions cost more than they save at this size. |
| Deployment | **Docker Compose, one box** | Kubernetes | ~1.8 GB at 100k chunks. |

---

## Foundations

### The vault is the source of truth

> `../Rag/decisions/vault-as-source-of-truth.md`

The Obsidian vault — a git repository of Markdown — is the single source of truth. Every
index, vector and graph edge in Qdrant and SQLite is a **derived cache** that can be thrown
away and rebuilt.

**What this buys.**

- **Disaster recovery is one command.** Delete both volumes, `docker compose up -d`,
  `vaultrag reindex`. Verified end to end in a real drill: fresh volumes → `git clone` the
  vault → compose up → reindex → search returned correct cited results, and write-back
  worked through the container.
- **Backups are already solved.** The vault's git remote *is* the backup. Qdrant snapshots
  and a SQLite copy are a restore-*speed* optimization, not a backup.
- **No lock-in.** Your knowledge is Markdown files in a folder. Uninstall VaultRAG and
  nothing is lost.

**What it constrains.** No fact may live only in the index. This is why parent sections are
reconstructed from the vault at query time rather than stored, and why write-back writes to
the *vault* first and indexes second.

**The one exception**, stated honestly: `query_log` is the only table nothing can rebuild.
If you back up one thing beyond the vault, back that up.

### Local-first, $0 software

Qdrant, Ollama, SQLite, ONNX runtime. No paid API is required to run anything.

Paid backends stay possible because `Embedder`, `Reranker`, `VectorStore` and `MetaStore`
are **protocols**. Swapping in Voyage embeddings or Cohere Rerank is a config change and an
implementation class — never a change to the pipeline.

This is also why the `pdf` extra is an extra: Docling pulls ~79 packages including torch.
Without it, PDFs are skipped cleanly and the rest of the vault still indexes. A heavy
optional capability must not tax the default install.

### MCP first, not REST first

> `../Rag/decisions/mcp-first-not-rest-first.md`

The consumers are coding agents. MCP is how they discover and call tools, so it is the
primary interface; REST, CLI and SDKs are wrappers over the same service core.

The mechanism matters more than the ordering: **one `VaultRAGService` object** holds every
operation, and both transports are given the same instance. A REST/MCP divergence is
therefore impossible by construction rather than merely tested for.

That was learned the hard way. Before the refactor, parent-resolver wiring, compact mode,
the query-log policy, vault containment and `overview`'s caps all lived inside the FastMCP
tool closures — so "REST is a thin wrapper" was true of retrieval and false of everything
else. And the first parity test written for it was **vacuous**: comparing responses alone
passed with `filters=None` hard-coded into a route, because the fake vector store returned
preset hits regardless of filter. It now asserts on the *kwargs* each transport hands the
engine.

### Snippets, not answers

> `../Rag/decisions/snippets-not-answers-by-default.md`

`search` returns cited chunk text. It does not synthesize.

The caller is Claude, or another frontier model. Running a small local LLM to summarize
context *for* a large one burns compute to **lose** information and to insert a second
place hallucinations can enter. An `answer` mode exists in the design as opt-in and is
deliberately unbuilt.

It also matters for safety: retrieved vault text is **untrusted data**. In `snippets` mode
VaultRAG never executes or interprets it. See [security](security.md#prompt-injection).

---

## Storage and retrieval

### Qdrant for both vector and keyword search

> `../Rag/decisions/vector-search-qdrant.md`,
> `../Rag/decisions/sparse-bm25-not-elasticsearch.md`

One engine serves dense vectors, BM25 sparse vectors, and payload filtering — and fuses the
two retrievers **server-side in one round-trip**.

**Rejected: Elasticsearch/OpenSearch for BM25.** A second stateful service to run, back up
and keep in sync, plus a client-side fusion step, to replace something Qdrant does natively
via FastEmbed-produced sparse vectors. On a single-box local-first system that trade is
clearly wrong.

**Rejected: pgvector.** Would consolidate onto one datastore, but gives up Qdrant's
quantization, HNSW tuning, tenant payload indexing and native sparse-vector support — the
four things doing the actual work here.

### Payload partitioning, not collection-per-project

> `../Rag/decisions/payload-partitioning-not-collection-per-project.md`

One collection, `project_id` as a keyword payload index with `is_tenant: true`.

Collection-per-project seems cleaner and isn't: each collection carries its own HNSW graph,
segment set and memory overhead, so a hundred small vaults cost dramatically more than one
collection holding the same points. The tenant index co-locates a project's points on disk,
which is what keeps per-tenant queries fast at thousands of projects.

**The isolation was verified adversarially**, because "it's just a filter" is exactly the
kind of claim that should not be taken on faith:

- A decoy project was indexed into the same collection holding **byte-identical note
  content** under `DECOY-` prefixed paths — every decoy chunk a perfect-duplicate
  competitor for its twin. Re-running the 72-case eval gave metrics **identical to the
  digit**, with zero decoy paths in any ranked list. A direct 12-query probe showed zero
  cross-contamination **in both directions**, and neither tenant over-filtered to empty.
- Tenancy held again at **100 000** foreign-tenant points in the same collection.

### Hybrid always, fused with RRF

Dev notes are full of things a dense embedder cannot represent: `ERR_CONN_RESET`,
`MERIDIAN_DEDUPE_TTL_SECONDS`, `MER-1422`, `docker-compose.prod.yml`. BM25 finds those
verbatim; the bi-encoder finds paraphrases. Either alone has a whole class of query it
reliably misses.

**Why RRF over a weighted score sum.** BM25 scores and cosine similarities are not
commensurable, and their scales drift with corpus and query. Reciprocal Rank Fusion uses
only rank — `Σ 1/(60 + rank)` — so it is scale-free and needs no weight tuning.

That property pays a second dividend: because a chunk ranked #1 by *both* retrievers scores
roughly twice one that only one surfaced, the **top-1/top-2 RRF margin is a real statement
about retriever agreement**. That is exactly what the reranker's `auto` mode gates on — no
LLM, no query classifier, just arithmetic on scores fusion already computed.

### Qwen3-Embedding-0.6B

> `../Rag/decisions/dense-embeddings-qwen3.md`

Apache-2.0, 1024 dimensions, Matryoshka-truncatable to 512, runs on CPU via Ollama and on
a GPU if one is present.

Changing the model or its dimensionality invalidates every stored vector — which is why
`embed_dims` is deliberately **not** per-project overridable. The Qdrant collection is
shared, so its vector dimensionality is a deployment-wide property.

### bge-reranker-v2-m3, off by default

> `../Rag/decisions/reranker-bge-v2-m3.md`

The reranker is the single biggest accuracy lever available, and it ships **off**. That
looks contradictory until you read the numbers, so here they are.

**Quality — it genuinely helps, on the right query distribution:**

| Config | MRR@5 (72-case corpus) |
|---|---|
| fusion only | 0.892 |
| `always` @ top_n=50 | 0.928 |
| `always` @ top_n=5 | 0.939 |
| `auto` @ top_n=5 | **0.946** |

**Latency — it is expensive:** ~500 ms **per passage** on Apple Silicon CPU (int8 502 ms,
quantized 509 ms — quantization is not the bottleneck). At the default `top_n=20` that is
~10 s of blocking time on an interactive tool.

**Memory — it is the largest single component:** +1 068 MiB resident, more than the entire
rest of the system.

So `rerank_mode` is a mode, not a boolean, and the default is `never`. On hardware that can
afford it, `auto` with a small `top_n` is both the cheapest and the best-scoring config.

**Two golden sets disagree, and both are right.** On the title-matchy P1 design set, fusion
already ranks near-perfectly and the cross-encoder can only shuffle a correct #1 down
(1.00/0.775 fusion vs 0.95/0.743 reranked). On the deliberately-paraphrased P2 corpus, the
cross-encoder is what closes the semantic gap. A single global boolean *has* to be wrong on
one of them.

**Deeper is worse, not just slower** — and the method that found this generalizes. Instead
of sweeping `top_n` by running the model per config (~1 h for 3 points), note that a
cross-encoder can only promote a chunk it is **shown**. So the useful depth is bounded by
how deep the correct note already sits in *fusion* order — computable with no model, in
seconds. That is `vaultrag eval --ceiling`. Coverage saturates at fusion rank 5 on the
corpus and 14 on the live design vault; everything beyond is pure cost, and distractors
scored high enough displace correct chunks.

### Heading-tree chunking

> `../Rag/decisions/chunking-heading-tree-not-fixed-size.md`

A child chunk is the contiguous run of non-heading blocks under the lowest heading in
effect: ~300-token target, 512 hard max, sub-120-token siblings merged.

Fixed-size sliding windows throw away structure the author already provided. They split
code fences mid-function, separate a table from its header, and produce chunks whose
`heading_path` is a lie. Heading-tree chunking gives every chunk an honest citation
(`path ▸ H1 ▸ H2 ▸ lines`) for free, which is what makes `open(path, heading=anchor)` work
as a follow-up.

**Atomic blocks are never split.** An oversized code fence becomes its own oversized chunk
rather than being cut — agents need exact identifiers.

### Parent-child without storing parents

Children are the only indexed vectors: small chunks rank precisely. But a lone child is
often too little context to answer from, so retrieval swaps it for its enclosing H1/H2
section.

That section is **reconstructed live from the current file**, using the same machinery the
`open` tool uses. The chunker stores only a `<path>#<anchor>` pointer.

Storing parent text would have been simpler and would have violated principle #1 — the
index would then hold a copy of vault content that could go stale independently. It would
also roughly double the storage for no ranking benefit, since parents are never embedded.

### The graph is Obsidian's own wikilinks

> `../Rag/decisions/no-full-graphrag.md`

**Rejected: full GraphRAG** — LLM entity and community extraction over the corpus. It is
expensive (an LLM pass over every document), slow to update, and lossy in a way that is
hard to audit.

The vault author has already drawn the graph, by hand, with intent: `[[wikilinks]]`. Parse
time captures them into a `links` table, and recursive CTEs walk it. Zero LLM calls, zero
extra infrastructure, sub-millisecond queries.

Two consumers:

1. **Authority boost** (retrieval stage 6) — a chunk whose document is linked *from other
   documents in the same result set* gets a small lift. Scored in-result-set only, so it is
   query-conditional rather than a global PageRank; otherwise every map-of-content note
   would dominate every search it grazes.
2. **The `related` tool** — links, backlinks, 2-hop neighbours, tag siblings.

**The boost is a fraction of the result set's score spread, not an absolute bonus.** That
gives it one scale-free meaning across RRF scores (~0.016), cross-encoder sigmoids (0–1)
and the no-op reranker's constant 1.0 — and buys a safety property: a chunk can only
overtake another if it was already within `boost × spread` of it. Authority settles a
photo-finish; it never overturns a decisive win. At `boost=1.00`, past that bound, the
corpus loses 0.151 MRR@5 — which is the measurement the 0.25 default is buying distance
from.

### Deterministic compression, not LLM summarization

> `../Rag/decisions/deterministic-compression-not-llm.md`

Near-dupe drop → MMR (λ=0.7) → per-doc-capped greedy pack → trim. Free, ~0 ms, no network,
fully reproducible — which also means the eval gate can score it exactly.

LLM summarization would cost a model call per query to produce a *lossy paraphrase* of text
the caller was going to read anyway. LLMLingua-2 token-pruning stays an unbuilt opt-in
rather than a knob shipped dark.

**Similarity is lexical (token-set Jaccard), not embedding cosine** — and the reason is not
the one the design predicted. The design worried about int8 imprecision; measurement killed
that worry (int8 cosine tracks full precision to max Δ 0.0013 and flips the 0.92 decision on
0 of 3 968 pairs). The real blocker is **parent-swap**: it is on by default and emits
snippets with no stored vector, and MMR needs one consistent metric across all candidates —
so the "vector fallback" would have been the common path, mixing two incommensurable scales.

### SQLite now, Postgres later

> `../Rag/decisions/metadata-store-sqlite-to-postgres.md`

WAL mode, one file, no daemon, and the same DDL is written to move to Postgres 16 unchanged
behind the `MetaStore` protocol.

The migration trigger is already visible and named: both container roles open the same
SQLite file on a shared volume. That works at this scale — WAL, a 5 s busy timeout, one
writer and one mostly-reader — and it is precisely the last stop before Postgres, not a
steady state to grow into.

### In-process jobs, not Redis/Celery

> `../Rag/decisions/in-process-jobs-not-redis-celery.md`

asyncio plus a SQLite `jobs` table, idempotent by `(project, path, hash)`.

A broker would add a second stateful service to run, monitor and back up. The durability it
would provide is already provided differently and better: every job is **re-derivable from
the vault**, so the worst case of losing the queue is a reconcile that recomputes it.

---

## Things deliberately not built

Being explicit about these matters as much as the choices above — each was considered and
declined for a stated reason, not merely skipped.

| Not built | Why |
|---|---|
| **LangChain / LlamaIndex as the framework** | Nine subsystems here have genuinely custom requirements (Obsidian parsing, parent reconstruction, tenancy injection, the eval gate). A framework would be an abstraction tax over code that would still need writing. |
| **Elasticsearch for BM25** | Qdrant sparse vectors, zero extra infra. |
| **Collection-per-project** | Per-tenant overhead. |
| **Full GraphRAG** | The wikilinks are already there. |
| **Fine-tuned embeddings** | The eval gate is at recall@10 1.000. Fine-tuning would optimize a metric that is already saturated, and lock the index to a private model. |
| **`answer` mode** | The caller is already an LLM. Building it would burn tokens to lose information. |
| **Query expansion (stage 1)** | Costs an LLM call the rest of the pipeline avoids; no measured evidence it helps on identifier-heavy notes. |
| **`POST /v1/projects`** (hot-add a tenant) | Answered honestly with **501** naming `vaultrag project add`, rather than 404-ing as though the spec were wrong. The authorization half is solved; the resource-lifecycle half (who closes the embedder if the request fails, what happens to an in-flight search when the served set mutates) is not — and that mutation must not land next to the tenancy boundary. |
| **VLM image captioning** | Needs a GPU or a paid API. Kept off *and unbuilt* rather than shipping a dead knob. |
| **The `langfuse` compose profile** | Nothing in VaultRAG emits a trace yet — no `Tracer` seam, no spans, no SDK. Self-hosting Langfuse needs Postgres + ClickHouse + Redis + object storage: **four more containers than the 1.8 GiB system they would observe.** Build the producer first. |
| **A `rate_result` feedback tool** | An MCP tool nothing calls is a dead knob, and relevance feedback with no consumer is a table that only grows. |

---

## Decisions that changed under measurement

The useful ones. Each was a documented plan that a real run overturned.

| Original plan | What measurement showed | Outcome |
|---|---|---|
| `rerank_top_n = 50` | `always`@5 (0.939) beats `always`@50 (0.928). Depth adds distractors, not wins. | Default → 20, chosen from a model-free ceiling analysis. |
| Rerank on by default | ~500 ms per passage on CPU; the reranker is +1 068 MiB. | Default `never`; a tri-state mode instead of a boolean. |
| `rerank_threshold = 0.3` | bge-v2-m3 sigmoids *relevant* chunks at ~0.02–0.25. A fixed floor evicted them. | Default 0.0 — no floor; per-vault knob. |
| Compression similarity = "cos > 0.92" | int8 was not the blocker; **parent-swap** was (vectorless snippets). | Lexical Jaccard, uniform over child and parent snippets. |
| `query_log` stores only `q_hash` | A hash cannot become a golden-set query, so the mining loop could never name one. | Nullable `query_text`, default on for local/stdio, off for shared HTTP. |
| Ingest lag would detect a dead indexer | An indexer that is *gone* stops producing lag observations entirely. | `up{job="vaultrag"} == 0` — a scrape-liveness alert. |
| "3–4 GB RAM at 100k chunks" | Measured **1.8 GiB** at the shipped default (727 MiB containers + ~1 070 MiB host Ollama). | The vault figure holds as an upper bound and overstates the default by ~2×. |

---

## Related reading

- [Architecture](architecture.md) — how these choices compose into a system
- [Retrieval pipeline](retrieval.md) — the stages, in depth
- [Benchmarks](benchmarks.md) — the full measurement record
- `../Rag/decisions/` — the canonical decision records
