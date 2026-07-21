"""Retrieval quality evaluation — the harness behind ``vaultrag eval`` and the CI gate.

P2 Stage 1 (``../Rag/projects/vaultrag/p2-step-plan.md``). This package is the "measurable
quality" spine of the phase: every later change that touches chunking, embedding, fusion or
rerank has to land through the gate it provides, so retrieval quality is a number in CI
rather than a feeling in a dogfooding session (repo ``CLAUDE.md`` §6).

It lives in ``src/`` rather than ``tests/`` for one concrete reason — the CLI has to import
it, and the CLI cannot import from the test tree. The *data* (fixture corpus, golden sets,
frozen vectors) stays under ``tests/eval/``, where it belongs.
"""

from vaultrag.eval.golden import (
    CORPUS_GOLDEN_PATH,
    CORPUS_VAULT_PATH,
    DESIGN_GOLDEN_PATH,
    VECTOR_FIXTURE_PATH,
    Case,
    GoldenSet,
    GoldenSetError,
    load_corpus_golden,
    load_design_golden,
    load_golden,
    verify_paths,
)
from vaultrag.eval.harness import (
    EVAL_BUDGET_TOKENS,
    EVAL_K,
    CaseResult,
    EvalReport,
    SearchStack,
    live_stack,
    replay_stack,
    run_eval,
)
from vaultrag.eval.metrics import (
    MRR_TARGET,
    RECALL_TARGET,
    Metrics,
    aggregate,
    first_relevant_rank,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
)
from vaultrag.eval.overlay import OverlayError, apply_overlay, load_overlay, overlay_label
from vaultrag.eval.replay import (
    RecordingEmbedder,
    ReplayEmbedder,
    VectorFixture,
    text_key,
)
from vaultrag.eval.report import (
    format_cases,
    format_comparison,
    format_misses,
    format_summary,
)

__all__ = [
    "CORPUS_GOLDEN_PATH",
    "CORPUS_VAULT_PATH",
    "DESIGN_GOLDEN_PATH",
    "EVAL_BUDGET_TOKENS",
    "EVAL_K",
    "MRR_TARGET",
    "RECALL_TARGET",
    "VECTOR_FIXTURE_PATH",
    "Case",
    "CaseResult",
    "EvalReport",
    "GoldenSet",
    "GoldenSetError",
    "Metrics",
    "OverlayError",
    "RecordingEmbedder",
    "ReplayEmbedder",
    "SearchStack",
    "VectorFixture",
    "aggregate",
    "apply_overlay",
    "first_relevant_rank",
    "format_cases",
    "format_comparison",
    "format_misses",
    "format_summary",
    "live_stack",
    "load_corpus_golden",
    "load_design_golden",
    "load_golden",
    "load_overlay",
    "ndcg_at_k",
    "overlay_label",
    "recall_at_k",
    "reciprocal_rank_at_k",
    "replay_stack",
    "run_eval",
    "text_key",
    "verify_paths",
]
