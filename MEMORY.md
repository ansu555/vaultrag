# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** P0 (see `CLAUDE.md` §4). **Step 0 (scaffold) DONE.** Next up: **Step 1 (core
  data models, `models.py`)**.
- **What works:** the project builds and runs. `uv sync` installs; `uv run vaultrag --help`
  lists `reindex` + `mcp` (both stubbed, log-and-exit); `ruff check .`, `mypy src`, and
  `pytest` (4 CLI smoke tests) are all green. Env-driven `config.py` (pydantic-settings,
  `VAULTRAG_` prefix) + structlog JSON logging wired.
- **What's next:** Step 1 — Pydantic `Document{blocks, metadata, links}`, `Block`, `Chunk`,
  and the `Snippet`/`SearchResult` return shape, in `src/vaultrag/models.py`. Refs:
  `../Rag/reference/reference-metadata-schema.md`, arch §5–§7. Full 11-step plan:
  `../Rag/projects/vaultrag/p0-step-plan.md`. Work in order; do not batch. Embedder backend
  for P0 = **Ollama** (behind the `Embedder` interface).
- **Design source of truth:** `../Rag` (Obsidian vault). Grep it before designing; see
  `CLAUDE.md` §1.
- **Open questions / blockers:** `search`/`open` response JSON shapes are unspecified in
  `../Rag/reference/reference-mcp-tools.md` (params only) — author the cited-snippet contract
  at Step 1 / Step 10, promote to `reference/` if it hardens.

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

### 2026-07-14 — P0 Step 0: project scaffold
- **Did:** Scaffolded the Python project. `pyproject.toml` (uv-managed, hatchling, src
  layout, entrypoint `vaultrag → vaultrag.cli:main`); deps `pydantic-settings`, `structlog`,
  `typer` + dev group `ruff/mypy/pytest`. Added `config.py` (env-driven `Settings`,
  `VAULTRAG_` prefix: vault path, project_id, state dir, qdrant/ollama urls, embed model/dims,
  retrieval defaults, log level), `logging.py` (structlog JSON, idempotent `configure_logging`),
  `cli.py` (Typer app with stubbed `reindex`/`mcp` + `--version`). Created the 8 empty module
  packages (`parsing chunking embedding index store indexer retrieval mcp`), each with a
  docstring + step pointer. Shipped `.gitignore` (`state/`, `.venv/`, tool caches), `.ragignore`
  (secret-deny + Obsidian/media globs), `README.md`, and 4 CLI smoke tests.
- **Why:** Step 0 of `../Rag/projects/vaultrag/p0-step-plan.md`. Establishes the role/interface
  skeleton so later steps drop into fixed module homes (arch §4–§11 ordering).
- **Files:** `pyproject.toml`, `uv.lock`, `.gitignore`, `.ragignore`, `README.md`,
  `src/vaultrag/{__init__,config,logging,cli}.py`, `src/vaultrag/{parsing,chunking,embedding,index,store,indexer,retrieval,mcp}/__init__.py`,
  `tests/{__init__,test_cli}.py`.
- **Decisions:** CLI framework = **Typer** (type-first, pairs with FastAPI/pydantic; not named
  in the vault stack — flag for `../Rag/agent-inbox/` if it hardens). uv resolved a **Python
  3.12** interpreter (`requires-python >=3.12`, ruff/mypy `target-version = py312`).
- **Verify (Step 0 gate, all green):** `uv run vaultrag --help` lists `reindex` + `mcp`;
  `uv run ruff check .` clean; `uv run mypy src` clean (12 files); `uv run pytest` 4 passed.
- **Next / handoff:** Start **Step 1 — core data models (`models.py`)**. Nothing committed yet
  (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-14 — P0 broken into steps + plan locked in vault
- **Did:** Gathered P0 design from the vault (arch doc + subsystem + reference + decisions
  notes) and decomposed P0 into **11 implement-one-at-a-time steps** (scaffold → models →
  SQLite store → scan+secret-guard → md parse → heading chunker → Ollama embedder → Qdrant
  store → reindex pipeline → hybrid retrieval → FastMCP `search`/`open`). Locked the durable
  plan as a new vault project-log note and pointed this log at it.
- **Why:** User asked to break P0 into steps (one at a time) and record it in the better home.
  Durable "what to build" plans belong in the vault (`CLAUDE.md` §7); MEMORY.md stays a log.
  Decisions this session: decompose **P0 only** for now (later phases get their own
  `pN-step-plan` sibling when they start); P0 embedder = **Ollama** behind the interface.
- **Files:** `../Rag/projects/vaultrag/p0-step-plan.md` (new), `../Rag/projects/vaultrag/build-roadmap.md`
  (linked + Log line), this `MEMORY.md`. No code yet. Neither git repo committed (awaiting ask).
- **Next / handoff:** Start **Step 0 — Project scaffold** (`uv init`, `pyproject.toml`,
  `src/vaultrag/{cli.py,config.py}`, ruff/mypy, `.gitignore`, `.ragignore`). Per-step verify +
  a MEMORY.md entry before moving on. Full step detail: `../Rag/projects/vaultrag/p0-step-plan.md`.

### 2026-07-14 — Repo scaffolding + agent guide
- **Did:** Created the `vaultrag/` implementation folder as a sibling of the design vault
  (`../Rag`). Added `CLAUDE.md` (agent guide: how to fetch design from the vault, the
  stack, roadmap, conventions, guardrails) and this `MEMORY.md`.
- **Why:** Give any coding agent a stable orientation so it reads the vault instead of
  re-deriving decisions, and a handoff log so sessions don't lose context.
- **Files:** `CLAUDE.md`, `MEMORY.md`.
- **Next / handoff:** Initialize the Python project (`uv init`, `pyproject.toml`, entry
  point `vaultrag`), add `docker-compose.yml` for Qdrant, then begin P0. Confirm exact
  schemas/tool signatures against `../Rag/reference/` before writing code. No git repo
  initialized here yet — `git init` when ready to start committing (`agent(...)` style).
