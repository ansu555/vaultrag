"""Cross-store drift detection: does the manifest agree with the vector store?

VaultRAG keeps two derived caches of the same ingest — chunk rows in SQLite and points
in Qdrant — written together but lost independently. Only one of them is consulted when
``reindex`` decides what to skip, so when Qdrant loses data that SQLite still records
(volume wiped, collection recreated, server-vs-``:memory:`` mix-up) the manifest reports
those documents ``unchanged`` and reindex skips exactly what needs rebuilding. The drift
is stable: re-running never converges.

Found live on ``vaultrag-design`` (2026-07-21): 173 chunks in SQLite, 50 in Qdrant, and
retrieval kept returning plausible results from under a third of the vault with no signal
anywhere. That is the real severity — it silently invalidates any quality number measured
in that state. See ``../Rag/agent-inbox/reindex-cannot-repair-a-drifted-vector-store.md``.

This module is the one-query check no code path used to perform. Deliberately not a
repair: it reports, and ``reindex --force`` fixes.
"""

from __future__ import annotations

from dataclasses import dataclass

from vaultrag.index.base import VectorStore
from vaultrag.store.base import MetaStore

__all__ = ["Drift", "DriftError", "check_drift"]


class DriftError(ValueError):
    """Raised when a caller must not proceed against a drifted index.

    Subclasses :class:`ValueError` so the eval CLI's existing "can't run" handling maps
    it to exit 2 — a refusal to measure, never a quality number.
    """


@dataclass(frozen=True, slots=True)
class Drift:
    """A disagreement between the manifest's chunk count and the vector store's."""

    project_id: str
    manifest_chunks: int
    vector_points: int

    @property
    def missing(self) -> int:
        """Chunks the manifest claims are indexed but the vector store does not hold.

        Negative when the vector store holds *more* than the manifest accounts for —
        orphaned points (a delete that half-landed), which is the rarer direction but
        just as much a disagreement.
        """
        return self.manifest_chunks - self.vector_points

    def describe(self) -> str:
        return (
            f"index drift in {self.project_id!r}: manifest has {self.manifest_chunks} chunks, "
            f"vector store has {self.vector_points}"
        )


def check_drift(
    project_id: str,
    *,
    store: MetaStore,
    vector_store: VectorStore,
) -> Drift | None:
    """Compare manifest chunks against vector points. ``None`` when they agree.

    Both counts are project-scoped and exact — this is two cheap aggregate queries, so
    it is affordable to run after every reindex and before every live eval rather than
    hiding behind a flag nobody sets until they already suspect a problem.
    """
    manifest_chunks = store.chunk_count(project_id)
    vector_points = vector_store.count(project_id)
    if manifest_chunks == vector_points:
        return None
    return Drift(
        project_id=project_id,
        manifest_chunks=manifest_chunks,
        vector_points=vector_points,
    )
