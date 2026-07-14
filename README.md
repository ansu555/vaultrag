# VaultRAG

Self-hosted, MCP-first RAG backend that coding agents query instead of loading docs
into context — and can write back into. See `CLAUDE.md` for the agent guide and the
Obsidian design vault at `../Rag` for *what to build and why*.

## Status

**P0 — in progress.** See `MEMORY.md` for the current step and the durable step plan at
`../Rag/projects/vaultrag/p0-step-plan.md`.

## Dev

```bash
uv sync                          # install deps (+ dev group)
uv run vaultrag --help           # CLI entrypoint
uv run vaultrag reindex --help   # (stubbed until Step 8)
uv run vaultrag mcp --help       # (stubbed until Step 10)
uv run ruff check . && uv run mypy src   # lint + types
uv run pytest                    # tests
```
