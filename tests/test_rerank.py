"""P1 Stage 1 verify: the cross-encoder rerank stage (``retrieval/rerank.py``).

The real model (bge-reranker-v2-m3 ONNX) is exercised end-to-end by hand during
dogfooding; here we pin the *stage policy* with a fake reranker — reordering, the 0.3
threshold, the known-item fast path, `keep` capping, and graceful degradation — plus the
composition-root `load_reranker` fallback. Nothing here touches the network or a model.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeReranker
from vaultrag.config import RerankMode
from vaultrag.index.base import ScoredChunk
from vaultrag.retrieval.rerank import (
    NoOpReranker,
    RerankError,
    RerankParams,
    load_reranker,
    rerank_candidates,
    should_rerank,
)


def _cand(point_id: str, score: float, text: str) -> ScoredChunk:
    return ScoredChunk(point_id=point_id, score=score, payload={"text": text})


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------
def test_rerank_reorders_a_fused_set_by_cross_encoder_score() -> None:
    """The best fusion hit is not the best rerank hit — rerank must reorder."""
    cands = [
        _cand("a", 0.30, "off-topic paragraph about compose files"),
        _cand("b", 0.20, "the answer: dense plus bm25 fused by rrf"),
        _cand("c", 0.10, "another off-topic note"),
    ]
    # a/c stay above the 0.3 threshold so this isolates *reordering*, not dropping; their
    # equal scores keep fusion order (stable sort), so b is promoted over both.
    reranker = FakeReranker({"the answer: dense plus bm25 fused by rrf": 0.9}, default=0.5)
    # dominant_ratio=0 disables the fast path so we always exercise the model.
    out = rerank_candidates("q", cands, reranker, keep=10, params=RerankParams(dominant_ratio=0))
    assert [c.point_id for c in out] == ["b", "a", "c"]
    assert out[0].score == pytest.approx(0.9)


def test_exact_identifier_query_still_ranks_first_after_rerank() -> None:
    """A verbatim identifier chunk the cross-encoder scores highest lands at #1."""
    cands = [
        _cand("prose", 0.40, "some prose that vaguely mentions errors"),
        _cand("ident", 0.35, "raise VectorStoreError('cannot reach Qdrant')"),
    ]
    reranker = FakeReranker({"raise VectorStoreError('cannot reach Qdrant')": 0.95}, default=0.5)
    out = rerank_candidates(
        "VectorStoreError", cands, reranker, keep=10, params=RerankParams(dominant_ratio=0)
    )
    assert out[0].point_id == "ident"


# ---------------------------------------------------------------------------
# Threshold — drop garbage, do not pad
# ---------------------------------------------------------------------------
def test_sub_threshold_candidates_are_dropped_not_padded() -> None:
    cands = [_cand("a", 0.5, "good"), _cand("b", 0.4, "garbage"), _cand("c", 0.3, "more garbage")]
    reranker = FakeReranker({"good": 0.8}, default=0.05)  # b, c below the 0.3 threshold
    out = rerank_candidates(
        "q", cands, reranker, keep=10, params=RerankParams(threshold=0.3, dominant_ratio=0)
    )
    assert [c.point_id for c in out] == ["a"]


def test_all_below_threshold_returns_empty_an_honest_no_result() -> None:
    cands = [_cand("a", 0.5, "x"), _cand("b", 0.4, "y")]
    reranker = FakeReranker(default=0.1)
    out = rerank_candidates(
        "q", cands, reranker, keep=10, params=RerankParams(threshold=0.3, dominant_ratio=0)
    )
    assert out == []


def test_default_threshold_is_no_floor_so_reorder_never_drops() -> None:
    """Default RerankParams has no absolute floor (0.0): rerank reorders + caps to keep, but
    never evicts on score alone — the measured fix for the vault's 0.3 recall regression
    (see agent-inbox/rerank-threshold-should-be-per-vault-not-global.md)."""
    assert RerankParams().threshold == 0.0
    cands = [_cand("a", 0.5, "x"), _cand("b", 0.4, "y")]
    reranker = FakeReranker(default=0.001)  # would be dropped by any positive floor
    out = rerank_candidates("q", cands, reranker, keep=10, params=RerankParams(dominant_ratio=0))
    assert {c.point_id for c in out} == {"a", "b"}


def test_keep_caps_the_number_of_survivors() -> None:
    cands = [_cand(str(i), 0.5, f"body {i}") for i in range(5)]
    reranker = FakeReranker(default=0.9)  # all pass threshold
    out = rerank_candidates("q", cands, reranker, keep=2, params=RerankParams(dominant_ratio=0))
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Known-item fast path
# ---------------------------------------------------------------------------
def test_dominant_top1_fusion_score_skips_the_model() -> None:
    cands = [_cand("a", 1.0, "x"), _cand("b", 0.4, "y"), _cand("c", 0.2, "z")]
    reranker = FakeReranker()
    out = rerank_candidates("q", cands, reranker, keep=10, params=RerankParams(dominant_ratio=1.5))
    assert reranker.calls == []  # model never called
    assert [c.point_id for c in out] == ["a", "b", "c"]  # fusion order preserved


def test_non_dominant_top1_does_not_trigger_the_fast_path() -> None:
    cands = [_cand("a", 0.50, "x"), _cand("b", 0.45, "y")]  # ratio 1.11 < 1.5
    reranker = FakeReranker(default=0.9)
    rerank_candidates("q", cands, reranker, keep=10, params=RerankParams(dominant_ratio=1.5))
    assert len(reranker.calls) == 1  # model was called


# ---------------------------------------------------------------------------
# Graceful / passthrough paths
# ---------------------------------------------------------------------------
def test_none_reranker_passes_candidates_through_trimmed_to_keep() -> None:
    cands = [_cand(str(i), 1.0 - i * 0.1, f"b{i}") for i in range(5)]
    out = rerank_candidates("q", cands, None, keep=3, params=RerankParams())
    assert [c.point_id for c in out] == ["0", "1", "2"]


def test_noop_reranker_preserves_fusion_order_and_drops_nothing() -> None:
    cands = [_cand("a", 0.9, "x"), _cand("b", 0.5, "y"), _cand("c", 0.1, "z")]
    out = rerank_candidates(
        "q", cands, NoOpReranker(), keep=10, params=RerankParams(dominant_ratio=0)
    )
    assert [c.point_id for c in out] == ["a", "b", "c"]
    assert all(c.score == 1.0 for c in out)


def test_single_candidate_is_returned_without_calling_the_model() -> None:
    reranker = FakeReranker()
    out = rerank_candidates("q", [_cand("a", 0.5, "x")], reranker, keep=10, params=RerankParams())
    assert [c.point_id for c in out] == ["a"]
    assert reranker.calls == []


def test_top_n_bounds_what_is_sent_to_the_model() -> None:
    cands = [_cand(str(i), 0.5, f"b{i}") for i in range(10)]
    reranker = FakeReranker(default=0.9)
    rerank_candidates("q", cands, reranker, keep=10, params=RerankParams(top_n=3, dominant_ratio=0))
    _, passages = reranker.calls[0]
    assert passages == ["b0", "b1", "b2"]


def test_top_n_below_keep_still_returns_keep_results() -> None:
    """`top_n` is a cost knob, not a recall cap — the unreviewed tail backfills to `keep`.

    Guards the confound that would otherwise make a `rerank_top_n` sweep unreadable: at
    top_n < k a shallow rerank must cost review *depth*, never results, or every cheap
    config scores worse for reasons that have nothing to do with the cross-encoder.
    """
    cands = [_cand(str(i), 0.5 - i / 100, f"b{i}") for i in range(10)]
    reranker = FakeReranker({"b2": 0.9}, default=0.1)
    out = rerank_candidates(
        "q", cands, reranker, keep=10, params=RerankParams(top_n=3, dominant_ratio=0)
    )
    assert len(out) == 10
    # Reviewed top-3 first (b2 promoted), then the untouched tail in fusion order.
    assert [c.point_id for c in out] == ["2", "0", "1", "3", "4", "5", "6", "7", "8", "9"]


def test_threshold_does_not_drop_the_unreviewed_tail() -> None:
    """A candidate the model never scored has no score to fail the threshold with."""
    cands = [_cand(str(i), 0.5 - i / 100, f"b{i}") for i in range(5)]
    reranker = FakeReranker(default=0.01)  # everything reviewed scores below the floor
    out = rerank_candidates(
        "q", cands, reranker, keep=10, params=RerankParams(top_n=2, threshold=0.3, dominant_ratio=0)
    )
    assert [c.point_id for c in out] == ["2", "3", "4"]


def test_score_count_mismatch_raises() -> None:
    class BrokenReranker:
        model = "broken"

        def score(self, query: str, passages: list[str]) -> list[float]:
            return [0.5]  # wrong length

    cands = [_cand("a", 0.5, "x"), _cand("b", 0.4, "y")]
    with pytest.raises(RerankError):
        rerank_candidates(
            "q", cands, BrokenReranker(), keep=10, params=RerankParams(dominant_ratio=0)
        )


# ---------------------------------------------------------------------------
# Composition-root loader
# ---------------------------------------------------------------------------
def test_load_reranker_returns_none_when_disabled() -> None:
    assert load_reranker(mode=RerankMode.never) is None


def test_load_reranker_falls_back_to_noop_when_the_model_cannot_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that can't be fetched/opened must degrade to NoOp, never raise — the server
    still comes up rerank-off. Monkeypatched so the test is hermetic (no HF network)."""

    def _boom(**_: object) -> object:
        raise RerankError("simulated offline / missing export")

    monkeypatch.setattr("vaultrag.retrieval.rerank.BgeReranker.load", _boom)
    assert isinstance(load_reranker(mode=RerankMode.always), NoOpReranker)


# ---------------------------------------------------------------------------
# RerankMode (P2): the user's choice, and `auto`'s per-query decision
# ---------------------------------------------------------------------------
def test_always_forces_the_gate_open() -> None:
    params = RerankParams.for_mode(RerankMode.always, dominant_ratio=1.5)
    assert params.dominant_ratio == 0
    assert should_rerank([_cand("a", 10.0, "x"), _cand("b", 0.1, "y")], params)


def test_auto_keeps_the_configured_decision_boundary() -> None:
    assert RerankParams.for_mode(RerankMode.auto, dominant_ratio=1.5).dominant_ratio == 1.5


def test_auto_skips_the_model_when_fusion_is_confident() -> None:
    # top1 is 10x top2 — both retrievers agree; a cross-encoder can only do damage.
    params = RerankParams.for_mode(RerankMode.auto, dominant_ratio=1.5)
    assert not should_rerank([_cand("a", 1.0, "x"), _cand("b", 0.1, "y")], params)


def test_auto_reranks_when_fusion_is_undecided() -> None:
    params = RerankParams.for_mode(RerankMode.auto, dominant_ratio=1.5)
    assert should_rerank([_cand("a", 1.0, "x"), _cand("b", 0.95, "y")], params)


def test_auto_does_not_claim_confidence_without_a_runner_up() -> None:
    params = RerankParams.for_mode(RerankMode.auto, dominant_ratio=1.5)
    # A zero-scored second place is not evidence the first is right.
    assert should_rerank([_cand("a", 1.0, "x"), _cand("b", 0.0, "y")], params)
    # ...but a lone candidate has nothing to reorder.
    assert not should_rerank([_cand("a", 1.0, "x")], params)


def test_mode_survives_a_round_trip_through_settings() -> None:
    from vaultrag.config import Settings
    from vaultrag.retrieval.rerank import rerank_params_for

    auto = rerank_params_for(Settings(rerank_mode=RerankMode.auto, rerank_dominant_ratio=2.0))
    always = rerank_params_for(Settings(rerank_mode=RerankMode.always, rerank_dominant_ratio=2.0))
    assert (auto.dominant_ratio, always.dominant_ratio) == (2.0, 0.0)


def test_mode_values_are_not_yaml_booleans() -> None:
    # A/B overlays are YAML, and YAML 1.1 turns bare `on`/`off` into booleans. Naming the
    # members `never`/`always` is what keeps `rerank_mode: always` a string in an overlay.
    import yaml

    assert yaml.safe_load("rerank_mode: always")["rerank_mode"] == RerankMode.always
    assert yaml.safe_load("rerank_mode: never")["rerank_mode"] == RerankMode.never
    assert yaml.safe_load("mode: on")["mode"] is True  # the trap being avoided


def test_the_retired_rerank_enabled_env_var_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Silently ignoring it would let someone set =true, see no reranking, and conclude the
    # stage is broken. pydantic-settings drops unknown env vars, so this must be explicit.
    from vaultrag.config import Settings

    monkeypatch.setenv("VAULTRAG_RERANK_ENABLED", "true")
    with pytest.raises(ValueError, match="VAULTRAG_RERANK_MODE"):
        Settings()
