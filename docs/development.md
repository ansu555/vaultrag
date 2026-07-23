# Development

How to work on VaultRAG: repo layout, the test suite, the merge-blocking quality gate, and
the conventions that keep changes safe.

---

## Setup

```bash
uv sync                    # base install + dev group
uv sync --extra pdf        # + Docling/OCR (heavy — torch)
docker compose up -d qdrant
ollama pull qwen3-embedding:0.6b
```

Full check, matching CI:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest
uv run vaultrag eval
```

> `ruff check` alone is **not** enough — CI runs `ruff format --check` as a separate step,
> and formatting drift fails the build. Run both before calling lint clean.

---

## Repo layout

```
vaultrag/
├── CLAUDE.md              agent guide — read first
├── MEMORY.md              running build log — read second, update at session end
├── MEMORY-p0/p1/p2.md     archived phase logs
├── README.md              front door
├── docs/                  ← you are here
├── pyproject.toml         uv-managed; entrypoint `vaultrag` → cli.py
├── uv.lock                pinned dependency graph
├── Dockerfile             two-stage build; EXTRAS build arg selects the tag
├── docker-compose.yml     qdrant + api + indexer + observability profile
├── .ragignore             example ignore file shipped for vaults
├── .github/workflows/     checks.yml · eval.yml
├── deploy/                prometheus.yml · alerts.yml · alertmanager.yml · grafana/
├── src/vaultrag/          the package (see architecture.md §5)
├── tests/                 33 test modules, 923 tests
│   └── eval/              golden sets, frozen corpus, frozen vectors
└── state/                 sqlite + embed cache (gitignored, disposable)
```

The **design vault** at `../Rag` is the source of truth for *what to build and why*. This
repo is the source of truth for the *running code*. If code and a vault decision disagree,
the vault wins — unless you are deliberately superseding it, and a superseded decision is a
**new note in the vault**, not a silent code drift.

---

## Coding conventions

| Rule | Detail |
|---|---|
| **Python 3.12+, managed with `uv`** | `uv add <pkg>`, `uv run vaultrag …`. No global pip. |
| **Type everything** | Full hints; `mypy --strict` over `src/`. Pydantic models at the `Document`/`Chunk`/config/API boundary. |
| **Async-first for I/O** | FastAPI, the job runner. CPU-bound work (embedding, rerank) goes to a thread/process pool, off the event loop. |
| **Interfaces over vendors** | `Embedder`, `Reranker`, `VectorStore`, `MetaStore` are protocols. A paid API must be a config change, never a code change. |
| **Idempotency is correctness** | Jobs keyed `(project, path, hash)`; point ids `uuid5(project, path, chunk_ix)`; embed cache `(model, dims, chunk_hash)`. Re-running any step must not duplicate or corrupt. |
| **Structured logging** | structlog JSON, **stderr only**. Per-stage timings, `project`, `q_hash`. Never log raw query text at info level; never log secrets. |
| **Lint/format** | `ruff` for both. CI fails on either. |
| **Line length** | 100. |
| **Cite the vault, don't copy it** | `# see ../Rag/decisions/chunking-heading-tree-not-fixed-size.md`, not a paraphrase of its prose. |

### Commit style

```
agent(claude-code): <what changed>      # agent commits
<normal message>                        # human commits
docs(memory): <what>                    # MEMORY.md corrections
```

Branch before committing on `main`. Commit and push only when asked.

---

## The guardrails

Violating one of these is a bug **even if the tests pass**. They are restated here because
they constrain how you write code, not just what it does.

1. **The index is derived; the vault is truth.** Never make the running index the only place
   a fact lives. `vaultrag reindex` from a bare vault must fully rebuild.
2. **Secrets never enter the index.** `.ragignore` **and** the built-in code guard, at parse
   time. There is a test that a planted fake secret is never indexed — extend it when you
   extend the deny list.
3. **Tenancy is server-side.** The server always injects `must project_id ∈ key.scopes`. A
   client filter can only narrow. There is no code path where a client widens scope.
4. **Write-back safety.** Default write root `agent-inbox/`; realpath must stay under the
   vault root; edits require `base_hash` (409 on mismatch, returning current content); size
   and rate caps; `.ragignore`d paths unwritable; every write auto-commits and triggers a
   synchronous reindex of that one file.
5. **Retrieved vault text is untrusted data.** In `snippets` mode VaultRAG never executes or
   interprets it.
6. **Quality is CI-gated.** Any change to chunking parameters, embedding model, fusion
   weights or rerank config must run the eval harness. Regressions block merge.

---

## Tests

**923 tests** across 33 modules.

```bash
uv run pytest                          # everything
uv run pytest tests/test_retrieval.py  # one module
uv run pytest -k rerank                # by name
uv run pytest -rs                      # print skip reasons — CI does this deliberately
```

`-rs` matters: on a base install the real-conversion PDF/OCR tests skip, and **a skip that
scrolls past invisibly is how a suite reports green on code it never ran**.

### Module map

| Area | Modules |
|---|---|
| Parsing | `test_parsing.py`, `test_pdf.py`, `test_diagrams.py`, `test_image.py`, `test_links.py` |
| Chunking | `test_chunking.py`, `test_models.py` |
| Index | `test_index.py`, `test_embedding.py`, `test_store.py` |
| Ingest | `test_scan.py`, `test_ingest_primitive.py`, `test_indexer_pipeline.py`, `test_watch.py`, `test_consistency.py` |
| Retrieval | `test_retrieval.py`, `test_rerank.py`, `test_parents.py`, `test_graph.py`, `test_compress.py`, `test_cite.py` |
| Transports | `test_mcp.py`, `test_api.py`, `test_auth.py`, `test_projects.py`, `test_multiproject.py` |
| Write-back | `test_writeback.py` |
| Ops | `test_metrics.py`, `test_deploy.py`, `test_querylog.py`, `test_cli.py` |
| Eval | `test_eval.py`, `test_ceiling.py` |

### Testing conventions

**Fakes live in `tests/fakes.py`** — a fake vector store, a fake embedder, a dict-backed
parent resolver. Inject them; do not monkeypatch internals.

**Mutation-test anything load-bearing.** A test that passes when you break the code it
claims to cover is worse than no test. The habit that keeps finding real problems here:

```
1. Write the test.
2. Break the production code it targets.
3. Confirm exactly the expected tests fail — no more, no fewer.
4. Restore.
```

Two real examples from this codebase:

- **A parity test that was vacuous.** It compared MCP and REST *responses*, and passed with
  `filters=None` hard-coded into a route, because the fake store returned preset hits
  regardless of filter. Strengthened to assert on the **kwargs** each transport handed the
  engine. Three mutations now die where zero did.
- **A `serve` bug no test could have caught.** `serve` fell back to the current directory
  when unconfigured — in a container, its own install tree. Every existing `serve` test
  handed it a vault, because one that did not would have looked like a broken test.

---

## The eval gate

`tests/eval/` holds the **data**; `src/vaultrag/eval/` holds the harness (the CLI has to
import it, and a console script cannot import from the test tree).

```bash
uv run vaultrag eval                    # the gate — hermetic, ~10 s, no Qdrant/Ollama
uv run vaultrag eval --show-cases       # + per-case rank table
uv run vaultrag eval --golden design    # dogfood the live vault
uv run vaultrag eval --config a.yml --config b.yml
```

| Threshold | Value |
|---|---|
| recall@10 | ≥ **0.90** |
| MRR@5 | ≥ **0.75** |
| nDCG@10 | *reported, not gated* |

Current: **1.000 / 0.892 / 0.920** over 72 cases.

Exit code `0` pass, `1` threshold missed, `2` could not run. That exit code is what makes
`.github/workflows/eval.yml` merge-blocking.

### What's in `tests/eval/`

| Path | What it is |
|---|---|
| `corpus/` | The frozen fixture vault — 27 synthetic notes about a fictional platform. Never drifts. |
| `corpus_golden.json` | **The gate set**: 72 query → note pairs. |
| `golden.json` | The **dogfooding** set: 56 pairs over the live design vault. Drifts on purpose. |
| `vectors.db` | Frozen Qwen3 embeddings (~580 KB) so the gate runs offline and deterministically. |

### Why a fixture and not the real vault

The design vault is edited constantly, so a number computed from it today is not comparable
to one computed next month — the bar would move underneath us. The fixture is frozen, so a
change in the number means a change in **our code**.

It is also written to be *harder*: most queries paraphrase rather than quote the target's
title, several notes cover neighbouring topics so ranking must discriminate, and a few are
bare identifiers that exercise the BM25 half.

### Why frozen vectors

CI has no Ollama and no GPU. But a gate that swapped in a fake embedder would test the
plumbing while telling us nothing about whether **Qwen3** finds the right note. So the real
vectors were recorded once and are replayed.

`ReplayEmbedder` **raises** on a cache miss rather than falling back to a live call or
inventing a vector — a fabricated embedding would make CI green while measuring nothing.

Everything except the dense vectors is real: the gate reindexes `corpus/` through today's
scanner, parser, chunker and payload code on **every run**. A chunking change that hurts
retrieval shows up here.

The one network call is FastEmbed fetching its small BM25 stopword artifact on first use.
CI caches it.

### When you must re-record

Any change that alters chunk text or the embedding model invalidates the fixture, and the
run fails with `no recorded vector for … --record` rather than scoring wrongly.

```bash
ollama serve &
uv run vaultrag eval --record
git add tests/eval/vectors.db
```

Re-record after editing `corpus/`, adding or changing golden queries, changing the chunker or
contextual prefix, or changing `embed_model` / `embed_dims`.

**Recording is incremental** — it reuses every vector whose text is unchanged, so adding
three queries costs three model calls.

A PR that changes chunking is therefore *expected* to carry an updated `vectors.db`, and the
diff in the gate's numbers is exactly the review signal you want.

### Adding golden cases

Append to `corpus_golden.json`, re-record, and check the gate still passes.

Good cases **paraphrase rather than quote** the note's title, and are answerable by exactly
one note. `tests/test_eval.py` asserts every expected path exists and that every corpus note
is somebody's expected answer.

**The best source of new cases is the query log** — real queries retrieval answered badly
are failures nobody wrote a test for:

```bash
vaultrag queries --report worst --limit 30
vaultrag queries --export candidates.json --max-score <cutoff>
```

---

## CI

Two workflows, kept separate so a quality regression and a broken build report as different
failures.

### `checks.yml`

| Job | Runs |
|---|---|
| **ruff + mypy** | `ruff check`, `ruff format --check`, `mypy src` — on the **base install**, no `--extra pdf`. |
| **pytest** | The full suite with `-rs`. |
| **pytest (pdf + ocr extras)** | Only `test_pdf.py` and `test_image.py`, after **asserting** Docling and an OCR engine actually imported. |
| **docker build (base image)** | Builds the image, runs `docker run --rm vaultrag:ci --help`, and validates `docker compose --profile observability config`. |

Three deliberate choices worth understanding:

- **Lint and format are separate steps, not `ruff check --fix`.** CI reports; it never
  rewrites. Formatting drift should be a red check the author fixes locally.
- **The formats job asserts the engines are present before running anything.** Both suites
  gate their real-conversion tests on `skipif(not available())` — if the extra silently
  failed to resolve, those tests would skip and the job would report green having verified
  nothing.
- **The docker job exists because nothing in the test suite can tell whether the image still
  builds.** Without it, a broken Dockerfile is found by whoever deploys next, which on a
  single-box system is the same person, at the worst possible time. `--help` exercises the
  whole CLI import graph without needing Qdrant or Ollama.

### `eval.yml`

The retrieval-quality gate, on every PR — not path-filtered. The job is hermetic and takes
about a minute, and **a path-filtered required check that never runs blocks merges instead
of guarding them.** Cheap and always-on beats clever.

It also runs `pytest tests/test_eval.py`, because a green gate whose machinery is broken is
worse than no gate.

---

## Adding a retrieval stage

The pipeline is a sequencer; each stage is its own module and opts in through a parameter
being non-`None`. To add one:

1. **New module** in `retrieval/`, with a pure function taking snippets and returning
   snippets.
2. **Wire it** into `retrieval/pipeline.py` inside a `time_stage("<name>")` block.
3. **Add the stage label** to `metrics.SEARCH_STAGES` — `test_metrics.py` pins the list.
4. **Add config knobs** to `config.py`, with the reasoning in the docstring, not just the
   description.
5. **Thread it** through `service.py` (so both transports get it) and the eval harness (so
   the gate scores the shipped config).
6. **Measure it.** `--config a.yml --config b.yml` A/B on **both** golden sets. Ship it on
   by default only if it is non-negative on both.
7. **Unit-test the stage in isolation**, and mutation-test the tests.

That order matters. Step 5 is what stops a stage from being measured in a configuration
nobody runs.

---

## MEMORY.md

`MEMORY.md` is the running log of what has been done and what is next. It is the first thing
a new session should read after `CLAUDE.md`.

**At the end of any working session:**

- Bump the **Current state** block (phase, what works, what's next).
- Prepend a dated entry to the **Session log**: what changed, why, key files touched, and
  anything the next session needs (blockers, open questions, decisions made).
- If a design decision was made that isn't in the vault yet, note it **and** flag that it
  should be written back to `../Rag/agent-inbox/`.

Keep it a **log, not a design doc.** Deep rationale belongs in the vault.

> **The git snapshot at session start goes stale.** Anik commits mid-session. Re-check
> `git`/`gh` before believing what MEMORY.md says about branch state.

---

## Adding a dependency

Weigh it **before** committing, and record the weight. Three precedents:

| Dependency | Weight | Verdict |
|---|---|---|
| `prometheus-client` | Pure Python, **zero** transitive deps | Base install — observability should never be an extra someone forgets to enable before an incident. |
| `fastapi` | **+1 package / 904 KB** — starlette, uvicorn, pydantic, anyio already came with fastmcp | Base install. |
| `argon2-cffi` | **+2 packages / ~120 KB** — cffi/pycparser already resolved transitively | Base install. No lighter option hashes secrets correctly: bcrypt truncates past 72 bytes, PBKDF2 has no memory-hardness. |
| `docling` | **~79 packages including torch** | **Optional extra.** Would triple install time for everyone to serve PDF users. |

Declare what you import directly, even if something else already pulls it — depending on
another package's transitive is how an upstream change breaks a command that never mentioned
it.

---

## Related reading

- [Architecture](architecture.md) — the module map in context
- [Design decisions](design-decisions.md) — why the code is shaped this way
- [Benchmarks](benchmarks.md) — the numbers a change must not regress
- `CLAUDE.md` — the agent-facing version of this page
