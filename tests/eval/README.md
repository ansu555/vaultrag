# The retrieval quality gate

P2 Stage 1 of `../../../Rag/projects/vaultrag/p2-step-plan.md`. This directory holds the
**data**; the harness itself lives in `src/vaultrag/eval/` (the CLI has to import it, and a
console script cannot import from the test tree).

```bash
uv run vaultrag eval                    # the gate — hermetic, ~10 s, no Qdrant/Ollama
uv run vaultrag eval --show-cases       # with the per-case rank table
uv run vaultrag eval --golden design    # dogfood the live ../Rag index
uv run vaultrag eval --config a.yaml --config b.yaml   # A/B two configs
```

Exit code is `0` when the run clears **recall@10 ≥ 0.9** and **MRR@5 ≥ 0.75**, `1` when it
does not, `2` when it could not run. That exit code is what makes the CI job in
`.github/workflows/eval.yml` merge-blocking (repo `CLAUDE.md` §6).

## What's here

| Path | What it is |
|---|---|
| `corpus/` | The frozen fixture vault — 27 synthetic notes about a fictional platform. Never drifts. |
| `corpus_golden.json` | **The gate set**: 72 `(query → expected note)` pairs over `corpus/`. |
| `golden.json` | The **dogfooding** set: 56 pairs over the live `../Rag` design vault. Drifts on purpose. |
| `vectors.db` | Frozen Qwen3 embeddings (~580 KB) so the gate runs offline and deterministically. |
| `run_eval.py` | Deprecated shim pointing at `vaultrag eval`. |

## Why a fixture corpus and not the design vault

Decision 2 of the P2 plan. The design vault is edited constantly, so a number computed
from it today is not comparable to one computed next month — the bar would move under us.
The fixture corpus is frozen, so a change in the number means a change in **our code**.

It is also written to be harder than the P1 set: most queries are paraphrases that share
no distinctive token with the expected note's title, several notes cover neighbouring
topics so ranking has to discriminate, and a few are bare identifiers (`MER-1422`,
`MERIDIAN_DEDUPE_TTL_SECONDS`) that exercise the BM25 half of hybrid search.

The design-vault set did not go away — it is the dogfooding run, and repo `CLAUDE.md` §1
still treats bad retrieval while building as a real bug report.

## Why frozen vectors

Decision 1. CI has no Ollama and no GPU, but a gate that swaps in a fake embedder tests
the plumbing while telling us nothing about whether **Qwen3** finds the right note. So the
real vectors were recorded once and are replayed: same model behaviour, deterministic,
offline. `ReplayEmbedder` **raises** on a cache miss rather than falling back to a live
call or inventing a vector — a fabricated embedding would make CI green while measuring
nothing.

Everything except the dense vectors is real: the gate reindexes `corpus/` through today's
scanner, parser, chunker and payload code into an in-process Qdrant on every run. That is
the point — a chunking change that hurts retrieval shows up here.

The one network call is FastEmbed fetching its small BM25 stopword artifact on first use
(`index/sparse.py`); CI caches it.

## When you must re-record

**Any change that alters chunk text or the embedding model invalidates the fixture**, and
the run will fail with `no recorded vector for … --record` rather than scoring wrongly.
That is deliberate: silently re-embedding would defeat determinism.

```bash
ollama serve &                       # the only step that needs a model server
uv run vaultrag eval --record        # re-records, then runs the gate
git add tests/eval/vectors.db
```

Re-record after: editing `corpus/`, adding or changing golden queries, changing the
chunker or contextual prefix, or changing `embed_model` / `embed_dims`. Recording is
incremental — it reuses every vector whose text is unchanged, so adding three queries
costs three model calls.

A PR that changes chunking is therefore expected to carry an updated `vectors.db`, and the
diff in the gate's numbers is exactly the review signal you want.

## Adding golden cases

Append to `corpus_golden.json`, re-record, and check the gate still passes. Good cases
paraphrase rather than quote the note's title, and are answerable by exactly one note —
`tests/test_eval.py` asserts every expected path exists and that every corpus note is
somebody's expected answer.
