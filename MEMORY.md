# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** P0 (see `CLAUDE.md` §4) — *planned, not started*. Next up: **Step 0 (scaffold)**.
- **What works:** nothing yet. Repo is orientation-only (`CLAUDE.md`, `MEMORY.md`).
- **What's next:** P0 is broken into 11 one-at-a-time steps — durable plan lives in the vault
  at `../Rag/projects/vaultrag/p0-step-plan.md` (linked from `build-roadmap.md`). Work them in
  order; do not batch. Embedder backend for P0 = **Ollama** (behind the `Embedder` interface).
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
