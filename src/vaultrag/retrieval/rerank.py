"""Cross-encoder reranking — retrieval stage 4 (P1 Stage 1), the biggest accuracy lever.

After RRF fusion (stage 3) the top candidates are re-scored by a cross-encoder that reads
``query x child chunk`` *together* (not two independent embeddings), which is far more
precise than the bi-encoder recall pass — see ``../Rag/decisions/reranker-bge-v2-m3.md``
and ``../Rag/projects/vaultrag/reranking.md`` (arch §12).

The stage (:func:`rerank_candidates`) is model-agnostic policy:

* pull the top ``top_n`` fused candidates (~50) and rerank **children, not parents**
  (less dilution — ``../Rag/projects/vaultrag/chunking-strategy.md``);
* a **score threshold** (default 0.0 — no floor) drops garbage instead of padding to ``k``
  — an honest "nothing relevant" beats confident noise, and agents handle empty results;
* a **per-query decision** (:func:`should_rerank`) skips the model when the top-1 fusion
  score already dominates the top-2 — both retrievers agreeing means a verbatim-identifier
  or exact-title hit that doesn't need re-ranking. :class:`RerankMode` turns that from an
  internal fast path into the user's choice: ``never`` / ``auto`` / ``always``.

The model itself sits behind the :class:`Reranker` protocol (CLAUDE.md §5, interfaces over
vendors). :class:`BgeReranker` is the local default (bge-reranker-v2-m3, ONNX int8, loaded
directly via ``onnxruntime`` — no ``torch``/``transformers``); :class:`NoOpReranker` is the
graceful fallback when that model can't be fetched, so retrieval never hard-depends on a
downloaded artifact to run at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from vaultrag.config import RerankMode, Settings
from vaultrag.index.base import ScoredChunk
from vaultrag.logging import get_logger

log = get_logger(__name__)

DEFAULT_TOP_N = 50
# No absolute floor by default. The vault's 0.3 (reranking.md / reranker-bge-v2-m3.md) was
# measured to systematically evict *relevant* chunks — bge-v2-m3's sigmoid scores land at
# ~0.02-0.25 on real content, so a fixed floor is score-distribution-dependent and can't
# generalize across vaults. A threshold sweep on the design vault (P1 Stage 1) showed 0.3
# was the worst point (recall@10 0.90) and 0.0 the best (1.00). Superseded pending
# ratification — see ../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md
# (the durable fix is a distribution-free / per-vault-calibrated gate, not another magic
# number). Raise this per-vault (VAULTRAG_RERANK_THRESHOLD) to drop low-relevance hits.
DEFAULT_THRESHOLD = 0.0
DEFAULT_DOMINANT_RATIO = 1.5
DEFAULT_MAX_LENGTH = 512
DEFAULT_BATCH = 16

DEFAULT_RERANK_REPO = "onnx-community/bge-reranker-v2-m3-ONNX"
DEFAULT_RERANK_FILE = "onnx/model_int8.onnx"


class RerankError(RuntimeError):
    """The reranker could not be built or could not score a batch.

    Raised (never swallowed into a fake score) so the composition root can decide to
    degrade to :class:`NoOpReranker` rather than silently reorder by a garbage number
    (CLAUDE.md §5, error handling).
    """


@dataclass(frozen=True, slots=True)
class RerankParams:
    """Tunables for the rerank stage — all env-configurable (see :class:`Settings`).

    ``top_n`` candidates go to the model; survivors of ``threshold`` are returned (capped
    to the caller's ``k``). ``dominant_ratio`` gates the per-query decision: when the top-1
    fusion score is at least this multiple of the top-2, fusion is trusted and the model is
    skipped. ``0`` means never skip — which is exactly how :attr:`RerankMode.always` is
    expressed, so mode and gating are one mechanism rather than two that can disagree.
    """

    top_n: int = DEFAULT_TOP_N
    threshold: float = DEFAULT_THRESHOLD
    dominant_ratio: float = DEFAULT_DOMINANT_RATIO

    @classmethod
    def for_mode(
        cls,
        mode: RerankMode,
        *,
        top_n: int = DEFAULT_TOP_N,
        threshold: float = DEFAULT_THRESHOLD,
        dominant_ratio: float = DEFAULT_DOMINANT_RATIO,
    ) -> RerankParams:
        """Build the params a mode implies. ``always`` forces the gate open (ratio 0).

        ``never`` needs no params at all — the composition root simply builds no reranker —
        but it maps to the same "always" shape so a caller can construct params without
        branching.
        """
        return cls(
            top_n=top_n,
            threshold=threshold,
            dominant_ratio=0.0 if mode is not RerankMode.auto else dominant_ratio,
        )


def rerank_params_for(settings: Settings) -> RerankParams:
    """The stage-4 tunables a config implies, with the mode already folded in.

    Lives here rather than on :class:`~vaultrag.config.Settings` because ``config`` is a
    dependency-free leaf module — everything imports it, so it must import nothing back.
    One place decides how a mode becomes gating behaviour, so the CLI and the eval harness
    cannot drift into measuring something other than what the server runs.
    """
    return RerankParams.for_mode(
        settings.rerank_mode,
        top_n=settings.rerank_top_n,
        threshold=settings.rerank_threshold,
        dominant_ratio=settings.rerank_dominant_ratio,
    )


def should_rerank(candidates: Sequence[ScoredChunk], params: RerankParams) -> bool:
    """``auto``'s per-query decision: does fusion look undecided enough to be worth it?

    The signal is the **top-1/top-2 RRF margin**, which is a real statement about
    retriever agreement rather than a heuristic about the query text. An RRF score is
    ``Σ 1/(60 + rank)`` over the lists a chunk appears in, so a chunk ranked #1 by *both*
    dense and BM25 scores roughly twice one that only one retriever surfaced. A dominant
    top-1 therefore means "both retrievers independently agree on this hit" — the
    known-item case (a verbatim identifier, an exact title) where a cross-encoder has
    nothing to add and everything to lose.

    Deliberately **not** an LLM call, and deliberately not query-text classification:
    this must stay $0, local, and deterministic enough that the eval gate reproduces it
    exactly (CLAUDE.md §2/§6). It is arithmetic on scores the fusion stage already
    computed, so ``auto`` costs nothing to *decide* — it only ever saves model calls.

    Note the threshold is score-distribution-dependent and so is per-vault, not a
    universal constant — see
    ``../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md``.
    """
    if params.dominant_ratio <= 0:
        return True  # mode `always` — gate held open
    if len(candidates) < 2:
        return False  # nothing to compare, and nothing for a reranker to reorder
    top1, top2 = candidates[0].score, candidates[1].score
    if top2 <= 0:
        return True  # no runner-up to measure against; don't claim confidence
    return top1 < params.dominant_ratio * top2


@runtime_checkable
class Reranker(Protocol):
    """Scores ``query x passage`` pairs. One score per passage, order preserved.

    Cross-encoders are CPU-bound, so implementations are synchronous and async callers run
    them in a worker thread (CLAUDE.md §5), exactly like the dense
    :class:`~vaultrag.embedding.base.Embedder`. Scores are relevance in ``[0, 1]`` (sigmoid
    of the model logit), directly comparable to :attr:`RerankParams.threshold`.
    """

    @property
    def model(self) -> str:
        """Model identifier — recorded for provenance/logging."""
        ...

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Return one relevance score in ``[0, 1]`` per passage, in input order."""
        ...


class NoOpReranker:
    """Pass-through reranker: every passage scores 1.0, so fusion order is preserved.

    Used when reranking is *enabled* but the model can't be loaded (offline, missing
    export): the pipeline keeps running and simply returns the fused top-k unchanged. A
    constant max score means the sort is a stable no-op and the 0.3 threshold drops
    nothing — the honest behavior for "we have no better signal than fusion".
    """

    @property
    def model(self) -> str:
        return "noop"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [1.0] * len(passages)


def rerank_candidates(
    query: str,
    candidates: list[ScoredChunk],
    reranker: Reranker | None,
    *,
    keep: int,
    params: RerankParams,
) -> list[ScoredChunk]:
    """Stage 4: re-score the fused ``candidates`` and return the best ``keep``.

    ``reranker is None`` means the stage is disabled entirely — candidates pass through
    (trimmed to ``keep``) with their RRF scores intact. Otherwise the top ``params.top_n``
    are scored ``query x child text``, re-sorted by score and thresholded; any candidates
    below ``top_n`` follow in fusion order, and the whole list is capped to ``keep``. So
    lowering ``top_n`` buys latency by reviewing fewer candidates — it never returns fewer
    results than fusion alone would. The known-item fast path (top-1 fusion dominance)
    returns the fused order without calling the model. A candidate whose payload has no
    ``text`` still scores (cross-encoders handle empty passages) — it just tends to rank
    last.
    """
    if reranker is None or len(candidates) <= 1:
        return candidates[:keep]

    if not should_rerank(candidates, params):
        log.debug("rerank.skip_dominant", top1=candidates[0].score, top2=candidates[1].score)
        return candidates[:keep]

    subset = candidates[: params.top_n]
    # Everything the cross-encoder never looked at. It keeps fusion order and sits *below*
    # every reranked survivor: `top_n` bounds what the model reads, not what search may
    # return, so a shallow `top_n` costs review depth rather than results. The tail is
    # deliberately exempt from `threshold` — an unscored candidate has no score to compare,
    # and dropping it would be claiming an irrelevance the model never asserted.
    tail = candidates[params.top_n :]
    scores = reranker.score(query, [_candidate_text(c) for c in subset])
    if len(scores) != len(subset):
        raise RerankError(f"reranker returned {len(scores)} scores for {len(subset)} passages")
    rescored = [replace(c, score=float(s)) for c, s in zip(subset, scores, strict=True)]
    # Stable sort: ties (e.g. every NoOp score) keep their fusion order.
    rescored.sort(key=lambda c: c.score, reverse=True)
    kept = [c for c in rescored if c.score >= params.threshold]
    return (kept + list(tail))[:keep]


def _candidate_text(candidate: ScoredChunk) -> str:
    """The child chunk body the cross-encoder scores (same field :func:`_to_snippet` reads)."""
    return str(candidate.payload.get("text", ""))


class BgeReranker:
    """bge-reranker-v2-m3 (ONNX int8) run locally via ``onnxruntime`` — the P1 default.

    Built by :meth:`load`, which pulls the int8 export + tokenizer from Hugging Face and
    opens a CPU inference session; the constructor itself takes already-built objects so
    tests can inject fakes without touching the network or a real model. The cross-encoder
    is XLM-RoBERTa-based: a ``(query, passage)`` pair tokenizes to
    ``<s> query </s></s> passage </s>`` and the model emits one logit per pair, turned into
    a ``[0, 1]`` score by a sigmoid. Only the input tensors the exported graph declares are
    fed (XLM-R needs ``input_ids`` + ``attention_mask``; ``token_type_ids`` is supplied as
    zeros iff the graph asks for it).
    """

    def __init__(
        self,
        *,
        session: Any,
        tokenizer: Any,
        model: str,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._model = model
        self._batch_size = batch_size
        self._input_names = frozenset(i.name for i in session.get_inputs())

    @classmethod
    def load(
        cls,
        *,
        repo_id: str = DEFAULT_RERANK_REPO,
        model_file: str = DEFAULT_RERANK_FILE,
        max_length: int = DEFAULT_MAX_LENGTH,
        batch_size: int = DEFAULT_BATCH,
    ) -> BgeReranker:
        """Download + open the reranker. Raises :class:`RerankError` on any failure.

        The heavy deps (``onnxruntime``, ``numpy``, ``tokenizers``, ``huggingface_hub``)
        are imported here, not at module load, so the read path stays light when reranking
        is off and a missing ML stack degrades to a clear error the caller turns into a
        :class:`NoOpReranker`, never an import crash at startup.
        """
        try:
            import numpy  # noqa: F401  — fail fast if the array stack is absent
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover — deps ship with the wheel
            raise RerankError(f"reranker dependencies are missing: {exc}") from exc

        try:
            model_path = hf_hub_download(repo_id=repo_id, filename=model_file)
            tokenizer_path = hf_hub_download(repo_id=repo_id, filename="tokenizer.json")
        except Exception as exc:  # network down, repo/file gone, offline cache miss
            raise RerankError(
                f"could not fetch reranker {repo_id!r} ({model_file}): {exc} — "
                "offline? set VAULTRAG_RERANK_ENABLED=false to run without a reranker"
            ) from exc

        try:
            session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise RerankError(f"could not open reranker ONNX session: {exc}") from exc

        tokenizer = Tokenizer.from_file(tokenizer_path)
        pad_id = tokenizer.token_to_id("<pad>")
        tokenizer.enable_truncation(max_length=max_length, strategy="only_second")
        tokenizer.enable_padding(pad_id=pad_id if pad_id is not None else 1, pad_token="<pad>")

        log.info(
            "rerank.loaded",
            model=repo_id,
            file=model_file,
            inputs=sorted(i.name for i in session.get_inputs()),
        )
        return cls(session=session, tokenizer=tokenizer, model=repo_id, batch_size=batch_size)

    @property
    def model(self) -> str:
        return self._model

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Score every ``(query, passage)`` pair; sigmoid of the model logit, in order."""
        if not passages:
            return []
        import numpy as np

        scores: list[float] = []
        for start in range(0, len(passages), self._batch_size):
            batch = passages[start : start + self._batch_size]
            encodings = self._tokenizer.encode_batch([(query, p) for p in batch])
            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            feed: dict[str, Any] = {"input_ids": input_ids, "attention_mask": attention_mask}
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)
            try:
                outputs = self._session.run(None, {n: feed[n] for n in self._input_names})
            except Exception as exc:
                raise RerankError(f"reranker inference failed: {exc}") from exc
            logits = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
            probs = 1.0 / (1.0 + np.exp(-logits))
            scores.extend(float(p) for p in probs)
        return scores


def load_reranker(
    *,
    mode: RerankMode,
    repo_id: str = DEFAULT_RERANK_REPO,
    model_file: str = DEFAULT_RERANK_FILE,
) -> Reranker | None:
    """Composition-root helper: build the configured reranker, degrading gracefully.

    Returns ``None`` for :attr:`RerankMode.never` (the stage is skipped and only ``k``
    candidates are fetched — no model, no download). For ``auto``/``always``, returns a
    :class:`BgeReranker`, or — if the model can't be fetched/opened — a
    :class:`NoOpReranker` so the server still starts and search still works, just without
    the rerank lift (a logged, observable degradation). This is what keeps ``auto`` a safe
    default on an offline or light install: it costs fusion order, not a crash.
    """
    if mode is RerankMode.never:
        return None
    try:
        return BgeReranker.load(repo_id=repo_id, model_file=model_file)
    except RerankError as exc:
        log.warning("rerank.load_failed", error=str(exc), fallback="noop")
        return NoOpReranker()
