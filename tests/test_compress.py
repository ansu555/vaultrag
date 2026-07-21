"""Stage 7 verify: the deterministic compressor (``retrieval/compress.py``, P3 Stage 1).

Covers each of the four steps in isolation — near-dupe drop, MMR diversity, per-doc-capped
budget pack, trim — plus the contract seams (empty input, neutral params = old behaviour,
total measured post-trim). The pipeline wiring is tested in ``test_retrieval.py``; here we
pin the compression logic itself.
"""

from __future__ import annotations

from vaultrag.chunking.tokens import count_tokens
from vaultrag.config import Settings
from vaultrag.models import Snippet
from vaultrag.retrieval.compress import (
    CompressParams,
    _jaccard,
    _terms,
    compress,
    compress_params_for,
)


def snip(
    *,
    text: str,
    score: float = 1.0,
    doc_id: str = "doc-1",
    path: str = "notes/x.md",
    tokens: int | None = None,
) -> Snippet:
    """A Snippet with just the fields compression reads (text/score/doc_id/tokens)."""
    return Snippet(
        path=path,
        text=text,
        score=score,
        tokens=tokens if tokens is not None else count_tokens(text),
        doc_id=doc_id,
    )


# ---------------------------------------------------------------------------
# Lexical similarity primitives (the Decision-3 metric)
# ---------------------------------------------------------------------------
def test_terms_are_casefolded_alphanumeric_runs_splitting_identifiers() -> None:
    assert _terms("The MER-1422 error") == {"the", "mer", "1422", "error"}


def test_jaccard_of_two_empty_token_sets_is_zero_not_one() -> None:
    """Token-less snippets must not be treated as duplicates of each other."""
    assert _jaccard(frozenset(), frozenset()) == 0.0


def test_jaccard_of_identical_sets_is_one() -> None:
    assert _jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0


# ---------------------------------------------------------------------------
# Step 1 — near-dupe removal
# ---------------------------------------------------------------------------
def test_near_dupe_drops_the_lower_scored_of_an_identical_pair() -> None:
    snippets = [
        snip(text="the quick brown fox jumps", score=0.9, doc_id="a"),
        snip(text="the quick brown fox jumps", score=0.5, doc_id="b"),
        snip(text="entirely unrelated content here", score=0.4, doc_id="c"),
    ]
    kept, _, truncated = compress(snippets, budget_tokens=10_000)
    texts = [s.text for s in kept]
    assert "the quick brown fox jumps" in texts
    assert texts.count("the quick brown fox jumps") == 1  # the twin is gone
    assert "entirely unrelated content here" in texts
    assert truncated is True  # a snippet the pipeline produced was dropped


def test_near_dupe_keeps_the_higher_scored_one() -> None:
    """The survivor is the higher-ranked (earlier) member of the pair."""
    snippets = [
        snip(text="shared body text", score=0.9, doc_id="keep"),
        snip(text="shared body text", score=0.1, doc_id="drop"),
    ]
    kept, _, _ = compress(snippets, budget_tokens=10_000)
    assert [s.doc_id for s in kept] == ["keep"]


def test_threshold_at_one_disables_near_dupe_removal() -> None:
    snippets = [
        snip(text="shared body text", score=0.9, doc_id="a"),
        snip(text="shared body text", score=0.5, doc_id="b"),
    ]
    kept, _, _ = compress(snippets, budget_tokens=10_000, params=CompressParams(dupe_threshold=1.0))
    assert len(kept) == 2  # J == 1.0 is not > 1.0, so both survive


def test_topically_similar_but_distinct_chunks_are_not_collapsed() -> None:
    snippets = [
        snip(text="hybrid search fuses dense and sparse retrieval", score=0.9, doc_id="a"),
        snip(text="the reranker reorders candidates by cross encoder score", score=0.5, doc_id="b"),
    ]
    kept, _, _ = compress(snippets, budget_tokens=10_000)
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# Step 2 — MMR diversity
# ---------------------------------------------------------------------------
def test_mmr_demotes_a_redundant_twin_below_a_diverse_candidate() -> None:
    """With diversity weighted heavily, the near-twin of the top hit loses its rank to a
    dissimilar candidate even though the twin had the higher incoming score."""
    snippets = [
        snip(text="alpha beta gamma delta topic", score=0.9, doc_id="top"),
        snip(text="alpha beta gamma delta topic near", score=0.6, doc_id="twin"),
        snip(text="zeta eta theta iota unrelated", score=0.3, doc_id="diverse"),
    ]
    kept, _, _ = compress(
        snippets, budget_tokens=10_000, params=CompressParams(mmr_lambda=0.3, dupe_threshold=1.0)
    )
    assert [s.doc_id for s in kept] == ["top", "diverse", "twin"]


def test_mmr_never_demotes_the_top_hit() -> None:
    snippets = [
        snip(text="alpha beta gamma topic", score=0.9, doc_id="top"),
        snip(text="alpha beta gamma topic twin", score=0.6, doc_id="twin"),
        snip(text="unrelated distinct material", score=0.3, doc_id="diverse"),
    ]
    kept, _, _ = compress(
        snippets, budget_tokens=10_000, params=CompressParams(mmr_lambda=0.1, dupe_threshold=1.0)
    )
    assert kept[0].doc_id == "top"


def test_mmr_lambda_one_preserves_incoming_order() -> None:
    snippets = [
        snip(text="first body one", score=0.9, doc_id="a"),
        snip(text="second body two", score=0.6, doc_id="b"),
        snip(text="third body three", score=0.3, doc_id="c"),
    ]
    kept, _, _ = compress(snippets, budget_tokens=10_000, params=CompressParams(mmr_lambda=1.0))
    assert [s.doc_id for s in kept] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Step 3 — per-doc cap + budget packing
# ---------------------------------------------------------------------------
def test_per_doc_cap_limits_chunks_from_one_document() -> None:
    snippets = [snip(text=f"section {i} body", score=1.0 - i / 10, doc_id="same") for i in range(5)]
    kept, _, truncated = compress(snippets, budget_tokens=10_000)  # default cap 3
    assert len(kept) == 3
    assert truncated is True


def test_per_doc_cap_zero_disables_the_cap() -> None:
    snippets = [snip(text=f"section {i} body", score=1.0 - i / 10, doc_id="same") for i in range(5)]
    kept, _, _ = compress(snippets, budget_tokens=10_000, params=CompressParams(per_doc_cap=0))
    assert len(kept) == 5


def test_cap_falls_back_to_path_when_doc_id_is_missing() -> None:
    """A parent-swapped snippet has doc_id set from its child, but a raw snippet might not;
    the cap keys on path in that case so it still bounds one note."""
    snippets = [
        Snippet(path="notes/x.md", text=f"body {i}", score=1.0 - i / 10, tokens=3) for i in range(5)
    ]
    kept, _, _ = compress(snippets, budget_tokens=10_000)
    assert len(kept) == 3


def test_budget_keeps_the_top_hit_even_when_oversized() -> None:
    snippets = [
        snip(text="oversized top", score=0.9, doc_id="a", tokens=5000),
        snip(text="small next", score=0.5, doc_id="b", tokens=10),
    ]
    kept, total, truncated = compress(snippets, budget_tokens=2000)
    assert [s.doc_id for s in kept] == ["a"]
    assert total == 5000
    assert truncated is True


# ---------------------------------------------------------------------------
# Step 4 — trim
# ---------------------------------------------------------------------------
def test_trim_passes_a_code_block_through_byte_identical() -> None:
    code = "```python\ndef f(x):\n    return  x + 1   # trailing spaces kept\n```"
    text = f"Here is the function.\n\n{code}\n\nand a dangling clause without"
    kept, _, _ = compress([snip(text=text)], budget_tokens=10_000)
    assert code in kept[0].text  # every byte of the fence survived


def test_trim_collapses_blank_runs() -> None:
    kept, _, _ = compress([snip(text="para one\n\n\n\npara two")], budget_tokens=10_000)
    assert kept[0].text == "para one\n\npara two"


def test_trim_cuts_a_trailing_partial_sentence() -> None:
    kept, _, _ = compress(
        [snip(text="This sentence is complete. And this one dangles without a")],
        budget_tokens=10_000,
    )
    assert kept[0].text == "This sentence is complete. …"


def test_trim_leaves_a_complete_sentence_untouched() -> None:
    kept, _, _ = compress([snip(text="A single complete sentence.")], budget_tokens=10_000)
    assert kept[0].text == "A single complete sentence."


def test_trim_never_cuts_a_structural_trailing_line() -> None:
    """A trailing list item / heading isn't a dangling clause — leave it."""
    text = "Intro paragraph here.\n\n- a final list item with no period"
    kept, _, _ = compress([snip(text=text)], budget_tokens=10_000)
    assert kept[0].text == text


def test_trim_leaves_a_lone_fragment_when_there_is_no_sentence_to_keep() -> None:
    kept, _, _ = compress([snip(text="just a fragment no period")], budget_tokens=10_000)
    assert kept[0].text == "just a fragment no period"


def test_total_tokens_is_measured_after_trimming() -> None:
    text = "Complete. Now a trailing fragment that will be"
    kept, total, _ = compress([snip(text=text)], budget_tokens=10_000)
    assert kept[0].text == "Complete. …"
    assert total == count_tokens("Complete. …")


# ---------------------------------------------------------------------------
# Contract seams
# ---------------------------------------------------------------------------
def test_empty_input_returns_empty() -> None:
    assert compress([], budget_tokens=10_000) == ([], 0, False)


def test_neutral_params_reproduce_plain_greedy_packing() -> None:
    """dupe ≥1, mmr 1.0, cap disabled → same membership/order as the old pack_budget."""
    snippets = [
        snip(text="first body", score=0.9, doc_id="a", tokens=800),
        snip(text="second body", score=0.6, doc_id="b", tokens=800),
        snip(text="third body", score=0.3, doc_id="c", tokens=800),
    ]
    neutral = CompressParams(dupe_threshold=2.0, mmr_lambda=1.0, per_doc_cap=0)
    kept, total, truncated = compress(snippets, budget_tokens=2000, params=neutral)
    assert [s.doc_id for s in kept] == ["a", "b"]
    assert total == 1600
    assert truncated is True


def test_compress_params_for_reads_settings() -> None:
    settings = Settings(
        compress_dupe_threshold=0.8, compress_mmr_lambda=0.5, compress_per_doc_cap=7
    )
    params = compress_params_for(settings)
    assert params == CompressParams(dupe_threshold=0.8, mmr_lambda=0.5, per_doc_cap=7)
