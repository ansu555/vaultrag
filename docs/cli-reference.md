# CLI reference

```
vaultrag [OPTIONS] COMMAND [ARGS]...
```

Every command below is reachable as `uv run vaultrag <command>` in a dev checkout, or as
`vaultrag <command>` inside the Docker image (the image's `ENTRYPOINT` is `vaultrag`, so
you pass only the subcommand).

| Command | One line |
|---|---|
| [`reindex`](#vaultrag-reindex) | Build or repair the index from a vault. |
| [`watch`](#vaultrag-watch) | Reconcile once, then index every edit as it happens. |
| [`mcp`](#vaultrag-mcp) | Serve the MCP tools over stdio — what Claude Code launches. |
| [`serve`](#vaultrag-serve) | Serve REST `/v1` over HTTP (`--role api`), or run the indexer (`--role indexer`). |
| [`project`](#vaultrag-project) | Manage the project registry: `add`, `list`, `show`, `rm`. |
| [`key`](#vaultrag-key) | Manage HTTP API keys: `add`, `list`, `revoke`. |
| [`queries`](#vaultrag-queries) | Report on the query log and mine it for golden-set candidates. |
| [`eval`](#vaultrag-eval) | Score retrieval quality against a golden set. The CI gate. |

Global options: `--version`, `--help`.

---

## `vaultrag reindex`

**Build the index from a vault (scan → parse → chunk → embed → upsert).**

```bash
vaultrag reindex [--project TEXT] [--vault PATH] [--force]
```

| Flag | Default | What it does |
|---|---|---|
| `--project` | registry default | Project/tenant scope for the index. |
| `--vault` | from registry | Path to the vault to index. Overrides the registered path. |
| `--force` | off | Re-ingest every file, ignoring the manifest. |

**Idempotent by default.** A file whose BLAKE3 content hash matches the manifest is
skipped entirely — no parse, no embed, no upsert. A second run over an unchanged vault is
a sub-second no-op (measured: 95–139 ms).

**When you need `--force`:** if the *vector store* lost data that the manifest still
records, an ordinary run cannot detect it and will report the missing documents as
`unchanged`. `--force` is the repair path. A drift warning prints after every run — if it
is non-zero, `--force` is what clears it.

```bash
vaultrag reindex --project mynotes            # incremental
vaultrag reindex --project mynotes --force    # repair a drifted vector store
```

**Requires** a reachable Qdrant and Ollama. See [indexing](indexing.md) for what happens
inside.

---

## `vaultrag watch`

**Live-watch a vault: reconcile once, then (re)index each edit as it happens.**

```bash
vaultrag watch [--project TEXT] [--vault PATH] [--metrics]
```

| Flag | Default | What it does |
|---|---|---|
| `--project` | registry default | Project/tenant scope to index. |
| `--vault` | from registry | Path to the vault to watch. |
| `--metrics` | off | Serve Prometheus metrics on `VAULTRAG_METRICS_PORT` (9464). |

Runs a **boot reconcile** first — catching anything that changed while the watcher was
down — then starts the watchdog observer and blocks until Ctrl-C.

Each changed path waits out a per-path quiet window (`VAULTRAG_WATCH_DEBOUNCE_SECONDS`,
default 1.5 s) before ingesting, because Obsidian saves through temp files and would
otherwise fire an event storm per keystroke-batch. Up to
`VAULTRAG_WATCH_WORKERS` (default 4) distinct files ingest in parallel; work on a single
path stays serial, last write wins.

This is what the **indexer role** runs. In a compose deployment you do not invoke it
directly — `serve --role indexer` does.

---

## `vaultrag mcp`

**Serve the MCP tools over stdio.** This is the command Claude Code launches.

```bash
vaultrag mcp [--project TEXT]... [--vault PATH] [--watch]
             [--write | --no-write] [--metrics] [--allow-drift]
```

| Flag | Default | What it does |
|---|---|---|
| `--project` | *all registered* | Project to serve. Repeat to serve several. Omit for every registered project. |
| `--vault` | from registry | Path the `open` tool reads from. |
| `--watch` | off | Also live-watch the vault(s) in background daemon threads. Opt-in — normally the indexer role owns the watcher. |
| `--write` / `--no-write` | `--write` | Expose the write tools. `--no-write` serves read-only. Also gated by `VAULTRAG_WRITEBACK_ENABLED`. |
| `--metrics` | off | Serve Prometheus metrics on port 9464. Off by default because *N* concurrent agent sessions are *N* processes, and they would contend for one port. |
| `--allow-drift` | off | Start even if the manifest and the vector store disagree. Drift is still measured and logged; only the refusal is waived. |

**Registration:**

```bash
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp
```

**stdout is the JSON-RPC channel.** Nothing in this process may print to it — logs go to
stderr, and the FastMCP banner is disabled. If you wrap this command in a shell script,
that script must not echo.

**Which tools appear depends on capability, not configuration guesswork:** `related` is
registered only when a link graph exists, `overview` only with a metadata store, and the
write tools only when at least one served project is writable. No capability, no tool —
rather than a tool that answers zero for everything.

See [MCP tools](mcp-tools.md) for the full surface.

---

## `vaultrag serve`

**Serve the REST API over HTTP (`role=api`), or run the indexer (`role=indexer`).**

```bash
vaultrag serve [--project TEXT]... [--vault PATH] [--host TEXT] [--port INT]
               [--role api|indexer] [--write | --no-write] [--watch | --no-watch]
               [--allow-drift] [--metrics]
```

| Flag | Default | What it does |
|---|---|---|
| `--role` | `VAULTRAG_ROLE` (`api`) | `api` serves REST `/v1` + `/metrics`; `indexer` runs the reconcile + watcher with no HTTP surface. |
| `--project` | *all registered* | Project to serve. Repeat for several. |
| `--vault` | from registry | Vault path, for a single project. |
| `--host` | `127.0.0.1` | Bind address. Loopback by default, deliberately. |
| `--port` | `8000` | Bind port. |
| `--write` / `--no-write` | `--write` | Expose the write-back endpoints (and, with them, reindex). |
| `--watch` / `--no-watch` | follows role | On for `indexer`, off for `api`. |
| `--allow-drift` | off | Start despite manifest/vector-store disagreement. |
| `--metrics` | off | Also expose the standalone **keyless** exporter on 9464. ORs with `VAULTRAG_METRICS_ENABLED`. |

**One image, two roles.** They are separate processes because they fail, restart and
scale for different reasons: an indexer chewing through a 100k-chunk reconcile must not
take query serving down with it, and an api container should be restartable without
re-reconciling a vault.

**Authentication is mandatory on the api role.** Every `/v1` route and `/metrics` needs
`Authorization: Bearer <key>`; `/healthz` is the one exception. An empty `api_keys` table
gets one admin-everywhere key minted and printed at startup, so there is always a way in.

**`serve` refuses to start unconfigured.** Unlike `mcp`, it will not fall back to the
current working directory — inside a container that directory is VaultRAG's own install
tree, and the failure mode is an indexer recursively watching its own `.venv`. Register a
project first.

```bash
vaultrag serve --role api                     # REST on 127.0.0.1:8000
vaultrag serve --role indexer                 # reconcile + watch, no port
vaultrag serve --host 0.0.0.0 --port 8000     # bind wide (a decision you must type out)
```

See [REST API](rest-api.md).

---

## `vaultrag project`

**Manage the project registry: one deployment, many vaults.**

The registry is a SQLite table every VaultRAG process reads at startup. It is what makes
`--project` mean something and what the tenancy boundary is enforced against.

### `project add`

```bash
vaultrag project add PROJECT_ID --vault PATH [--name TEXT] [--set KEY=VALUE]...
```

| Argument / flag | Required | What it does |
|---|---|---|
| `PROJECT_ID` | yes | Unique project/tenant id — what agents pass as `project=`. |
| `--vault` | yes | Path to the project's vault. |
| `--name` | no | Display name (defaults to the id). |
| `--set` | no | Per-project settings override, `key=value`, repeatable. |

**Idempotent** — re-adding updates the row in place.

```bash
vaultrag project add design --vault ~/Desktop/Rag --name "VaultRAG design vault"
vaultrag project add work --vault ~/vaults/work --set default_top_k=25 --set rerank_mode=auto
```

> Not every setting is per-project overridable. `embed_dims` in particular is **not** —
> the Qdrant collection is shared, so its vector dimensionality is a deployment-wide
> property, and letting one project change it would corrupt the collection for all of
> them.

### `project list`

```bash
vaultrag project list
```

Lists registered projects with their indexed document counts.

### `project show`

```bash
vaultrag project show PROJECT_ID
```

One project's registry row: vault path, status, document count, per-project overrides.

### `project rm`

```bash
vaultrag project rm PROJECT_ID [--keep-vectors]
```

Removes the project and **cascades both derived caches** — SQLite rows *and* Qdrant
points. The vault on disk is untouched.

The vector delete runs **first** on purpose: if it fails, the project is still registered
and the command can simply be re-run. The other order would deregister a project whose
vectors are still live and no longer reachable through the registry to clean up.

`--keep-vectors` leaves the points orphaned in the collection. That is for recovering a
mistaken removal, not a normal operation.

---

## `vaultrag key`

**Manage API keys for the HTTP transport.** MCP over stdio has no keys — it inherits
local-user trust. Keys exist because HTTP is the first transport with a caller narrower
than "whoever can run this binary."

### `key add`

```bash
vaultrag key add [--scope PROJECT:LEVEL]... [--name TEXT] [--rate-limit INT]
```

| Flag | Default | What it does |
|---|---|---|
| `--scope` | none | `project:level`, repeatable. Level is `read`, `write` or `admin`, each implying the ones before it. |
| `--name` | none | Label shown by `key list` (e.g. `ci-bot`). |
| `--rate-limit` | `60` | Requests/minute this key's token bucket refills to. `0` disables the limit. |

```bash
vaultrag key add --name ci-bot --scope design:read --scope work:write --rate-limit 120
```

**Prints the key once. Nothing holds it again.** The store keeps only an argon2 hash, so
a lost key has no recovery path — revoke it and mint a new one, the same as any password.

The key format is `id.secret`: a public, indexed `id` for O(1) lookup and a secret half
that is the only thing argon2 ever sees. Re-hashing a presented secret against *every*
stored key would not scale, because argon2 is deliberately slow.

### `key list`

```bash
vaultrag key list
```

Lists keys by id, name and scope — never the key itself, never its hash.

### `key revoke`

```bash
vaultrag key revoke KEY_ID
```

Takes effect on that key's **very next request**, not the next restart. There is no cache
to invalidate.

---

## `vaultrag queries`

**Report on the query log and mine it for golden-set candidates.**

```bash
vaultrag queries [--project TEXT] [--report REPORT] [--limit INT] [--since-days INT]
                 [--max-results INT] [--max-score FLOAT] [--export PATH] [--prune]
```

| Flag | Default | What it does |
|---|---|---|
| `--project` | registry default | Project whose query log to report on. |
| `--report` | `summary` | One of `summary`, `frequent`, `slowest`, `recent`, `worst`, `zero-hit`. |
| `--limit` | `20` | Rows per section. |
| `--since-days` | all | Only consider queries from the last *N* days. |
| `--max-results` | — | Keep only queries whose best run returned at most *N* snippets. |
| `--max-score` | — | Keep only queries whose best snippet ever scored at or below this. **The useful export selector.** |
| `--export` | — | Write the selected queries to a golden-set-shaped candidate file. |
| `--prune` | off | Apply the retention caps now and report what was dropped. |

The log is written by the **core retrieval pipeline**, not by a transport, so it covers
every search the process served — MCP and REST record the identical shape through the
identical code.

### Start with `--report worst`, not `--report zero-hit`

A hybrid retriever ranks *something* first for any query, so genuinely-empty results are
rare. Measured on the design vault: a question about Stripe webhooks returned **eight
confident-looking hits** from a vault that has never heard of Stripe. What separates
those from real answers is the **score** of the best hit, not the count.

So: read a cutoff off `worst`, then pass it to `--export --max-score`. There is
deliberately **no default threshold**, because the score scale depends on which retrieval
stages ran — RRF (~0.016), a cross-encoder sigmoid, or a constant 1.0.

```bash
vaultrag queries --project mynotes --report worst --limit 30
vaultrag queries --project mynotes --export candidates.json --max-score 0.017
```

The export leaves `expected_paths` blank — only you can say what *should* have answered
the query. Fill them in, append to a golden set, and the eval harness can score them
forever after. That is the loop from "retrieval felt bad" to "a test now catches it."

### Retention

Rows are pruned as the log is written: `VAULTRAG_QUERY_LOG_RETENTION_DAYS` (90) and
`VAULTRAG_QUERY_LOG_MAX_ROWS` (200 000). Set either to `0` to disable that cap.

> `query_log` is the **only table nothing can rebuild.** Everything else in `state/` is a
> derived cache. If you back up one thing beyond the vault itself, back up this.

---

## `vaultrag eval`

**Score retrieval quality against a golden set.** This is the merge gate.

```bash
vaultrag eval [--golden NAME|PATH] [--live] [--config PATH]... [--project TEXT]
              [--gate | --no-gate] [--show-cases] [--rerank MODE]
              [--graph | --no-graph] [--record] [--ceiling]
```

| Flag | Default | What it does |
|---|---|---|
| `--golden` | `corpus` | `corpus` (hermetic gate), `design` (live dogfooding vault), or a path to a golden JSON file. |
| `--live` | off | Score the running index instead of building the fixture in memory. Implied by `--golden design`. |
| `--config` | — | Settings-override file (YAML/JSON) applied on top of the environment. **Repeat to A/B**; the first is the baseline. |
| `--project` | from golden set | Override the project scope named in the golden set. |
| `--gate` / `--no-gate` | `--gate` | Exit non-zero when a config misses recall@10 ≥ 0.9 or MRR@5 ≥ 0.75. |
| `--show-cases` | off | Print the per-case rank table, not just the summary. |
| `--rerank` | `never` | Rerank mode for this run. **Pinned, not inherited** — see below. |
| `--graph` / `--no-graph` | `--graph` | Retrieval stage 6. **Not** pinned — the gate measures the shipped default. |
| `--record` | off | Re-record the frozen vector fixture from live Ollama, then run. |
| `--ceiling` | off | Report how deep the reranker would have to look, instead of scoring configs. Runs no model — takes seconds. |

### The default run is hermetic

```bash
uv run vaultrag eval
```

It reindexes the committed fixture vault into an **in-process Qdrant** using **frozen
Qwen3 vectors**, so it needs no Docker, no Ollama and no network, and returns the same
numbers on every machine. That is exactly what CI runs.

Everything except the dense vectors is real: today's scanner, parser, chunker and payload
code run on every invocation. A chunking change that hurts retrieval shows up here.

`ReplayEmbedder` **raises** on a cache miss rather than falling back to a live call or
inventing a vector — a fabricated embedding would make CI green while measuring nothing.

### The rerank stage is pinned off unless asked for

The gate inherits every other setting from the environment, but not this one. A
cross-encoder run downloads a ~600 MB ONNX model and scores ~20 passages per query, which
would turn a hermetic sub-minute check into a slow, network-dependent one. Measuring
rerank is a deliberate act.

### Common invocations

```bash
uv run vaultrag eval                                  # the gate
uv run vaultrag eval --show-cases                     # + per-case rank table
uv run vaultrag eval --golden design                  # dogfood the live index
uv run vaultrag eval --rerank always                  # measure the cross-encoder
uv run vaultrag eval --no-graph                       # A/B retrieval stage 6
uv run vaultrag eval --config a.yml --config b.yml    # A/B two configs, deltas vs the first
uv run vaultrag eval --ceiling                        # how deep does rerank need to look?
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Cleared recall@10 ≥ 0.9 **and** MRR@5 ≥ 0.75. |
| `1` | Missed a threshold. The miss list prints. |
| `2` | Could not run (missing fixture, unreachable index, bad config). |

That exit code is what makes `.github/workflows/eval.yml` merge-blocking.

### Re-recording the fixture

Any change that alters chunk text or the embedding model invalidates the frozen vectors,
and the run fails with `no recorded vector for … --record` rather than scoring wrongly.

```bash
ollama serve &
uv run vaultrag eval --record
git add tests/eval/vectors.db
```

Recording is incremental — it reuses every vector whose text is unchanged, so adding
three golden queries costs three model calls. A PR that changes chunking is **expected**
to carry an updated `vectors.db`, and the diff in the gate's numbers is precisely the
review signal you want.

See [development](development.md#the-eval-gate) and [benchmarks](benchmarks.md).

---

## Exit-code summary

| Command | 0 | non-zero |
|---|---|---|
| `reindex`, `watch`, `project`, `key`, `queries` | success | unreachable dependency, invalid arguments, unknown project |
| `mcp`, `serve` | clean shutdown | startup refusal (unconfigured, index drift), bind failure |
| `eval` | gate passed | `1` gate failed · `2` could not run |
