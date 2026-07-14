# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** P0 (see `CLAUDE.md` §4). **Steps 0 (scaffold) + 1 (models) + 2 (store) + 3 (scan) DONE.**
  Next up: **Step 4 (markdown parsing → `Document` — `parsing/`)**.
- **What works:** the project builds and runs. `uv sync` installs; `uv run vaultrag --help`
  lists `reindex` + `mcp` (both stubbed, log-and-exit); `ruff check .`, `mypy src` (17 files),
  and `pytest` (37 tests: 4 CLI + 7 models + 13 store + 13 scan) are all green. Env-driven
  `config.py` (pydantic-settings, `VAULTRAG_` prefix) + structlog JSON logging wired.
  `models.py` holds the normalized `Document{metadata, blocks, links}` / `Block` / `Chunk`
  shape, the `Snippet`/`SearchResult` return contract, and deterministic `chunk_point_id` +
  `document_id`. `store/` has the `MetaStore` protocol + `SQLiteMetaStore` (WAL): P0 tables
  `projects, documents, chunks, jobs, embed_cache, tags`, content-addressed idempotent upserts,
  FK cascade delete, embed-cache keyed `(model, dims, chunk_hash)`. `indexer/` now has the
  vault scan: `scan.py` (walk → BLAKE3 → `.ragignore` + secret guard → `diff_manifest` →
  `ScanDiff{creates,updates,deletes,renames,unchanged}`) and `secret_guard.py` (non-overridable
  filename + content secret blocking). Deps added: `blake3`, `pathspec`.
- **What's next:** Step 4 — `parsing/`: markdown-it-py + python-frontmatter + custom Obsidian
  rules (wikilinks/embeds/callouts/dataview) → the normalized `Document{metadata, blocks, links}`
  from `models.py`. Refs: `../Rag/projects/vaultrag/` chunking/parsing notes + arch §5. The
  scan feeds Step 4/8: `scan_vault(vault, store.document_manifest(project))` returns the diff;
  **enqueuing jobs + applying renames as metadata-only updates is deferred to Step 8** (needs the
  job runner + parser) — scan only *produces* the diff. Full 11-step plan:
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

### 2026-07-15 — P0 Step 3: vault scan + manifest diff + secret guard (`indexer/`)
- **Did:** Completed Step 3. Added `indexer/secret_guard.py` (the non-overridable half of
  the secrets barrier: `is_secret_filename` — `.env*`/`id_rsa*`/`*.pem`/`*credentials*` +
  key-material globs; `scan_for_secrets` — high-precision content scan: PEM private-key
  blocks, AWS/GitHub/Slack/Google/Stripe/JWT tokens, plus a keyword-anchored high-entropy
  assignment rule gated on Shannon entropy so git SHAs/UUIDs/placeholders don't trip it;
  **returns only the rule name, never the secret**). Added `indexer/scan.py`: `walk_vault`
  (os.walk, symlinks off, prunes `.git/.obsidian/.trash/.smart-env/node_modules` + `.ragignore`
  dirs, markdown-only, both secret gates, BLAKE3 per file) → `ScannedFile`; `diff_manifest`
  (pure) → `ScanDiff{creates,updates,deletes,renames,unchanged}` with rename recovery (a create
  whose hash matches a delete becomes a `Rename`, one-to-one/greedy); `scan_vault` wrapper
  Step 8 will call with `store.document_manifest(project)`. Exported the API from
  `indexer/__init__.py`.
- **Why:** Step 3 of `../Rag/projects/vaultrag/p0-step-plan.md`; boot-reconcile front of the
  indexer (`../Rag/projects/vaultrag/ingestion-pipeline.md`, arch §4). Secret guardrail is
  `CLAUDE.md` §6 / `reference-security-model.md` (first-class threat). Full-rebuild property
  (`CLAUDE.md` §6): empty manifest → every file a create, so `reindex` from a bare vault rebuilds.
- **Files:** `src/vaultrag/indexer/{scan,secret_guard,__init__}.py`, `tests/test_scan.py` (new,
  13 tests), `pyproject.toml`/`uv.lock` (+`blake3`, +`pathspec`), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **Scope boundary:** scan *produces* the `ScanDiff`; enqueuing jobs + applying renames as
  metadata-only updates (vectors untouched) is Step 8's job runner — kept out of Step 3 to honor
  "work in order, don't batch". Renames are surfaced first-class in the diff so Step 8 can act.
  (2) `diff_manifest` is a **pure function** (manifest in, diff out) → the classifier is tested
  without a filesystem. A `_MISSING` sentinel distinguishes "path absent" (create) from
  "path present with NULL hash" (a half-recorded doc → treated as update, never a rename source).
  (3) **Content secret-block is whole-file at this boundary** for P0 (a note carrying live-looking
  key material never enters the index); line-level *redaction* is deferred to Step 4 parsing.
  (4) Directory-level `.ragignore` prunes aren't counted in `ScanStats.ignored` (their files are
  never visited — that's the perf win); only file-level ignores increment it. (5) pathspec API:
  used the non-deprecated `gitignore`/`GitIgnoreSpecPattern` (the `gitwildmatch` factory is
  deprecated in pathspec 1.1.1 and emitted warnings).
- **Verify (Step 3 gate, all green):** `uv run pytest` 37 passed (4 CLI + 7 models + 13 store +
  13 scan); `uv run ruff check .` clean; `uv run mypy src` clean (17 files). **Required
  guardrail gate met:** `test_secret_file_never_indexed` (files named `.env`/`id_rsa`/`*.pem`/
  `*credentials*` never surface) and `test_note_with_embedded_secret_never_indexed` (markdown
  notes embedding an AWS key / PEM block / high-entropy assignment are all withheld;
  `secrets_blocked == 3`). Plus content-addressed hashing, ragignore, rename detection, NULL-hash
  update, and the empty-manifest→all-creates full-rebuild property.
- **Next / handoff:** Start **Step 4 — markdown parsing → `Document` (`parsing/`)**. Reuse
  `scan_for_secrets` there for line-level redaction (the deferred half of decision #3). Nothing
  committed yet (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 2: metadata store (SQLite `MetaStore`, `store/`)
- **Did:** Completed Step 2. Added `store/base.py` (the `MetaStore` `Protocol` +
  `runtime_checkable`, row dataclasses `ProjectRow/DocumentRow/ChunkRow/JobRow`, `JobState`/
  `JobKind` enums) and `store/sqlite.py` (`SQLiteMetaStore`, WAL). DDL for **P0 tables only**
  — `projects, documents, chunks, jobs, embed_cache, tags` (deferred `links, api_keys,
  query_log`) — written to carry to Postgres unchanged. Methods: project/document upsert +
  read, `document_manifest` (scan-diff source for Step 3), `upsert_chunks`/`chunk_hashes`/
  `get_chunk`, `set_tags`, jobs enqueue/state, embed-cache get/put (batched IN, chunked at
  900 vars). Added `document_id(project, path)` to `models.py` alongside `chunk_point_id`.
- **Why:** Step 2 of `../Rag/projects/vaultrag/p0-step-plan.md`. Holds structure + ops state
  (vectors live in Qdrant; this is a derived cache). Schema from
  `../Rag/reference/reference-database-schema.md`; SQLite-WAL choice from
  `../Rag/decisions/metadata-store-sqlite-to-postgres.md`.
- **Files:** `src/vaultrag/store/{base,sqlite,__init__}.py`, `src/vaultrag/models.py`
  (+`document_id`), `tests/test_store.py` (new, 13 tests), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault schema — no write-back needed):**
  (1) Identity is content-addressed: `documents.id = uuid5(project, path)`,
  `chunks.id = uuid5(project, path, ix)` → every write is an idempotent upsert, delete-by-doc
  is a filter. (2) Document/chunk upserts use `ON CONFLICT DO UPDATE`, **never `INSERT OR
  REPLACE`** — a REPLACE would cascade-delete child chunks/tags (regression-guarded by
  `test_reupserting_document_preserves_its_chunks`). (3) `PRAGMA foreign_keys=ON` +
  `ON DELETE CASCADE` so `delete_document` cleans chunks+tags. (4) `JobKind.ingest`
  (not `index`) to avoid shadowing `str.index` under StrEnum.
- **Verify (Step 2 gate, all green):** `uv run pytest` 24 passed (4 CLI + 7 models + 13
  store); `uv run ruff check .` clean; `uv run mypy src` clean (15 files). Store tests cover
  round-trips, tag replacement, chunk re-embed-in-place, cascade delete, job idempotency,
  embed-cache dims-scoping, and `isinstance(store, MetaStore)`.
- **Next / handoff:** Start **Step 3 — vault scan + manifest diff + secret guard
  (`indexer/scan.py`)**. Diff against `store.document_manifest(project)`. The planted-fake-
  secret test is a required guardrail gate (`CLAUDE.md` §6). Nothing committed yet (still on
  `main`; branch + `agent(claude-code): …` commit when Anik asks).

### 2026-07-15 — P0 Step 1: core data models (`models.py`)
- **Did:** Completed Step 1. `models.py` had been drafted in a prior session but was broken
  (imported `StrEnum`, declared the enums as `class X(str, Enum)` with `Enum` never imported →
  `NameError` on import). Fixed the three enums (`DocType`, `BlockType`, `LinkKind`) to subclass
  `StrEnum` (the 3.12 idiom, consistent with the existing import), and replaced two ambiguous
  en-dashes (`–`) in the module docstring with hyphens to satisfy ruff `RUF002`. Models now
  import and the whole shape is exercised by `tests/test_models.py` (round-trip in dict + JSON
  modes, block atomicity, `embed_text` prefix, deterministic scoped `point_id`, `extra=forbid`,
  `SearchResult` shape).
- **Why:** Step 1 of `../Rag/projects/vaultrag/p0-step-plan.md` — the one normalized
  `Document{metadata, blocks, links}` / `Block` / `Chunk` / `Snippet`/`SearchResult` shape every
  later stage depends on (arch §5, ordering constraint #1). Fields sourced from
  `reference-metadata-schema.md` + `reference-database-schema.md`.
- **Files:** `src/vaultrag/models.py`, `tests/test_models.py` (both pre-existing this session;
  edited `models.py` only), `MEMORY.md`.
- **Decisions:** Authored the P0 cited-snippet return contract inline as `Snippet` +
  `SearchResult` (the open question from the step plan — `reference-mcp-tools.md` specifies
  params only). If it hardens through Step 10, promote it to `../Rag/reference/`.
- **Verify (Step 1 gate, all green):** `uv run pytest` 11 passed (4 CLI + 7 models);
  `uv run ruff check .` clean; `uv run mypy src` clean (13 files).
- **Next / handoff:** Start **Step 2 — metadata store (SQLite `MetaStore`, `store/`)**. Nothing
  committed yet (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

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
