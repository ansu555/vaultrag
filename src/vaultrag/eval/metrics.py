"""Ranking metrics for the retrieval quality gate — P2 Stage 1.

Three numbers, each answering a different question about a ranked result list
(``../Rag/projects/vaultrag/evaluation-framework.md``):

* **recall@10** — did the right note come back *at all*? Retrieval health. A miss here is
  a hard failure: no amount of reranking recovers a note that was never fetched.
* **MRR@5** — how *high* did it rank? This is what the agent actually reads, since a
  budgeted response rarely shows more than a handful of snippets.
* **nDCG@10** — position-discounted gain over the whole returned window. Unlike MRR it
  keeps scoring past rank 5, so it registers "we moved the answer from #9 to #6" —
  movement a hard cut at 5 is blind to. Measured **pre-rerank**, where it reads as a
  health check on fusion itself.

Every function takes the ranked paths a search returned plus the set of paths that count
as correct, so they are pure and trivially testable — no search, no index, no I/O. Golden
cases are single-answer today (one ``expected_path``), but the signatures take a *set* so
a multi-answer case is a data change rather than a code change.

Ranks are 1-based throughout: rank 1 is the top hit.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Targets from ../Rag/projects/vaultrag/evaluation-framework.md, and the numbers repo
# CLAUDE.md §6 makes merge-blocking. nDCG has no published target — it is reported for
# trend/diagnosis, not gated, because we have no baseline to justify a threshold yet.
RECALL_TARGET = 0.9
MRR_TARGET = 0.75


def first_relevant_rank(ranked: Sequence[str], relevant: Iterable[str]) -> int | None:
    """1-based rank of the first relevant path in ``ranked``, or ``None`` if absent.

    "First relevant" rather than "the expected one": with a single-answer golden case the
    two coincide, and with a multi-answer case the first hit is what the reciprocal-rank
    definition wants.
    """
    wanted = set(relevant)
    for i, path in enumerate(ranked, start=1):
        if path in wanted:
            return i
    return None


def recall_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the relevant set that appears in the top ``k``.

    With one relevant path this is the binary "did we find it", which is how the golden
    set uses it. Returns 0.0 when nothing is relevant — an empty relevant set is a
    malformed case, and scoring it 1.0 would let a broken case inflate the gate.
    """
    wanted = set(relevant)
    if not wanted:
        return 0.0
    found = sum(1 for path in ranked[:k] if path in wanted)
    return found / len(wanted)


def reciprocal_rank_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """``1/rank`` of the first relevant hit, or 0.0 if it is not in the top ``k``.

    Averaged across cases this is MRR@k. The hard cutoff is deliberate: a result the agent
    never sees inside its budget is worth nothing, not a small fraction.
    """
    rank = first_relevant_rank(ranked, relevant)
    return 1.0 / rank if rank is not None and rank <= k else 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Normalized discounted cumulative gain over the top ``k``, binary relevance.

    Each relevant hit contributes ``1/log2(rank+1)``; the result is divided by the DCG of
    the ideal ordering (every relevant path packed at the top), so a perfect ranking scores
    1.0 regardless of how many answers a case has. Duplicate paths in ``ranked`` are
    counted once — a search that returned the same note twice has not found it twice.
    """
    wanted = set(relevant)
    if not wanted:
        return 0.0

    dcg = 0.0
    seen: set[str] = set()
    for i, path in enumerate(ranked[:k], start=1):
        if path in wanted and path not in seen:
            seen.add(path)
            dcg += 1.0 / math.log2(i + 1)

    ideal_hits = min(len(wanted), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


@dataclass(frozen=True)
class Metrics:
    """Aggregate scores for one config over one golden set."""

    recall_at_10: float
    mrr_at_5: float
    ndcg_at_10: float
    cases: int

    def meets_targets(self) -> bool:
        """True when both **gated** metrics clear their targets (nDCG is reported only)."""
        return self.recall_at_10 >= RECALL_TARGET and self.mrr_at_5 >= MRR_TARGET

    def failures(self) -> list[str]:
        """Human-readable reasons the gate would fail; empty when it passes."""
        reasons = []
        if self.recall_at_10 < RECALL_TARGET:
            reasons.append(f"recall@10 {self.recall_at_10:.3f} < {RECALL_TARGET}")
        if self.mrr_at_5 < MRR_TARGET:
            reasons.append(f"MRR@5 {self.mrr_at_5:.3f} < {MRR_TARGET}")
        return reasons


def aggregate(
    rankings: Sequence[tuple[Sequence[str], Iterable[str]]],
    *,
    pre_rerank: Sequence[tuple[Sequence[str], Iterable[str]]] | None = None,
) -> Metrics:
    """Mean the per-case metrics over a whole golden set.

    ``rankings`` is one ``(ranked_paths, relevant_paths)`` pair per case, in the final
    (post-rerank) order — that is what recall@10 and MRR@5 score, because it is what the
    agent receives. ``pre_rerank`` supplies the fusion-order rankings for nDCG@10; when it
    is omitted (rerank off, so the two orders are identical) nDCG is computed from
    ``rankings``.
    """
    n = len(rankings)
    if n == 0:
        return Metrics(recall_at_10=0.0, mrr_at_5=0.0, ndcg_at_10=0.0, cases=0)

    recall = sum(recall_at_k(r, rel, 10) for r, rel in rankings) / n
    mrr = sum(reciprocal_rank_at_k(r, rel, 5) for r, rel in rankings) / n
    ndcg_source = pre_rerank if pre_rerank is not None else rankings
    ndcg = sum(ndcg_at_k(r, rel, 10) for r, rel in ndcg_source) / len(ndcg_source)
    return Metrics(recall_at_10=recall, mrr_at_5=mrr, ndcg_at_10=ndcg, cases=n)
