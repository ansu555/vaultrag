"""Ollama-backed :class:`Embedder` — the P0 dense backend (Qwen3-Embedding-0.6B).

Ollama serves the model locally (GPU if present, else CPU) and is free, so it is the
default per ``../Rag/decisions/dense-embeddings-qwen3.md`` (arch §8). This talks to the
newer batch endpoint ``POST /api/embed`` (``input`` accepts a list), so 32-64 chunks go
in one round-trip. ONNX int8 and paid APIs are future impls behind the same
:class:`~vaultrag.embedding.base.Embedder` protocol — a config swap, never a rewrite.

Requires a running Ollama with the model pulled::

    ollama pull qwen3-embedding:0.6b

If it is unreachable or the model is missing, calls raise :class:`EmbeddingError` with
the server's message rather than degrading silently (CLAUDE.md §5).
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import httpx
import structlog

from vaultrag.config import Settings
from vaultrag.embedding.base import EmbeddingError, Vector, fit_dims

log = structlog.get_logger(__name__)

# Ollama batch endpoint: {"model", "input": [str, ...]} -> {"embeddings": [[float, ...]]}.
_EMBED_PATH = "/api/embed"
_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_BATCH = 64  # embedding-pipeline.md: batch 32-64 chunks
_DEFAULT_TIMEOUT = 120.0  # CPU cold-start on a large batch is slow; keep generous


class OllamaEmbedder:
    """Dense embedder backed by a local Ollama server.

    Vectors are truncated to ``dims`` and L2-normalized (Matryoshka — see
    :func:`~vaultrag.embedding.base.fit_dims`). Inputs are split into ``batch_size``
    requests so one huge list can't blow up a single request. Pass ``client`` to inject a
    transport (tests use :class:`httpx.MockTransport`); otherwise one is owned and should
    be released via :meth:`close`.
    """

    def __init__(
        self,
        *,
        model: str,
        dims: int,
        base_url: str = _DEFAULT_BASE_URL,
        batch_size: int = _DEFAULT_BATCH,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        if dims <= 0:
            raise ValueError(f"dims must be positive, got {dims}")
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        self._model = model
        self._dims = dims
        self._batch_size = batch_size
        # Own the client only if we made it, so we don't close a caller's shared client.
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    @classmethod
    def from_settings(cls, settings: Settings) -> OllamaEmbedder:
        """Build from env-driven :class:`~vaultrag.config.Settings` (ollama url/model/dims)."""
        return cls(
            model=settings.embed_model,
            dims=settings.embed_dims,
            base_url=settings.ollama_url,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Embed ``texts`` → one unit-norm :class:`Vector` per input, order preserved."""
        if not texts:
            return []
        out: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            out.extend(self._embed_batch(batch))
        return out

    def close(self) -> None:
        """Close the underlying HTTP client (only if this embedder created it)."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OllamaEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- internals ------------------------------------------------------------
    def _embed_batch(self, batch: Sequence[str]) -> list[Vector]:
        t0 = time.perf_counter()
        payload = {"model": self._model, "input": list(batch)}
        try:
            resp = self._client.post(_EMBED_PATH, json=payload)
        except httpx.HTTPError as exc:  # connection refused, timeout, DNS, ...
            # Never log the texts — only shape/timing (CLAUDE.md §5, structured logging).
            log.error("embed.request_failed", model=self._model, n=len(batch), error=str(exc))
            raise EmbeddingError(
                f"Ollama request to {self._client.base_url}{_EMBED_PATH} failed: {exc} "
                "(is Ollama running?)"
            ) from exc

        if resp.status_code != httpx.codes.OK:
            detail = _error_detail(resp)
            log.error("embed.http_error", model=self._model, status=resp.status_code, detail=detail)
            raise EmbeddingError(
                f"Ollama returned {resp.status_code} for model {self._model!r}: {detail} "
                f"(is the model pulled? `ollama pull {self._model}`)"
            )

        embeddings = _extract_embeddings(resp, expected=len(batch), model=self._model)
        vectors = [fit_dims(raw, self._dims) for raw in embeddings]
        log.debug(
            "embed.batch",
            model=self._model,
            n=len(batch),
            dims=self._dims,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return vectors


def _error_detail(resp: httpx.Response) -> str:
    """Best-effort human-readable body — Ollama sends ``{"error": "..."}`` on failure."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict) and "error" in body:
        return str(body["error"])
    return str(body)[:200]


def _extract_embeddings(resp: httpx.Response, *, expected: int, model: str) -> list[list[float]]:
    """Pull and validate the ``embeddings`` array from an ``/api/embed`` 200 response."""
    try:
        body = resp.json()
    except ValueError as exc:
        raise EmbeddingError(f"Ollama returned non-JSON for model {model!r}") from exc
    embeddings = body.get("embeddings") if isinstance(body, dict) else None
    if not isinstance(embeddings, list):
        raise EmbeddingError(
            f"Ollama response for model {model!r} missing an 'embeddings' array "
            "(unexpected endpoint or version?)"
        )
    if len(embeddings) != expected:
        raise EmbeddingError(
            f"Ollama returned {len(embeddings)} embeddings for {expected} inputs "
            f"(model {model!r})"
        )
    return embeddings
