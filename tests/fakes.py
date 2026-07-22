"""Shared test doubles for the read path (Step 9 retrieval + Step 10 MCP).

Both layers need the same three fakes — a dense embedder, a BM25 encoder, and a vector
store that records what it was asked — so they live here rather than being copy-pasted
per module: two drifting copies of a protocol stand-in is how a test suite ends up
green against an interface the real code no longer has.

These are structural stand-ins, not mocks: each satisfies the corresponding protocol in
``vaultrag.embedding.base`` / ``vaultrag.index.base``, which is what the protocol
conformance tests in ``test_index.py`` pin down for the real implementations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from vaultrag.index.base import ScoredChunk, SearchFilters, SparseVector, VectorStoreError
from vaultrag.models import Chunk, Document
from vaultrag.retrieval import ParentSection
from vaultrag.store.base import ApiKeyRow

PROJECT = "vaultrag-design"


class FakeEmbedder:
    """Deterministic dense ``Embedder`` stand-in — only the call count matters."""

    def __init__(self, *, dims: int = 4) -> None:
        self._dims = dims
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FakeSparse:
    """Records the exact query text it was asked to encode."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[], values=[]) for _ in texts]

    def embed_query(self, text: str) -> SparseVector:
        self.queries.append(text)
        return SparseVector(indices=[1], values=[1.0])


class FakeReranker:
    """Scores passages by a caller-supplied ``text -> score`` map (default 0.0).

    Records the ``(query, passages)`` it was asked to score so tests can assert the stage
    called it (or didn't, on the dominant fast path). Satisfies
    :class:`~vaultrag.retrieval.rerank.Reranker`.
    """

    def __init__(self, scores: dict[str, float] | None = None, *, default: float = 0.0) -> None:
        self._scores = scores or {}
        self._default = default
        self.calls: list[tuple[str, list[str]]] = []

    @property
    def model(self) -> str:
        return "fake-reranker"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        return [self._scores.get(p, self._default) for p in passages]


class FakeParentResolver:
    """Returns a preset :class:`ParentSection` per ``(path, anchor)``; records every lookup.

    A ``(path, anchor)`` with no entry resolves to ``None`` — the "parent unavailable" case
    that must degrade to keeping the child. Satisfies
    :class:`~vaultrag.retrieval.parents.ParentResolver` without touching a vault.
    """

    def __init__(self, sections: Mapping[tuple[str, str], ParentSection] | None = None) -> None:
        self._sections = dict(sections or {})
        self.calls: list[tuple[str, str]] = []

    def resolve(self, path: str, anchor: str) -> ParentSection | None:
        self.calls.append((path, anchor))
        return self._sections.get((path, anchor))


def make_parent(
    *,
    path: str,
    anchor: str,
    text: str = "Full enclosing section body.",
    tokens: int = 40,
    heading: str | None = None,
    heading_path: Sequence[str] | None = None,
    line_start: int = 1,
    line_end: int = 5,
) -> ParentSection:
    """One canned reconstructed parent section (``retrieval/parents.py``)."""
    return ParentSection(
        path=path,
        heading=heading or anchor,
        anchor=anchor,
        heading_path=list(heading_path) if heading_path is not None else [anchor],
        line_start=line_start,
        line_end=line_end,
        text=text,
        tokens=tokens,
    )


class FakeVectorStore:
    """Records the kwargs ``.query()`` was called with; returns a preset hit list."""

    def __init__(self, hits: Sequence[ScoredChunk] = ()) -> None:
        self._hits = list(hits)
        self.calls: list[dict[str, object]] = []

    def ensure_collection(self) -> None:
        raise NotImplementedError

    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        vectors: Mapping[str, list[float]],
        *,
        embed_model: str,
        document: Document | None = None,
    ) -> int:
        raise NotImplementedError

    def delete_by_doc(self, project_id: str, doc_id: str) -> None:
        raise NotImplementedError

    def query(
        self,
        *,
        project_id: str,
        dense: list[float],
        sparse: SparseVector,
        limit: int,
        prefetch_limit: int = 100,
        filters: SearchFilters | None = None,
    ) -> list[ScoredChunk]:
        self.calls.append(
            {
                "project_id": project_id,
                "dense": dense,
                "sparse": sparse,
                "limit": limit,
                "prefetch_limit": prefetch_limit,
                "filters": filters,
            }
        )
        return self._hits

    def count(self, project_id: str | None = None) -> int:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DownVectorStore(FakeVectorStore):
    """Simulates Qdrant being unreachable at query time."""

    def query(self, **kwargs: object) -> list[ScoredChunk]:  # type: ignore[override]
        raise VectorStoreError("cannot reach Qdrant (is it running? `docker compose up -d qdrant`)")


class FakeKeyStore:
    """In-memory ``auth.KeyStore`` — no filesystem, no real database.

    Satisfies the same narrow protocol :class:`~vaultrag.store.sqlite.SQLiteMetaStore`
    implements for real (``auth.KeyAuthenticator`` only ever calls these five methods), so
    a test can stand up a key store in a line instead of opening a SQLite file it then has
    to close.
    """

    def __init__(self) -> None:
        self._rows: dict[str, ApiKeyRow] = {}

    def create_api_key(
        self,
        *,
        key_id: str,
        name: str | None,
        key_hash: str,
        scopes: Mapping[str, str],
        rate_limit: int,
    ) -> None:
        self._rows[key_id] = ApiKeyRow(
            id=key_id,
            name=name,
            key_hash=key_hash,
            scopes=dict(scopes),
            rate_limit=rate_limit,
            created_ts=0,
            last_used=None,
        )

    def get_api_key(self, key_id: str) -> ApiKeyRow | None:
        return self._rows.get(key_id)

    def list_api_keys(self) -> list[ApiKeyRow]:
        return list(self._rows.values())

    def delete_api_key(self, key_id: str) -> bool:
        return self._rows.pop(key_id, None) is not None

    def touch_api_key(self, key_id: str, *, used_ts: int) -> None:
        row = self._rows.get(key_id)
        if row is not None:
            self._rows[key_id] = replace(row, last_used=used_ts)


def make_hit(
    *,
    point_id: str = "point-1",
    score: float = 1.0,
    path: str = "projects/vaultrag/hybrid-search.md",
    text: str = "Dense catches paraphrase; BM25 catches exact tokens.",
    tokens: int = 10,
    **extra_payload: object,
) -> ScoredChunk:
    """One canned hit, shaped like a real payload (``index/payload.py``)."""
    payload: dict[str, object] = {
        "path": path,
        "title": "Hybrid Search Details",
        "heading_path": ["Design"],
        "anchor": "design",
        "text": text,
        "tokens": tokens,
        "doc_id": "doc-1",
        **extra_payload,
    }
    return ScoredChunk(point_id=point_id, score=score, payload=payload)
