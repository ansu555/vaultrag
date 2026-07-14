"""Step 2 verify: create db; insert project/doc/chunks/tags; query back; no dupes.

Idempotency is a correctness requirement (CLAUDE.md §5), so most assertions here are
about re-running a write and getting the same rows — not new ones.
"""

from __future__ import annotations

import pytest

from vaultrag.models import Chunk, DocumentMetadata, chunk_point_id, document_id
from vaultrag.store import JobState, MetaStore, SQLiteMetaStore

PROJECT = "vaultrag-design"
PATH = "projects/vaultrag/chunking-strategy.md"


@pytest.fixture
def store(tmp_path):  # type: ignore[no-untyped-def]
    db = tmp_path / "state" / "vaultrag.db"
    s = SQLiteMetaStore(db)
    # a project must exist first — documents FK-reference it.
    s.upsert_project(PROJECT, name=PROJECT, vault_path=str(tmp_path))
    yield s
    s.close()


def _meta(hash: str = "blake3:aaa", tags: list[str] | None = None) -> DocumentMetadata:
    return DocumentMetadata(
        path=PATH,
        title="Chunking Strategy",
        hash=hash,
        tags=["project/vaultrag", "type/design"] if tags is None else tags,
        modified_ts=1_700_000_100,
    )


def _chunks(n: int = 3, hash_suffix: str = "v1") -> list[Chunk]:
    return [
        Chunk(
            project_id=PROJECT,
            doc_id=document_id(PROJECT, PATH),
            path=PATH,
            ix=i,
            text=f"chunk {i}",
            heading_path=["Design"],
            chunk_hash=f"blake3:{hash_suffix}-{i}",
            tokens=10 + i,
            parent_id=None if i == 0 else chunk_point_id(PROJECT, PATH, 0),
        )
        for i in range(n)
    ]


def test_sqlite_satisfies_protocol(store: SQLiteMetaStore) -> None:
    assert isinstance(store, MetaStore)


def test_init_schema_is_idempotent(store: SQLiteMetaStore) -> None:
    store.init_schema()  # second call must not raise
    assert store.get_project(PROJECT) is not None


def test_project_round_trip(store: SQLiteMetaStore) -> None:
    store.upsert_project(PROJECT, name=PROJECT, vault_path="/v", settings={"k": 1})
    p = store.get_project(PROJECT)
    assert p is not None
    assert p.name == PROJECT
    assert p.vault_path == "/v"
    assert p.settings == {"k": 1}
    assert store.get_project("nope") is None


def test_document_and_tags_round_trip(store: SQLiteMetaStore) -> None:
    doc_id = store.upsert_document(PROJECT, _meta())
    assert doc_id == document_id(PROJECT, PATH)

    doc = store.get_document(PROJECT, PATH)
    assert doc is not None
    assert doc.path == PATH
    assert doc.hash == "blake3:aaa"
    assert doc.mtime == 1_700_000_100
    assert doc.type == "note"
    assert store.tags_for_doc(doc_id) == ["project/vaultrag", "type/design"]
    assert store.document_manifest(PROJECT) == {PATH: "blake3:aaa"}


def test_document_upsert_is_idempotent_and_updates_in_place(store: SQLiteMetaStore) -> None:
    store.upsert_document(PROJECT, _meta(hash="blake3:aaa"))
    store.upsert_document(PROJECT, _meta(hash="blake3:bbb", tags=["only/one"]))
    # one row, updated fields, replaced tag set.
    assert store.document_manifest(PROJECT) == {PATH: "blake3:bbb"}
    doc_id = document_id(PROJECT, PATH)
    assert store.tags_for_doc(doc_id) == ["only/one"]


def test_chunks_upsert_query_and_no_dupes(store: SQLiteMetaStore) -> None:
    doc_id = store.upsert_document(PROJECT, _meta())
    store.upsert_chunks(_chunks(3))
    store.upsert_chunks(_chunks(3))  # re-insert must not duplicate

    hashes = store.chunk_hashes(doc_id)
    assert len(hashes) == 3
    assert set(hashes.values()) == {"blake3:v1-0", "blake3:v1-1", "blake3:v1-2"}

    c0 = store.get_chunk(chunk_point_id(PROJECT, PATH, 0))
    assert c0 is not None
    assert c0.ix == 0
    assert c0.heading_path == ["Design"]
    assert c0.parent_id is None


def test_chunk_edit_reembeds_only_changed(store: SQLiteMetaStore) -> None:
    doc_id = store.upsert_document(PROJECT, _meta())
    store.upsert_chunks(_chunks(3, hash_suffix="v1"))
    before = store.chunk_hashes(doc_id)
    # same ids (same project/path/ix), new content hashes → in-place update, still 3 rows.
    store.upsert_chunks(_chunks(3, hash_suffix="v2"))
    after = store.chunk_hashes(doc_id)
    assert set(after) == set(before)
    assert set(after.values()) == {"blake3:v2-0", "blake3:v2-1", "blake3:v2-2"}


def test_reupserting_document_preserves_its_chunks(store: SQLiteMetaStore) -> None:
    doc_id = store.upsert_document(PROJECT, _meta())
    store.upsert_chunks(_chunks(3))
    # a REPLACE-style upsert would cascade-delete the chunks; DO UPDATE must not.
    store.upsert_document(PROJECT, _meta(hash="blake3:bbb"))
    assert len(store.chunk_hashes(doc_id)) == 3


def test_delete_document_cascades(store: SQLiteMetaStore) -> None:
    doc_id = store.upsert_document(PROJECT, _meta())
    store.upsert_chunks(_chunks(3))
    store.delete_document(PROJECT, PATH)
    assert store.get_document(PROJECT, PATH) is None
    assert store.chunk_hashes(doc_id) == {}
    assert store.tags_for_doc(doc_id) == []


def test_delete_chunks_for_doc(store: SQLiteMetaStore) -> None:
    doc_id = store.upsert_document(PROJECT, _meta())
    store.upsert_chunks(_chunks(3))
    store.delete_chunks_for_doc(doc_id)
    assert store.chunk_hashes(doc_id) == {}
    assert store.get_document(PROJECT, PATH) is not None  # doc itself survives


def test_job_enqueue_is_idempotent(store: SQLiteMetaStore) -> None:
    j1 = store.enqueue_job(PROJECT, "index", path=PATH, hash="blake3:aaa")
    store.set_job_state(j1, JobState.done, increment_attempts=True)
    j2 = store.enqueue_job(PROJECT, "index", path=PATH, hash="blake3:aaa")
    assert j1 == j2
    job = store.get_job(j1)
    assert job is not None
    # re-enqueue was ignored — the completed state and attempt count survive.
    assert job.state == JobState.done
    assert job.attempts == 1
    # a different content hash is a distinct job.
    j3 = store.enqueue_job(PROJECT, "index", path=PATH, hash="blake3:bbb")
    assert j3 != j1


def test_job_failure_state(store: SQLiteMetaStore) -> None:
    jid = store.enqueue_job(PROJECT, "index", path=PATH, hash="h")
    store.set_job_state(jid, JobState.dead, error="boom", increment_attempts=True)
    job = store.get_job(jid)
    assert job is not None
    assert job.state == JobState.dead
    assert job.error == "boom"
    assert job.attempts == 1


def test_embed_cache(store: SQLiteMetaStore) -> None:
    store.put_cached_vector("qwen3", 1024, "h0", b"\x00\x01")
    store.put_cached_vector("qwen3", 1024, "h1", b"\x02\x03")
    got = store.get_cached_vectors("qwen3", 1024, ["h0", "h1", "missing"])
    assert got == {"h0": b"\x00\x01", "h1": b"\x02\x03"}
    # different dims is a different cache entry (Matryoshka truncation).
    assert store.get_cached_vectors("qwen3", 512, ["h0"]) == {}
    # re-put overwrites.
    store.put_cached_vector("qwen3", 1024, "h0", b"\xff")
    assert store.get_cached_vectors("qwen3", 1024, ["h0"]) == {"h0": b"\xff"}
    assert store.get_cached_vectors("qwen3", 1024, []) == {}
