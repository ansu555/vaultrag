"""Frozen embeddings so the gate can run with no Ollama and no Docker.

Decision 1 of ``../Rag/projects/vaultrag/p2-step-plan.md``: CI must produce the *same*
number on every run, on a box with no model server. The three ways to get there are not
equally good.

* A fake/hash embedder is hermetic but tests nothing — it would gate the plumbing while
  telling us nothing about whether **Qwen3** actually retrieves the right note. A gate
  that cannot fail for the reason we care about is theatre.
* Running the model in CI is honest but slow, heavy, and still not bit-reproducible
  across runtimes.
* **Recording real Qwen3 vectors once and replaying them** keeps the real model's
  behaviour and makes the run deterministic and offline. That is what this module does.

The recorded artefact is a small SQLite file (``tests/eval/vectors.db``) keyed
``(model, dims, blake3(text))`` — the same content-addressed shape as the production
``embed_cache`` table, extended to cover **query** strings too, which the chunk-keyed
cache cannot hold. Refresh it with ``vaultrag eval --record`` whenever the corpus, the
golden queries, or the embedding model changes.

:class:`ReplayEmbedder` **raises on a miss**. It never falls back to a live call and never
invents a vector: a silently-fabricated embedding would make the gate green while
measuring nothing, which is the one failure mode this whole design exists to prevent
(repo ``CLAUDE.md`` §5, "never fake success").
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from blake3 import blake3

from vaultrag.embedding.base import (
    Embedder,
    EmbeddingError,
    Vector,
    deserialize_vector,
    serialize_vector,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    model     TEXT NOT NULL,
    dims      INTEGER NOT NULL,
    text_sha  TEXT NOT NULL,
    vector    BLOB NOT NULL,
    PRIMARY KEY (model, dims, text_sha)
);
"""


def text_key(text: str) -> str:
    """Content address for a piece of embeddable text (chunk body or query).

    BLAKE3 to match ``Chunk.chunk_hash``; the fixture is content-addressed for the same
    reason the embed cache is — an unchanged note or query keeps its recorded vector, so
    re-recording after an unrelated edit is nearly free.
    """
    return blake3(text.encode("utf-8")).hexdigest()


class VectorFixture:
    """Read/write access to the committed frozen-vector file."""

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self._path = path
        if not create and not path.exists():
            raise EmbeddingError(
                f"frozen vector fixture not found: {path}\n"
                "record it against a live Ollama with: uv run vaultrag eval --record"
            )
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def get(self, model: str, dims: int, text: str) -> Vector | None:
        row = self._conn.execute(
            "SELECT vector FROM vectors WHERE model = ? AND dims = ? AND text_sha = ?",
            (model, dims, text_key(text)),
        ).fetchone()
        return deserialize_vector(row[0]) if row else None

    def put(self, model: str, dims: int, text: str, vector: Sequence[float]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO vectors (model, dims, text_sha, vector) VALUES (?, ?, ?, ?)",
            (model, dims, text_key(text), serialize_vector(vector)),
        )
        self._conn.commit()

    def count(self, model: str | None = None, dims: int | None = None) -> int:
        if model is None or dims is None:
            row = self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM vectors WHERE model = ? AND dims = ?", (model, dims)
            ).fetchone()
        return int(row[0])

    def vacuum(self) -> None:
        """Compact the file — it is committed to git, so size is a real cost."""
        self._conn.execute("VACUUM")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> VectorFixture:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class ReplayEmbedder:
    """An :class:`~vaultrag.embedding.base.Embedder` that only ever reads the fixture.

    Satisfies the same protocol the indexer and query path already depend on, so the gate
    exercises the **real** chunking → embed → upsert → hybrid-query pipeline with the real
    recorded vectors; the only substitution is where the floats come from.
    """

    def __init__(self, fixture: VectorFixture, *, model: str, dims: int) -> None:
        self._fixture = fixture
        self._model = model
        self._dims = dims

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for text in texts:
            vector = self._fixture.get(self._model, self._dims, text)
            if vector is None:
                raise EmbeddingError(
                    f"no recorded vector for {self._model}@{self._dims} "
                    f"(text starts {text[:60]!r}).\n"
                    "The corpus or the golden queries changed since the fixture was "
                    "recorded — refresh it with: uv run vaultrag eval --record"
                )
            vectors.append(vector)
        return vectors

    def close(self) -> None:
        """No-op: the fixture's owner closes it."""


class RecordingEmbedder:
    """Wraps a live embedder and writes every vector it produces into the fixture.

    Used only by ``vaultrag eval --record``. Reads through the fixture first, so
    re-recording after adding three golden queries costs three model calls, not a
    full re-embed of the corpus.
    """

    def __init__(self, inner: Embedder, fixture: VectorFixture) -> None:
        self._inner = inner
        self._fixture = fixture
        self.recorded = 0
        self.reused = 0

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dims(self) -> int:
        return self._inner.dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        model, dims = self._inner.model, self._inner.dims
        out: list[Vector] = []
        missing: list[tuple[int, str]] = []
        for i, text in enumerate(texts):
            cached = self._fixture.get(model, dims, text)
            if cached is None:
                out.append([])  # placeholder, filled below
                missing.append((i, text))
            else:
                out.append(cached)
                self.reused += 1
        if missing:
            fresh = self._inner.embed([text for _, text in missing])
            for (i, text), vector in zip(missing, fresh, strict=True):
                self._fixture.put(model, dims, text, vector)
                out[i] = vector
                self.recorded += 1
        return out

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()
