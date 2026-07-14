# VaultRAG — agent memory / build log

Running log of what's been done and what's next. **Read this right after `CLAUDE.md`** at
the start of a session, and **update it at the end** (instructions in `CLAUDE.md` §7).
This is a log, not a design doc — durable design lives in the vault at `../Rag`.

---

## Current state

- **Phase:** P0 (see `CLAUDE.md` §4). **Steps 0 (scaffold) + 1 (models) + 2 (store) + 3 (scan)
  + 4 (parsing) DONE.** Next up: **Step 5 (heading-tree chunker — `chunking/`)**.
- **What works:** the project builds and runs. `uv sync` installs; `uv run vaultrag --help`
  lists `reindex` + `mcp` (both stubbed, log-and-exit); `ruff check .`, `mypy src` (20 files),
  and `pytest` (70 tests: 4 CLI + 7 models + 13 store + 13 scan + 33 parsing) are all green.
  Env-driven `config.py` (pydantic-settings, `VAULTRAG_` prefix) + structlog JSON logging wired.
  `models.py` holds the normalized `Document{metadata, blocks, links}` / `Block` / `Chunk`
  shape, the `Snippet`/`SearchResult` return contract, and deterministic `chunk_point_id` +
  `document_id`. `store/` has the `MetaStore` protocol + `SQLiteMetaStore` (WAL): P0 tables
  `projects, documents, chunks, jobs, embed_cache, tags`, content-addressed idempotent upserts,
  FK cascade delete, embed-cache keyed `(model, dims, chunk_hash)`. `indexer/` has the vault
  scan: `scan.py` (walk → BLAKE3 → `.ragignore` + secret guard → `diff_manifest` →
  `ScanDiff{creates,updates,deletes,renames,unchanged}`) and `secret_guard.py` (non-overridable
  filename + content secret blocking). `parsing/` now turns one file's raw text into a
  `Document`: `markdown.py` (`parse_document`, markdown-it-py `"default"` preset tokenizer walk
  → source-ordered `Block`s; code/table/list/blockquote kept atomic; heading-trail
  `heading_path`; file-relative `line_start`/`line_end`), `obsidian.py` (wikilink/embed regex,
  inline-tag regex + nested `a/b` expansion, dataview `key:: value`, callout header parsing —
  all the conventions markdown-it doesn't know about), `frontmatter.py` (YAML → typed fields,
  malformed-YAML degrades to no-frontmatter rather than raising). Deps added: `blake3`,
  `pathspec`, `markdown-it-py`, `python-frontmatter`, (`types-pyyaml` dev-only).
- **What's next:** Step 5 — `chunking/`: heading-tree chunker turning each `Document.blocks`
  into indexed `Chunk` children (~300 tok target, 512 hard max, no overlap, merge tiny siblings
  ≥120 tok, never split atomic blocks, contextual prefix «Project ▸ folder/note ▸ H1 ▸ H2» +
  tags line, `parent_id`/`prev_id`/`next_id` stitching). Refs:
  `../Rag/projects/vaultrag/chunking-strategy.md`,
  `../Rag/decisions/chunking-heading-tree-not-fixed-size.md`, arch §6. A token counter needs
  picking (tiktoken vs. a cheap heuristic — not yet decided, check the vault first). Full
  11-step plan: `../Rag/projects/vaultrag/p0-step-plan.md`. Work in order; do not batch.
  Embedder backend for P0 = **Ollama** (behind the `Embedder` interface).
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

### 2026-07-15 — P0 Step 4: markdown parsing → `Document` (`parsing/`)
- **Did:** Completed Step 4. Added `parsing/obsidian.py` (wikilink/embed regex covering all
  four forms — `[[Note]]`/`[[Note|Alias]]`/`[[Note#Heading]]`/`[[Note#^block]]` — plus embeds
  `![[...]]`; inline `#tag`/`#a/b` regex with a lookbehind that excludes ATX headings and
  wikilink anchors; `expand_nested_tags` prefix expansion; dataview `key:: value` line regex;
  callout header parsing `[!type]+/- Title`; link-masking so tag/dataview extraction never
  looks inside a `[[...]]` span), `parsing/frontmatter.py` (YAML → typed fields via
  python-frontmatter, `coerce_str_list`/`coerce_epoch` for YAML's list-or-string-or-scalar and
  auto-parsed-date-or-string inconsistency, `custom_frontmatter` strips the promoted keys so
  nothing lives in two places, malformed YAML degrades to no-frontmatter rather than raising),
  and `parsing/markdown.py` (`parse_document`: markdown-it-py `"default"` preset — verified
  empirically to cover GFM tables with no extra plugin — tokenizes the post-frontmatter body;
  a flat-token-stream walk builds source-ordered `Block`s, treating code/table/list/blockquote
  as atomic by slicing raw source lines via `.map` and skipping past nested tokens rather than
  walking them; a `heading_stack` trail produces each block's `heading_path`; line numbers are
  shifted back to file-relative via `frontmatter_line_offset`).
- **Why:** Step 4 of `../Rag/projects/vaultrag/p0-step-plan.md`
  (`../Rag/projects/vaultrag/parsing-pipeline.md`, arch §5) — the format-agnostic normalization
  every later stage (chunking, embedding, indexing) depends on.
- **Files:** `src/vaultrag/parsing/{obsidian,frontmatter,markdown,__init__}.py`,
  `tests/test_parsing.py` (new, 33 tests), `pyproject.toml`/`uv.lock` (+`markdown-it-py`,
  +`python-frontmatter`, dev +`types-pyyaml`), `MEMORY.md`.
- **Decisions (impl-level, consistent with the vault — no write-back needed):**
  (1) **Redact at the point of extraction, not as a final pass.** Every block's text is checked
  against `secret_guard.scan_for_secrets` *before* it can reach `heading_stack` or the
  wikilink/tag/dataview regexes — not after building the block. An earlier draft redacted as a
  final pass over the finished block list, which had two real leaks: a secret shaped like
  `api_key:: <value>` would already have been captured into `metadata.frontmatter["api_key"]`
  by the dataview regex before the pass ran, and a secret used as heading text would already be
  baked into every descendant block's `heading_path`. Both are covered by regression tests
  (`test_secret_never_leaks_via_dataview_extraction`, `test_secret_never_leaks_via_heading_path`).
  (2) **Redaction granularity is block-level, not line-level** (the MEMORY.md forward-note from
  Step 3 said "line-level" — refined here with reasoning): a PEM private key's body lines don't
  self-identify to `scan_for_secrets` (only the `-----BEGIN...` header line matches a rule), so
  redacting line-by-line would strip the header and leave the base64 key body searchable.
  Quarantining the whole block is what actually closes that gap
  (`test_multiline_secret_body_is_redacted_not_just_header_line`). In the normal pipeline this
  code path never fires at all — Step 3's whole-file gate already withholds any file containing
  a secret pattern before Step 4 ever sees it; this is a backstop for direct/standalone parser
  use (tests, a future watch-mode). (3) **`Link.anchor` is stored verbatim, not slugified** —
  re-reading its docstring ("heading slug or ^block id, the part after '#'") against how it's
  actually used clarified that anchor *resolution* (matching a link's raw anchor text to an
  actual heading's slug) is a retrieval-time concern, not parsing's; inventing a slugify
  function here would have been unrequested scope. (4) **`python-frontmatter`'s `.content` is
  bypassed when no `---` block is present**, using `raw_text` verbatim instead: `.content` does
  a boundary `.strip()` that silently eats leading indentation on a body's first line, corrupting
  a document that opens directly with a 4-space-indented code block (turned it into an ordinary
  paragraph — caught by a failing test, not by inspection). Fixed narrowly for the no-frontmatter
  case only (zero boundary-detection ambiguity there — the whole input *is* the body); the
  with-frontmatter path still trusts `.content` as-is rather than reimplementing boundary
  detection, which would risk a worse failure mode (frontmatter leaking into the body) for a
  narrower bug. (5) **Lists, blockquotes, and tables are one atomic `Block` each**, sliced
  straight from source lines and never walked into — consistent with the chunker (Step 5) needing
  to treat them as unsplittable; container `_open` tokens' `.map` already spans the whole nested
  structure, and the next token back at the *same* nesting `.level` is always the matching
  `_close`, verified empirically against markdown-it-py's real token output for mixed
  ordered/nested-bullet lists. (6) `MarkdownIt("default", {"typographer": False, "html": False})`
  — typographer (smart quotes/dashes) would mutate indexed text away from what's on disk, which
  fights BM25 exact-match on identifiers/error codes (arch principle #5); raw HTML passthrough
  is unneeded in P0 and off by default anyway.
- **Verify (Step 4 gate, all green):** `uv run pytest` 70 passed (4 CLI + 7 models + 13 store +
  13 scan + 33 parsing); `uv run ruff check .` clean; `uv run mypy src` clean (20 files).
  **Required guardrail gate met:** every planted-secret test (`test_secret_in_block_is_redacted`,
  `test_secret_never_leaks_via_dataview_extraction`, `test_secret_never_leaks_via_heading_path`,
  `test_multiline_secret_body_is_redacted_not_just_header_line`) asserts the secret string is
  absent from the *entire serialized `Document`*, not just the one field it was planted in.
  Plus frontmatter typed-field promotion, all four wikilink forms + embeds, nested tag
  expansion/merge, callout vs. plain-blockquote distinction, dataview merge, code/table/list
  atomicity (including a fake heading marker inside a fence proving it isn't tokenized as real),
  heading_path sibling-reset correctness, and file-relative line-number math across the
  frontmatter boundary.
- **Next / handoff:** Start **Step 5 — heading-tree chunker (`chunking/`)**. It consumes
  `Document.blocks` directly; atomic blocks (`Block.is_atomic`) must never be split. Still
  undecided: token-counting approach (tiktoken vs. a cheap word/char heuristic) — check the
  vault before picking one, it wasn't specified in `chunking-strategy.md` at last read. Nothing
  committed yet (still on `main`; branch + `agent(claude-code): …` commit when Anik asks).

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
