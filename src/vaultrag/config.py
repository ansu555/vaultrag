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
