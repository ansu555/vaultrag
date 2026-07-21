"""P1 Stage 5 verify: the write-back mechanism (:class:`vaultrag.writeback.NoteWriter`).

Drives the real writer against a real SQLite store + in-memory Qdrant + a fake embedder,
so every accepted write actually parses, chunks, (fake-)embeds, upserts, and becomes
retrievable — the memory loop, proven end-to-end without live services. Then the
guardrails (repo ``CLAUDE.md`` §6 / ``write-back-design.md``): containment, the write
allowlist, the optimistic lock, the size/rate caps, and the first-class one — **a planted
secret is unwritable *and* never indexed** (the write-side twin of the scan-boundary test).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from blake3 import blake3
from qdrant_client import QdrantClient

from vaultrag.embedding.base import EmbeddingError, Vector
from vaultrag.index import QdrantVectorStore, SparseVector
from vaultrag.index.base import VectorStoreError
from vaultrag.indexer.pipeline import IngestContext
from vaultrag.retrieval import obsidian_uri
from vaultrag.store.sqlite import SQLiteMetaStore
from vaultrag.writeback import NoteWriter, RefusalReason, WriteConflict, WriteRefused

PROJECT = "test-project"
DIMS = 4
# A syntactically real AWS access-key id (the canonical AWS docs example) — the write-side
# twin of the scan boundary's planted secret. It must never reach disk or the index.
PLANTED_SECRET = "AKIAIOSFODNN7EXAMPLE"


class FakeSparse:
    @property
    def model(self) -> str:
        return "fake-bm25"

    def embed_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [SparseVector(indices=[1], values=[float(len(t))]) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[1], values=[1.0])


class FakeEmbedder:
    def __init__(self, *, dims: int = DIMS) -> None:
        self._dims = dims

    @property
    def model(self) -> str:
        return "fake-embed-model"

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [[float(len(t) + i) for i in range(self._dims)] for t in texts]

    def close(self) -> None:
        pass


class Clock:
    """A hand-cranked wall clock so the rate window is deterministic."""

    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


NOTE_BODY = "## Notes\n\nFirst line of the running note.\n\n## Refs\n\nSee also the design doc.\n"


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SQLiteMetaStore(tmp_path / "state" / "vaultrag.db")
    s.upsert_project(PROJECT, name=PROJECT, vault_path=str(tmp_path / "vault"))
    yield s
    s.close()


@pytest.fixture
def vector_store() -> QdrantVectorStore:
    vs = QdrantVectorStore(
        dims=DIMS, collection="chunks", client=QdrantClient(":memory:"), sparse=FakeSparse()
    )
    vs.ensure_collection()
    return vs


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


def make_ctx(vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore) -> IngestContext:
    return IngestContext(
        project_id=PROJECT,
        vault_path=vault,
        store=store,
        vector_store=vector_store,
        embedder=FakeEmbedder(),
    )


def make_writer(
    vault: Path,
    store: SQLiteMetaStore,
    vector_store: QdrantVectorStore,
    **kwargs: object,
) -> NoteWriter:
    return NoteWriter(make_ctx(vault, store, vector_store), **kwargs)  # type: ignore[arg-type]


def git_init(vault: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=vault, check=True)


# ---------------------------------------------------------------------------
# write_note — the create path
# ---------------------------------------------------------------------------
def test_write_note_creates_commits_and_indexes(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    git_init(vault)
    writer = make_writer(vault, store, vector_store, vault_name="Vault")

    result = writer.write_note(
        title="JWT Refresh Decision",
        content="## Decision\n\nUse rotating refresh tokens.\n",
        tags=["decision", "auth"],
    )

    assert result.action == "created"
    assert result.path == "agent-inbox/jwt-refresh-decision.md"
    # On disk with provenance frontmatter.
    written = (vault / result.path).read_text(encoding="utf-8")
    assert "title: JWT Refresh Decision" in written
    assert "via: vaultrag" in written
    assert "author: claude-code" in written
    assert "Use rotating refresh tokens." in written
    # Committed to git and indexed so it's retrievable this session.
    assert result.committed is True and result.commit
    assert result.indexed is True and result.chunks >= 1
    assert store.get_document(PROJECT, result.path) is not None
    assert vector_store.count(PROJECT) >= 1
    assert result.uri == obsidian_uri("Vault", result.path)
    assert result.warning is None


def test_write_note_refuses_to_overwrite_an_existing_note(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    writer.write_note(title="Same Title", content="body one\n")
    with pytest.raises(WriteRefused) as exc:
        writer.write_note(title="Same Title", content="body two\n")
    assert exc.value.reason is RefusalReason.exists


def test_write_note_hash_is_the_base_hash_for_a_later_patch(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    created = writer.write_note(title="Note", content="## H\n\nbody\n")
    # The returned hash equals the file's content hash → usable directly as base_hash.
    patched = writer.patch_frontmatter(
        path=created.path, updates={"status": "final"}, base_hash=created.hash
    )
    assert patched.action == "patched"
    assert "status: final" in (vault / created.path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# append_section
# ---------------------------------------------------------------------------
def test_append_section_inserts_at_section_end_and_reindexes(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    created = writer.write_note(title="Running Log", content=NOTE_BODY)

    result = writer.append_section(
        path=created.path, heading="Notes", content="Second line, appended."
    )

    assert result.action == "appended"
    assert result.indexed is True
    text = (vault / created.path).read_text(encoding="utf-8")
    notes_idx = text.index("## Notes")
    refs_idx = text.index("## Refs")
    appended_idx = text.index("Second line, appended.")
    # The new line landed inside the Notes section, before the next heading.
    assert notes_idx < appended_idx < refs_idx


def test_append_section_refuses_an_unknown_heading(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    created = writer.write_note(title="Log", content=NOTE_BODY)
    with pytest.raises(WriteRefused) as exc:
        writer.append_section(path=created.path, heading="Nonexistent", content="x")
    assert exc.value.reason is RefusalReason.heading_not_found
    assert "Notes" in str(exc.value)  # names the real headings to retry with


def test_append_section_refuses_a_missing_note(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    with pytest.raises(WriteRefused) as exc:
        writer.append_section(path="agent-inbox/nope.md", heading="H", content="x")
    assert exc.value.reason is RefusalReason.not_found


# ---------------------------------------------------------------------------
# patch_frontmatter — optimistic lock
# ---------------------------------------------------------------------------
def test_patch_frontmatter_sets_keys_and_preserves_body(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    created = writer.write_note(title="Decision", content="## Body\n\nThe body stays.\n")

    writer.patch_frontmatter(
        path=created.path,
        updates={"status": "accepted", "tags": ["decision"]},
        base_hash=created.hash,
    )

    text = (vault / created.path).read_text(encoding="utf-8")
    assert "status: accepted" in text
    assert "The body stays." in text  # body untouched


def test_patch_frontmatter_conflict_returns_current_hash_and_content(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    created = writer.write_note(title="Decision", content="## Body\n\nbody\n")

    with pytest.raises(WriteConflict) as exc:
        writer.patch_frontmatter(
            path=created.path, updates={"status": "x"}, base_hash="stale-hash-000"
        )
    assert exc.value.current_hash == created.hash
    assert "body" in exc.value.current_text
    # A stale patch left the file untouched.
    assert "status: x" not in (vault / created.path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
def test_planted_secret_content_is_unwritable_and_never_indexed(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """First-class guardrail (CLAUDE.md §6): a note whose body is key material is refused,
    and nothing about it reaches disk or the index — the write-side twin of the scan test."""
    writer = make_writer(vault, store, vector_store)
    with pytest.raises(WriteRefused) as exc:
        writer.write_note(
            title="Prod Keys",
            content=f"## Keys\n\nAWS_ACCESS_KEY_ID = {PLANTED_SECRET}\n",
        )
    assert exc.value.reason is RefusalReason.secret
    assert not (vault / "agent-inbox" / "prod-keys.md").exists()
    assert store.get_document(PROJECT, "agent-inbox/prod-keys.md") is None
    assert vector_store.count(PROJECT) == 0


def test_secret_filename_is_refused(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    # slug 'my-credentials' → matches the *credentials* filename deny glob.
    with pytest.raises(WriteRefused) as exc:
        writer.write_note(title="my credentials", content="body\n")
    assert exc.value.reason is RefusalReason.gated


def test_write_refuses_path_traversal(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store)
    with pytest.raises(WriteRefused) as exc:
        writer.append_section(path="../../etc/passwd", heading="H", content="x")
    assert exc.value.reason in (RefusalReason.escapes_vault, RefusalReason.outside_write_root)
    assert not (vault.parent / "etc").exists()


# ---------------------------------------------------------------------------
# The create-then-grow memory loop — write_note must emit the H1 it promises
# ---------------------------------------------------------------------------
def test_write_note_writes_the_h1_its_schema_promises(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """The MCP schema says ``title`` "becomes the H1/frontmatter title"; it wrote only
    frontmatter, so the file had no heading at all."""
    writer = make_writer(vault, store, vector_store, git_enabled=False)
    result = writer.write_note(title="Gamma Findings", content="First observation.\n")
    written = (vault / result.path).read_text(encoding="utf-8")
    assert "# Gamma Findings" in written
    assert "title: Gamma Findings" in written  # frontmatter title still there too


def test_append_section_can_target_an_agent_created_note(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """The documented memory loop, end to end. Without an H1 this failed with
    "heading not found; headings here: (none)" — broken for exactly the notes agents make."""
    writer = make_writer(vault, store, vector_store, git_enabled=False)
    created = writer.write_note(title="Gamma Findings", content="First observation.\n")

    appended = writer.append_section(
        path=created.path, heading="Gamma Findings", content="Second observation."
    )
    assert appended.action == "appended"
    written = (vault / appended.path).read_text(encoding="utf-8")
    assert "First observation." in written
    assert "Second observation." in written


def test_write_note_does_not_stack_a_duplicate_h1(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """Content that already opens with an H1 keeps its own — two H1s would split the note
    and make `append_section(heading=title)` ambiguous."""
    writer = make_writer(vault, store, vector_store, git_enabled=False)
    result = writer.write_note(title="Gamma Findings", content="# Gamma Findings\n\nBody.\n")
    written = (vault / result.path).read_text(encoding="utf-8")
    assert written.count("# Gamma Findings") == 1


def test_write_note_adds_an_h1_when_content_starts_at_h2(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """A note opening at `##` has no H1, so one is still added (and `##` is not mistaken
    for one)."""
    writer = make_writer(vault, store, vector_store, git_enabled=False)
    result = writer.write_note(title="Gamma Findings", content="## Detail\n\nBody.\n")
    written = (vault / result.path).read_text(encoding="utf-8")
    assert "# Gamma Findings" in written
    assert "## Detail" in written


# ---------------------------------------------------------------------------
# Markdown-only — writability is not inherited from indexability
# ---------------------------------------------------------------------------
# A minimal but genuinely binary PDF: byte 0x80 is invalid UTF-8, so a lossy read
# followed by a UTF-8 write is unambiguously destructive.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n\x80\x81\xfe\ntrailer\n%%EOF\n"


def test_patch_frontmatter_cannot_destroy_a_pdf(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """The reported two-call kill: 409 leaks the live hash, call two overwrites the file.

    P2 made ``.pdf`` indexable, and the writer inferred writability from that — so a PDF
    became a legal target for a tool whose every path ends in a UTF-8 text write.
    """
    writer = make_writer(vault, store, vector_store)
    target = vault / "agent-inbox" / "paper.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PDF_BYTES)

    # Call 1 — a wrong base_hash must NOT hand back the live hash for a file that could
    # never be legally written; the suffix gate fires before the optimistic lock.
    with pytest.raises(WriteRefused) as exc:
        writer.patch_frontmatter(
            path="agent-inbox/paper.pdf", updates={"tags": ["x"]}, base_hash=""
        )
    assert exc.value.reason is RefusalReason.not_markdown

    # Call 2 — even holding the true hash, the write is refused.
    with pytest.raises(WriteRefused) as exc:
        writer.patch_frontmatter(
            path="agent-inbox/paper.pdf",
            updates={"tags": ["x"]},
            base_hash=blake3(PDF_BYTES).hexdigest(),
        )
    assert exc.value.reason is RefusalReason.not_markdown
    assert target.read_bytes() == PDF_BYTES  # byte-identical: nothing was rewritten


@pytest.mark.parametrize("name", ["scan.pdf", "diagram.png", "photo.jpeg", "shot.webp"])
def test_indexable_binaries_are_not_writable(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore, name: str
) -> None:
    """Every suffix P2 added to the *index* set must stay outside the *write* set."""
    writer = make_writer(vault, store, vector_store)
    target = vault / "agent-inbox" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PDF_BYTES)

    with pytest.raises(WriteRefused) as exc:
        writer.append_section(path=f"agent-inbox/{name}", heading="H", content="x")
    assert exc.value.reason is RefusalReason.not_markdown
    assert target.read_bytes() == PDF_BYTES


def test_markdown_stays_writable(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """The gate narrows writes to markdown — it must not narrow them past markdown."""
    writer = make_writer(vault, store, vector_store)
    for rel in ("agent-inbox/a.md", "agent-inbox/b.markdown"):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text("---\ntitle: T\n---\n\n# T\n\nbody\n", encoding="utf-8")
        result = writer.patch_frontmatter(
            path=rel,
            updates={"status": "reviewed"},
            base_hash=blake3((vault / rel).read_bytes()).hexdigest(),
        )
        assert result.action == "patched"


def test_undecodable_markdown_is_refused_not_mangled(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """A .md file holding non-UTF-8 bytes is unfixable by a rewrite — refuse, don't
    silently commit U+FFFD over the original bytes (the other half of the same defect)."""
    writer = make_writer(vault, store, vector_store)
    rel = "agent-inbox/latin1.md"
    raw = "---\ntitle: Caf\xe9\n---\n\n# Heading\n\nbody\n".encode("latin-1")
    (vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vault / rel).write_bytes(raw)

    with pytest.raises(WriteRefused) as exc:
        writer.patch_frontmatter(path=rel, updates={"x": 1}, base_hash=blake3(raw).hexdigest())
    assert exc.value.reason is RefusalReason.not_utf8

    with pytest.raises(WriteRefused) as exc:
        writer.append_section(path=rel, heading="Heading", content="more")
    assert exc.value.reason is RefusalReason.not_utf8
    assert (vault / rel).read_bytes() == raw
    assert b"\xef\xbf\xbd" not in (vault / rel).read_bytes()  # no U+FFFD committed


def test_write_refuses_outside_the_allowlist(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store, allow_roots=["agent-inbox"])
    with pytest.raises(WriteRefused) as exc:
        writer.write_note(title="Direct Decision", content="body\n", folder="decisions")
    assert exc.value.reason is RefusalReason.outside_write_root


def test_allowlist_can_be_widened_per_vault(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store, allow_roots=["agent-inbox", "decisions"])
    result = writer.write_note(title="Direct Decision", content="body\n", folder="decisions")
    assert result.path == "decisions/direct-decision.md"
    assert (vault / result.path).is_file()


def test_write_refuses_oversize_note(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    writer = make_writer(vault, store, vector_store, max_bytes=50)
    with pytest.raises(WriteRefused) as exc:
        writer.write_note(title="Big", content="x" * 500)
    assert exc.value.reason is RefusalReason.too_large
    assert not (vault / "agent-inbox" / "big.md").exists()


def test_rate_limit_refuses_after_the_cap(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    clock = Clock()
    writer = make_writer(vault, store, vector_store, max_writes_per_minute=2, now=clock)

    writer.write_note(title="One", content="a\n")
    writer.write_note(title="Two", content="b\n")
    with pytest.raises(WriteRefused) as exc:
        writer.write_note(title="Three", content="c\n")
    assert exc.value.reason is RefusalReason.rate_limited

    # The window slides: 61 s later a write is accepted again.
    clock.t += 61.0
    result = writer.write_note(title="Four", content="d\n")
    assert result.action == "created"


# ---------------------------------------------------------------------------
# git audit (best-effort)
# ---------------------------------------------------------------------------
def test_git_commit_attributes_the_write_to_the_agent(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    git_init(vault)
    writer = make_writer(vault, store, vector_store, author="claude-code")

    result = writer.write_note(title="Audited", content="body\n")

    assert result.committed is True
    log = subprocess.run(
        ["git", "log", "-1", "--format=%an%n%s"],
        cwd=vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert log[0] == "claude-code"  # committed as the agent identity, not the human
    assert log[1] == f"agent(claude-code): created {result.path}"


def test_non_git_vault_still_writes_and_indexes(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    """A vault that isn't a git repo loses only the audit line, never the write."""
    writer = make_writer(vault, store, vector_store)  # vault fixture is not a git repo
    result = writer.write_note(title="No Git", content="## H\n\nbody\n")
    assert result.committed is False and result.commit is None
    assert result.indexed is True
    assert (vault / result.path).is_file()


def test_git_disabled_skips_commit_but_still_indexes(
    vault: Path, store: SQLiteMetaStore, vector_store: QdrantVectorStore
) -> None:
    git_init(vault)
    writer = make_writer(vault, store, vector_store, git_enabled=False)
    result = writer.write_note(title="No Commit", content="## H\n\nbody\n")
    assert result.committed is False
    assert result.indexed is True
    # git_enabled=False → no commit was made even though the vault is a repo.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=vault, capture_output=True, text=True, check=False
    )
    assert log.stdout.strip() == ""


# ---------------------------------------------------------------------------
# graceful degradation: the index lagging must not lose the write
# ---------------------------------------------------------------------------
def test_index_failure_is_nonfatal_write_survives(
    vault: Path,
    store: SQLiteMetaStore,
    vector_store: QdrantVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = make_writer(vault, store, vector_store)

    def boom(*_a: object, **_k: object) -> None:
        raise VectorStoreError("qdrant is down")

    monkeypatch.setattr(vector_store, "delete_by_doc", boom)
    result = writer.write_note(title="Resilient", content="## H\n\nbody\n")

    assert result.indexed is False
    assert result.warning is not None and "indexing failed" in result.warning
    # The note is still safely on disk — the vault is the source of truth.
    assert (vault / result.path).is_file()


def test_index_embedding_failure_is_nonfatal(
    vault: Path,
    store: SQLiteMetaStore,
    vector_store: QdrantVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(vault, store, vector_store)

    def boom(_texts: Sequence[str]) -> list[Vector]:
        raise EmbeddingError("ollama is down")

    monkeypatch.setattr(ctx.embedder, "embed", boom)
    writer = NoteWriter(ctx)
    result = writer.write_note(title="Still Written", content="## H\n\nbody\n")

    assert result.indexed is False
    assert (vault / result.path).is_file()
