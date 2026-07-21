"""The ``rerank_top_n`` ceiling (``eval/ceiling.py``) — what rerank *could* recover.

The coverage math is pure and tested directly; one hermetic run over the committed corpus
pins that it reads real fusion order and agrees with the shipped ``rerank_top_n`` default.
"""

from __future__ import annotations

import pytest

from tests.test_eval import gate_settings
from vaultrag.eval.ceiling import CeilingReport, format_ceiling, rank_ceiling
from vaultrag.eval.golden import VECTOR_FIXTURE_PATH, load_corpus_golden
from vaultrag.eval.harness import replay_stack


def _report(ranks: list[int | None]) -> CeilingReport:
    return CeilingReport(golden_name="t", depth=50, ranks=ranks)


# ---------------------------------------------------------------------------
# Coverage math
# ---------------------------------------------------------------------------
def test_coverage_counts_only_cases_reachable_within_top_n() -> None:
    report = _report([1, 3, 8, None])
    assert report.coverage(1) == pytest.approx(0.25)
    assert report.coverage(3) == pytest.approx(0.50)
    assert report.coverage(8) == pytest.approx(0.75)
    # The unreachable case never becomes reachable, however deep top_n goes.
    assert report.coverage(50) == pytest.approx(0.75)


def test_saturation_rank_is_the_deepest_winnable_case() -> None:
    """The smallest sufficient top_n — the whole point of the measurement."""
    assert _report([1, 2, 14, 3]).saturation_rank == 14


def test_saturation_rank_is_none_when_nothing_is_reachable() -> None:
    assert _report([None, None]).saturation_rank is None


def test_an_empty_golden_set_does_not_divide_by_zero() -> None:
    assert _report([]).coverage(10) == 0.0


def test_format_flags_unreachable_cases_as_a_recall_problem() -> None:
    """A miss at depth 50 is a fusion failure; saying "raise top_n" would be wrong advice."""
    out = format_ceiling(_report([1, None]))
    assert "no expected chunk in top-50" in out
    assert "not a rerank one" in out


# ---------------------------------------------------------------------------
# Against the real committed corpus
# ---------------------------------------------------------------------------
def test_ceiling_on_the_corpus_justifies_the_shipped_top_n() -> None:
    """The shipped default must be at or above where this corpus stops gaining.

    This is the anti-magic-number guard: `rerank_top_n` is a measured quantity, and if
    someone lowers the default below the corpus saturation point the gate should say so
    rather than let a silently-lossy window ship.
    """
    from vaultrag.retrieval.rerank import DEFAULT_TOP_N

    golden = load_corpus_golden()
    with replay_stack(golden, gate_settings(), fixture_path=VECTOR_FIXTURE_PATH) as stack:
        report = rank_ceiling(golden, stack)

    assert report.total == len(golden.cases)
    saturation = report.saturation_rank
    assert saturation is not None
    assert saturation <= DEFAULT_TOP_N, (
        f"corpus needs top_n >= {saturation} but the default is {DEFAULT_TOP_N}"
    )
    assert report.coverage(DEFAULT_TOP_N) == pytest.approx(1.0)
