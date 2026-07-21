"""How deep does the reranker actually have to look? — the ``rerank_top_n`` ceiling.

A cross-encoder can only promote a chunk it is *shown*. Stage 4 shows it the first
``rerank_top_n`` fused candidates, so for any case whose expected note has no chunk within
that window, reranking is arithmetically incapable of helping — no model run required to
know it. That makes the distribution of **fusion ranks of expected notes** a hard upper
bound on the achievable rerank lift at every ``top_n``, computable with no cross-encoder at
all and therefore in seconds rather than the ~0.5 s x top_n x cases a real sweep costs.

Two things fall out of it, and both matter more than they sound:

* **Where to set the default.** The smallest ``top_n`` whose coverage has saturated is the
  cheapest setting that gives up nothing. Everything above it is pure cost.
* **Why deeper is not safer.** Candidates past the saturation point contain no winnable
  cases but plenty of distractors, and a distractor scored highly enough displaces a
  correct chunk. Measured on the corpus set: MRR@5 was 0.928 at ``top_n=50`` and 0.939 at
  ``top_n=5``. Reviewing more was worse *and* ten times slower.

This is deliberately a **ceiling, not a prediction** — it says what rerank *could* recover,
never what it *will*. Realized lift is always at or below it, and only a real run measures
that. What the ceiling buys is the right to stop running configs that cannot possibly win.

Note the number is per-vault: a small corpus saturates early simply because it is small
(the committed fixture is 51 chunks, so ``top_n=50`` is "score the whole index" and the
saturation it reports is an artifact of corpus size, not evidence about retrieval). Always
read it on a vault whose size resembles the deployment.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from vaultrag.eval.golden import GoldenSet
from vaultrag.eval.harness import SearchStack

# Reported cutoffs. Dense where the decision actually lives (a reranker that has to look 30
# deep is not an interactive feature) and sparse above it, where the curve is flat.
CEILING_CUTOFFS: tuple[int, ...] = (1, 2, 3, 5, 8, 10, 15, 20, 30, 50)
CEILING_DEPTH = 50


@dataclass(frozen=True)
class CeilingReport:
    """Fusion-rank reachability for one golden set: what each ``top_n`` could recover."""

    golden_name: str
    depth: int
    ranks: list[int | None]  # per case: fusion rank of the first expected chunk, None = miss

    @property
    def total(self) -> int:
        return len(self.ranks)

    @property
    def found(self) -> list[int]:
        return [r for r in self.ranks if r is not None]

    @property
    def saturation_rank(self) -> int | None:
        """Deepest fusion rank still holding a winnable case — the smallest sufficient
        ``top_n``. ``None`` when no expected note was reachable at all."""
        return max(self.found) if self.found else None

    def coverage(self, top_n: int) -> float:
        """Share of cases a reranker limited to ``top_n`` could still get right."""
        if not self.ranks:
            return 0.0
        return sum(1 for r in self.found if r <= top_n) / self.total

    def histogram(self) -> dict[int, int]:
        return dict(sorted(Counter(self.found).items()))


def rank_ceiling(
    golden: GoldenSet, stack: SearchStack, *, depth: int = CEILING_DEPTH
) -> CeilingReport:
    """Find, per case, the fusion rank of the first chunk belonging to an expected note.

    Queries the vector store directly rather than going through
    :func:`~vaultrag.retrieval.search`, because the question is about **chunk** order at
    the point stage 4 slices it. ``search`` dedupes to notes and applies parent-swap and
    budget packing, all of which happen after the rerank window has already been cut — so a
    note-level rank would answer a different question than the one ``top_n`` asks.
    """
    ranks: list[int | None] = []
    for case in golden.cases:
        hits = stack.vector_store.query(
            project_id=stack.project_id,
            dense=stack.embedder.embed_query(case.query),
            sparse=stack.sparse.embed_query(case.query),
            limit=depth,
        )
        ranks.append(
            next(
                (
                    i + 1
                    for i, h in enumerate(hits)
                    if str(h.payload.get("path", "")) in case.expected
                ),
                None,
            )
        )
    return CeilingReport(golden_name=golden.name, depth=depth, ranks=ranks)


def format_ceiling(report: CeilingReport) -> str:
    """Render the coverage curve, the saturation point, and the unreachable cases."""
    width = 74
    lines = [
        "=" * width,
        f"rerank_top_n ceiling  ·  {report.golden_name}  ·  {report.total} cases",
        "-" * width,
        f"{'top_n':<10}{'reachable':<14}{'coverage':<12}gain",
    ]
    prev = 0
    for cutoff in CEILING_CUTOFFS:
        if cutoff > report.depth:
            break
        n = sum(1 for r in report.found if r <= cutoff)
        gain = f"+{n - prev}" if n != prev else "—"
        lines.append(f"{cutoff:<10}{f'{n}/{report.total}':<14}{n / report.total:<12.3f}{gain}")
        prev = n
    lines.append("-" * width)

    saturation = report.saturation_rank
    if saturation is None:
        lines.append(f"no expected note reachable within top-{report.depth} — rerank cannot help")
    else:
        lines.append(
            f"saturates at fusion rank {saturation}: a smaller rerank_top_n gives up "
            f"reachable cases, a larger one only adds distractors."
        )
    unreachable = report.total - len(report.found)
    if unreachable:
        lines.append(
            f"{unreachable} case(s) have no expected chunk in top-{report.depth} at all — "
            "those are a fusion/recall problem, not a rerank one."
        )
    lines.append(f"fusion rank histogram: {report.histogram()}")
    lines.append("=" * width)
    return "\n".join(lines)
