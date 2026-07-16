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
    # DISABLED by default: on the P1 golden set, fusion-only beat rerank at every threshold
    # (recall@10 1.00 / MRR@5 0.825 vs rerank's best 1.00 / 0.796) — we don't ship a measured
    # regression on by default. The stage is code-complete behind this flag; flip it on with
    # VAULTRAG_RERANK_ENABLED=true once it beats fusion on a harder golden set. See
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
