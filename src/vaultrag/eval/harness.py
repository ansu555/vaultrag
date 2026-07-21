"""The eval harness — build a stack, run a golden set through it, score the result.

Two ways to get a searchable stack:

* :func:`replay_stack` — **hermetic**. Reindexes the committed fixture vault into an
  in-process Qdrant (``:memory:``) and a throwaway SQLite manifest, embedding through the
  frozen vectors in ``tests/eval/vectors.db``. No Ollama, no Docker, no network, same
  number every run. This is what CI gates on.
* :func:`live_stack` — **dogfooding**. Points at the real Qdrant and Ollama from
  ``Settings`` and searches whatever is actually indexed. This is what tells us how
  retrieval feels on the vault we use daily.

Both hand back the same :class:`SearchStack`, so :func:`run_eval` does not know or care
which one it is scoring.

**What the harness deliberately does not do.** It passes no ``parent_resolver`` and no
``vault_name``: parent-swap repackages a hit *within* the same note and citation stamps a
URI, so neither can move a path-based ranking metric — including them would add live vault
reads to every case and measure nothing new. It also runs with a large token budget by
default (:data:`EVAL_BUDGET_TOKENS`), because the greedy budget packer can truncate a
result list before rank 10 and we want recall@10 to measure *retrieval*, not packing. Pass
a production-sized budget explicitly to see the end-to-end view instead.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from qdrant_client import QdrantClient

from vaultrag.config import Settings
from vaultrag.embedding.base import Embedder, EmbeddingError
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.embedding.ollama import OllamaEmbedder
from vaultrag.eval.golden import Case, GoldenSet
from vaultrag.eval.metrics import Metrics, aggregate, first_relevant_rank
from vaultrag.eval.replay import ReplayEmbedder, VectorFixture
from vaultrag.index.base import SparseEmbedder, VectorStore
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.index.sparse import Bm25Embedder
from vaultrag.indexer.pipeline import reindex_vault
from vaultrag.retrieval import Reranker, RerankParams, load_reranker, search
from vaultrag.store.sqlite import SQLiteMetaStore

# Big enough that the budget packer never truncates a 10-hit result list. See the module
# docstring: the gate scores ranking, and a packer-induced miss is not a retrieval miss.
EVAL_BUDGET_TOKENS = 1_000_000
# Fallback only. Both stacks take k from ``Settings.default_top_k`` so an A/B overlay can
# move it — and so a config that returns fewer hits is scored honestly rather than being
# quietly topped back up to 10.
EVAL_K = 10


@dataclass
class SearchStack:
    """Everything :func:`~vaultrag.retrieval.search` needs, plus how to tear it down."""

    project_id: str
    vector_store: VectorStore
    embedder: QueryEmbedder
    sparse: SparseEmbedder
    reranker: Reranker | None = None
    rerank_params: RerankParams = field(default_factory=RerankParams)
    k: int = EVAL_K
    budget_tokens: int = EVAL_BUDGET_TOKENS

    def ranked_paths(self, query: str, *, reranker: Reranker | None) -> list[str]:
        """Run one query and return the ordered note paths, deduped in rank order.

        Several chunks of one note can come back; for a note-level golden set the second
        occurrence carries no information, and leaving duplicates in would let a single
        note fill the top 10 and quietly inflate nDCG.
        """
        result = search(
            query,
            project_id=self.project_id,
            vector_store=self.vector_store,
            embedder=self.embedder,
            sparse=self.sparse,
            k=self.k,
            budget_tokens=self.budget_tokens,
            reranker=reranker,
            rerank=self.rerank_params,
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for snippet in result.snippets:
            if snippet.path not in seen:
                seen.add(snippet.path)
                ordered.append(snippet.path)
        return ordered


@dataclass(frozen=True)
class CaseResult:
    """Per-case detail — what the summary table is built from, and what you read when
    a case regresses."""

    case: Case
    rank: int | None  # 1-based rank of the expected note, post-rerank; None = miss
    pre_rank: int | None  # same, in fusion order (identical when rerank is off)
    ranked: list[str]

    @property
    def hit(self) -> bool:
        return self.rank is not None and self.rank <= 10


@dataclass(frozen=True)
class EvalReport:
    """One config scored over one golden set."""

    label: str
    golden_name: str
    metrics: Metrics
    results: list[CaseResult]
    reranked: bool

    @property
    def misses(self) -> list[CaseResult]:
        """Cases whose expected note never made the top 10 — where to look first."""
        return [r for r in self.results if not r.hit]


def run_eval(golden: GoldenSet, stack: SearchStack, *, label: str = "config") -> EvalReport:
    """Score every case in ``golden`` against ``stack``.

    When a reranker is attached, each query runs twice — once through fusion only, once
    reranked. That is not waste: nDCG@10 is defined pre-rerank (it measures whether fusion
    *found* the material at all), while recall@10 and MRR@5 score the order the agent
    actually receives. With rerank off the two passes would be identical, so only one runs.
    """
    reranked = stack.reranker is not None
    results: list[CaseResult] = []
    post_rankings: list[tuple[Sequence[str], frozenset[str]]] = []
    pre_rankings: list[tuple[Sequence[str], frozenset[str]]] = []

    for case in golden.cases:
        final = stack.ranked_paths(case.query, reranker=stack.reranker)
        fusion = stack.ranked_paths(case.query, reranker=None) if reranked else final
        results.append(
            CaseResult(
                case=case,
                rank=first_relevant_rank(final, case.expected),
                pre_rank=first_relevant_rank(fusion, case.expected),
                ranked=final,
            )
        )
        post_rankings.append((final, case.expected))
        pre_rankings.append((fusion, case.expected))

    return EvalReport(
        label=label,
        golden_name=golden.name,
        metrics=aggregate(post_rankings, pre_rerank=pre_rankings),
        results=results,
        reranked=reranked,
    )


# ---------------------------------------------------------------------------
# Stacks
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def replay_stack(
    golden: GoldenSet,
    settings: Settings,
    *,
    fixture_path: Path,
    embedder: Embedder | None = None,
) -> Iterator[SearchStack]:
    """Build the whole index from the fixture vault in memory, then yield a stack.

    ``embedder`` is injectable so ``--record`` can drive the same build through a
    :class:`~vaultrag.eval.replay.RecordingEmbedder` against live Ollama; by default it
    replays the frozen vectors and never touches the network.

    Reindexing per run (rather than committing a prebuilt Qdrant snapshot) is the point:
    it means the gate exercises today's scanner, parser, chunker and payload code. A change
    to chunking that hurts retrieval shows up here, which is precisely what the gate is
    for.
    """
    if golden.vault_path is None:
        raise ValueError(
            f"golden set {golden.name!r} has no vault_path — replay mode needs a committed "
            "corpus (use live mode for the design-vault set)"
        )
    if not golden.vault_path.is_dir():
        raise ValueError(f"corpus vault not found: {golden.vault_path}")

    fixture = VectorFixture(fixture_path) if embedder is None else None
    owned_embedder = embedder or ReplayEmbedder(
        # mypy: fixture is non-None exactly when embedder is None.
        fixture,  # type: ignore[arg-type]
        model=settings.embed_model,
        dims=settings.embed_dims,
    )
    client = QdrantClient(":memory:")
    vector_store = QdrantVectorStore(
        dims=settings.embed_dims,
        collection=settings.qdrant_collection,
        client=client,
    )
    with tempfile.TemporaryDirectory(prefix="vaultrag-eval-") as tmp:
        store = SQLiteMetaStore(Path(tmp) / "eval.db")
        try:
            stats = reindex_vault(
                project_id=golden.project_id,
                vault_path=golden.vault_path,
                store=store,
                vector_store=vector_store,
                embedder=owned_embedder,
            )
            # reindex_vault is deliberately resilient: one unparseable note parks its job
            # and the rest of the vault still indexes. That is right for a live vault and
            # wrong here. A fixture miss makes ReplayEmbedder raise per file, which that
            # resilience would downgrade to "27 of 28 notes indexed" — and the gate would
            # then score a *partial* index and still report PASS, which is the exact
            # silent-green failure ReplayEmbedder exists to prevent. The corpus is
            # committed and known-good, so any failure here means the fixture is stale,
            # not that the note is bad.
            if stats.failed:
                hint = (
                    "the corpus or the embedding model changed since tests/eval/vectors.db "
                    "was recorded — refresh it with: uv run vaultrag eval --record"
                    if embedder is None
                    else "the live embedder failed while recording"
                )
                raise EmbeddingError(
                    f"{stats.failed} of {stats.failed + stats.created} corpus notes failed "
                    f"to index, so the golden set would be scored against an incomplete "
                    f"index: {hint}. See the reindex.ingest_failed log lines above for the "
                    f"offending paths."
                )
            yield SearchStack(
                project_id=golden.project_id,
                vector_store=vector_store,
                embedder=QueryEmbedder(owned_embedder),
                sparse=Bm25Embedder(),
                reranker=_maybe_reranker(settings),
                rerank_params=_rerank_params(settings),
                k=settings.default_top_k,
            )
        finally:
            store.close()
            vector_store.close()
            if fixture is not None:
                fixture.close()


@contextlib.contextmanager
def live_stack(golden: GoldenSet, settings: Settings) -> Iterator[SearchStack]:
    """Search the real running index — Qdrant on its URL, Ollama for query embeds.

    Indexes nothing: it scores whatever ``vaultrag reindex`` last put there, which is the
    honest thing to measure when the question is "how is retrieval doing on my vault".
    """
    vector_store = QdrantVectorStore.from_settings(settings)
    embedder = OllamaEmbedder.from_settings(settings)
    try:
        yield SearchStack(
            project_id=golden.project_id,
            vector_store=vector_store,
            embedder=QueryEmbedder(embedder),
            sparse=Bm25Embedder(),
            reranker=_maybe_reranker(settings),
            rerank_params=_rerank_params(settings),
            k=settings.default_top_k,
        )
    finally:
        embedder.close()
        vector_store.close()


def _rerank_params(settings: Settings) -> RerankParams:
    return RerankParams(
        top_n=settings.rerank_top_n,
        threshold=settings.rerank_threshold,
        dominant_ratio=settings.rerank_dominant_ratio,
    )


def _maybe_reranker(settings: Settings) -> Reranker | None:
    """Load the cross-encoder only when the config asks for it.

    ``load_reranker`` degrades a fetch/load failure to a no-op reranker so a server keeps
    serving. That is right for a server and wrong here: an eval run that silently measured
    fusion while reporting "reranked" would be a lie in a results table, so the harness
    checks and the caller surfaces it.
    """
    if not settings.rerank_enabled:
        return None
    return load_reranker(
        enabled=True,
        repo_id=settings.rerank_model,
        model_file=settings.rerank_model_file,
    )
