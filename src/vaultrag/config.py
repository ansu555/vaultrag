"""Runtime settings, env-driven.

All configuration is read from the environment (prefix ``VAULTRAG_``) or a local
``.env`` file. Defaults target a single-box local-first deploy (principle #2): Ollama
for embeddings, Qdrant in Docker, SQLite for metadata. Paid backends are a config change
(a different URL/model), never a code change.

See ``../Rag/reference/reference-technology-stack.md`` for the stack these defaults track.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RerankMode(StrEnum):
    """Whether the cross-encoder runs — the user-facing choice (``VAULTRAG_RERANK_MODE``).

    Defined here, not in retrieval.rerank, because config is a dependency-free
    leaf that everything imports — it cannot import a retrieval module back without a
    cycle. rerank.py imports this enum from here.

    Reranking is not a free win: it is the single biggest accuracy lever *and* a measured
    regression on some vaults, and the two golden sets disagree for an understood reason.
    On the **P1 design-vault set** (title-matchy: queries share tokens with the target's
    title) fusion already ranks near-perfectly and the cross-encoder can only shuffle a
    correct #1 down — measured 1.00/0.775 fusion vs 0.95/0.743 reranked. On the **P2
    Meridian corpus** (deliberately paraphrased: most queries share no title token) the
    cross-encoder is what closes the semantic gap — measured MRR@5 0.892 → 0.928.

    Neither result is wrong; they are the same model meeting two different query
    distributions. A single global boolean has to be wrong on one of them, which is why
    the choice is a mode rather than a flag:

    * ``never``  — the stage never runs. Cheapest, and no model download.
    * ``auto``   — run it **per query**, only when fusion looks *undecided* (see
      :func:`should_rerank`). Confident known-item hits keep fusion's answer and pay
      nothing; ambiguous paraphrase queries get the cross-encoder. Zero-LLM and
      deterministic — the decision is arithmetic on scores already computed.
    * ``always`` — run it for every query, no gating. The honest A/B baseline, and the
      right pick for a vault measured to want it everywhere.

    **Why the default is** ``never`` **— latency, not quality.** Measured 2026-07-21
    (Apple Silicon, onnxruntime CPU): bge-v2-m3 costs **~500 ms per passage**
    (``model_int8`` 502, ``model_quantized`` 509 — quantization is not the bottleneck), so
    at the default ``rerank_top_n=50`` one reranked query spends **~25 s** in this stage.
    ``search`` is an interactive tool an agent blocks on, so that is not a trade-off worth
    offering by default. ``auto`` halves the exposure (it skips 37.5% of the corpus
    queries) but 25 s on the remainder is still 25 s. The quality lift is real — take it
    with ``always`` plus a smaller ``rerank_top_n`` on hardware that can afford it, or on a
    GPU/API reranker behind the same :class:`~vaultrag.retrieval.rerank.Reranker` protocol.
    """

    # Values are `never`/`always`, not `on`/`off`, on purpose: A/B overlays are YAML,
    # and YAML 1.1 parses bare `on`/`off` as *booleans* — `rerank_mode: on` would arrive
    # as True and fail validation in the one workflow this mode exists to support.
    never = "never"
    auto = "auto"
    always = "always"


class Settings(BaseSettings):
    """Env-driven configuration for the indexer and query service."""

    model_config = SettingsConfigDict(
        env_prefix="VAULTRAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- vault / project ------------------------------------------------------
    vault_path: Path = Field(
        default=Path("."),
        description="Filesystem root of the Obsidian vault to index.",
    )
    project_id: str = Field(
        default="default",
        description="Tenant/project scope for this index (payload-partitioned in Qdrant).",
    )
    vault_name: str | None = Field(
        default=None,
        description="Obsidian display name of the vault, for the obsidian://open citation "
        "URIs (retrieval stage 8). Not derivable from vault_path — the app names a vault "
        "independently of its folder — so it's configured; falls back to the vault "
        "directory name at the composition root. Unset (and no fallback) → no URI is emitted.",
    )

    # --- local state (derived cache; safe to delete + reindex) ----------------
    state_dir: Path = Field(
        default=Path("state"),
        description="Directory for the SQLite db and embed cache. Rebuildable via reindex.",
    )

    # --- vector store (Qdrant) ------------------------------------------------
    qdrant_url: str = Field(
        default="http://localhost:6333",
        description="Qdrant HTTP endpoint.",
    )
    qdrant_collection: str = Field(
        default="chunks",
        description="Single Qdrant collection; tenants separated by payload, not collection.",
    )

    # --- embeddings (Ollama for P0) -------------------------------------------
    ollama_url: str = Field(
        default="http://localhost:11434",
        description="Ollama endpoint serving the dense embedding model.",
    )
    embed_model: str = Field(
        default="qwen3-embedding:0.6b",
        description="Dense embedding model tag (behind the Embedder interface).",
    )
    embed_dims: int = Field(
        default=1024,
        description="Dense vector dimensionality (Qwen3-0.6B native 1024; Matryoshka-truncatable).",
    )

    # --- retrieval defaults ---------------------------------------------------
    default_top_k: int = Field(
        default=10,
        description="Default number of snippets returned by search.",
    )
    budget_tokens: int = Field(
        default=2000,
        description="Default token budget a search response must respect.",
    )

    # --- reranker (retrieval stage 4; P1 Stage 1, mode added P2) --------------
    # bge-reranker-v2-m3 ONNX-int8, loaded directly via onnxruntime (no torch/transformers).
    # See ../Rag/decisions/reranker-bge-v2-m3.md and projects/vaultrag/reranking.md.
    #
    # A tri-state, not a boolean, because the two golden sets genuinely disagree and both
    # are right: on the title-matchy P1 design set fusion beats rerank (1.00/0.775 vs
    # 0.95/0.743 — the cross-encoder shuffles an already-correct #1 down), while on the
    # paraphrase-heavy P2 Meridian corpus rerank wins (MRR@5 0.892 → 0.928). A single
    # global boolean has to be wrong on one of those distributions. `auto` decides per
    # query from the top-1/top-2 fusion margin (retrieval/rerank.should_rerank) — no LLM,
    # so the decision itself is free and only ever *saves* model calls (measured: it skips
    # 37.5% of the corpus queries at the default ratio).
    #
    # DEFAULT `never`, and the reason is LATENCY, not quality. Measured 2026-07-21 on this
    # box (Apple Silicon, onnxruntime CPU): bge-v2-m3 costs ~500 ms **per passage** —
    # model_int8 502, model_quantized 509 — so stage 4 costs ~0.5 s x rerank_top_n per
    # query. `search` is an interactive MCP tool an agent blocks on, so the passage count is
    # the whole ballgame; see `rerank_top_n` for why it dropped 50 -> 20. This stays a
    # user-set mode rather than a shipped default because the affordable point is
    # box-dependent. See ../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md.
    # `auto`/`always` degrade to a no-op reranker (fusion order) when the model can't be
    # fetched, so an offline or light install loses the lift, never the search.
    rerank_mode: RerankMode = Field(
        default=RerankMode.never,
        description="Cross-encoder rerank stage: 'never', 'auto' (per query, only when the "
        "fusion margin says the result is undecided), or 'always' (every query).",
    )
    rerank_model: str = Field(
        default="onnx-community/bge-reranker-v2-m3-ONNX",
        description="Hugging Face repo id of the reranker (ONNX int8, no torch/transformers).",
    )
    rerank_model_file: str = Field(
        default="onnx/model_int8.onnx",
        description="Model file within the rerank repo (int8 export for CPU).",
    )
    rerank_top_n: int = Field(
        default=20,
        description="Number of top fused candidates fed to the cross-encoder. Cost is linear "
        "in this; 20 sits just above the deepest fusion rank at which either golden set "
        "still has a winnable case (see `vaultrag eval --ceiling`). Lower it to buy latency.",
    )
    rerank_threshold: float = Field(
        default=0.0,
        description="Absolute rerank-score floor. Default 0.0 (no floor): a fixed cutoff is "
        "score-distribution-dependent and evicts relevant chunks (the vault's 0.3 was "
        "measured to regress recall — see agent-inbox/rerank-threshold-should-be-per-vault). "
        "Raise per-vault to drop low-relevance hits.",
    )
    rerank_dominant_ratio: float = Field(
        default=1.5,
        description="The 'auto' decision boundary: skip reranking when the top-1 fusion "
        "score is at least this multiple of the top-2 (both retrievers agree — a known-item "
        "hit). Higher reranks more often; 0 always reranks. Ignored unless mode is 'auto'. "
        "Score-distribution-dependent, so it is a per-vault knob, not a universal constant.",
    )

    # --- parent-swap (retrieval stage 5; P1 Stage 2) --------------------------
    # After rerank, swap a child for its enclosing H1/H2 section and dedupe same-section
    # children into one parent snippet, reconstructing the parent live from the vault (the
    # index stays derived; vault is truth — CLAUDE.md §6 #1). See
    # ../Rag/projects/vaultrag/chunking-strategy.md and arch §6/§10-stage5. ON by default:
    # unlike rerank it doesn't move which notes rank (it repackages within a note), so it's
    # golden-set-neutral while giving the agent coherent sections instead of fragments.
    parent_swap_enabled: bool = Field(
        default=True,
        description="Swap retrieved children for their enclosing section (retrieval stage "
        "5). Reconstructs the parent from the vault at query time; no parent text is stored.",
    )
    parent_max_tokens: int = Field(
        default=2000,
        description="Largest enclosing section (tokens) eligible for parent-swap; a bigger "
        "section is too coarse, so its child is kept instead (the ~2k cap, arch §6).",
    )

    # --- live watch (indexer; P1 Stage 4) -------------------------------------
    # watchdog Observer → per-path debounce → worker pool → ingest_path. Owned by
    # serve/indexer and `vaultrag watch`; `vaultrag mcp` runs it only with --watch (a
    # daemon thread, solo dogfooding). See ../Rag/projects/vaultrag/ingestion-pipeline.md
    # and decisions/in-process-jobs-not-redis-celery.md.
    watch_debounce_seconds: float = Field(
        default=1.5,
        description="Per-path quiet window before a changed file is (re)ingested. "
        "Obsidian saves via temp files; the debounce collapses one save's event storm "
        "into a single ingest.",
    )
    watch_workers: int = Field(
        default=4,
        description="Live-watch worker-pool size — how many distinct files ingest in "
        "parallel (per-path work still runs serially; last write wins).",
    )

    # --- write-back (MCP write tools + git auto-commit; P1 Stage 5) ------------
    # Agents create/edit notes through VaultRAG and the write is committed + reindexed
    # so it's retrievable the same session — this closes the memory loop (arch §17,
    # ../Rag/projects/vaultrag/write-back-design.md, ../Rag/reference/reference-security-
    # model.md). The guardrails below are the config surface of CLAUDE.md §6's write
    # safety; every default is deliberately conservative and per-vault overridable (no
    # baked-in magic — an open-source user tunes these, they aren't hardcoded cutoffs).
    writeback_enabled: bool = Field(
        default=True,
        description="Expose the write tools (write_note/append_section/patch_frontmatter). "
        "On by default — the memory loop is the point of P1. A read-only deployment sets "
        "this false (or `vaultrag mcp --no-write`); then no MetaStore is opened for writing.",
    )
    writeback_allow_roots: list[str] = Field(
        default_factory=lambda: ["agent-inbox"],
        description="Vault-relative folders writes may target (realpath must resolve under "
        "one of them). The default write root `agent-inbox/` is the first entry; add e.g. "
        "'decisions' to let agents edit those directly. Empty list disables all writes.",
    )
    writeback_author: str = Field(
        default="claude-code",
        description="Agent identity stamped into provenance frontmatter (author:) and used "
        "as the git commit author, so agent writes are distinguishable from a human's.",
    )
    writeback_max_bytes: int = Field(
        default=1_000_000,
        description="Largest note (UTF-8 bytes) a single write may produce — a runaway "
        "write is refused rather than indexed. Raise per-vault for genuinely large notes.",
    )
    writeback_max_writes_per_minute: int = Field(
        default=30,
        description="Per-session sliding-window cap on accepted writes; over it, writes are "
        "refused until the window clears. Guards against a looping agent flooding the vault.",
    )
    writeback_git_enabled: bool = Field(
        default=True,
        description="Auto-commit each accepted write to the vault's git repo "
        "(`agent(<author>): <what>`). Best-effort: a non-git vault still gets the write + "
        "index, the commit is just skipped. The vault, not git, is the source of truth.",
    )

    # --- logging --------------------------------------------------------------
    log_level: str = Field(
        default="INFO",
        description="Root log level for structlog output.",
    )

    @model_validator(mode="after")
    def _reject_retired_rerank_env(self) -> Settings:
        """Fail loudly if the retired ``VAULTRAG_RERANK_ENABLED`` is still set.

        ``rerank_enabled`` (bool) became :attr:`rerank_mode` (never/auto/always) in P2.
        pydantic-settings ignores env vars that match no field, so leaving the old one in a
        shell profile or compose file would silently do **nothing** — someone would set
        ``=true``, see no reranking, and conclude the stage is broken. An unhelpful error
        beats a silent no-op (CLAUDE.md §6: quality is measured, not assumed).
        """
        legacy = os.environ.get("VAULTRAG_RERANK_ENABLED")
        if legacy is not None:
            wanted = "always" if legacy.strip().lower() in {"1", "true", "yes", "on"} else "never"
            raise ValueError(
                "VAULTRAG_RERANK_ENABLED was replaced by VAULTRAG_RERANK_MODE "
                f"(never|auto|always) — unset it and use VAULTRAG_RERANK_MODE={wanted}, "
                "or 'auto' to let the fusion margin decide per query."
            )
        return self

    @property
    def db_path(self) -> Path:
        """Path to the SQLite metadata database inside the state dir."""
        return self.state_dir / "vaultrag.db"


def load_settings() -> Settings:
    """Construct :class:`Settings` from the environment."""
    return Settings()
