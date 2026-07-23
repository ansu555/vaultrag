# VaultRAG

Self-hosted, MCP-first RAG backend that coding agents query instead of loading docs
into context — and can write back into. See `CLAUDE.md` for the agent guide and the
Obsidian design vault at `../Rag` for *what to build and why*.

## Status

**P3 — production posture.** Stages 1–6 shipped: real stage-7 compression, a multi-project
registry, the query log and its mining CLI, the REST `/v1` transport, scoped API keys, and
the Docker deployment below. See `MEMORY.md` for exactly where things are and
`../Rag/projects/vaultrag/p3-step-plan.md` for the plan.

## Dev

```bash
uv sync                                  # install deps (+ dev group)
uv sync --extra pdf                      # + Docling/OCR (heavy: torch)
docker compose up -d qdrant              # just the vector store
uv run vaultrag project add mydocs --vault ~/vaults/mydocs
uv run vaultrag reindex --project mydocs
uv run vaultrag mcp                      # MCP over stdio, every registered project
uv run vaultrag serve                    # REST /v1 + /metrics over HTTP
uv run ruff check src tests && uv run mypy src   # lint + types
uv run pytest                            # tests
uv run pytest tests/eval                 # the retrieval-quality gate
```

Register the stdio server with Claude Code:

```bash
claude mcp add vaultrag -- uv run --directory "$PWD" vaultrag mcp
```

## Deployment

One image, two roles (`VAULTRAG_ROLE=api|indexer`), three services. The compose file *is*
the VPS deployment — moving off a laptop changes config, never code
(`../Rag/agent-inbox/vps-deployment-migration-checklist.md`).

```bash
# Ollama runs on the HOST (so it can use a GPU); the containers reach it through
# host.docker.internal. Make sure the embedding model is pulled:
ollama pull qwen3-embedding:0.6b

# Put your vaults under one directory, or point VAULTRAG_VAULTS_DIR at wherever they live.
mkdir -p vaults && cp -R ~/my-notes vaults/

# Register the vault first. `run --rm` (not `exec`) because on an unconfigured box the
# services deliberately refuse to start: `serve` never falls back to its own working
# directory the way `mcp` does. They heal themselves on their next restart once a project
# exists — the entrypoint is `vaultrag`, so the subcommand is all you pass.
docker compose run --rm api project add mydocs --vault /vaults/my-notes
docker compose run --rm api reindex --project mydocs

docker compose up -d --build                    # qdrant + api + indexer
docker compose logs api | head                  # the bootstrap API key is printed once
```

The API is published on `127.0.0.1:8000` only. Every `/v1` route and `/metrics` needs
`Authorization: Bearer <key>`; `/healthz` is the one exception. On a first start with an
empty `api_keys` table the api container mints one admin-everywhere key and prints it —
after that, `vaultrag key add|list|revoke`. Remote access is Tailscale/WireGuard, not a
published port.

```bash
curl -H "Authorization: Bearer $VAULTRAG_KEY" \
  'http://127.0.0.1:8000/v1/mydocs/search?q=how+does+chunking+work&top_k=5'
```

### Two image tags

| Tag | Build | Contains |
|---|---|---|
| `vaultrag:latest` | `docker build -t vaultrag:latest .` | everything but PDF ingestion |
| `vaultrag:formats` | `docker build -t vaultrag:formats --build-arg EXTRAS=--extra=pdf .` | + Docling + OCR (≈2.5 GB more) |

The base image skips PDFs cleanly and indexes the rest of the vault; the `formats` build
asserts Docling and an OCR engine actually import, so a broken extra fails the build rather
than shipping an image that quietly has no PDF support. Point compose at it with
`EXTRAS: --extra=pdf` in the `build.args` block.

### Observability (optional profile)

```bash
docker compose --profile observability up -d
open http://127.0.0.1:3000    # Grafana — the VaultRAG dashboard is provisioned
open http://127.0.0.1:9090    # Prometheus — /alerts shows rule state
```

Prometheus scrapes both roles on the **keyless** exporter (port 9464, compose network only
— it never reaches the host). The api's own `/metrics` serves the identical registry behind
a bearer key. The dashboard, the scrape config and the five alert rules live in `deploy/`
and are checked against the live metric registry by `tests/test_deploy.py`, so a panel or a
rule can never quietly query a series that does not exist.

To actually get notified, point Alertmanager at a topic you own — the default route goes
nowhere on purpose:

```bash
echo 'VAULTRAG_ALERT_WEBHOOK=https://ntfy.sh/your-random-topic' >> .env
docker compose --profile observability up -d alertmanager
```

### Backup and disaster recovery

The **vault's git remote is the backup.** Everything in the `state` and `qdrant_data`
volumes is a derived cache: delete both, `docker compose up -d`, `vaultrag reindex`, and
the system is fully rebuilt from bare markdown. Qdrant snapshots and a SQLite copy are a
restore-*speed* optimization, not a backup — with one exception, `query_log`, which is the
only table nothing can rebuild.

### Running the image without compose

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  -e VAULTRAG_API_HOST=0.0.0.0 \
  -e VAULTRAG_QDRANT_URL=http://host.docker.internal:6333 \
  -e VAULTRAG_OLLAMA_URL=http://host.docker.internal:11434 \
  -v "$PWD/vaults:/vaults:rw" -v vaultrag_state:/state \
  vaultrag:latest serve --role api
```

`VAULTRAG_API_HOST` is not defaulted in the image: the code default is loopback
(`reference-security-model` — binding wider is a decision that must be typed out), and
inside a container that makes the port unreachable. Compose sets it next to the
loopback-only publish that is what actually keeps the API off the network.
