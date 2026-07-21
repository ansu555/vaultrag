"""Tests for the retrieval quality gate — P2 Stage 1.

Three layers, cheapest first:

1. **Metrics** are pure functions over a ranked list; they are tested against hand-worked
   numbers, because a gate whose arithmetic is wrong is worse than no gate.
2. **Loading** — golden sets, config overlays, the frozen-vector fixture. The recurring
   theme is that malformed input must *raise*, never silently shrink the set being scored.
3. **The gate itself** — the real hermetic run over the fixture corpus, asserting both
   that the shipped config clears the targets and that a deliberately-bad config does
   **not**. A gate nobody has watched fail is not known to be a gate.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from vaultrag.config import RerankMode, Settings, load_settings
from vaultrag.embedding.base import EmbeddingError
from vaultrag.eval import (
    CORPUS_VAULT_PATH,
    VECTOR_FIXTURE_PATH,
    GoldenSetError,
    Metrics,
    OverlayError,
    ReplayEmbedder,
    VectorFixture,
    aggregate,
    apply_overlay,
    first_relevant_rank,
    format_comparison,
    format_misses,
    format_summary,
    load_corpus_golden,
    load_design_golden,
    load_golden,
    load_overlay,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
    replay_stack,
    run_eval,
    text_key,
    verify_paths,
)

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
RANKED = ["a.md", "b.md", "c.md", "d.md", "e.md"]


def gate_settings() -> Settings:
    """Settings for a hermetic measurement — rerank pinned off, exactly like `vaultrag eval`.

    Nothing that measures retrieval may *inherit* the rerank mode. The product default is
    `auto`, which builds a cross-encoder: a ~600 MB download and ~50 scored passages per
    query would turn this suite into a slow, network-dependent one and silently change what
    the gate is measuring. The gate states its mode; it never assumes it.
    """
    return load_settings().model_copy(update={"rerank_mode": RerankMode.never})


def test_first_relevant_rank_is_one_based() -> None:
    assert first_relevant_rank(RANKED, {"a.md"}) == 1
    assert first_relevant_rank(RANKED, {"c.md"}) == 3
    assert first_relevant_rank(RANKED, {"zzz.md"}) is None


def test_first_relevant_rank_takes_the_earliest_of_several() -> None:
    assert first_relevant_rank(RANKED, {"d.md", "b.md"}) == 2


def test_recall_is_the_fraction_of_the_relevant_set_found() -> None:
    assert recall_at_k(RANKED, {"c.md"}, 10) == 1.0
    assert recall_at_k(RANKED, {"zzz.md"}, 10) == 0.0
    assert recall_at_k(RANKED, {"a.md", "zzz.md"}, 10) == 0.5


def test_recall_respects_the_cutoff() -> None:
    assert recall_at_k(RANKED, {"d.md"}, 3) == 0.0
    assert recall_at_k(RANKED, {"d.md"}, 4) == 1.0


def test_recall_of_an_empty_relevant_set_is_zero_not_one() -> None:
    # A malformed case must not score as a free pass — it would inflate the gate.
    assert recall_at_k(RANKED, set(), 10) == 0.0


def test_reciprocal_rank_is_one_over_the_rank() -> None:
    assert reciprocal_rank_at_k(RANKED, {"a.md"}, 5) == 1.0
    assert reciprocal_rank_at_k(RANKED, {"d.md"}, 5) == pytest.approx(0.25)


def test_reciprocal_rank_is_zero_past_the_cutoff() -> None:
    # Rank 5 counts at k=5; nothing beyond the window an agent reads is worth a fraction.
    assert reciprocal_rank_at_k(RANKED, {"e.md"}, 5) == pytest.approx(0.2)
    assert reciprocal_rank_at_k(RANKED, {"e.md"}, 4) == 0.0


def test_ndcg_is_one_for_a_perfect_ranking() -> None:
    assert ndcg_at_k(RANKED, {"a.md"}, 10) == pytest.approx(1.0)
    assert ndcg_at_k(RANKED, {"a.md", "b.md"}, 10) == pytest.approx(1.0)


def test_ndcg_discounts_by_position() -> None:
    # One relevant doc at rank 3: DCG = 1/log2(4), IDCG = 1/log2(2) = 1.
    assert ndcg_at_k(RANKED, {"c.md"}, 10) == pytest.approx(1.0 / math.log2(4))


def test_ndcg_counts_a_repeated_path_once() -> None:
    # A search that returned the same note twice has not found it twice.
    duped = ["a.md", "a.md", "b.md"]
    assert ndcg_at_k(duped, {"a.md", "b.md"}, 10) == pytest.approx(
        (1.0 + 1.0 / math.log2(4)) / (1.0 + 1.0 / math.log2(3))
    )


def test_aggregate_means_over_cases() -> None:
    metrics = aggregate([(RANKED, {"a.md"}), (RANKED, {"c.md"})])
    assert metrics.cases == 2
    assert metrics.recall_at_10 == 1.0
    assert metrics.mrr_at_5 == pytest.approx((1.0 + 1 / 3) / 2)


def test_aggregate_scores_ndcg_on_the_pre_rerank_order_when_given_one() -> None:
    post = [(["a.md"], {"a.md"})]
    pre = [(["x.md", "y.md", "a.md"], {"a.md"})]
    metrics = aggregate(post, pre_rerank=pre)
    assert metrics.mrr_at_5 == pytest.approx(1.0)  # from the post-rerank order
    assert metrics.ndcg_at_10 == pytest.approx(1.0 / math.log2(4))  # from fusion order


def test_aggregate_of_nothing_is_zero_not_a_crash() -> None:
    assert aggregate([]) == Metrics(0.0, 0.0, 0.0, 0)


def test_meets_targets_and_failure_messages() -> None:
    assert Metrics(0.95, 0.80, 0.9, 10).meets_targets()
    assert Metrics(0.95, 0.80, 0.9, 10).failures() == []
    below = Metrics(0.5, 0.5, 0.5, 10)
    assert not below.meets_targets()
    assert len(below.failures()) == 2


# ---------------------------------------------------------------------------
# Golden sets
# ---------------------------------------------------------------------------
def test_corpus_golden_loads_and_is_a_real_set() -> None:
    golden = load_corpus_golden()
    assert len(golden) >= 50, "evaluation-framework.md specifies a 50-150 pair golden set"
    assert golden.vault_path == CORPUS_VAULT_PATH.resolve()


def test_every_corpus_expected_path_exists() -> None:
    # A typo'd expected path is unretrievable forever, which silently lowers the ceiling
    # of the whole gate rather than failing loudly.
    assert verify_paths(load_corpus_golden()) == []


def test_corpus_golden_has_no_duplicate_queries() -> None:
    queries = [c.query for c in load_corpus_golden().cases]
    assert len(queries) == len(set(queries))


def test_corpus_golden_covers_most_of_the_corpus() -> None:
    golden = load_corpus_golden()
    covered = {p for case in golden.cases for p in case.expected}
    notes = {
        str(p.relative_to(CORPUS_VAULT_PATH))
        for p in CORPUS_VAULT_PATH.rglob("*.md")
        if p.name != "README.md"
    }
    assert notes - covered == set(), "every corpus note should be someone's expected answer"


def test_design_golden_loads() -> None:
    golden = load_design_golden()
    assert golden.vault_path is None  # live vault: not committed, so not verifiable here
    assert len(golden) >= 50


def test_golden_rejects_a_case_with_no_expected_path(tmp_path: Path) -> None:
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"cases": [{"query": "hi"}]}), encoding="utf-8")
    with pytest.raises(GoldenSetError, match="no expected path"):
        load_golden(path)


def test_golden_rejects_an_empty_set(tmp_path: Path) -> None:
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"cases": []}), encoding="utf-8")
    with pytest.raises(GoldenSetError, match="no cases"):
        load_golden(path)


def test_golden_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "g.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(GoldenSetError, match="not valid JSON"):
        load_golden(path)


def test_golden_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GoldenSetError, match="not found"):
        load_golden(tmp_path / "nope.json")


def test_golden_accepts_multiple_expected_paths(tmp_path: Path) -> None:
    path = tmp_path / "g.json"
    path.write_text(
        json.dumps({"cases": [{"query": "q", "expected_paths": ["a.md", "b.md"]}]}),
        encoding="utf-8",
    )
    assert load_golden(path).cases[0].expected == frozenset({"a.md", "b.md"})


def test_verify_paths_names_the_missing_note(tmp_path: Path) -> None:
    (tmp_path / "corpus").mkdir()
    path = tmp_path / "g.json"
    path.write_text(
        json.dumps({"vault_path": "corpus", "cases": [{"query": "q", "expected_path": "gone.md"}]}),
        encoding="utf-8",
    )
    missing = verify_paths(load_golden(path))
    assert len(missing) == 1 and "gone.md" in missing[0]


# ---------------------------------------------------------------------------
# Config overlays (A/B)
# ---------------------------------------------------------------------------
def test_overlay_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text("rerank_mode: always\ndefault_top_k: 5\n", encoding="utf-8")
    assert load_overlay(path) == {"rerank_mode": "always", "default_top_k": 5}


def test_overlay_reads_json(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text('{"default_top_k": 3}', encoding="utf-8")
    assert load_overlay(path) == {"default_top_k": 3}


def test_overlay_rejects_an_unknown_setting(tmp_path: Path) -> None:
    # A typo that silently changed nothing would make an A/B compare a config with itself.
    path = tmp_path / "a.yaml"
    path.write_text("rerank_enable: true\n", encoding="utf-8")
    with pytest.raises(OverlayError, match="unknown setting"):
        load_overlay(path)


def test_overlay_rejects_a_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(OverlayError, match="mapping"):
        load_overlay(path)


def test_empty_overlay_is_no_changes(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text("\n", encoding="utf-8")
    assert load_overlay(path) == {}


def test_apply_overlay_returns_a_revalidated_copy() -> None:
    base = Settings(default_top_k=10)
    out = apply_overlay(base, {"default_top_k": 3})
    assert out.default_top_k == 3
    assert base.default_top_k == 10  # unchanged


def test_apply_overlay_rejects_an_invalid_value() -> None:
    with pytest.raises(OverlayError, match="invalid config overlay"):
        apply_overlay(Settings(), {"default_top_k": "many"})


# ---------------------------------------------------------------------------
# Frozen vectors
# ---------------------------------------------------------------------------
def test_vector_fixture_round_trips(tmp_path: Path) -> None:
    with VectorFixture(tmp_path / "v.db", create=True) as fixture:
        fixture.put("m", 3, "hello", [0.5, -0.25, 0.125])
        assert fixture.get("m", 3, "hello") == pytest.approx([0.5, -0.25, 0.125])
        assert fixture.get("m", 3, "other") is None
        assert fixture.get("other-model", 3, "hello") is None  # model is part of the key
        assert fixture.count() == 1


def test_text_key_is_content_addressed() -> None:
    assert text_key("abc") == text_key("abc")
    assert text_key("abc") != text_key("abd")


def test_replay_embedder_returns_recorded_vectors(tmp_path: Path) -> None:
    with VectorFixture(tmp_path / "v.db", create=True) as fixture:
        fixture.put("m", 2, "q", [1.0, 0.0])
        embedder = ReplayEmbedder(fixture, model="m", dims=2)
        assert embedder.embed(["q"]) == [[1.0, 0.0]]


def test_replay_embedder_raises_rather_than_inventing_a_vector(tmp_path: Path) -> None:
    # The whole gate rests on this: a fabricated embedding would make CI green while
    # measuring nothing (repo CLAUDE.md §5 — never fake success).
    with VectorFixture(tmp_path / "v.db", create=True) as fixture:
        embedder = ReplayEmbedder(fixture, model="m", dims=2)
        with pytest.raises(EmbeddingError, match="--record"):
            embedder.embed(["never recorded"])


def test_missing_fixture_file_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(EmbeddingError, match="--record"):
        VectorFixture(tmp_path / "absent.db")


def test_committed_fixture_covers_the_shipped_config() -> None:
    settings = gate_settings()
    with VectorFixture(VECTOR_FIXTURE_PATH) as fixture:
        assert fixture.count(settings.embed_model, settings.embed_dims) > 0, (
            "the committed fixture holds no vectors for the configured model/dims — "
            "re-record with `uv run vaultrag eval --record`"
        )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def shipped_report():  # type: ignore[no-untyped-def]
    """Run the real hermetic gate once and share it across the assertions below."""
    golden = load_corpus_golden()
    settings = gate_settings()
    with replay_stack(golden, settings, fixture_path=VECTOR_FIXTURE_PATH) as stack:
        return run_eval(golden, stack, label="shipped")


def test_the_gate_passes_on_the_shipped_config(shipped_report) -> None:  # type: ignore[no-untyped-def]
    assert shipped_report.metrics.meets_targets(), (
        f"retrieval quality regressed: {'; '.join(shipped_report.metrics.failures())}\n"
        f"{format_misses(shipped_report)}"
    )


def test_the_gate_scores_every_case(shipped_report) -> None:  # type: ignore[no-untyped-def]
    assert shipped_report.metrics.cases == len(load_corpus_golden())
    assert all(r.ranked for r in shipped_report.results), "a case returned nothing at all"


def test_reports_render(shipped_report) -> None:  # type: ignore[no-untyped-def]
    assert "recall@10" in format_summary(shipped_report)
    assert "A/B" in format_comparison([shipped_report, shipped_report])
    assert format_misses(shipped_report)  # non-empty string either way


def test_the_gate_actually_bites_on_a_bad_config() -> None:
    """A config that truncates results to one hit must fail, not squeak through.

    This is the test that makes the gate meaningful. It proves the machinery can produce a
    below-target number and report it as a failure — without it we would only ever have
    observed the passing case, which any no-op implementation also produces.
    """
    golden = load_corpus_golden()
    broken = apply_overlay(gate_settings(), {"default_top_k": 1})
    with replay_stack(golden, broken, fixture_path=VECTOR_FIXTURE_PATH) as stack:
        report = run_eval(golden, stack, label="top_k=1")
    assert not report.metrics.meets_targets()
    assert report.metrics.failures()
    assert report.misses, "a failing gate must name the cases that missed"


def test_the_harness_is_offline() -> None:
    """Replay mode must not reach Ollama — CI has no model server (Decision 1)."""
    golden = load_corpus_golden()
    settings = apply_overlay(gate_settings(), {"ollama_url": "http://127.0.0.1:1"})
    with replay_stack(golden, settings, fixture_path=VECTOR_FIXTURE_PATH) as stack:
        report = run_eval(golden, stack, label="offline")
    assert report.metrics.meets_targets()


def test_a_stale_fixture_aborts_instead_of_scoring_a_partial_index(tmp_path: Path) -> None:
    """A note the fixture has no vector for must stop the run, not shrink the index.

    ``reindex_vault`` is deliberately resilient — one bad note parks its job and the rest
    of the vault still indexes. In the gate that resilience is a hazard: it turns
    ``ReplayEmbedder``'s refusal to invent a vector into a quietly smaller index, and the
    run then scores the surviving notes and reports PASS. Regression test for exactly
    that: before the fix this exited 0 at recall 0.958 with one note silently missing.
    """
    corpus = tmp_path / "corpus"
    shutil.copytree(CORPUS_VAULT_PATH, corpus)
    (corpus / "unrecorded.md").write_text(
        "# Unrecorded\nThis note was never embedded into the fixture.\n", encoding="utf-8"
    )
    golden = load_corpus_golden()
    stale = replace(golden, vault_path=corpus)

    with (
        pytest.raises(EmbeddingError, match="incomplete index"),
        replay_stack(stale, gate_settings(), fixture_path=VECTOR_FIXTURE_PATH) as stack,
    ):
        run_eval(stale, stack, label="stale")
