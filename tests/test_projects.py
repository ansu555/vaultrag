"""P3 Stage 2 — project registry + per-project settings resolution.

Covers the store's registry read/delete side (``list_projects``/``delete_project`` +
the ``overview`` aggregates) and :class:`vaultrag.projects.ProjectRegistry` (CRUD,
settings overlay, and the tenant-isolation the Stage-2 verify hinges on: one project's
deletion leaves every other project's rows intact).
"""

from __future__ import annotations

import pytest

from vaultrag.config import Settings
from vaultrag.models import Chunk, DocumentMetadata, chunk_point_id, document_id
from vaultrag.projects import ProjectRegistry, UnknownProjectError
from vaultrag.store import SQLiteMetaStore


@pytest.fixture
def store(tmp_path):  # type: ignore[no-untyped-def]
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    yield s
    s.close()


def _seed_project(store: SQLiteMetaStore, pid: str, tags: list[str]) -> str:
    """Register a project and give it one document (with tags, chunks) and one job."""
    store.upsert_project(pid, name=pid, vault_path=f"/vaults/{pid}")
    path = f"notes/{pid}.md"
    doc_id = store.upsert_document(
        pid,
        DocumentMetadata(path=path, title=pid.title(), hash="blake3:x", tags=tags, modified_ts=1),
    )
    store.upsert_chunks(
        [
            Chunk(
                project_id=pid,
                doc_id=doc_id,
                path=path,
                ix=i,
                text=f"chunk {i}",
                chunk_hash=f"blake3:{pid}-{i}",
                tokens=10 + i,
                parent_id=None if i == 0 else chunk_point_id(pid, path, 0),
            )
            for i in range(2)
        ]
    )
    store.enqueue_job(pid, "ingest", path=path, hash="blake3:x")
    return doc_id


def _job_count(store: SQLiteMetaStore, pid: str) -> int:
    """White-box job count — jobs carry a bare project_id (no FK), so cascade is manual."""
    row = store._conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE project_id = ?", (pid,)
    ).fetchone()
    return int(row["n"])


# --- store: registry read + delete ------------------------------------------
def test_list_projects_is_empty_then_ordered(store: SQLiteMetaStore) -> None:
    assert store.list_projects() == []
    store.upsert_project("zeta", name="zeta", vault_path="/z")
    store.upsert_project("alpha", name="alpha", vault_path="/a")
    assert [p.id for p in store.list_projects()] == ["alpha", "zeta"]  # ORDER BY id


def test_delete_project_reports_existence(store: SQLiteMetaStore) -> None:
    store.upsert_project("a", name="a", vault_path="/a")
    assert store.delete_project("a") is True
    assert store.delete_project("a") is False  # already gone
    assert store.get_project("a") is None


def test_delete_project_cascades_only_its_own_rows(store: SQLiteMetaStore) -> None:
    _seed_project(store, "keep", ["t/keep"])
    _seed_project(store, "drop", ["t/drop"])

    assert store.delete_project("drop") is True

    # the dropped tenant is gone, top to bottom …
    assert store.get_project("drop") is None
    assert store.document_count("drop") == 0
    assert store.document_manifest("drop") == {}
    assert store.top_tags("drop") == []
    # … including its jobs, which carry a bare project_id with no FK to projects
    assert _job_count(store, "drop") == 0

    # … and the survivor is fully intact
    assert store.get_project("keep") is not None
    assert store.document_count("keep") == 1
    keep_doc = document_id("keep", "notes/keep.md")
    assert len(store.chunk_hashes(keep_doc)) == 2
    assert store.tags_for_doc(keep_doc) == ["t/keep"]
    assert _job_count(store, "keep") == 1


def test_document_count_and_top_tags(store: SQLiteMetaStore) -> None:
    store.upsert_project("p", name="p", vault_path="/p")
    for i, tags in enumerate([["a", "b"], ["a"], ["a", "c"]]):
        store.upsert_document(
            "p",
            DocumentMetadata(
                path=f"n{i}.md", title=f"n{i}", hash="blake3:h", tags=tags, modified_ts=1
            ),
        )
    assert store.document_count("p") == 3
    assert store.top_tags("p") == [("a", 3), ("b", 1), ("c", 1)]  # freq desc, then tag asc
    assert store.top_tags("p", limit=1) == [("a", 3)]


# --- registry: CRUD ----------------------------------------------------------
def _base(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(project_id="__base__", vault_path=tmp_path, state_dir=tmp_path / "state")


def test_add_registers_resolves_absolute_and_defaults_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "vaultA"
    vault.mkdir()
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), _base(tmp_path))
    row = reg.add("a", vault)
    assert row.name == "a"  # defaults to the id
    assert row.vault_path == str(vault.resolve())  # stored absolute
    assert [p.id for p in reg.list()] == ["a"]


def test_add_is_idempotent_and_updates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "v1").mkdir()
    (tmp_path / "v2").mkdir()
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), _base(tmp_path))
    reg.add("a", tmp_path / "v1", name="First")
    reg.add("a", tmp_path / "v2", name="Second")  # re-add updates in place
    assert len(reg.list()) == 1
    row = reg.get("a")
    assert row is not None and row.name == "Second"
    assert row.vault_path == str((tmp_path / "v2").resolve())


def test_add_rejects_a_non_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), _base(tmp_path))
    with pytest.raises(NotADirectoryError):
        reg.add("a", tmp_path / "does-not-exist")


def test_remove_returns_existence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "v").mkdir()
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), _base(tmp_path))
    reg.add("a", tmp_path / "v")
    assert reg.remove("a") is True
    assert reg.remove("a") is False


# --- registry: settings resolution ------------------------------------------
def test_resolve_overlays_vault_and_project_onto_base(tmp_path) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "v"
    vault.mkdir()
    base = _base(tmp_path)
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), base)
    reg.add("a", vault)

    resolved = reg.resolve("a")
    assert resolved.project_id == "a"
    assert resolved.vault_path == vault.resolve()
    # shared infrastructure stays the base's — one db, one collection, N tenants
    assert resolved.state_dir == base.state_dir
    assert resolved.qdrant_collection == base.qdrant_collection


def test_resolve_applies_per_project_overrides(tmp_path) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "v"
    vault.mkdir()
    base = _base(tmp_path)  # base default_top_k is 10
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), base)
    reg.add("a", vault, settings={"default_top_k": 25, "embed_model": "custom:1b"})

    resolved = reg.resolve("a")
    assert resolved.default_top_k == 25
    assert resolved.embed_model == "custom:1b"
    assert base.default_top_k == 10  # base is untouched


def test_resolve_unknown_project_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), _base(tmp_path))
    with pytest.raises(UnknownProjectError):
        reg.resolve("ghost")


def test_add_rejects_reserved_or_unknown_settings_keys(tmp_path) -> None:  # type: ignore[no-untyped-def]
    vault = tmp_path / "v"
    vault.mkdir()
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), _base(tmp_path))
    with pytest.raises(ValueError, match="reserved"):
        reg.add("a", vault, settings={"qdrant_url": "http://evil:6333"})  # reserved
    with pytest.raises(ValueError, match="unknown or reserved"):
        reg.add("a", vault, settings={"nonsense": 1})  # not a Settings field


def test_resolve_or_base_falls_back_for_unregistered(tmp_path) -> None:  # type: ignore[no-untyped-def]
    base = _base(tmp_path)
    reg = ProjectRegistry(SQLiteMetaStore(tmp_path / "state" / "db"), base)
    assert reg.resolve_or_base(None) is base
    assert reg.resolve_or_base("unregistered") is base  # single-project shorthand
    (tmp_path / "v").mkdir()
    reg.add("a", tmp_path / "v")
    assert reg.resolve_or_base("a").project_id == "a"
