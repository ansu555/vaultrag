"""Rendering eval results as text — what ``vaultrag eval`` prints and CI logs.

Kept separate from the harness so scoring has no opinion about presentation, and so the
gate's exit code is decided by :class:`~vaultrag.eval.metrics.Metrics`, never by
something that formats a table.

The output is designed to be read in a failed CI job by someone who did not write the
change: the summary says pass or fail against the targets, and the miss list names the
queries that broke so the next step is obvious.
"""

from __future__ import annotations

from vaultrag.eval.harness import EvalReport
from vaultrag.eval.metrics import MRR_TARGET, RECALL_TARGET

_WIDTH = 74


def format_cases(report: EvalReport, *, limit: int | None = None) -> str:
    """One line per case: where the expected note ranked, and where it ranked pre-rerank."""
    lines = [f"--- {report.label} · {report.golden_name} ({report.metrics.cases} cases) ---"]
    shown = report.results if limit is None else report.results[:limit]
    for result in shown:
        mark = f"#{result.rank}" if result.rank is not None else "MISS"
        moved = ""
        if report.reranked and result.pre_rank != result.rank:
            before = f"#{result.pre_rank}" if result.pre_rank is not None else "MISS"
            moved = f"  (fusion {before})"
        lines.append(
            f"  {mark:>5}  {result.case.expected_path:<48}  {result.case.query[:40]}{moved}"
        )
    if limit is not None and len(report.results) > limit:
        lines.append(f"  … {len(report.results) - limit} more")
    return "\n".join(lines)


def format_misses(report: EvalReport) -> str:
    """Only the cases that failed recall@10 — the actionable part of a gate failure."""
    misses = report.misses
    if not misses:
        return "no misses: every expected note ranked inside the top 10."
    lines = [f"misses ({len(misses)}/{report.metrics.cases}) — expected note not in top 10:"]
    for result in misses:
        lines.append(f"  {result.case.query}")
        lines.append(f"      want: {result.case.expected_path}")
        lines.append(f"      got:  {', '.join(result.ranked[:3]) or '(nothing)'}")
    return "\n".join(lines)


def format_summary(report: EvalReport) -> str:
    """Metrics table for a single config, with pass/fail against the targets."""
    m = report.metrics
    status = "PASS" if m.meets_targets() else "FAIL"
    lines = [
        "=" * _WIDTH,
        f"{report.label}  ·  {report.golden_name}  ·  {m.cases} cases"
        f"  ·  rerank {report.rerank_mode}"
        f"  ·  graph {'on' if report.graph_expand else 'off'}",
        "-" * _WIDTH,
        f"{'metric':<20}{'value':>12}{'target':>12}   ",
        f"{'recall@10':<20}{m.recall_at_10:>12.3f}{RECALL_TARGET:>12.3f}   "
        f"{_ok(m.recall_at_10 >= RECALL_TARGET)}",
        f"{'MRR@5':<20}{m.mrr_at_5:>12.3f}{MRR_TARGET:>12.3f}   {_ok(m.mrr_at_5 >= MRR_TARGET)}",
        f"{'nDCG@10 (pre-rr)':<20}{m.ndcg_at_10:>12.3f}{'—':>12}   (reported)",
        "-" * _WIDTH,
        f"gate: {status}",
        "=" * _WIDTH,
    ]
    return "\n".join(lines)


def format_comparison(reports: list[EvalReport]) -> str:
    """Side-by-side A/B table; deltas are measured against the first config.

    The first column is the baseline by convention — pass the shipping config first and the
    candidate second, and the delta column reads as "what this change does".
    """
    if not reports:
        return "(no configs to compare)"
    base = reports[0].metrics
    header = f"{'config':<26}{'recall@10':>11}{'MRR@5':>10}{'nDCG@10':>10}{'Δ MRR@5':>10}"
    title = f"A/B · {reports[0].golden_name} · {base.cases} cases"
    lines = ["=" * _WIDTH, title, "-" * _WIDTH, header]
    for i, report in enumerate(reports):
        m = report.metrics
        delta = "" if i == 0 else f"{m.mrr_at_5 - base.mrr_at_5:+.3f}"
        lines.append(
            f"{report.label[:25]:<26}{m.recall_at_10:>11.3f}{m.mrr_at_5:>10.3f}"
            f"{m.ndcg_at_10:>10.3f}{delta:>10}"
        )
    lines += [
        "-" * _WIDTH,
        f"targets: recall@10 >= {RECALL_TARGET}, MRR@5 >= {MRR_TARGET}",
        "=" * _WIDTH,
    ]
    return "\n".join(lines)


def _ok(passed: bool) -> str:
    return "ok" if passed else "BELOW TARGET"
