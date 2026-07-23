# Troubleshooting

Symptom first. Each entry names the cause and the fix.

---

## Install and setup

### `uv: command not found`

Install it: <https://docs.astral.sh/uv/getting-started/installation/>. `pip install` is not
a supported path — dependencies are locked in `uv.lock`.

### `uv sync` fails resolving `docling`

You ran `uv sync --extra pdf` and hit a platform issue. The base install does not need it:

```bash
uv sync
```

PDFs are then skipped cleanly and the rest of the vault indexes normally.

### `mypy` fails on `docling` imports

Expected on a base install and already handled — `docling` is in `pyproject.toml`'s
`ignore_missing_imports` list precisely so `uv run mypy src` passes for the install people
actually run. If it still fails, you have a partial `--extra pdf` install; re-run
`uv sync --extra pdf` or `uv sync` cleanly.

---

## Connecting Qdrant and Ollama

### `cannot reach Qdrant for collection 'chunks' … (is it running?)`

```bash
docker compose up -d qdrant
curl -s http://127.0.0.1:6333/healthz
```

If it is running but unreachable, check `VAULTRAG_QDRANT_URL`. **Inside a container** it
must be `http://qdrant:6333` (compose service name) or
`http://host.docker.internal:6333` (host Qdrant), never `localhost`.

### Embedding calls fail / hang

```bash
curl -s http://127.0.0.1:11434/api/tags | grep qwen3-embedding
ollama pull qwen3-embedding:0.6b
```

Ollama runs **on the host**, not as a compose service, so it can use a GPU. From a container
it is reached at `host.docker.internal:11434`. On Linux, that name only exists because the
compose file declares `extra_hosts: - "host.docker.internal:host-gateway"`.

### `vector dimension mismatch`

You changed `VAULTRAG_EMBED_MODEL` or `VAULTRAG_EMBED_DIMS` after the collection was
created. The collection is created with a fixed dimensionality.

```bash
# destroy and rebuild the derived cache
docker compose down -v
docker compose up -d qdrant
vaultrag reindex --project <p> --force
uv run vaultrag eval --record     # re-record the frozen eval fixture too
```

---

## Startup refusals

### `serve` refuses to start: no project configured

**By design.** Unlike `mcp`, `serve` never falls back to the current working directory —
inside a container that directory is VaultRAG's own install tree, and the observed failure
was an indexer recursively watching its own `.venv`.

```bash
docker compose run --rm api project add mydocs --vault /vaults/my-notes
```

Use `run --rm`, not `exec`: on an unconfigured box there is nothing running to exec into.
The services heal themselves on their next restart once a project exists.

### Startup refuses for index drift

The manifest and the vector store disagree about a served project's chunk count. **The
refusal is deliberate** because the failure it catches is silent — a drifted index answers
every query plausibly from a fraction of the vault.

```bash
vaultrag reindex --project <p> --force
```

To start anyway for one run (drift is still measured and logged):

```bash
vaultrag serve --allow-drift
```

### `VAULTRAG_RERANK_ENABLED was replaced by VAULTRAG_RERANK_MODE`

The old boolean is retired and now **fails loudly**. Unset it and use:

```bash
export VAULTRAG_RERANK_MODE=never   # or auto | always
```

The loud failure is intentional: pydantic-settings silently ignores unknown variables, so
leaving the old one in a shell profile would do *nothing* — you would set `=true`, see no
reranking, and conclude the stage is broken.

### `VAULTRAG_API_KEY was replaced by the api_keys table`

Same shape of trap, one field over. Unset it and use `vaultrag key add`. An empty `api_keys`
table still gets one admin key minted and printed at `serve` startup, so unsetting it does
not leave the server keyless.

---

## MCP and Claude Code

### Claude Code doesn't see the tools

1. `claude mcp list` — is `vaultrag` registered?
2. Restart Claude Code, or start a new session. Registration is read at session start.
3. Run the command by hand and watch stderr:
   ```bash
   uv run --directory /path/to/vaultrag vaultrag mcp
   ```
   It should start and wait on stdin. Any traceback is your answer.

### The MCP server starts and immediately dies

Almost always **something printed to stdout**. stdout is the JSON-RPC channel; a single
stray line corrupts the protocol.

VaultRAG itself writes all logs to stderr and disables the FastMCP banner. If you wrapped
the command in a shell script, that script must not echo.

### `related` / `overview` / write tools are missing

Registration follows **capability**:

| Missing tool | Requires |
|---|---|
| `related` | A link graph — reindex so the `links` table is populated. |
| `overview` | A metadata store. |
| `write_note` et al. | At least one **writable** project, and `VAULTRAG_WRITEBACK_ENABLED=true` (and not `--no-write`). |

### "unknown project 'x'"

```bash
vaultrag project list
```

On a multi-project server every tool needs an explicit `project`. Agents should call
`list_projects` first.

### The agent isn't calling `search`

Nudge the prompt: "search the vault first." The server ships instructions telling clients to
prefer one focused `search` over reading many files, but a client can ignore them.

---

## Indexing

### `reindex` reports everything `unchanged` but search finds nothing

The **vector store lost data the manifest still records.** An ordinary run cannot detect
this — every hash still matches.

```bash
vaultrag reindex --project <p> --force
```

The drift warning printed after every run is the signal for this.

### A note never appears in search results

Walk the gates in order:

1. **Filename** — does it match a secret deny pattern (`*secret*` and `*credentials*` are
   broader than people expect)?
2. **`.ragignore`** — is the path or its folder excluded?
3. **Suffix** — is it a supported type? Unsupported suffixes are skipped *quietly*, by
   design.
4. **Content** — does the file contain something the secret scanner flags? A single pasted
   `AKIA…` withholds the **entire file**.
5. **Watcher** — is anything actually watching? A vault with no `watch` or indexer role only
   updates on an explicit `reindex`.

```bash
vaultrag reindex --project <p>       # look at the per-file outcome
docker compose logs indexer | grep <filename>
```

### Edits take minutes to become searchable

- The per-path debounce is 1.5 s (`VAULTRAG_WATCH_DEBOUNCE_SECONDS`) — not the cause of
  minutes.
- Check `vaultrag_jobs_queue_depth`: a backlog, or a per-file slowdown?
- Check the embed cache hit ratio. A collapsed ratio means something is re-embedding
  everything — usually a model change or a chunker change that altered chunk text.
- Large PDFs are legitimately slow.

### Ingest is very slow on first run

Expected. ~5 s per document on CPU (measured: 67 docs → 261 chunks in 326 s). It is
dominated entirely by embedding. A GPU-backed Ollama changes this dramatically.

Subsequent runs are near-instant (95–139 ms for an unchanged vault).

---

## Retrieval quality

### Results are confident but wrong

A hybrid retriever ranks *something* first for **any** query. Measured on the design vault, a
question about Stripe webhooks returned **eight confident-looking hits** from a vault that
has never heard of Stripe.

What separates a real answer from a confident miss is the **score** of the best hit, not the
count:

```bash
vaultrag queries --project <p> --report worst --limit 30
vaultrag queries --project <p> --export candidates.json --max-score <cutoff-you-read-off>
```

There is deliberately no default threshold — the score scale depends on which stages ran
(RRF ~0.016, cross-encoder sigmoid, or a constant 1.0).

### A long overview document outranks the focused note

**A known, measured limitation.** Dogfooded across 8 subsystem queries on the live design
vault: a long `*-step-plan.md` ranked #1 on **3 of 8**, while the subsystem's own design note
ranked #1 on only **1 of 8**.

Long documents that mention every subsystem outrank the focused note about one. The gate
corpus contains no long cross-cutting documents, so this failure class is invisible to it by
construction.

Workarounds today: narrow with `filters.paths` or `filters.tags`, or `--no-graph` to A/B
whether stage 6 is contributing.

### Exact identifiers aren't found

They should be — BM25 is half of hybrid search. Check:

- The identifier is in the **indexed text**, not only in a filename.
- The file wasn't withheld by the secret scanner (identifiers that look like tokens can trip
  it).
- The BM25 encoder initialized. FastEmbed fetches a small stopword artifact from HuggingFace
  on first encode; on a fully air-gapped box, pre-populate `~/.cache/fastembed`.

### Reranking made things worse

That is a real, measured outcome on some vaults — not a bug. On a **title-matchy** query
distribution, fusion already ranks near-perfectly and the cross-encoder can only shuffle a
correct #1 down (measured 1.00/0.775 fusion vs 0.95/0.743 reranked).

Try `VAULTRAG_RERANK_MODE=auto`, which skips the queries where both retrievers already
agree, and lower `rerank_top_n` — deeper is measurably **worse**, not just slower.

Measure it rather than guessing:

```bash
uv run vaultrag eval --rerank never
uv run vaultrag eval --rerank auto
uv run vaultrag eval --rerank always
uv run vaultrag eval --ceiling      # how deep does rerank even need to look?
```

### Search is slow

Break it down by `stage` on the dashboard, or with `--show-cases`:

| Slow stage | Fix |
|---|---|
| `rerank` | **~500 ms per passage on CPU.** Lower `rerank_top_n`, switch to `auto`, or turn it off. |
| `embed` | Ollama — check load, consider a GPU. |
| `fetch` | Qdrant — check container health and memory. |
| `parents` | Vault filesystem reads — a network mount will hurt here. |

---

## Write-back

### `write_note` is refused

| Refusal | Cause |
|---|---|
| Outside the write allowlist | Default is `agent-inbox/` only. Widen with `VAULTRAG_WRITEBACK_ALLOW_ROOTS`. |
| Path traversal | The realpath escaped the vault root — including via a symlink. |
| Secret content | The note body tripped the content scanner. |
| Not markdown | Only `.md` is writable. *Indexable* is a strictly wider set than *writable*. |
| Too large | `VAULTRAG_WRITEBACK_MAX_BYTES`, default 1 MB. |
| Rate cap | `VAULTRAG_WRITEBACK_MAX_WRITES_PER_MINUTE`, default 30. |
| Note already exists | `write_note` never overwrites. Use `append_section` or a different title. |

### `committed: false`

The vault is not a git repo, or the commit was skipped. **The write still landed** — git is
best-effort, and the vault (not git) is the source of truth.

```bash
cd /path/to/vault && git init && git add -A && git commit -m "initial"
```

### `indexed: false` with a warning

The vault write and the commit succeeded; the synchronous reindex did not (usually Qdrant or
Ollama unavailable). The note is safe on disk and will be picked up by the next reconcile.

### Permission denied writing to the vault (Docker)

`VAULTRAG_UID`/`VAULTRAG_GID` must match the host user that owns the vault.

```bash
id -u; id -g
echo 'VAULTRAG_UID=501' >> .env
echo 'VAULTRAG_GID=20'  >> .env
docker compose up -d --force-recreate
```

### `409` on `patch_frontmatter`

Working as designed. The note changed since you took `base_hash`. The error carries the
**live hash and the current content** — merge and retry with the new `base_hash`. No silent
overwrite.

---

## REST API

### `401 Unauthorized`

No key, an unknown key, or a revoked one.

```bash
docker compose logs api | grep -i "api key"   # the bootstrap key, printed once
vaultrag key list
vaultrag key add --name me --scope <project>:admin
```

A revoked key fails on its **very next** request — no restart, no cache.

### `403 insufficient-scope`

The key authenticated fine but lacks the level on this project. Re-sending the same header
will not help — that is precisely why it is 403 and not 401.

```bash
vaultrag key list          # check the scopes
vaultrag key add --scope <project>:write
```

### `403` on a project you believe exists

A **real but unscoped** project answers 403; a **nonexistent** one answers 404. If you expect
404, check `vaultrag project list` — the project probably does exist, and your key just
cannot see it.

### `429 Too Many Requests`

The key's token bucket is empty. Default 60 req/min.

```bash
vaultrag key add --name high-volume --scope <p>:read --rate-limit 600
```

### `501` on `POST /v1/projects`

Deliberate. Use `vaultrag project add <id> --vault <path>` and restart the server. See
[the reasoning](rest-api.md#post-v1projects).

### `409 reindex-in-progress`

Single-flight. A full reindex of that project is already running; two concurrent reconciles
race on the manifest and the second has nothing to add. Wait, or reindex a single `path`
(which is synchronous and unaffected).

---

## Eval and CI

### `no recorded vector for … --record`

You changed something that alters chunk text or the embedding model, so the frozen fixture
no longer covers it. **This is intended** — silently re-embedding would defeat determinism.

```bash
ollama serve &
uv run vaultrag eval --record
git add tests/eval/vectors.db
```

Recording is incremental: adding three golden queries costs three model calls.

### The eval gate fails after a chunking change

That is the gate doing its job. Investigate with:

```bash
uv run vaultrag eval --show-cases        # which cases moved, and to what rank
```

Thresholds are recall@10 ≥ 0.9 and MRR@5 ≥ 0.75. A PR that changes chunking is expected to
carry an updated `vectors.db`, and the diff in the numbers is exactly the review signal you
want.

### Live eval numbers move between runs

**Expected and unavoidable.** Three consecutive live evals against an unchanged index gave
MRR@5 0.885 / 0.892 / 0.871 — spread 0.021, from approximate HNSW.

Live numbers are **not comparable run to run**. Use the hermetic gate (`vaultrag eval`, no
`--live`) for anything you intend to compare.

### CI is green but retrieval feels bad

Believe the feeling. The gate measures what the golden set contains, and the corpus has no
long cross-cutting documents — so [that failure class is invisible to it by
construction](benchmarks.md#known-gaps).

Mine the query log, add the failing cases to a golden set, re-record, and the gate can see
it from then on.

---

## Observability

### A Grafana panel is empty

Two possibilities, and they look identical:

1. **No data yet.** Nothing has searched or ingested since the exporter started.
2. **The panel queries a metric that doesn't exist.** This renders an empty graph forever
   and looks exactly like a healthy system.

`tests/test_deploy.py` exists to make (2) impossible for committed panels — it resolves every
metric name and label against the live registry. If you added a panel, add it there too.

### Prometheus shows a target as down

The exporter is not bound. Metrics are **recorded** whether or not the endpoint is open;
`VAULTRAG_METRICS_ENABLED` only opens the scrape door.

The compose file sets `VAULTRAG_METRICS_ENABLED=true` and `VAULTRAG_METRICS_ADDR=0.0.0.0`
for both roles. If you are running outside compose, set them yourself.

### Metrics port already in use

A bind clash **warns and continues** — it never fails the process. This is normal when
several `vaultrag mcp` sessions run at once, which is exactly why `--metrics` is off by
default for that command.

Change `VAULTRAG_METRICS_PORT` to run a second exporting process on one box.

---

## Still stuck

1. `VAULTRAG_LOG_LEVEL=DEBUG` and re-run — logs are structured JSON on **stderr**.
2. `uv run pytest` — if the suite fails, the environment is the problem, not your usage.
3. `uv run vaultrag eval` — if this fails, retrieval itself is broken, not your vault.
4. Check [`MEMORY.md`](../MEMORY.md) for the current build state and known issues.
5. Search the design vault (`../Rag`) for the subsystem — most behaviour has a written
   rationale, and if code and a vault decision disagree, the vault wins.
