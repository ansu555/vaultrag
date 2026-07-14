"""Step 6 verify: embed sample chunks -> dims-wide vectors; re-embed hits cache (0 calls).

The Ollama backend is exercised against an ``httpx.MockTransport`` so these tests need no
live Ollama server (the real one is an external dependency this repo doesn't control). The
cache-through path uses the real SQLite ``embed_cache`` table from Step 2.
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from vaultrag.config import Settings
from vaultrag.embedding import (
    Embedder,
    EmbeddingError,
    OllamaEmbedder,
    QueryEmbedder,
    deserialize_vector,
    embed_chunks,
    fit_dims,
    l2_normalize,
    serialize_vector,
)
from vaultrag.models import Chunk, document_id
from vaultrag.store import SQLiteMetaStore

PROJECT = "vaultrag-design"
PATH = "projects/vaultrag/embedding-pipeline.md"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeEmbedder:
    """An in-memory :class:`Embedder` that counts calls — for cache-behavior tests."""

    def __init__(self, *, dims: int = 4, model: str = "fake-model") -> None:
        self._dims = dims
        self._model = model
        self.calls = 0  # number of .embed() invocations
        self.embedded = 0  # total texts embedded

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts):
        self.calls += 1
        self.embedded += len(texts)
        # Deterministic, small integers → exactly representable in float32.
        return [[float(len(t) + i) for i in range(self._dims)] for t in texts]


def _mock_client(embed_fn, recorder, base_url="http://localhost:11434"):
    """An httpx.Client whose /api/embed returns ``embed_fn(text)`` per input."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        body = json.loads(request.content)
        embeddings = [embed_fn(t) for t in body["input"]]
        return httpx.Response(200, json={"model": body["model"], "embeddings": embeddings})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


def _chunk(ix: int, text: str, chunk_hash: str) -> Chunk:
    return Chunk(
        project_id=PROJECT,
        doc_id=document_id(PROJECT, PATH),
        path=PATH,
        ix=ix,
        text=text,
        chunk_hash=chunk_hash,
        tokens=len(text),
    )


@pytest.fixture
def store(tmp_path):
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Vector codec + post-processing
# ---------------------------------------------------------------------------
def test_vector_codec_round_trips_float32() -> None:
    vec = [0.0, 1.0, -0.5, 0.25, 123.5]
    back = deserialize_vector(serialize_vector(vec))
    assert back == pytest.approx(vec, abs=1e-6)
    # float32: 4 bytes each.
    assert len(serialize_vector(vec)) == 4 * len(vec)


def test_l2_normalize_gives_unit_norm() -> None:
    out = l2_normalize([3.0, 4.0])
    assert out == pytest.approx([0.6, 0.8])
    assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, abs_tol=1e-9)


def test_l2_normalize_leaves_zero_vector_alone() -> None:
    assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_fit_dims_truncates_and_renormalizes() -> None:
    out = fit_dims([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 4)
    assert len(out) == 4
    assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, abs_tol=1e-9)


def test_fit_dims_passthrough_when_native_matches() -> None:
    out = fit_dims([0.6, 0.8], 2)  # already unit norm
    assert out == pytest.approx([0.6, 0.8])


def test_fit_dims_rejects_too_few_dims() -> None:
    with pytest.raises(EmbeddingError, match="cannot pad up"):
        fit_dims([1.0, 2.0], 4)


# ---------------------------------------------------------------------------
# OllamaEmbedder (mocked transport)
# ---------------------------------------------------------------------------
def test_ollama_embedder_satisfies_protocol() -> None:
    emb = OllamaEmbedder(model="m", dims=4, client=_mock_client(lambda _t: [0.0] * 4, []))
    assert isinstance(emb, Embedder)


def test_ollama_embed_returns_unit_vectors_in_order() -> None:
    recorder: list[httpx.Request] = []
    emb = OllamaEmbedder(
        model="qwen3",
        dims=4,
        client=_mock_client(lambda t: [float(len(t))] * 6, recorder),  # native 6 -> fit to 4
    )
    out = emb.embed(["a", "bb", "ccc"])
    assert len(out) == 3
    assert all(len(v) == 4 for v in out)
    for v in out:
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-6)
    # one request, correct endpoint + payload, order preserved
    assert len(recorder) == 1
    assert recorder[0].url.path == "/api/embed"
    sent = json.loads(recorder[0].content)
    assert sent == {"model": "qwen3", "input": ["a", "bb", "ccc"]}


def test_ollama_embed_splits_into_batches() -> None:
    recorder: list[httpx.Request] = []
    emb = OllamaEmbedder(
        model="m",
        dims=2,
        batch_size=2,
        client=_mock_client(lambda t: [float(ord(t[0]))] * 2, recorder),
    )
    texts = ["a", "b", "c", "d", "e"]
    out = emb.embed(texts)
    assert len(out) == 5
    assert len(recorder) == 3  # 2 + 2 + 1
    # concatenation preserves global order: batch boundaries are seamless
    inputs = [json.loads(r.content)["input"] for r in recorder]
    assert inputs == [["a", "b"], ["c", "d"], ["e"]]


def test_ollama_embed_empty_makes_no_request() -> None:
    recorder: list[httpx.Request] = []
    emb = OllamaEmbedder(model="m", dims=4, client=_mock_client(lambda _t: [0.0] * 4, recorder))
    assert emb.embed([]) == []
    assert recorder == []


def test_ollama_embed_rejects_dim_mismatch() -> None:
    emb = OllamaEmbedder(model="m", dims=8, client=_mock_client(lambda _t: [0.1] * 4, []))
    with pytest.raises(EmbeddingError, match="embed_dims=8"):
        emb.embed(["x"])


def test_ollama_embed_surfaces_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'qwen3' not found, try pulling it"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    emb = OllamaEmbedder(model="qwen3", dims=4, client=client)
    with pytest.raises(EmbeddingError, match="not found"):
        emb.embed(["x"])


def test_ollama_embed_surfaces_connection_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    emb = OllamaEmbedder(model="m", dims=4, client=client)
    with pytest.raises(EmbeddingError, match="is Ollama running"):
        emb.embed(["x"])


def test_ollama_embed_rejects_count_mismatch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3, 0.4]]})  # 1 for 2 inputs

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    emb = OllamaEmbedder(model="m", dims=4, client=client)
    with pytest.raises(EmbeddingError, match="2 inputs"):
        emb.embed(["a", "b"])


def test_ollama_embed_rejects_missing_embeddings_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    emb = OllamaEmbedder(model="m", dims=4, client=client)
    with pytest.raises(EmbeddingError, match="embeddings"):
        emb.embed(["a"])


def test_from_settings_wires_model_dims_url() -> None:
    settings = Settings(
        embed_model="qwen3-embedding:0.6b",
        embed_dims=512,
        ollama_url="http://ollama.local:11434",
    )
    emb = OllamaEmbedder.from_settings(settings)
    try:
        assert emb.model == "qwen3-embedding:0.6b"
        assert emb.dims == 512
        assert str(emb._client.base_url) == "http://ollama.local:11434"
    finally:
        emb.close()


# ---------------------------------------------------------------------------
# Cache-through (embed_chunks) — the Step 6 "0 model calls on re-embed" property
# ---------------------------------------------------------------------------
def test_embed_chunks_caches_and_reembeds_nothing(store: SQLiteMetaStore) -> None:
    emb = FakeEmbedder(dims=4)
    chunks = [_chunk(0, "alpha", "h0"), _chunk(1, "beta", "h1"), _chunk(2, "gamma", "h2")]

    first = embed_chunks(emb, store, chunks)
    assert set(first) == {"h0", "h1", "h2"}
    assert all(len(v) == 4 for v in first.values())
    assert emb.calls == 1
    assert emb.embedded == 3

    # Second run: everything is cached → the model is never called.
    second = embed_chunks(emb, store, chunks)
    assert emb.calls == 1  # unchanged
    assert emb.embedded == 3  # unchanged
    # Cached vectors round-trip through float32 but match the originals.
    for h in first:
        assert second[h] == pytest.approx(first[h], abs=1e-6)


def test_embed_chunks_dedupes_identical_hashes(store: SQLiteMetaStore) -> None:
    emb = FakeEmbedder(dims=4)
    # Two chunks share a body hash → embed once, both map to the same vector.
    chunks = [_chunk(0, "same body", "dup"), _chunk(1, "same body", "dup")]
    out = embed_chunks(emb, store, chunks)
    assert set(out) == {"dup"}
    assert emb.embedded == 1


def test_embed_chunks_cache_is_scoped_by_dims(store: SQLiteMetaStore) -> None:
    chunks = [_chunk(0, "alpha", "h0")]
    small = FakeEmbedder(dims=4, model="qwen3")
    embed_chunks(small, store, chunks)
    assert small.calls == 1

    # Same model + hash but a different dims → different cache key → a real miss.
    big = FakeEmbedder(dims=8, model="qwen3")
    embed_chunks(big, store, chunks)
    assert big.calls == 1


def test_embed_chunks_empty_is_noop(store: SQLiteMetaStore) -> None:
    emb = FakeEmbedder()
    assert embed_chunks(emb, store, []) == {}
    assert emb.calls == 0


# ---------------------------------------------------------------------------
# Query LRU
# ---------------------------------------------------------------------------
def test_query_embedder_memoizes_repeat_queries() -> None:
    emb = FakeEmbedder(dims=4)
    qe = QueryEmbedder(emb)

    v1 = qe.embed_query("how does chunking work")
    v2 = qe.embed_query("how does chunking work")
    assert v1 == v2
    assert emb.calls == 1  # second query served from cache

    qe.embed_query("a different query")
    assert emb.calls == 2


def test_query_embedder_returns_fresh_list_each_call() -> None:
    emb = FakeEmbedder(dims=4)
    qe = QueryEmbedder(emb)
    v1 = qe.embed_query("q")
    v1.append(999.0)  # mutating the result must not corrupt the cache
    v2 = qe.embed_query("q")
    assert 999.0 not in v2
    assert emb.calls == 1
