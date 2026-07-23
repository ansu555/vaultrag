# Configuration

All configuration is environment-driven, prefix **`VAULTRAG_`**, read from the environment
or a `.env` file beside the working directory. Unknown variables are ignored.

Source of truth: `src/vaultrag/config.py`.

```bash
# .env
VAULTRAG_QDRANT_URL=http://localhost:6333
VAULTRAG_OLLAMA_URL=http://localhost:11434
VAULTRAG_RERANK_MODE=auto
VAULTRAG_BUDGET_TOKENS=3000
```

**Per-project overrides.** Most settings can be overridden per project at registration:

```bash
vaultrag project add work --vault ~/vaults/work --set default_top_k=25 --set rerank_mode=auto
```

> **`embed_dims` is deliberately *not* per-project overridable.** The Qdrant collection is
> shared, so its vector dimensionality is a deployment-wide property. Letting one project
> change it would corrupt the collection for all of them.

---

## Vault and project

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_VAULT_PATH` | path | `.` | Filesystem root of the vault to index. |
| `VAULTRAG_PROJECT_ID` | str | `default` | Tenant/project scope. Payload-partitioned in Qdrant. |
| `VAULTRAG_VAULT_NAME` | str? | *unset* | The vault's **Obsidian display name**, for `obsidian://open` citation URIs. |

`vault_name` is not derivable from `vault_path` — the app names a vault independently of
its folder — so it is configured, falling back to the directory name at the composition
root. Unset with no fallback → no URI is emitted, and `Snippet.uri` stays `null`.

> **`serve` refuses to start unconfigured.** Unlike `mcp`, it will not fall back to the
> current working directory. Inside a container that directory is VaultRAG's own install
> tree, and the observed failure was an indexer recursively inotify-watching its own
> `.venv`. Register a project first.

---

## Local state

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_STATE_DIR` | path | `state` | Directory for the SQLite DB and embed cache. |

Everything here is a **derived cache** and safe to delete — `reindex` rebuilds it. The one
exception is the `query_log` table, which nothing can reconstruct.

---

## Vector store

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_QDRANT_URL` | str | `http://localhost:6333` | Qdrant HTTP endpoint. |
| `VAULTRAG_QDRANT_COLLECTION` | str | `chunks` | Single collection; tenants separated by payload, not by collection. |

---

## Embeddings

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_OLLAMA_URL` | str | `http://localhost:11434` | Ollama endpoint serving the dense model. |
| `VAULTRAG_EMBED_MODEL` | str | `qwen3-embedding:0.6b` | Dense model tag, behind the `Embedder` interface. |
| `VAULTRAG_EMBED_DIMS` | int | `1024` | Vector dimensionality. Qwen3-0.6B native 1024, Matryoshka-truncatable to 512. |

**Changing either model or dims invalidates every stored vector.** The embed cache is keyed
`(model, dims, chunk_hash)`, so old entries simply stop matching — but the *Qdrant
collection* is created with a fixed dimensionality. Changing `embed_dims` requires
recreating the collection and reindexing.

After any change here, re-record the eval fixture:
`uv run vaultrag eval --record`.

---

## Retrieval defaults

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_DEFAULT_TOP_K` | int | `10` | Snippets returned by `search` when `k` is omitted. |
| `VAULTRAG_BUDGET_TOKENS` | int | `2000` | Default response token budget. |

---

## Reranker — retrieval stage 4

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_RERANK_MODE` | `never` \| `auto` \| `always` | **`never`** | Whether the cross-encoder runs. |
| `VAULTRAG_RERANK_MODEL` | str | `onnx-community/bge-reranker-v2-m3-ONNX` | HF repo id. ONNX int8 — no torch, no transformers. |
| `VAULTRAG_RERANK_MODEL_FILE` | str | `onnx/model_int8.onnx` | Model file within the repo. |
| `VAULTRAG_RERANK_TOP_N` | int | `20` | Candidates fed to the cross-encoder. **Cost is linear in this.** |
| `VAULTRAG_RERANK_THRESHOLD` | float | `0.0` | Absolute score floor. Default = **no floor**. |
| `VAULTRAG_RERANK_DOMINANT_RATIO` | float | `1.5` | The `auto` boundary: skip when top-1 ≥ this × top-2. |

### Why the default is `never`

**Latency, not quality.** Measured on Apple Silicon CPU: ~500 ms **per passage**, so
`top_n=20` costs ~10 s per query. `search` is an interactive tool an agent blocks on.

It is also **+1 068 MiB resident** — the largest single component of the system. This is a
memory knob as much as a latency one.

### Why it is a mode, not a boolean

Two golden sets disagree and both are right: on a title-matchy set fusion already ranks
near-perfectly and the cross-encoder can only shuffle a correct #1 down; on a
paraphrase-heavy set the cross-encoder is what closes the semantic gap. A single global
boolean has to be wrong on one of them.

`auto` decides **per query** from the top-1/top-2 fusion margin — arithmetic on scores
already computed, so deciding is free and it only ever *saves* model calls.

### Tuning notes

- `rerank_top_n` — deeper is measurably **worse**, not just slower. Run
  `vaultrag eval --ceiling` to find the fusion depth at which your vault's correct answers
  saturate; anything beyond is pure cost and pure distractor risk.
- `rerank_threshold` — a fixed cutoff is score-distribution-dependent and evicts relevant
  chunks. The design vault's suggested 0.3 was measured to **regress recall** (bge-v2-m3
  sigmoids relevant chunks at ~0.02–0.25). Raise it per-vault, from measurement.
- `rerank_dominant_ratio` — score-distribution-dependent, so per-vault, not a universal
  constant. Higher reranks more often; `0` always reranks.

**Graceful degradation:** if the model cannot be fetched, `auto`/`always` fall back to a
no-op reranker preserving fusion order. An offline install loses the lift, never the search.

> **Retired variable.** `VAULTRAG_RERANK_ENABLED` no longer exists and **fails loudly** if
> set. pydantic-settings silently ignores unknown variables, so leaving it in a shell
> profile would do nothing at all — someone would set `=true`, see no reranking, and
> conclude the stage is broken. An unhelpful error beats a silent no-op.

---

## Parent swap — retrieval stage 5

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_PARENT_SWAP_ENABLED` | bool | `true` | Swap a retrieved child for its enclosing H1/H2 section. |
| `VAULTRAG_PARENT_MAX_TOKENS` | int | `2000` | Largest section eligible for the swap. |

The parent is reconstructed from the vault at query time; **no parent text is stored**. A
section bigger than the cap is too coarse to swap in, so its child is kept instead.

**On by default, unlike rerank**, because it does not move *which notes* rank — it
repackages within a note. Golden-set-neutral, and the agent reads coherent sections instead
of fragments.

---

## Graph expand — retrieval stage 6

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_GRAPH_EXPAND_ENABLED` | bool | `true` | The whole stage. |
| `VAULTRAG_GRAPH_BOOST` | float | `0.25` | Largest share of the result set's score spread that link authority can add. `0` disables the reorder, keeps the neighbour append. |
| `VAULTRAG_GRAPH_TOP_DOCS` | int | `3` | How many top documents seed the neighbour append. |
| `VAULTRAG_GRAPH_MAX_RELATED` | int | `10` | Cap on appended neighbour pointers per search. |
| `VAULTRAG_GRAPH_MAX_TAG_SIBLINGS` | int | `5` | Cap on shared-tag siblings returned by `related`. |

**`graph_boost` is a fraction, not an absolute bonus**, because incoming scores are RRF
(~0.016), a cross-encoder sigmoid (0–1), or a constant 1.0 depending on which stages ran.
An absolute number would be a no-op on one scale and decisive on another.

Sweep results and the safety bound: [benchmarks](benchmarks.md#graph-expand-ab--the-graph_boost-sweep).
**Above 0.25 measured better on a link-dense vault; 1.00 is destructive.**

`graph_max_tag_siblings` is small on purpose — tags are prefix-expanded, so every note
under one `project/` namespace is a weak sibling of every other. Measured: one `related`
call returned **46 tag siblings** beside 3 real links. Raise it on a vault with a flat,
meaningful tag vocabulary.

---

## Compression — retrieval stage 7

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_COMPRESS_DUPE_THRESHOLD` | float | `0.9` | Token-set Jaccard above which a lower-ranked snippet is dropped as a near-duplicate. `≥1` disables. |
| `VAULTRAG_COMPRESS_MMR_LAMBDA` | float | `0.7` | MMR relevance weight (0–1). Higher trusts the incoming rank order; lower diversifies harder. `1.0` disables the reorder. |
| `VAULTRAG_COMPRESS_PER_DOC_CAP` | int | `3` | Most chunks of one document the packer keeps. `0` disables. |

Similarity is **lexical**, not embedding cosine — parent-swap emits snippets with no stored
vector, and MMR needs one metric across all candidates.

The **top hit is never demoted** regardless of `mmr_lambda`.

Per-vault: raise `dupe_threshold` if distinct chunks legitimately share boilerplate.

---

## Query log

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_QUERY_LOG_ENABLED` | bool | `true` | Record one row per served search. Off skips the write entirely. |
| `VAULTRAG_QUERY_LOG_TEXT` | bool | `true` | Store the raw query text alongside the hash. |
| `VAULTRAG_QUERY_LOG_RETENTION_DAYS` | int | `90` | Drop rows older than this. `0` disables. |
| `VAULTRAG_QUERY_LOG_MAX_ROWS` | int | `200000` | Hard ceiling; oldest dropped past it. `0` disables. |

**`query_log_text` defaults on for local/stdio deployments.** Without it the mining loop
cannot *name* a query, so a zero-hit row can never become a golden-set case. Set it
**false** for shared multi-tenant HTTP; the row is still written, hash-only.

It does **not** relax the logging rules: raw query text never enters the log stream or the
metrics under either setting, and a test pins that.

---

## Live watch

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_WATCH_DEBOUNCE_SECONDS` | float | `1.5` | Per-path quiet window before a changed file is (re)ingested. |
| `VAULTRAG_WATCH_WORKERS` | int | `4` | How many *distinct* files ingest in parallel. |

Obsidian saves via temp files; the debounce collapses one save's event storm into a single
ingest. Per-path work stays serial — last write wins.

---

## Write-back

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_WRITEBACK_ENABLED` | bool | `true` | Expose the write tools at all. |
| `VAULTRAG_WRITEBACK_ALLOW_ROOTS` | list[str] | `["agent-inbox"]` | Vault-relative folders writes may target. **Empty list disables all writes.** |
| `VAULTRAG_WRITEBACK_AUTHOR` | str | `claude-code` | Identity stamped into provenance frontmatter and used as the git commit author. |
| `VAULTRAG_WRITEBACK_MAX_BYTES` | int | `1000000` | Largest note a single write may produce. |
| `VAULTRAG_WRITEBACK_MAX_WRITES_PER_MINUTE` | int | `30` | Per-session sliding-window cap. |
| `VAULTRAG_WRITEBACK_GIT_ENABLED` | bool | `true` | Auto-commit each accepted write. |

**On by default — the memory loop is the point.** A read-only deployment sets
`VAULTRAG_WRITEBACK_ENABLED=false` or passes `vaultrag mcp --no-write`; then no metadata
store is opened for writing at all.

Git auto-commit is **best-effort**: a non-git vault still gets the write and the index, the
commit is just skipped. The vault, not git, is the source of truth.

Every default here is deliberately conservative and per-vault overridable — an open-source
user tunes these; they are not hardcoded cutoffs.

---

## Logging

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_LOG_LEVEL` | str | `INFO` | Root level for structlog output. |

Structured JSON via structlog, **stderr only** (stdout is the MCP JSON-RPC channel). Per-stage
timings, `project`, `q_hash`. Raw query text is never logged at info level; secrets are never
logged at any level.

---

## Metrics

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_METRICS_ENABLED` | bool | `false` | Serve Prometheus metrics on `metrics_port`. |
| `VAULTRAG_METRICS_PORT` | int | `9464` | Port for the standalone `/metrics` endpoint. |
| `VAULTRAG_METRICS_ADDR` | str | `127.0.0.1` | Bind address. |

**Off by default** because `vaultrag mcp` runs as a stdio subprocess of an agent, and *N*
concurrent sessions each binding a port is a surprise plus a conflict. Metrics are
**recorded** either way — this flag only opens the scrape endpoint.

A bind clash **warns and continues**; it never fails the process.

Set `VAULTRAG_METRICS_ADDR=0.0.0.0` to let a Prometheus in another container scrape it. The
compose file does exactly this, on a network that is never published to the host.

---

## Serve / role switch

| Variable | Type | Default | Description |
|---|---|---|---|
| `VAULTRAG_ROLE` | `api` \| `indexer` | `api` | What `vaultrag serve` runs as. |
| `VAULTRAG_API_HOST` | str | `127.0.0.1` | Bind address for `serve`. |
| `VAULTRAG_API_PORT` | int | `8000` | Port for `serve`. |
| `VAULTRAG_STARTUP_DRIFT_CHECK` | bool | `true` | Refuse to start when the manifest and the vector store disagree. |

**Loopback by default and deliberately so.** Remote access goes through Tailscale or
WireGuard, not an open port. Setting `0.0.0.0` is a decision, so it must be typed out.

`api_host` is deliberately **not defaulted in the Docker image**: the code default is
loopback, and inside a container that makes the port unreachable. The compose file sets it
next to the loopback-only publish, so both halves of the decision are visible in one file.

**The drift check is on because the failure it catches is silent** — a drifted index answers
every query plausibly from a fraction of the vault. `--allow-drift` overrides it for one
run; the drift is still measured and logged.

> **Retired variable.** `VAULTRAG_API_KEY` no longer exists and **fails loudly** if set. It
> was replaced by the `api_keys` table, which has no env-var form because keys are per-key
> rows, not one process-wide setting. Use `vaultrag key add`. An empty table still gets one
> admin key minted and printed at startup, so unsetting it does not leave the server
> keyless.

---

## Docker-only variables

Read by `docker-compose.yml` on the host, not by the application.

| Variable | Default | Description |
|---|---|---|
| `VAULTRAG_VAULTS_DIR` | `./vaults` | Host directory holding the vaults, mounted at `/vaults`. |
| `VAULTRAG_UID` / `VAULTRAG_GID` | `1000` | uid:gid the containers run as. **Must own the vault** for write-back. |
| `VAULTRAG_ALERT_WEBHOOK` | a dead local address | Where Alertmanager POSTs. |

The alert webhook defaults to `http://127.0.0.1:9/unconfigured` on purpose — a route that
shipped somewhere real out of the box would publish this box's operational state the moment
the profile came up.

---

## Configuration precedence

```
CLI flag  >  per-project override (project add --set)  >  environment  >  .env  >  code default
```

Eval overlay files (`vaultrag eval --config x.yml`) apply on top of the environment for
that run only.

---

## Complete example

```bash
# .env — a tuned single-box deployment

# stores
VAULTRAG_QDRANT_URL=http://localhost:6333
VAULTRAG_OLLAMA_URL=http://localhost:11434
VAULTRAG_STATE_DIR=/var/lib/vaultrag

# retrieval
VAULTRAG_DEFAULT_TOP_K=10
VAULTRAG_BUDGET_TOKENS=3000
VAULTRAG_RERANK_MODE=auto            # this box has the headroom
VAULTRAG_RERANK_TOP_N=10             # measured against `eval --ceiling`
VAULTRAG_GRAPH_BOOST=0.4             # link-dense vault; swept, not guessed

# write-back
VAULTRAG_WRITEBACK_ALLOW_ROOTS=["agent-inbox","scratch"]
VAULTRAG_WRITEBACK_AUTHOR=claude-code

# ops
VAULTRAG_METRICS_ENABLED=true
VAULTRAG_METRICS_ADDR=0.0.0.0
VAULTRAG_LOG_LEVEL=INFO
```

---

## Related reading

- [Retrieval pipeline](retrieval.md) — what each stage knob actually does
- [Benchmarks](benchmarks.md) — the sweeps behind these defaults
- [Security model](security.md) — the settings with a threat model attached
- [Operations](operations.md) — deployment-time configuration
