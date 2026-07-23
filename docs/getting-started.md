# Getting started

From nothing to "Claude Code is searching my vault" in about fifteen minutes, most of
which is a model download.

Two paths are documented here. Pick one:

- **[Local development install](#path-a--local-install)** — `uv`, a Qdrant container,
  Ollama on your machine. Best for trying it, and for the stdio MCP server that Claude
  Code launches per session.
- **[Docker deployment](#path-b--docker)** — the full three-service compose stack. Best
  for a box that stays up and serves several agents over HTTP.

They share the same concepts and the same CLI. Read Path A first even if you plan to
deploy — the vault registration and indexing steps are identical.

---

## 0. What you need

| Requirement | Why | Check |
|---|---|---|
| **Python 3.12+** | The whole codebase targets 3.12 (`StrEnum`, PEP 695 syntax). | `python3 --version` |
| **[uv](https://docs.astral.sh/uv/)** | Dependency and venv management. `pip` is not supported. | `uv --version` |
| **Docker** | Runs Qdrant. (Also runs everything else on Path B.) | `docker --version` |
| **[Ollama](https://ollama.com/)** | Serves the local embedding model. | `ollama --version` |
| **An Obsidian vault** | Any directory of Markdown works — Obsidian itself is not required. | — |
| **~2 GB RAM free** | Measured footprint at 100k chunks is ≈1.8 GB. See [benchmarks](benchmarks.md#memory-footprint). | — |
| **git** *(recommended)* | Write-back auto-commits into the vault's repo. Works without it, just no commits. | `git --version` |

> **Everything is local and free.** No API key is required to run VaultRAG. Paid
> embedding/rerank providers are supported, but only as a config change behind an
> interface — never a dependency. See
> [design decisions](design-decisions.md#local-first-0-software).

---

## Path A — local install

### 1. Install the code

```bash
git clone <your-fork-or-this-repo> vaultrag
cd vaultrag

uv sync                    # base install
uv sync --extra pdf        # optional: + Docling/OCR for PDFs (heavy — pulls torch)
```

`uv sync` creates `.venv/` and installs everything pinned in `uv.lock`. Verify:

```bash
uv run vaultrag --help
```

You should see the command list. If you do, the install is done.

> **The `pdf` extra is genuinely optional.** Without it, PDFs are skipped cleanly and the
> rest of the vault indexes normally — nothing errors. With it, you get Docling PDF
> conversion (Markdown + page anchors) and OCR for images. It costs ~79 extra packages
> including torch, which is why it is not the default.

### 2. Start Qdrant

```bash
docker compose up -d qdrant
```

This starts *only* the vector store, published on `127.0.0.1:6333` (REST + dashboard)
and `127.0.0.1:6334` (gRPC). Data lives in the `qdrant_data` Docker volume.

Check it:

```bash
curl -s http://127.0.0.1:6333/healthz     # → healthz check passed
open http://127.0.0.1:6333/dashboard      # optional: the Qdrant web UI
```

### 3. Pull the embedding model

```bash
ollama pull qwen3-embedding:0.6b
```

~600 MB. This is the default dense embedder: 1024 dimensions, Matryoshka-truncatable to
512. It runs on your machine, and on a GPU if Ollama finds one.

Check it:

```bash
curl -s http://127.0.0.1:11434/api/tags | grep qwen3-embedding
```

> **Swapping the model is a config change, not a code change.** Set
> `VAULTRAG_EMBED_MODEL` and `VAULTRAG_EMBED_DIMS`. But note that changing either
> invalidates every stored vector — reindex with `--force` afterwards, and re-record the
> eval fixture. See [configuration](configuration.md#embeddings).

### 4. Register your vault

A **project** is one vault plus its settings. Registering it writes a row that every
VaultRAG process reads at startup, so you do this once.

```bash
uv run vaultrag project add mynotes --vault ~/Documents/MyVault
```

| Flag | Meaning |
|---|---|
| `mynotes` | The project id. This is what agents pass as `project=`. Keep it short and stable. |
| `--vault` | Absolute path to the vault root. |
| `--name` | Optional display name (defaults to the id). |
| `--set k=v` | Per-project setting override, repeatable — e.g. `--set default_top_k=25`. |

Confirm:

```bash
uv run vaultrag project list
uv run vaultrag project show mynotes
```

`project add` is **idempotent** — re-running it updates the row in place.

#### Add a `.ragignore` (recommended)

Copy the shipped example into your vault root and edit it:

```bash
cp .ragignore ~/Documents/MyVault/.ragignore
```

It is gitignore-syntax. It controls what gets indexed — Obsidian internals, media,
scratch folders.

> **`.ragignore` can only ever narrow what is indexed.** A separate, non-overridable
> secret guard is enforced in code: `.env*`, `id_rsa*`, `*.pem`, `*credentials*` and
> friends are blocked by filename, and note *content* is scanned for embedded key
> material at parse time. Nothing you configure can widen that. See
> [security](security.md#the-secret-guard).

### 5. Build the index

```bash
uv run vaultrag reindex --project mynotes
```

This scans the vault, diffs it against the stored manifest, then parses → chunks →
embeds → upserts everything that changed. First run does the whole vault.

Expect roughly **5 seconds per note** on CPU (measured: 67 documents → 261 chunks in
326 s). A GPU-backed Ollama is dramatically faster. Subsequent runs are near-instant —
unchanged files are skipped by content hash, and the embed cache means even a moved file
re-embeds nothing.

The run prints a summary and a drift check. Verify the index is real:

```bash
curl -s 'http://127.0.0.1:6333/collections/chunks' | head -20
```

### 6. Connect it to Claude Code

This is the payoff. One command:

```bash
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp
```

That registers a **stdio** MCP server. Claude Code launches it as a subprocess per
session, it serves every registered project, and it exits with the session.

Restart Claude Code (or start a new session) and ask it something your vault knows. It
should call `search` and come back with cited snippets carrying `path`, `heading_path`
and an `obsidian://` link.

Verify the registration:

```bash
claude mcp list
```

<details>
<summary><b>Variants</b></summary>

```bash
# serve exactly one project instead of all registered ones
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp --project mynotes

# read-only: no write_note/append_section/patch_frontmatter tools
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp --no-write

# also live-watch the vault from inside the MCP process (solo dogfooding only —
# normally the indexer role owns the watcher)
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp --watch
```

For **other MCP clients**, the equivalent config block is:

```json
{
  "mcpServers": {
    "vaultrag": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/vaultrag", "vaultrag", "mcp"]
    }
  }
}
```

`stdout` is the JSON-RPC channel, so VaultRAG writes **all** logs to stderr and disables
the FastMCP banner. Never add anything to the command that prints to stdout.
</details>

### 7. Keep the index live

The index only tracks the vault if something is watching it:

```bash
uv run vaultrag watch --project mynotes
```

This does a boot reconcile (catching whatever changed while it was down), then starts a
watchdog observer and blocks. Edits become searchable within a couple of seconds — a
1.5 s per-path debounce collapses Obsidian's temp-file save storm into one ingest.

Leave it running in a terminal, or run the indexer role under Docker (Path B).

**You are done.** The rest of this page is the Docker path and the optional extras.

---

## Path B — Docker

One image, two roles (`VAULTRAG_ROLE=api|indexer`), three services. The compose file *is*
the deployment: moving from a laptop to a VPS changes config, never code.

### 1. Ollama stays on the host

Ollama is deliberately **not** a compose service — on the host it can use a GPU, and the
containers reach it through `host.docker.internal`.

```bash
ollama pull qwen3-embedding:0.6b
```

### 2. Put the vaults where the containers can see them

```bash
mkdir -p vaults
cp -R ~/Documents/MyVault vaults/
```

Or point `VAULTRAG_VAULTS_DIR` at wherever they already live. The directory is mounted at
`/vaults` **read-write**, because write-back edits the vault and commits to its git repo.

```bash
cat >> .env <<'EOF'
VAULTRAG_VAULTS_DIR=/absolute/path/to/vaults
VAULTRAG_UID=1000
VAULTRAG_GID=1000
EOF
```

`VAULTRAG_UID`/`GID` must own the vault on the host, or write-back will fail on
permissions.

### 3. Register and index *before* first `up`

On an unconfigured box the services **deliberately refuse to start**: `serve` never falls
back to its own working directory the way `mcp` does, because inside a container that
directory is VaultRAG's own install tree. Use `run --rm`, not `exec` — there is nothing
running yet to exec into.

```bash
docker compose run --rm api project add mynotes --vault /vaults/MyVault
docker compose run --rm api reindex --project mynotes
```

Note the paths are **container paths** (`/vaults/...`), not host paths.

### 4. Bring it up

```bash
docker compose up -d --build
docker compose ps
docker compose logs api | head
```

Three containers: `vaultrag-qdrant`, `vaultrag-api`, `vaultrag-indexer`. The api serves
REST `/v1` + `/metrics` on `127.0.0.1:8000`; the indexer runs the boot reconcile and the
file watcher and opens no port.

### 5. Get your API key

On a first start with an empty `api_keys` table, the api container **mints one
admin-everywhere key and prints it once**. Grab it from the logs:

```bash
docker compose logs api | grep -i "api key"
```

After that, manage keys properly:

```bash
docker compose exec api key add --name ci-bot --scope mynotes:read --rate-limit 120
docker compose exec api key list
docker compose exec api key revoke <key-id>
```

> The printed bootstrap key lives only in that container's log. If you recreate the
> container before saving it, the key still exists but its secret is gone — mint a new
> one with `key add` and revoke the old id.

### 6. Use it

Every `/v1` route and `/metrics` requires `Authorization: Bearer <key>`. `/healthz` is the
one exception.

```bash
export VAULTRAG_KEY='<the key you just got>'

curl -H "Authorization: Bearer $VAULTRAG_KEY" \
  'http://127.0.0.1:8000/v1/projects'

curl -H "Authorization: Bearer $VAULTRAG_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"how does chunking work","k":5}' \
  'http://127.0.0.1:8000/v1/mynotes/search'
```

Interactive API docs are at `http://127.0.0.1:8000/docs`.

### 7. Connect an agent over HTTP

For a remote box, expose it over **Tailscale or WireGuard** — not a published port. The
API binds `127.0.0.1` by default and the compose file publishes it loopback-only, on
purpose. See [security](security.md#network-posture).

### Two image tags

| Tag | Build command | Contains |
|---|---|---|
| `vaultrag:latest` | `docker build -t vaultrag:latest .` | Everything except PDF ingestion. ~757 MB. |
| `vaultrag:formats` | `docker build -t vaultrag:formats --build-arg EXTRAS=--extra=pdf .` | + Docling + OCR (≈2.5 GB more). |

The `formats` build **asserts** that Docling and an OCR engine actually import, so a
broken extra fails the build rather than shipping an image that quietly has no PDF
support. Point compose at it with `EXTRAS: --extra=pdf` in the `build.args` block.

### Optional: observability

```bash
docker compose --profile observability up -d
open http://127.0.0.1:3000    # Grafana — the VaultRAG dashboard is provisioned
open http://127.0.0.1:9090    # Prometheus — /alerts shows rule state
```

To actually get paged, point Alertmanager at a topic you own — the default route goes
nowhere on purpose:

```bash
echo 'VAULTRAG_ALERT_WEBHOOK=https://ntfy.sh/your-random-topic' >> .env
docker compose --profile observability up -d alertmanager
```

Full detail in [operations](operations.md#observability).

---

## Next steps

### Register a second vault

Multi-project is first-class. Each project is its own tenant, enforced server-side.

```bash
uv run vaultrag project add work --vault ~/vaults/work
uv run vaultrag reindex --project work
```

An MCP server with no `--project` flag serves **all** registered projects, and every tool
gains a required `project` argument. Agents call `list_projects` to discover them.

### Turn on the reranker (if your hardware can afford it)

The cross-encoder is the single biggest accuracy lever *and* the single biggest latency
cost. It is **off by default** for latency, not quality.

```bash
export VAULTRAG_RERANK_MODE=auto      # never | auto | always
export VAULTRAG_RERANK_TOP_N=20
```

Measured on Apple Silicon CPU: ~500 ms **per passage**, so `top_n=20` costs ~10 s per
query. `auto` only reranks when the fusion margin says the result is undecided, which
skips most known-item queries. The quality lift is real and measured (MRR@5 0.892 →
0.946 at `auto`@5 on the fixture corpus). Read
[retrieval §stage 4](retrieval.md#stage-4--cross-encoder-rerank) before enabling it.

### Let agents write back

Write-back is **on by default** — that is what closes the memory loop. Agents create
notes under `agent-inbox/`, VaultRAG git-commits them as `agent(claude-code): <what>` and
synchronously reindexes that one file so it is searchable in the same session.

To widen where they may write:

```bash
export VAULTRAG_WRITEBACK_ALLOW_ROOTS='["agent-inbox","decisions"]'
```

To turn it off entirely: `vaultrag mcp --no-write`, or
`VAULTRAG_WRITEBACK_ENABLED=false`.

The full guardrail list is in [security §write-back](security.md#write-back-safety).

### Mine the query log

Every served search writes a row. That log is how retrieval quality improves:

```bash
uv run vaultrag queries --project mynotes --report worst
uv run vaultrag queries --project mynotes --export candidates.json --max-score 0.02
```

`--report worst` first, not `--report zero-hit`: a hybrid retriever ranks *something*
first for any query, so genuinely-empty results are rare. What separates a real answer
from a confident miss is the *score* of the best hit. See
[CLI §queries](cli-reference.md#vaultrag-queries).

---

## Configuration file

All settings come from the environment (prefix `VAULTRAG_`) or a `.env` file beside the
working directory. A minimal one:

```bash
# .env
VAULTRAG_QDRANT_URL=http://localhost:6333
VAULTRAG_OLLAMA_URL=http://localhost:11434
VAULTRAG_EMBED_MODEL=qwen3-embedding:0.6b
VAULTRAG_DEFAULT_TOP_K=10
VAULTRAG_BUDGET_TOKENS=2000
VAULTRAG_LOG_LEVEL=INFO
```

Every setting, its default, and the reasoning behind the default:
[configuration](configuration.md).

---

## Verify the whole thing works

```bash
uv run pytest                      # 923 tests
uv run pytest tests/eval           # the retrieval-quality gate machinery
uv run vaultrag eval               # the gate itself — hermetic, ~10 s, no Docker/Ollama
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src
```

`vaultrag eval` should print `recall@10 1.000 · MRR@5 0.892 · nDCG@10 0.920` and exit 0.
If it does, your install reproduces the reference numbers exactly — the fixture replays
frozen Qwen3 vectors, so the result is deterministic across machines.

---

## Where to go next

- [CLI reference](cli-reference.md) — everything the binary can do
- [MCP tools](mcp-tools.md) — what your agent actually sees
- [Architecture](architecture.md) — how the pieces fit
- [Troubleshooting](troubleshooting.md) — when one of the steps above didn't work
