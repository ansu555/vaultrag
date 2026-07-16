"""Run the minimal golden set against the live index, fusion vs reranked.

Decision 3 of ``../Rag/projects/vaultrag/p1-step-plan.md``: ship the rerank stage behind
a real number, not a vibe (repo ``CLAUDE.md`` §6), without building the full P2 harness.
For every ``(query -> expected_path)`` pair in ``golden.json`` it runs :func:`search`
twice — once with the reranker off (P0 fusion order) and once with it on — and reports
**recall@10** (did the expected note make the returned set) and **MRR@5** (how high did it
rank, which is what the agent actually reads). Targets from
``projects/vaultrag/evaluation-framework.md``: recall@10 >= 0.9, MRR@5 >= 0.75.

Needs a live Qdrant with the project indexed, Ollama for query embeds, and (for the
reranked run) the bge-reranker model. Not a CI gate — run it by hand::

    docker compose up -d qdrant && ollama serve &
    uv run vaultrag reindex --project vaultrag-design --vault ../Rag   # if not indexed
    uv run python -m tests.eval.run_eval --project vaultrag-design
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from vaultrag.config import load_settings
from vaultrag.embedding.base import EmbeddingError
from vaultrag.embedding.cache import QueryEmbedder
from vaultrag.embedding.ollama import OllamaEmbedder
from vaultrag.index.base import VectorStore, VectorStoreError
from vaultrag.index.qdrant import QdrantVectorStore
from vaultrag.index.sparse import Bm25Embedder
from vaultrag.retrieval import Reranker, RerankParams, load_reranker, search

GOLDEN_PATH = Path(__file__).with_name("golden.json")
RECALL_TARGET = 0.9
MRR_TARGET = 0.75


@dataclass(frozen=True)
class Case:
    query: str
    expected_path: str


@dataclass(frozen=True)
class Metrics:
    recall_at_10: float
    mrr_at_5: float


def load_golden() -> tuple[str, list[Case]]:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    cases = [Case(query=c["query"], expected_path=c["expected_path"]) for c in data["cases"]]
    return data.get("project", "vaultrag-design"), cases


def _first_rank(paths: list[str], expected: str) -> int | None:
    """1-based rank of the first snippet whose path matches ``expected`` (``None`` if absent)."""
    for i, path in enumerate(paths, start=1):
        if path == expected:
            return i
    return None


def evaluate(
    cases: list[Case],
    *,
    project_id: str,
    vector_store: VectorStore,
    embedder: QueryEmbedder,
    sparse: Bm25Embedder,
    reranker: Reranker | None,
    rerank_params: RerankParams,
    k: int,
    label: str,
) -> Metrics:
    recall_hits = 0
    mrr_total = 0.0
    print(f"\n=== {label} ===", flush=True)
    for case in cases:
        result = search(
            case.query,
            project_id=project_id,
            vector_store=vector_store,
            embedder=embedder,
            sparse=sparse,
            k=k,
            reranker=reranker,
            rerank=rerank_params,
        )
        paths = [s.path for s in result.snippets]
        rank = _first_rank(paths, case.expected_path)
        in_recall = rank is not None and rank <= 10
        recall_hits += int(in_recall)
        rr = 1.0 / rank if (rank is not None and rank <= 5) else 0.0
        mrr_total += rr
        marker = f"#{rank}" if rank is not None else "MISS"
        print(f"  {marker:>5}  {case.expected_path:<52}  {case.query[:44]}", flush=True)
    n = len(cases)
    return Metrics(recall_at_10=recall_hits / n, mrr_at_5=mrr_total / n)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=None, help="Project scope (default: golden.json).")
    parser.add_argument("--k", type=int, default=10, help="Snippets per query (default 10).")
    args = parser.parse_args()

    settings = load_settings()
    golden_project, cases = load_golden()
    project_id = args.project or golden_project

    vector_store = QdrantVectorStore.from_settings(settings)
    embedder = QueryEmbedder(OllamaEmbedder.from_settings(settings))
    sparse = Bm25Embedder()
    rerank_params = RerankParams(
        top_n=settings.rerank_top_n,
        threshold=settings.rerank_threshold,
        dominant_ratio=settings.rerank_dominant_ratio,
    )

    try:
        print(f"golden: {len(cases)} cases · project={project_id!r} · k={args.k}")
        fusion = evaluate(
            cases,
            project_id=project_id,
            vector_store=vector_store,
            embedder=embedder,
            sparse=sparse,
            reranker=None,
            rerank_params=rerank_params,
            k=args.k,
            label="fusion (rerank OFF)",
        )
        reranker = load_reranker(
            enabled=True,
            repo_id=settings.rerank_model,
            model_file=settings.rerank_model_file,
        )
        reranked = evaluate(
            cases,
            project_id=project_id,
            vector_store=vector_store,
            embedder=embedder,
            sparse=sparse,
            reranker=reranker,
            rerank_params=rerank_params,
            k=args.k,
            label=f"reranked ({getattr(reranker, 'model', 'none')})",
        )
    except (VectorStoreError, EmbeddingError) as exc:
        print(f"\neval could not run: {exc}", file=sys.stderr)
        print(
            "is Qdrant up (`docker compose up -d qdrant`), Ollama serving, and the project "
            "indexed (`uv run vaultrag reindex --project ... --vault ...`)?",
            file=sys.stderr,
        )
        return 2
    finally:
        vector_store.close()

    print("\n" + "=" * 60)
    print(f"{'config':<24}{'recall@10':>12}{'MRR@5':>12}")
    print(f"{'fusion (off)':<24}{fusion.recall_at_10:>12.3f}{fusion.mrr_at_5:>12.3f}")
    print(f"{'reranked':<24}{reranked.recall_at_10:>12.3f}{reranked.mrr_at_5:>12.3f}")
    print(f"{'targets':<24}{RECALL_TARGET:>12.3f}{MRR_TARGET:>12.3f}")
    print("=" * 60)
    mrr_delta = reranked.mrr_at_5 - fusion.mrr_at_5
    print(f"rerank MRR@5 lift: {mrr_delta:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
