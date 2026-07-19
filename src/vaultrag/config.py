"""Runtime settings, env-driven.

All configuration is read from the environment (prefix ``VAULTRAG_``) or a local
``.env`` file. Defaults target a single-box local-first deploy (principle #2): Ollama
for embeddings, Qdrant in Docker, SQLite for metadata. Paid backends are a config change
(a different URL/model), never a code change.

See ``../Rag/reference/reference-technology-stack.md`` for the stack these defaults track.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- reranker (retrieval stage 4; P1 Stage 1) -----------------------------
    # bge-reranker-v2-m3 ONNX-int8, loaded directly via onnxruntime (no torch/transformers).
    # See ../Rag/decisions/reranker-bge-v2-m3.md and projects/vaultrag/reranking.md.
    # DISABLED by default: on the P1 golden set, fusion-only beats rerank (re-measured
    # 2026-07-16 on the Stage-2 index: fusion recall@10 1.00 / MRR@5 0.775 vs rerank 0.95 /
    # 0.743 — rerank even drops a note out of recall@10). Exact numbers drift with the index;
    # the ordering (fusion > rerank on this title-matchy set) is the stable finding. We don't
    # ship a measured regression on by default. The stage is code-complete behind this flag;
    # flip on with VAULTRAG_RERANK_ENABLED=true once it beats fusion on a harder golden set. See
    # ../Rag/agent-inbox/rerank-threshold-should-be-per-vault-not-global.md. When on, a
    # missing/unfetchable model degrades gracefully to a no-op reranker (fusion order).
    rerank_enabled: bool = Field(
        default=False,
        description="Run the cross-encoder rerank stage after RRF fusion. Off by default "
        "(fusion currently scores higher on the golden set); enable per-vault once it wins.",
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
        default=50,
        description="Number of top fused candidates fed to the cross-encoder (top-50 → top-k).",
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
        description="Skip reranking when the top-1 fusion score is at least this multiple "
        "of the top-2 (known-item fast path). Set to 0 to always rerank.",
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

    @property
    def db_path(self) -> Path:
        """Path to the SQLite metadata database inside the state dir."""
        return self.state_dir / "vaultrag.db"


def load_settings() -> Settings:
    """Construct :class:`Settings` from the environment."""
    return Settings()
