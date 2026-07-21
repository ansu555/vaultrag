"""Retrieval stage 7 — deterministic context compression (P3 Stage 1).

Replaces P0's greedy :func:`~vaultrag.retrieval.pipeline.pack_budget` with the four-step
pipeline ``../Rag/projects/vaultrag/context-compression.md`` specifies, in order:

1. **near-dupe removal** — drop a snippet that is lexically near-identical to a
   higher-ranked one (token-set Jaccard > ``dupe_threshold``), keeping the higher-scored
   one (the earlier of the pair, since hits arrive in score order).
2. **MMR** (Maximal Marginal Relevance, ``mmr_lambda``) — reorder for cross-document
   diversity, so the greedy packer spends the budget on distinct information instead of a
   run of near-repeats. The top hit is never moved (MMR's first pick is pure relevance).
3. **greedy budget packer** — fill ``budget_tokens`` best-first, capped at ``per_doc_cap``
   chunks per document. The parent→child fallback the note also lists is already handled
   upstream in stage 5 (``retrieval/parents.py`` caps the swap at the response budget), so
   by the time a snippet reaches here the parent-vs-child choice is made.
4. **snippet trim** — collapse blank runs and cut a trailing partial sentence; fenced code
   blocks pass through **byte-identical** (agents need exact identifiers, CLAUDE.md §6).

Deterministic, free, ~0 ms — no LLM, no network (``deterministic-compression-not-llm.md``).
LLMLingua-2 token-pruning stays an unbuilt opt-in, not a knob shipped dark.

**Similarity is LEXICAL (token-set Jaccard), not embedding cosine — P3 Decision 3.**
The design note writes "cos > 0.92"; the plan asked which vector source MMR/near-dupe
should use. Two findings settled it on lexical (see
``../Rag/agent-inbox/compression-similarity-is-lexical-not-vector.md``):

* **int8 is not the blocker.** Measured on ``tests/eval/vectors.db``: int8-quantized
  cosine tracks full precision to max Δ 0.0013 (p95 0.0007) and flips the 0.92 decision on
  0 of 3968 pairs. The note's stated worry does not hold.
* **Parent-swap is.** Stage 5 is ON by default and emits snippets reconstructed live from
  the vault with **no stored vector** (``parents.py``: ``chunk_id=None``). MMR needs one
  consistent similarity across *all* candidates, and re-embedding at query time is rejected
  by Decision 3. A lexical metric is the only one that applies uniformly to child and
  parent snippets — and it is genuinely free/~0 ms, needing no extra Qdrant round-trip.

Ref: ``../Rag/projects/vaultrag/context-compression.md``,
``../Rag/decisions/deterministic-compression-not-llm.md``,
``../Rag/projects/vaultrag/retrieval-pipeline.md`` stage 7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vaultrag.chunking.tokens import count_tokens
from vaultrag.models import Snippet

if TYPE_CHECKING:
    from vaultrag.config import Settings

# Lexical near-dupe threshold: token-set Jaccard, *not* embedding cosine (see the module
# docstring / Decision 3). 0.9 collapses only near-identical text — heading-tree chunks are
# disjoint sections, so a real near-dupe is duplicated content (a transclusion, a note and
# its embed), which lands well above this; two merely on-topic chunks sit far below it.
DEFAULT_DUPE_THRESHOLD = 0.9
# MMR relevance/diversity trade-off. 0.7 keeps relevance dominant (the reranked/authority
# order is trusted); the diversity term only reshuffles genuinely redundant neighbours.
# 1.0 is pure relevance — MMR becomes a no-op that preserves the incoming order exactly,
# which is how the eval overlay reproduces the pre-compression ranking.
DEFAULT_MMR_LAMBDA = 0.7
# Per-document chunk cap in the packer: at most this many chunks of one note, so a single
# long note can't spend the whole budget and crowd out other documents (arch §13). 0 or a
# large value disables the cap.
DEFAULT_PER_DOC_CAP = 3

# Tokens for the lexical similarity: alphanumeric runs, case-folded. Deliberately dumb — no
# stemming, no stopwords — so it is deterministic and dependency-free. Identifiers like
# `MER-1422` split into `mer`/`1422`, which is fine for a *set*-overlap similarity.
_TOKEN_RE = re.compile(r"[0-9a-z]+")
# A fence line: ``` or ~~~ (any indent, any info string). Toggles verbatim code mode in the
# trimmer so code — the one thing that must survive byte-identical — is never reflowed.
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
# A line the trailing-sentence cut must never touch: headings, list items, table rows,
# blockquotes. Cutting these would drop structure, not a dangling clause.
_STRUCTURAL_RE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||>)")
# Sentence terminators for the trailing-fragment cut.
_SENTENCE_END = ".!?…"


@dataclass(frozen=True, slots=True)
class CompressParams:
    """Tunables for stage 7 — all env-configurable (see :class:`~vaultrag.config.Settings`).

    ``dupe_threshold`` is the token-set Jaccard above which a lower-ranked snippet is
    dropped as a near-duplicate; ``mmr_lambda`` is MMR's relevance weight (1.0 disables the
    diversity reorder); ``per_doc_cap`` bounds how many chunks of one document the packer
    keeps. Neutral values (``dupe_threshold`` ≥ 1, ``mmr_lambda`` = 1.0, a large
    ``per_doc_cap``) reduce the stage to P0's plain greedy pack — which is exactly how an
    A/B overlay measures the stage's contribution.
    """

    dupe_threshold: float = DEFAULT_DUPE_THRESHOLD
    mmr_lambda: float = DEFAULT_MMR_LAMBDA
    per_doc_cap: int = DEFAULT_PER_DOC_CAP


def compress_params_for(settings: Settings) -> CompressParams:
    """The stage-7 tunables a config implies.

    Lives here rather than on :class:`~vaultrag.config.Settings` for the same reason
    :func:`~vaultrag.retrieval.rerank.rerank_params_for` and
    :func:`~vaultrag.retrieval.graph.graph_params_for` do: ``config`` is a dependency-free
    leaf that everything imports, so it must import nothing back. One place maps settings →
    behaviour, so the served server and the eval harness cannot drift into measuring
    something other than what ships.
    """
    return CompressParams(
        dupe_threshold=settings.compress_dupe_threshold,
        mmr_lambda=settings.compress_mmr_lambda,
        per_doc_cap=settings.compress_per_doc_cap,
    )


@dataclass(slots=True)
class _Cand:
    """One snippet plus what compression needs to reason about it, computed once."""

    snippet: Snippet
    terms: frozenset[str]
    pos: int  # rank in the incoming (score) order — relevance + deterministic tie-break


def compress(
    snippets: list[Snippet], budget_tokens: int, params: CompressParams | None = None
) -> tuple[list[Snippet], int, bool]:
    """Stage 7: dedupe → MMR → budget pack (per-doc cap) → trim.

    Returns ``(kept, total_tokens, truncated)`` with the same contract as
    :func:`~vaultrag.retrieval.pipeline.pack_budget`: ``total_tokens`` is the token sum of
    what is returned (measured **after** trimming, so it is honest about the bytes handed
    back), and ``truncated`` is True whenever a snippet the pipeline produced did not make
    the response — whether the budget, the per-doc cap, or a near-dupe dropped it.
    """
    p = params if params is not None else CompressParams()
    if not snippets:
        return [], 0, False

    cands = [_Cand(snippet=s, terms=_terms(s.text), pos=i) for i, s in enumerate(snippets)]
    original_count = len(cands)

    cands = _drop_near_dupes(cands, p.dupe_threshold)
    cands = _mmr(cands, p.mmr_lambda)
    kept = _pack(cands, budget_tokens, p.per_doc_cap)

    # Step 4 last (per the note's order): trim changes text/tokens but never membership or
    # order, so it can't reintroduce anything the budget dropped. Trimming only ever *shrinks*
    # a snippet, so the packed total stays within budget; report the post-trim sum.
    trimmed = [_trim(c.snippet) for c in kept]
    total = sum(s.tokens for s in trimmed)
    truncated = len(trimmed) < original_count
    return trimmed, total, truncated


def _terms(text: str) -> frozenset[str]:
    """The token set for lexical similarity — case-folded alphanumeric runs."""
    return frozenset(_TOKEN_RE.findall(text.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Token-set Jaccard in ``[0, 1]``. Two empty sets are *not* duplicates (0.0): a
    token-less snippet — pure punctuation, say — carries no evidence of being a repeat of
    another, so it is kept rather than silently collapsed."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _drop_near_dupes(cands: list[_Cand], threshold: float) -> list[_Cand]:
    """Step 1: keep the higher-ranked of any near-identical pair.

    Candidates arrive in score order, so the first-seen of a near-dupe pair is the
    higher-scored one — exactly what the note says to keep. A ``threshold`` ≥ 1 keeps
    everything (no Jaccard reaches it), the stage's off switch.
    """
    if threshold > 1.0:
        return cands
    kept: list[_Cand] = []
    for c in cands:
        if any(_jaccard(c.terms, k.terms) > threshold for k in kept):
            continue
        kept.append(c)
    return kept


def _mmr(cands: list[_Cand], lam: float) -> list[_Cand]:
    """Step 2: greedy MMR reorder for cross-document diversity.

    Relevance is **rank-based** (position in the incoming order), not the raw score — the
    same scale-free choice ``retrieval/graph.py`` makes and for the same reason: incoming
    scores are RRF (~0.016), a cross-encoder sigmoid, or a constant 1.0 depending on which
    stages ran, so a raw score mixed with a ``[0, 1]`` diversity term would let diversity
    dominate on one scale and vanish on another. With rank relevance the first pick is
    always the top hit (nothing selected yet → pure relevance), so MMR never demotes the
    best result. ``lam`` = 1.0 short-circuits to the incoming order.
    """
    n = len(cands)
    if n <= 2 or lam >= 1.0:
        return cands
    # rel[i] in [0, 1], strictly decreasing with incoming rank.
    rel = {c.pos: (n - 1 - i) / (n - 1) for i, c in enumerate(cands)}
    selected: list[_Cand] = []
    remaining = list(cands)
    while remaining:
        best: _Cand | None = None
        best_score = float("-inf")
        for c in remaining:
            diversity = max((_jaccard(c.terms, s.terms) for s in selected), default=0.0)
            score = lam * rel[c.pos] - (1.0 - lam) * diversity
            # Deterministic tie-break: prefer the higher incoming rank (lower pos).
            ties_lower = score == best_score and best is not None and c.pos < best.pos
            if score > best_score or ties_lower:
                best_score = score
                best = c
        assert best is not None
        selected.append(best)
        remaining.remove(best)
    return selected


def _pack(cands: list[_Cand], budget_tokens: int, per_doc_cap: int) -> list[_Cand]:
    """Step 3: greedy best-first fill, capped at ``per_doc_cap`` chunks per document.

    Mirrors :func:`~vaultrag.retrieval.pipeline.pack_budget` — the top candidate is always
    kept even if it alone exceeds the budget (an oversized best match beats an empty
    response), and packing stops at the first candidate that would overflow — but skips a
    candidate whose document is already at the cap, so a long note can't monopolise the
    budget. A ``per_doc_cap`` ≤ 0 disables the cap.
    """
    kept: list[_Cand] = []
    total = 0
    per_doc: dict[str, int] = {}
    for c in cands:
        doc = c.snippet.doc_id or c.snippet.path
        if per_doc_cap > 0 and per_doc.get(doc, 0) >= per_doc_cap:
            continue
        if kept and total + c.snippet.tokens > budget_tokens:
            break
        kept.append(c)
        total += c.snippet.tokens
        per_doc[doc] = per_doc.get(doc, 0) + 1
    return kept


def _trim(snippet: Snippet) -> Snippet:
    """Step 4: collapse blank runs + cut a trailing partial sentence; code stays verbatim."""
    trimmed = _trim_text(snippet.text)
    if trimmed == snippet.text:
        return snippet
    return snippet.model_copy(update={"text": trimmed, "tokens": count_tokens(trimmed)})


def _trim_text(text: str) -> str:
    """Collapse blank runs outside code, then cut a dangling trailing sentence fragment.

    Fenced code blocks pass through byte-identical: the trimmer walks line by line and, once
    a ``` / ~~~ fence opens, copies every line unchanged until the fence closes. Only prose
    is reflowed. The trailing-fragment cut is conservative — it fires only when the response
    ends in prose (not code, not a heading/list/table row) whose last line lacks terminal
    punctuation *and* an earlier sentence terminator exists to cut back to — so it removes a
    half-finished clause, never a whole paragraph or a structural line.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    pending_blank = False
    tail_is_prose = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_code = not in_code
            out.append(line)
            pending_blank = False
            tail_is_prose = False
            continue
        if in_code:
            out.append(line)
            tail_is_prose = False
            continue
        if line.strip() == "":
            # Collapse a run of blank lines to a single blank; defer emitting it so trailing
            # blanks vanish entirely.
            pending_blank = True
            continue
        if pending_blank and out:
            out.append("")
        pending_blank = False
        out.append(line)
        tail_is_prose = not _STRUCTURAL_RE.match(line)

    if in_code:
        # Unbalanced fence — treat the rest as code and never risk cutting it. Return the
        # blank-collapsed body as-is.
        return "\n".join(out)

    if tail_is_prose:
        out = _cut_trailing_fragment(out)
    return "\n".join(out)


def _cut_trailing_fragment(lines: list[str]) -> list[str]:
    """If the final prose line is a dangling fragment, cut it back to the last full sentence.

    Operates on the trailing paragraph (the run of prose lines since the last blank). Leaves
    the text untouched unless it both lacks terminal punctuation *and* contains an earlier
    terminator to fall back to — so a single unpunctuated line (a title-like fragment with no
    sentence to keep) is preserved rather than deleted.
    """
    last = lines[-1].rstrip()
    if last and last[-1] in _SENTENCE_END:
        return lines  # already ends on a complete sentence
    # Find where the trailing paragraph starts (after the last blank line).
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "":
            start = i + 1
            break
    paragraph = "\n".join(lines[start:]).rstrip()
    cut = max(paragraph.rfind(ch) for ch in _SENTENCE_END)
    if cut == -1:
        return lines  # no complete sentence to keep — leave the fragment intact
    kept_paragraph = paragraph[: cut + 1] + " …"
    return lines[:start] + kept_paragraph.split("\n")
