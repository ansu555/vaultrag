# Resources

Every external link that matters, plus the map from this repo to the design vault.

---

## Core stack

| Component | Role in VaultRAG | Link |
|---|---|---|
| **Qdrant** | Vector + sparse + payload-filtered search, one collection, payload-partitioned tenancy | <https://qdrant.tech/documentation/> |
| **Ollama** | Serves the local dense embedding model | <https://ollama.com/> |
| **Qwen3-Embedding-0.6B** | The default dense embedder (Apache-2.0, 1024d, Matryoshka) | <https://huggingface.co/Qwen/Qwen3-Embedding-0.6B> |
| **FastEmbed** | BM25 sparse vectors, in-process, no extra infrastructure | <https://github.com/qdrant/fastembed> |
| **bge-reranker-v2-m3** | The cross-encoder (retrieval stage 4) | <https://huggingface.co/BAAI/bge-reranker-v2-m3> |
| ↳ ONNX int8 export | What we actually load — no torch, no transformers | <https://huggingface.co/onnx-community/bge-reranker-v2-m3-ONNX> |
| **ONNX Runtime** | Runs the reranker on CPU | <https://onnxruntime.ai/> |
| **SQLite (WAL)** | Metadata, wikilink graph, jobs, query log, API keys | <https://sqlite.org/wal.html> |

## Interfaces

| Component | Role | Link |
|---|---|---|
| **Model Context Protocol** | The primary interface — what agents speak | <https://modelcontextprotocol.io/> |
| **FastMCP** | The MCP server implementation (stdio + HTTP) | <https://github.com/jlowin/fastmcp> |
| **FastAPI** | The REST `/v1` transport | <https://fastapi.tiangolo.com/> |
| **Claude Code** | The reference MCP client | <https://claude.com/claude-code> |
| ↳ MCP setup | `claude mcp add …` | <https://docs.claude.com/en/docs/claude-code/mcp> |

## Parsing and ingestion

| Component | Role | Link |
|---|---|---|
| **markdown-it-py** | CommonMark + GFM tokenizer | <https://markdown-it-py.readthedocs.io/> |
| **python-frontmatter** | YAML frontmatter | <https://github.com/eyeseast/python-frontmatter> |
| **Docling** | PDF → Markdown with page anchors (`--extra pdf`) | <https://github.com/docling-project/docling> |
| **watchdog** | Filesystem events for live watch | <https://python-watchdog.readthedocs.io/> |
| **BLAKE3** | Content hashing — the manifest and the embed-cache key | <https://github.com/BLAKE3-team/BLAKE3> |
| **pathspec** | gitignore-syntax matching for `.ragignore` | <https://github.com/cpburnz/python-pathspec> |
| **Obsidian** | The vault format this indexes | <https://obsidian.md/> |
| ↳ Obsidian URI | The `obsidian://open` citation scheme | <https://help.obsidian.md/Advanced+topics/Using+obsidian+URI> |

## Tooling and operations

| Component | Role | Link |
|---|---|---|
| **uv** | Dependency and venv management | <https://docs.astral.sh/uv/> |
| **ruff** | Lint **and** format | <https://docs.astral.sh/ruff/> |
| **mypy** | Strict type checking | <https://mypy.readthedocs.io/> |
| **Typer** | The CLI | <https://typer.tiangolo.com/> |
| **Pydantic** / **pydantic-settings** | Models and env-driven config | <https://docs.pydantic.dev/> |
| **structlog** | Structured JSON logging | <https://www.structlog.org/> |
| **argon2-cffi** | API-key hashing | <https://argon2-cffi.readthedocs.io/> |
| **prometheus-client** | The metrics registry | <https://prometheus.github.io/client_python/> |
| **Prometheus** | Scraping and alert rules | <https://prometheus.io/docs/> |
| **Grafana** | The provisioned dashboard | <https://grafana.com/docs/> |

---

## Background reading — the techniques

The papers and articles behind the choices in
[design decisions](design-decisions.md).

| Technique | Where it's used | Reference |
|---|---|---|
| **Reciprocal Rank Fusion** | Stage 3 — fusing dense and sparse | Cormack, Clarke & Büttcher (SIGIR 2009), *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods* — <https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf> |
| **BM25** | The sparse retriever | Robertson & Zaragoza (2009), *The Probabilistic Relevance Framework: BM25 and Beyond* |
| **HNSW** | Qdrant's ANN index (`m=16`, `ef_construct=128`) | Malkov & Yashunin — <https://arxiv.org/abs/1603.09320> |
| **Maximal Marginal Relevance** | Stage 7 — diversity reorder | Carbonell & Goldstein (SIGIR 1998), *The Use of MMR, Diversity-Based Reranking…* |
| **Matryoshka Representation Learning** | Why 1024d truncates cleanly to 512 | Kusupati et al. — <https://arxiv.org/abs/2205.13147> |
| **Cross-encoder reranking** | Stage 4 | Nogueira & Cho, *Passage Re-ranking with BERT* — <https://arxiv.org/abs/1901.04085> |
| **Contextual retrieval** | The per-chunk `project ▸ H1 ▸ H2` prefix | Anthropic — <https://www.anthropic.com/news/contextual-retrieval> |
| **Parent-document retrieval** | Stage 5 — index children, return parents | The "small-to-big" pattern, common to LlamaIndex/LangChain; here implemented without either |
| **Scalar quantization** | int8 vectors, ~4× memory reduction | <https://qdrant.tech/documentation/guides/quantization/> |
| **Multitenancy by payload** | One collection, `is_tenant: true` | <https://qdrant.tech/documentation/guides/multiple-partitions/> |

## Standards

| Standard | Where |
|---|---|
| **RFC 9457** — Problem Details for HTTP APIs | Every REST error body |
| **RFC 9110** — HTTP semantics (the 401/403 distinction) | `api/deps.py`, `api/errors.py` |
| **CommonMark** | The Markdown baseline — <https://commonmark.org/> |
| **JSON-RPC 2.0** | What MCP over stdio speaks |

---

## The design vault

The design and knowledge for this system lives in an Obsidian vault at `../Rag`
(absolute: `/Users/anik/Desktop/Rag`). **That vault is the source of truth for *what to
build and why*.** This repo is the source of truth for the *running code*.

> One-line mental model: **the vault decides, this repo executes, and the running index is a
> disposable cache derived from the vault.**

### Navigating it

| You need… | Look in |
|---|---|
| The full canonical system design, one doc | `_meta/vaultrag-architecture.md` |
| A specific *decision* + its rationale | `decisions/*.md` |
| Stable technical shapes — DB schema, metadata payload, MCP signatures, REST API, compose, stack | `reference/*.md` |
| Per-subsystem design | `projects/vaultrag/*.md` |
| Tag taxonomy / component map | `_meta/tags.md` |
| Agent-written notes awaiting review | `agent-inbox/*.md` |

### Decision records

| Note | Summarized in |
|---|---|
| `vault-as-source-of-truth.md` | [Design decisions §foundations](design-decisions.md#the-vault-is-the-source-of-truth) |
| `vector-search-qdrant.md` | [§Qdrant](design-decisions.md#qdrant-for-both-vector-and-keyword-search) |
| `payload-partitioning-not-collection-per-project.md` | [§payload partitioning](design-decisions.md#payload-partitioning-not-collection-per-project) |
| `sparse-bm25-not-elasticsearch.md` | [§hybrid](design-decisions.md#hybrid-always-fused-with-rrf) |
| `dense-embeddings-qwen3.md` | [§Qwen3](design-decisions.md#qwen3-embedding-06b) |
| `reranker-bge-v2-m3.md` | [§reranker](design-decisions.md#bge-reranker-v2-m3-off-by-default) |
| `chunking-heading-tree-not-fixed-size.md` | [§chunking](design-decisions.md#heading-tree-chunking) |
| `no-full-graphrag.md` | [§graph](design-decisions.md#the-graph-is-obsidians-own-wikilinks) |
| `metadata-store-sqlite-to-postgres.md` | [§SQLite](design-decisions.md#sqlite-now-postgres-later) |
| `in-process-jobs-not-redis-celery.md` | [§jobs](design-decisions.md#in-process-jobs-not-rediscelery) |
| `deterministic-compression-not-llm.md` | [§compression](design-decisions.md#deterministic-compression-not-llm-summarization) |
| `snippets-not-answers-by-default.md` | [§snippets](design-decisions.md#snippets-not-answers) |
| `mcp-first-not-rest-first.md` | [§MCP first](design-decisions.md#mcp-first-not-rest-first) |

### Reference shapes

| Note | Implemented in |
|---|---|
| `reference-database-schema.md` | `src/vaultrag/store/sqlite.py` |
| `reference-metadata-schema.md` | `src/vaultrag/index/payload.py`, `models.py` |
| `reference-mcp-tools.md` | `src/vaultrag/mcp/server.py` |
| `reference-rest-api.md` | `src/vaultrag/api/routes.py` |
| `reference-security-model.md` | `auth.py`, `api/deps.py`, `indexer/secret_guard.py`, `writeback/writer.py` |
| `reference-docker-compose.md` | `docker-compose.yml`, `Dockerfile` |
| `reference-technology-stack.md` | `pyproject.toml`, `config.py` |

### Subsystem designs

| Note | Documented here as |
|---|---|
| `parsing-pipeline.md` | [Indexing §parse](indexing.md#3-parse) |
| `chunking-strategy.md` | [Indexing §chunk](indexing.md#4-chunk--the-heading-tree) |
| `embedding-pipeline.md` | [Indexing §embed](indexing.md#5-embed) |
| `indexing-pipeline.md` / `ingestion-pipeline.md` | [Indexing](indexing.md) |
| `hybrid-search.md` | [Retrieval §2–3](retrieval.md#stages-23--hybrid-prefetch-and-rrf-fusion) |
| `reranking.md` | [Retrieval §4](retrieval.md#stage-4--cross-encoder-rerank) |
| `knowledge-graph-integration.md` | [Retrieval §6](retrieval.md#stage-6--graph-expand) |
| `context-compression.md` | [Retrieval §7](retrieval.md#stage-7--compress) |
| `retrieval-pipeline.md` | [Retrieval](retrieval.md) |
| `write-back-design.md` | [Security §write-back](security.md#write-back-safety) |
| `multi-tenant-architecture.md` | [Architecture §7](architecture.md#7-multi-tenancy) |
| `evaluation-framework.md` | [Development §the eval gate](development.md#the-eval-gate) |
| `monitoring-observability.md` | [Operations §observability](operations.md#observability) |
| `performance-scaling-triggers.md` | [Operations §capacity](operations.md#capacity-planning) |
| `cost-model.md` | [Benchmarks §memory](benchmarks.md#memory-footprint) |
| `agent-integration-layer.md` | [MCP tools](mcp-tools.md) |
| `build-roadmap.md` / `p0`–`p3-step-plan.md` | `MEMORY.md` |

### Searching the vault

Until VaultRAG indexes itself, use ripgrep:

```bash
rg -i "reranker" ../Rag --type md -l          # which notes mention it
rg -i -C2 "parent.child|parent swap" ../Rag   # with context
```

Once `search`/`open` work, register the design vault as a project and query it via MCP
instead — using the tool to build the tool is the best possible eval signal:

```bash
vaultrag project add vaultrag-design --vault ../Rag
vaultrag reindex --project vaultrag-design
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp
```

Then, from an agent session:

```
search("how does the heading-tree chunker merge tiny sections", project="vaultrag-design")
open("projects/vaultrag/chunking-strategy.md", heading="Sizing")
```

**When retrieval feels bad while building, that is a real bug report.** Capture it in the
query log and turn it into a golden case.

---

## Repo landmarks

| File | What it is |
|---|---|
| [`CLAUDE.md`](../CLAUDE.md) | The agent guide. Read first if you are an agent. |
| [`MEMORY.md`](../MEMORY.md) | The running build log — current state and session history. |
| `MEMORY-p0.md` … `MEMORY-p2.md` | Archived phase logs. |
| [`README.md`](../README.md) | The front door. |
| [`docs/`](README.md) | This documentation set. |
| `pyproject.toml` | Dependencies, with the *reasoning* for each in comments. |
| `docker-compose.yml` | The deployment, with both halves of every network decision written out. |
| `deploy/` | Prometheus scrape config, alert rules, Alertmanager route, provisioned Grafana dashboard. |
| `.ragignore` | The example ignore file, shipped for vaults. |
| `tests/eval/README.md` | The gate's own documentation. |

---

## Related reading

- [Documentation index](README.md)
- [Design decisions](design-decisions.md) — where most of these links are applied
- [Development](development.md) — how to change any of it
